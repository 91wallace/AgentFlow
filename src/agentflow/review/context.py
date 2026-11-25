"""Selective context, Python symbols and associated tests from captured content."""

import ast
import difflib
from pathlib import PurePosixPath


def build_packet(before, after, changes, job, *, max_files=12, requested=()):
    changed = {item["path"] for item in changes}
    selected = set(changed) | set(requested)
    files = after["files"]
    if any(path not in files or files[path] is None or files[path]["text"] is None for path in requested):
        raise ValueError("review_context_unavailable")
    related = set()
    symbols = {}
    for change in changes:
        name = change["path"]
        old = (change["before"] or {}).get("text") or ""
        current = (change["after"] or {}).get("text")
        if current is None:
            continue
        if name.endswith(".py"):
            try:
                tree = ast.parse(current)
            except (SyntaxError, ValueError):
                continue
            lines = set()
            for tag, _, _, first, last in difflib.SequenceMatcher(a=old.splitlines(), b=current.splitlines(), autojunk=False).get_opcodes():
                if tag != "equal":
                    lines.update(range(first+1, max(first+2, last+1)))
            symbols[name] = [{"name": node.name, "line": node.lineno, "end_line": node.end_lineno}
                for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and any(node.lineno <= line <= node.end_lineno for line in lines)]
            modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
            for module in modules:
                suffix = module.replace(".", "/")
                related.update(path for path in files if path.endswith(suffix + ".py") or path.endswith(suffix + "/__init__.py"))
        stem = PurePosixPath(name).stem.removeprefix("test_")
        related.update(path for path in files if PurePosixPath(path).name in {
            "test_" + stem + ".py", stem + "_test.py"} or
            PurePosixPath(path).parent == PurePosixPath(name).parent and PurePosixPath(path).name in {"contracts.py", "models.py", "__init__.py"})
    # Never silently cut the changed files or requested evidence. Related context
    # can be expanded explicitly by the reviewer within the same bounded policy.
    if len(selected) > max_files:
        raise ValueError("review_context_file_limit")
    selected.update(sorted(related - selected)[:max_files-len(selected)])
    context = {path: files[path] for path in sorted(selected) if files.get(path) is not None}
    if any(value["text"] is None for value in context.values()):
        raise ValueError("review_binary_or_oversized_context")
    return {"schema_version": 1, "baseline": before["fingerprint"], "fingerprint": after["fingerprint"],
            "task": {"id": job.job_id, "instruction": job.instruction, "scope": list(job.required_scope),
                     "acceptance_criteria": list(job.acceptance_criteria)},
            "changes": changes, "context": context, "changed_symbols": symbols,
            "available_paths": sorted(path for path, value in files.items() if value and value["text"] is not None)}
