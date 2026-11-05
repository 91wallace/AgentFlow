"""Strict versioned TOML loading and deterministic dependency validation."""

from dataclasses import asdict
import hashlib
import json
import re
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
import tomllib

from .models import (AllowlistedCommand, Criterion, GateDefinition, Plan,
                     Specification, Task)


class PlanError(ValueError):
    """Invalid specification, command allowlist or task/gate graph."""


ID = re.compile(r"^[a-z][a-z0-9-]*$")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
PLACEHOLDERS = {"{snapshot_dir}", "{output_dir}", "{gate_id}",
                "{definition_revision}", "{snapshot_digest}"}
SHELL_NAMES = {"sh", "bash", "zsh", "fish", "dash", "cmd", "cmd.exe",
               "powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def task_data(task: Task) -> dict:
    """Stable task payload; empty F4/F5 fields preserve old plan digests."""
    data = {"id": task.id, "title": task.title, "description": task.description,
            "acceptance_criteria": [asdict(item) for item in task.acceptance_criteria],
            "depends_on": list(task.depends_on)}
    if task.versioned_paths:
        data["versioned_paths"] = list(task.versioned_paths)
    if task.gates:
        data["gates"] = [gate_data(gate) for gate in task.gates]
    return data


def gate_data(gate: GateDefinition) -> dict:
    return {"id": gate.gate_id, "version": gate.version,
            "criterion_ids": list(gate.criterion_ids), "command_id": gate.command_id,
            "depends_on": list(gate.depends_on), "required": gate.required,
            "timeout_seconds": gate.timeout_seconds,
            "output_limit_bytes": gate.output_limit_bytes}


def specification_data(specification: Specification) -> dict:
    """Preserve the F2 digest when the v1 allowlist is semantically empty."""
    data = {"project_id": specification.project_id, "title": specification.title,
            "objective": specification.objective,
            "acceptance_criteria": [asdict(item) for item in specification.acceptance_criteria]}
    if specification.validation_commands:
        data["validation_commands"] = [command_data(item)
                                       for item in specification.validation_commands]
    return data


def command_data(command: AllowlistedCommand) -> dict:
    data = {"command_id": command.command_id, "platform": command.platform,
            "executable": command.executable, "executable_digest": command.executable_digest,
            "arguments": list(command.arguments),
            "working_directory": command.working_directory,
            "expected_version": command.expected_version}
    if command.container_image:
        data["container_image"] = command.container_image
        data["container_executable"] = command.container_executable
    return data


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


def _read(path: Path, allowed: set[str], required: set[str]) -> tuple[dict, int]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise PlanError(f"{path}: {exc}") from exc
    table = _table(data, allowed | {"schema_version"}, required | {"schema_version"}, str(path))
    if path.name == "spec.toml":
        versions = (1, 2)
    else:
        versions = (1, 2, 3)
    version = table["schema_version"]
    if type(version) is not int or version not in versions:
        message = "versão 1 ou 2 exigida" if path.name == "spec.toml" else f"versões suportadas: {', '.join(map(str, versions))}"
        raise PlanError(f"{path}.schema_version: {message}")
    return table, version


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


def _absolute_executable(value: object, platform: str, where: str) -> str:
    executable = _text(value, where)
    if platform == "windows":
        path = PureWindowsPath(executable)
        if not path.is_absolute() or path.name.casefold() in SHELL_NAMES:
            raise PlanError(f"{where}: executável Windows absoluto e não-shell esperado")
        return str(path)
    path = PurePosixPath(executable)
    if not path.is_absolute() or path.name.casefold() in SHELL_NAMES:
        raise PlanError(f"{where}: executável Linux absoluto e não-shell esperado")
    return str(path)


def _allowlist(value: object, where: str) -> tuple[AllowlistedCommand, ...]:
    table = _table(value, {"commands"}, {"commands"}, where)
    raw_commands = table["commands"]
    if not isinstance(raw_commands, list):
        raise PlanError(f"{where}.commands: lista esperada (pode ser vazia)")
    result = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_commands):
        place = f"{where}.commands[{index}]"
        item = _table(raw, {"id", "platform", "executable", "sha256", "arguments",
                            "working_directory", "version", "container_image",
                            "container_executable"},
                      {"id", "platform", "executable", "sha256", "arguments",
                       "working_directory", "version"}, place)
        command_id = _text(item["id"], f"{place}.id", True)
        platform = _text(item["platform"], f"{place}.platform")
        if platform not in {"linux", "windows"}:
            raise PlanError(f"{place}.platform: somente linux ou windows é suportado")
        key = (command_id, platform)
        if key in seen:
            raise PlanError(f"{place}: command id duplicado para {platform}: {command_id}")
        seen.add(key)
        executable = _absolute_executable(item["executable"], platform, f"{place}.executable")
        executable_digest = _text(item["sha256"], f"{place}.sha256")
        if not SHA256.fullmatch(executable_digest):
            raise PlanError(f"{place}.sha256: digest sha256:<64 hex> esperado")
        arguments = item["arguments"]
        if not isinstance(arguments, list) or any(type(arg) is not str for arg in arguments):
            raise PlanError(f"{place}.arguments: lista de argumentos argv esperada")
        for arg in arguments:
            if ("{" in arg or "}" in arg) and arg not in PLACEHOLDERS:
                raise PlanError(f"{place}.arguments: placeholder não permitido: {arg!r}")
            if arg in {"-c", "-e", "-Command", "/c", "/k"}:
                raise PlanError(f"{place}.arguments: execução inline de código/shell recusada")
        working_directory = _text(item["working_directory"], f"{place}.working_directory")
        if working_directory not in {"snapshot", "output"}:
            raise PlanError(f"{place}.working_directory: use snapshot ou output")
        expected_version = _text(item["version"], f"{place}.version")
        container_image = item.get("container_image", "")
        if type(container_image) is not str:
            raise PlanError(f"{place}.container_image: string esperado")
        if container_image and not re.fullmatch(
                r"[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}", container_image):
            raise PlanError(f"{place}.container_image: imagem deve ser imutável por sha256 digest")
        container_executable = item.get("container_executable", "")
        if (type(container_executable) is not str or
                (container_executable and (not container_executable.startswith("/")
                 or "\\" in container_executable or "\0" in container_executable
                 or PurePosixPath(container_executable).name.casefold() in SHELL_NAMES))):
            raise PlanError(f"{place}.container_executable: caminho absoluto POSIX não-shell esperado")
        if bool(container_image) != bool(container_executable):
            raise PlanError(f"{place}: container_image e container_executable devem ser definidos juntos")
        result.append(AllowlistedCommand(command_id, platform, executable,
                                         executable_digest, tuple(arguments),
                                         working_directory, expected_version,
                                         container_image, container_executable))
    return tuple(sorted(result, key=lambda item: (item.command_id, item.platform)))


def load_spec(path: Path) -> Specification:
    data, version = _read(path, {"project_id", "title", "objective", "acceptance_criteria", "validation"},
                          {"project_id", "title", "objective", "acceptance_criteria"})
    if version == 1 and "validation" in data:
        raise PlanError(f"{path}.validation: exige schema_version=2")
    if version == 2 and "validation" not in data:
        raise PlanError(f"{path}.validation: allowlist obrigatória no schema v2 (commands pode ser vazia)")
    commands = _allowlist(data["validation"], f"{path}.validation") if version == 2 else ()
    return Specification(_text(data["project_id"], f"{path}.project_id", True),
                         _text(data["title"], f"{path}.title"),
                         _text(data["objective"], f"{path}.objective"),
                         _criteria(data["acceptance_criteria"], f"{path}.acceptance_criteria"),
                         commands, version)


def _criteria_ids(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise PlanError(f"{where}: lista não vazia de IDs esperada")
    if any(type(item) is not str or not ID.fullmatch(item) for item in value):
        raise PlanError(f"{where}: IDs inválidos")
    if len(value) != len(set(value)):
        raise PlanError(f"{where}: referência duplicada")
    return tuple(sorted(value))


def _gates(value: object, where: str, criterion_ids: set[str]) -> tuple[GateDefinition, ...]:
    if not isinstance(value, list):
        raise PlanError(f"{where}: lista de gates esperada")
    gates = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        place = f"{where}[{index}]"
        item = _table(raw, {"id", "version", "criterion_ids", "command_id", "depends_on",
                            "required", "timeout_seconds", "output_limit_bytes"},
                      {"id", "version", "criterion_ids", "command_id"}, place)
        gate_id = _text(item["id"], f"{place}.id", True)
        if gate_id in seen:
            raise PlanError(f"{place}.id: duplicado: {gate_id}")
        seen.add(gate_id)
        version = item["version"]
        if type(version) is not int or version < 1:
            raise PlanError(f"{place}.version: inteiro positivo esperado")
        covered = _criteria_ids(item["criterion_ids"], f"{place}.criterion_ids")
        unknown_criteria = set(covered) - criterion_ids
        if unknown_criteria:
            raise PlanError(f"{place}.criterion_ids: referência ausente: {', '.join(sorted(unknown_criteria))}")
        command_id = _text(item["command_id"], f"{place}.command_id", True)
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or any(type(dep) is not str or not ID.fullmatch(dep)
                                                     for dep in dependencies):
            raise PlanError(f"{place}.depends_on: lista de IDs esperada")
        if len(dependencies) != len(set(dependencies)):
            raise PlanError(f"{place}.depends_on: referência duplicada")
        required = item.get("required", True)
        if type(required) is not bool:
            raise PlanError(f"{place}.required: boolean esperado")
        timeout = item.get("timeout_seconds", 600)
        output_limit = item.get("output_limit_bytes", 8 * 1024**2)
        try:
            gates.append(GateDefinition(gate_id, version, covered, command_id,
                                        tuple(sorted(dependencies)), required, timeout, output_limit))
        except (TypeError, ValueError) as exc:
            raise PlanError(f"{place}: {exc}") from exc
    return tuple(sorted(gates, key=lambda gate: gate.gate_id))


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


def load_tasks(path: Path) -> tuple[Task, ...]:
    data, schema_version = _read(path, {"tasks"}, {"tasks"})
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise PlanError(f"{path}.tasks: lista não vazia esperada")
    tasks = []
    seen: set[str] = set()
    all_gate_ids: set[str] = set()
    for index, raw in enumerate(data["tasks"]):
        place = f"{path}.tasks[{index}]"
        item = _table(raw, {"id", "title", "description", "acceptance_criteria", "depends_on",
                            "versioned_paths", "gates"},
                      {"id", "title", "description", "acceptance_criteria"}, place)
        if schema_version >= 2 and "versioned_paths" not in item:
            raise PlanError(f"{place}.versioned_paths: obrigatório no schema v{schema_version} (use lista vazia para tarefa sem escrita versionada)")
        if schema_version < 3 and "gates" in item:
            raise PlanError(f"{place}.gates: exige schema_version=3")
        if schema_version == 3 and "gates" not in item:
            raise PlanError(f"{place}.gates: obrigatório no schema v3 (use lista vazia se não houver gates)")
        name = _text(item["id"], f"{place}.id", True)
        if name in seen:
            raise PlanError(f"{place}.id: duplicado: {name}")
        seen.add(name)
        criteria = _criteria(item["acceptance_criteria"], f"{place}.acceptance_criteria")
        deps = item.get("depends_on", [])
        if not isinstance(deps, list) or any(type(dep) is not str or not ID.fullmatch(dep) for dep in deps):
            raise PlanError(f"{place}.depends_on: lista de IDs esperada")
        if len(deps) != len(set(deps)):
            raise PlanError(f"{place}.depends_on: referência duplicada")
        gates = _gates(item.get("gates", []), f"{place}.gates", {criterion.id for criterion in criteria})
        duplicates = all_gate_ids.intersection(gate.gate_id for gate in gates)
        if duplicates:
            raise PlanError(f"{place}.gates: IDs duplicados no projeto: {', '.join(sorted(duplicates))}")
        all_gate_ids.update(gate.gate_id for gate in gates)
        tasks.append(Task(name, _text(item["title"], f"{place}.title"),
                          _text(item["description"], f"{place}.description"), criteria,
                          tuple(sorted(deps)), _versioned_paths(item.get("versioned_paths", []),
                                                               f"{place}.versioned_paths"), gates))
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


def gate_order(tasks: tuple[Task, ...]) -> tuple[str, ...]:
    """Return stable topological gate order; references cannot cross task boundaries."""
    result = []
    gates_by_task = {task.id: {gate.gate_id: gate for gate in task.gates} for task in tasks}
    global_ids = {gate_id for gates in gates_by_task.values() for gate_id in gates}
    for task in tasks:
        by_id = gates_by_task[task.id]
        visiting: list[str] = []
        finished: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                cycle = visiting[visiting.index(name):] + [name]
                raise PlanError(f"ciclo de gates em tasks.{task.id}: {' -> '.join(cycle)}")
            if name in finished:
                return
            visiting.append(name)
            for dep in by_id[name].depends_on:
                if dep not in by_id:
                    reason = "referência cruzada entre tarefas" if dep in global_ids else "referência ausente"
                    raise PlanError(f"tasks.{task.id}.gates.{name}.depends_on: {reason}: {dep}")
                visit(dep)
            visiting.pop()
            finished.add(name)
            result.append(name)

        for gate_id in sorted(by_id):
            visit(gate_id)
    return tuple(result)


def load_plan(directory: Path) -> Plan:
    spec_path = directory / "spec.toml"
    tasks_path = directory / "tasks.toml"
    specification = load_spec(spec_path)
    tasks = load_tasks(tasks_path)
    order = task_order(tasks)
    all_commands = {command.command_id for command in specification.validation_commands}
    for task in tasks:
        for gate in task.gates:
            if gate.command_id not in all_commands:
                raise PlanError(f"tasks.{task.id}.gates.{gate.gate_id}.command_id: não está na allowlist do projeto: {gate.command_id}")
    gates = gate_order(tasks)
    return Plan(specification, tasks, order,
                digest(specification_data(specification)),
                digest([task_data(task) for task in tasks]),
                specification.schema_version,
                _read(tasks_path, {"tasks"}, {"tasks"})[1],
                gates)
