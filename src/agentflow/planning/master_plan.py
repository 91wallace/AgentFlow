"""Digest-bound Markdown context and progressive, read-only line retrieval."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .loader import PlanError

DEFAULT_CONTEXT_BYTES = 16384


def _signature(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(65536):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


@dataclass(frozen=True)
class Section:
    heading: str
    title: str
    level: int
    start_line: int
    end_line: int
    hierarchy: str

    @property
    def section_id(self):
        return f"L{self.start_line}"

    def reference(self):
        return {"id": self.section_id, "heading": self.heading, "hierarchy": self.hierarchy,
                "start_line": self.start_line, "end_line": self.end_line}


@dataclass(frozen=True)
class MasterPlan:
    path: Path
    relative_path: str
    sha256: str
    line_count: int
    sections: tuple[Section, ...]
    context_bytes: int = DEFAULT_CONTEXT_BYTES

    def verify(self):
        try:
            if _digest(self.path) != self.sha256:
                raise PlanError("master_plan mudou; refaça o planejamento")
        except OSError as exc:
            raise PlanError("master_plan indisponível durante recuperação") from exc

    def reference(self):
        return {"path": self.relative_path, "sha256": self.sha256, "line_count": self.line_count,
                "context_bytes": self.context_bytes}

    def index_page(self, cursor=0):
        if type(cursor) is not int or not 0 <= cursor <= len(self.sections):
            raise PlanError("cursor de índice inválido")
        entries = []
        next_cursor = cursor
        for index in range(cursor, len(self.sections)):
            section = self.sections[index]
            next_cursor = index + 1
            if section.level > 3:
                continue
            entry = section.reference()
            if len(json.dumps(entries + [entry], ensure_ascii=False).encode()) > self.context_bytes // 2:
                if not entries:
                    # Oversized titles remain readable by line; the document is never cut.
                    entry = {"id": section.section_id, "start_line": section.start_line,
                             "end_line": section.end_line, "heading_in_source": True}
                    entries.append(entry)
                else:
                    next_cursor = index
                break
            entries.append(entry)
        return {**self.reference(), "toc": entries,
                "next_cursor": next_cursor if next_cursor < len(self.sections) else None}

    def read_lines(self, start_line, end_line):
        if (type(start_line) is not int or type(end_line) is not int
                or not 1 <= start_line <= end_line <= self.line_count):
            raise PlanError("faixa de linhas inválida")
        self.verify()
        lines, size = [], 0
        overhead = len(json.dumps({**self.reference(), "start_line": start_line,
                                  "end_line": end_line, "text": ""}, ensure_ascii=False).encode())
        try:
            with self.path.open(encoding="utf-8-sig") as stream:
                for number, line in enumerate(stream, 1):
                    if number > end_line:
                        break
                    if number >= start_line:
                        size += len(json.dumps(line, ensure_ascii=False)[1:-1].encode())
                        if size + overhead > self.context_bytes:
                            self.verify()
                            return {"error": "range_exceeds_context_budget", "start_line": start_line,
                                    "end_line": end_line, "action": "request fewer lines"}
                        lines.append(line)
        except (OSError, UnicodeError) as exc:
            raise PlanError("master_plan indisponível durante leitura de linhas") from exc
        self.verify()
        return {**self.reference(), "start_line": start_line, "end_line": end_line, "text": "".join(lines)}

    def initial_context(self, selectors=()):
        selected = []
        for selector in selectors:
            matches = ([section for section in self.sections if selector == section.section_id]
                       if re.fullmatch(r"L[0-9]+", selector) else
                       [section for section in self.sections if selector in
                        (section.heading, section.title, section.hierarchy)])
            if len(matches) != 1:
                raise PlanError(f"plan_sections ausente ou ambígua: {selector}; use hierarquia ou ID L<n>")
            selected.append(matches[0])
        # Ancestor selections already include their children; don't repeat their text.
        ranges = []
        for section in sorted(selected, key=lambda item: (item.start_line, -item.end_line)):
            if not ranges or section.start_line > ranges[-1][1]:
                ranges.append((section.start_line, section.end_line))
            else:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], section.end_line))
        excerpts = []
        for start, end in ranges:
            excerpts.append(self.read_lines(start, end))
            if "error" in excerpts[-1] or len(json.dumps(excerpts, ensure_ascii=False).encode()) > self.context_bytes:
                break
        text = json.dumps({**self.reference(), "excerpts": excerpts}, ensure_ascii=False, separators=(",", ":"))
        if selected and len(excerpts) == len(ranges) and all("error" not in item for item in excerpts) and len(text.encode()) <= self.context_bytes:
            return text
        page = self.index_page()
        page["selected_sections"] = [section.reference() for section in selected]
        if len(json.dumps(page, ensure_ascii=False).encode()) > self.context_bytes:
            page["selected_sections"] = [{"id": section.section_id} for section in selected]
        if len(json.dumps(page, ensure_ascii=False).encode()) > self.context_bytes:
            page["selected_sections"] = {"count": len(selected), "references_available_in_index": True}
        return json.dumps(page, ensure_ascii=False, separators=(",", ":"))


def inspect_master_plan(root, path, *, context_bytes=DEFAULT_CONTEXT_BYTES):
    if type(context_bytes) is not int or context_bytes < 1024:
        raise PlanError("plan_context_bytes deve ser inteiro >= 1024 (orçamento do prompt, não do arquivo)")
    try:
        path = path.resolve(strict=True)
        root = root.resolve(strict=True)
        if path.suffix.casefold() != ".md" or not path.is_relative_to(root):
            raise PlanError("master_plan deve ser Markdown dentro do projeto")
        before = _signature(path)
        digest = _digest(path)
        headings, stack, fence, line_count, nonempty = [], [], None, 0, False
        with path.open(encoding="utf-8-sig") as stream:
            for line_count, line in enumerate(stream, 1):
                nonempty |= bool(line.strip())
                stripped = line.lstrip(" ")
                if len(line) - len(stripped) > 3:
                    continue
                marker = re.match(r"(`{3,}|~{3,})", stripped)
                if marker:
                    run = marker.group(1)
                    if fence is None:
                        fence = (run[0], len(run))
                    elif run[0] == fence[0] and len(run) >= fence[1] and not stripped[len(run):].strip():
                        fence = None
                    continue
                if fence:
                    continue
                match = re.match(r"(#{1,6})[ \t]+(.+?)\s*$", stripped)
                if not match:
                    continue
                level = len(match[1])
                title = re.sub(r"\s+#+\s*$", "", match[2])
                while stack and stack[-1][0] >= level:
                    stack.pop()
                hierarchy = " / ".join([item[1] for item in stack] + [title])
                headings.append((f"{match[1]} {title}", title, level, line_count, hierarchy))
                stack.append((level, title))
        if not nonempty:
            raise PlanError("master_plan vazio")
        if _signature(path) != before or _digest(path) != digest:
            raise PlanError("master_plan alterado durante leitura")
        ends, active = {}, []
        for index, heading in enumerate(headings):
            while active and headings[active[-1]][2] >= heading[2]:
                ends[active.pop()] = heading[3] - 1
            active.append(index)
        sections = tuple(Section(item[0], item[1], item[2], item[3], ends.get(index, line_count), item[4])
                         for index, item in enumerate(headings))
        return MasterPlan(path, path.relative_to(root).as_posix(), digest, line_count, sections, context_bytes)
    except (OSError, UnicodeError) as exc:
        raise PlanError(f"master_plan indisponível: {exc}") from exc
