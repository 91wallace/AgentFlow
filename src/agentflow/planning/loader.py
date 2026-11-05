"""Strict TOML loading and deterministic dependency validation."""

from dataclasses import asdict
import hashlib
import json
import unicodedata
from pathlib import Path
import re
import tomllib

from .models import Criterion, Plan, Specification, Task


class PlanError(ValueError):
    """Invalid specification or task graph."""


ID = re.compile(r"^[a-z][a-z0-9-]*$")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def task_data(task: Task) -> dict:
    """Stable task payload; empty F4 scope preserves v1/F2/F3 digests."""
    data = {"id": task.id, "title": task.title, "description": task.description,
            "acceptance_criteria": [asdict(item) for item in task.acceptance_criteria],
            "depends_on": list(task.depends_on)}
    if task.versioned_paths:
        data["versioned_paths"] = list(task.versioned_paths)
    return data


def _versioned_paths(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise PlanError(f"{where}: lista de caminhos esperada")
    result: list[str] = []
    for item in value:
        normalized = unicodedata.normalize("NFC", item)
        parts = normalized.split("/")
        if (not normalized or "\x00" in normalized or normalized.startswith("/") or "\\" in normalized
                or re.match(r"^[A-Za-z]:", normalized) or any(part in ("", ".", "..") for part in parts)
                or len(normalized.encode("utf-8")) > 1024
                or any(len(part.encode("utf-8")) > 255 for part in parts)):
            raise PlanError(f"{where}: caminho relativo inválido: {item!r}")
        if normalized in result:
            raise PlanError(f"{where}: caminho duplicado: {normalized!r}")
        result.append(normalized)
    return tuple(sorted(result, key=lambda path: path.encode("utf-8")))


def _table(value: object, allowed: set[str], required: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise PlanError(f"{where}: tabela esperada")
    if missing := required - value.keys():
        raise PlanError(f"{where}: campos ausentes: {', '.join(sorted(missing))}")
    if extra := value.keys() - allowed:
        raise PlanError(f"{where}: campos desconhecidos: {', '.join(sorted(extra))}")
    return value


def _text(value: object, where: str, identifier: bool = False) -> str:
    if type(value) is not str or not value.strip() or (identifier and not ID.fullmatch(value)):
        raise PlanError(f"{where}: texto {'ID' if identifier else 'não vazio'} esperado")
    return value


def _criteria(value: object, where: str) -> tuple[Criterion, ...]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"{where}: lista não vazia esperada")
    seen: set[str] = set()
    items = []
    for index, raw in enumerate(value):
        place = f"{where}[{index}]"
        data = _table(raw, {"id", "description"}, {"id", "description"}, place)
        name = _text(data["id"], f"{place}.id", True)
        if name in seen:
            raise PlanError(f"{place}.id: duplicado: {name}")
        seen.add(name)
        items.append(Criterion(name, _text(data["description"], f"{place}.description")))
    return tuple(sorted(items, key=lambda item: item.id))


def _read(path: Path, allowed: set[str], required: set[str]) -> dict:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PlanError(f"{path}: {exc}") from exc
    table = _table(data, allowed | {"schema_version"}, required | {"schema_version"}, str(path))
    versions = (1, 2) if path.name == "tasks.toml" else (1,)
    if type(table["schema_version"]) is not int or table["schema_version"] not in versions:
        message = "versão 1 exigida" if versions == (1,) else f"versões suportadas: {', '.join(map(str, versions))}"
        raise PlanError(f"{path}.schema_version: {message}")
    return table


def load_spec(path: Path) -> Specification:
    data = _read(path, {"project_id", "title", "objective", "acceptance_criteria"},
                 {"project_id", "title", "objective", "acceptance_criteria"})
    return Specification(_text(data["project_id"], f"{path}.project_id", True),
                         _text(data["title"], f"{path}.title"),
                         _text(data["objective"], f"{path}.objective"),
                         _criteria(data["acceptance_criteria"], f"{path}.acceptance_criteria"))


def load_tasks(path: Path) -> tuple[Task, ...]:
    data = _read(path, {"tasks"}, {"tasks"})
    schema_version = data["schema_version"]
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise PlanError(f"{path}.tasks: lista não vazia esperada")
    tasks = []
    seen: set[str] = set()
    for index, raw in enumerate(data["tasks"]):
        place = f"{path}.tasks[{index}]"
        item = _table(raw, {"id", "title", "description", "acceptance_criteria", "depends_on", "versioned_paths"},
                      {"id", "title", "description", "acceptance_criteria"}, place)
        if schema_version == 2 and "versioned_paths" not in item:
            raise PlanError(f"{place}.versioned_paths: obrigatório no schema v2 (use lista vazia para tarefa sem escrita versionada)")
        name = _text(item["id"], f"{place}.id", True)
        if name in seen:
            raise PlanError(f"{place}.id: duplicado: {name}")
        seen.add(name)
        deps = item.get("depends_on", [])
        if not isinstance(deps, list) or any(type(dep) is not str or not ID.fullmatch(dep) for dep in deps):
            raise PlanError(f"{place}.depends_on: lista de IDs esperada")
        if len(deps) != len(set(deps)):
            raise PlanError(f"{place}.depends_on: referência duplicada")
        tasks.append(Task(name, _text(item["title"], f"{place}.title"),
                          _text(item["description"], f"{place}.description"),
                          _criteria(item["acceptance_criteria"], f"{place}.acceptance_criteria"),
                          tuple(sorted(deps)),
                          _versioned_paths(item.get("versioned_paths", []), f"{place}.versioned_paths")))
    return tuple(sorted(tasks, key=lambda task: task.id))


def task_order(tasks: tuple[Task, ...]) -> tuple[str, ...]:
    by_id = {task.id: task for task in tasks}
    visiting: list[str] = []
    finished: set[str] = set()
    order: list[str] = []

    def visit(name: str) -> None:
        if name in visiting:
            cycle = visiting[visiting.index(name):] + [name]
            raise PlanError(f"ciclo de dependências: {' -> '.join(cycle)}")
        if name in finished:
            return
        visiting.append(name)
        for dep in by_id[name].depends_on:
            if dep not in by_id:
                raise PlanError(f"tasks.{name}.depends_on: referência ausente: {dep}")
            visit(dep)
        visiting.pop()
        finished.add(name)
        order.append(name)

    for name in sorted(by_id):
        visit(name)
    return tuple(order)


def load_plan(directory: Path) -> Plan:
    spec = load_spec(directory / "spec.toml")
    tasks = load_tasks(directory / "tasks.toml")
    order = task_order(tasks)
    return Plan(spec, tasks, order, digest(asdict(spec)), digest([task_data(task) for task in tasks]))
