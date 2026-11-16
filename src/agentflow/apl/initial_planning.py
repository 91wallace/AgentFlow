"""Initial objective planning through the existing APL plan contracts.

Only structural workspace metadata is sent to the planner. Validation commands
are selected locally and are never accepted from planner output.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
from typing import Any

from agentflow.planning import load_plan
from agentflow.planning.loader import digest


class InitialPlanningError(ValueError):
    """A safe, verifiable initial plan could not be produced."""


@dataclass(frozen=True)
class ValidationCheck:
    check_id: str
    label: str
    executable: str
    executable_digest: str
    arguments: tuple[str, ...]
    version: str
    package_directory: str = ""
    script_name: str = ""
    script_digest: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {"id": self.check_id, "label": self.label,
                "executable": self.executable, "sha256": self.executable_digest,
                "arguments": list(self.arguments), "version": self.version,
                "package_directory": self.package_directory,
                "script_name": self.script_name, "script_sha256": self.script_digest}


@dataclass(frozen=True)
class ValidationStrategy:
    runner: str
    runner_executable: str
    runner_version: str
    agentflow_executable: str
    agentflow_executable_digest: str
    host_platform: str
    checks: tuple[ValidationCheck, ...] = ()


@dataclass(frozen=True)
class InitialPlanningContext:
    objective: str
    workspace: Path
    inventory: tuple[str, ...]
    technologies: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    workspace_digest: str
    strategy: ValidationStrategy
    validation_scope_paths: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "workspace_files": list(self.inventory),
            "technology_markers": list(self.technologies),
            "allowed_scope_paths": list(self.allowed_paths),
            "workspace_inventory_digest": self.workspace_digest,
            "validation": {
                "checks": [item.to_mapping() for item in self.strategy.checks],
                "platform": self.strategy.host_platform,
                "behavior": "execute only statically discovered allowlisted project checks; no shell",
                "objective_artifacts_required": True,
            },
            "limits": {"tasks": 12, "criteria_per_task": 8,
                       "scope_paths_per_task": 32},
        }


_ID = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_SECRET_PARTS = {".git", ".agentflow", ".venv", "venv", "node_modules",
                 "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                 "autonomous-development-runner", "credentials", "secrets"}
_SECRET_SUFFIXES = (".pem", ".p12", ".pfx", ".key", ".sqlite", ".sqlite3",
                    ".db", ".token")


def _sensitive_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    for part in path.parts:
        lower = part.casefold()
        if lower in _SECRET_PARTS or lower.startswith(".env") or "credential" in lower:
            return True
        if lower.endswith(_SECRET_SUFFIXES) or lower in {"id_rsa", "id_ed25519"}:
            return True
    return False


def _workspace_inventory(root: Path, *, max_files: int = 4000,
                         max_total_bytes: int = 256 * 1024 * 1024) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "--full-name"],
            check=True, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InitialPlanningError("workspace_inventory_unavailable") from exc
    entries = sorted({line.decode("utf-8", errors="strict").replace("\\", "/")
                      for line in completed.stdout.splitlines() if line}, key=str.casefold)
    safe_files: list[str] = []
    total = 0
    has_python = False
    for relative in entries:
        if len(safe_files) >= max_files or _sensitive_path(relative):
            continue
        path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_total_bytes - total:
            continue
        total += size
        safe_files.append(relative)
        has_python |= path.suffix.casefold() == ".py" or path.name.casefold() in {
            "pyproject.toml", "setup.cfg", "setup.py", "pytest.ini"}

    markers = set()
    known_markers = {
        "pyproject.toml": "python", "setup.py": "python", "setup.cfg": "python",
        "pytest.ini": "pytest", "package.json": "node", "Cargo.toml": "rust",
        "go.mod": "go", "pom.xml": "java-maven", "build.gradle": "java-gradle",
        "build.gradle.kts": "java-gradle", "*.sln": "dotnet", "*.csproj": "dotnet",
    }
    for name, technology in known_markers.items():
        if name.startswith("*"):
            if any(Path(item).match(name) for item in safe_files):
                markers.add(technology)
        elif name in {Path(item).name for item in safe_files}:
            markers.add(technology)
    if "tsconfig.json" in {Path(item).name for item in safe_files}:
        markers.add("typescript")
    for relative in safe_files:
        if Path(relative).name.casefold() != "package.json":
            continue
        package_path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            if package_path.stat().st_size > 1024 * 1024:
                continue
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(package, dict):
            continue
        dependencies = set()
        for category in ("dependencies", "devDependencies", "peerDependencies"):
            value = package.get(category, {})
            if isinstance(value, dict):
                dependencies.update(name for name in value if isinstance(name, str))
        if "react" in dependencies:
            markers.add("react")
        if "next" in dependencies:
            markers.update(("next", "react"))
    if has_python:
        markers.add("python")

    allowed = {item.split("/", 1)[0] for item in safe_files}
    if "python" in markers:
        allowed.update(("src", "tests"))
    allowed = {item for item in allowed if item and not _sensitive_path(item)}
    if len(allowed) > 128:
        allowed = set(sorted(allowed, key=str.casefold)[:128])
    inventory = tuple(safe_files[:2000])
    payload = json.dumps({"files": inventory, "technologies": sorted(markers),
                          "scope_roots": sorted(allowed)}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return inventory, tuple(sorted(markers)), digest(hashlib.sha256(payload).hexdigest())


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _probe_version(executable: Path, arguments: tuple[str, ...], cwd: Path) -> str | None:
    try:
        result = subprocess.run([str(executable), *arguments], cwd=cwd,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=10, check=False,
                                env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or len(result.stdout) + len(result.stderr) > 4096:
        return None
    value = (result.stdout or result.stderr).decode("utf-8", errors="replace").strip()
    return value[:120] if value else None


def _python_checks(root: Path, files: tuple[str, ...], technologies: tuple[str, ...]) -> list[ValidationCheck]:
    if "python" not in technologies:
        return []
    test_files = [item for item in files
                  if ((Path(item).name.casefold().startswith("test_")
                       and Path(item).suffix.casefold() == ".py")
                      or Path(item).name.casefold().endswith("_test.py"))]
    if not test_files:
        return []
    candidates = ([root / ".venv" / "Scripts" / "python.exe",
                   root / ".venv" / "bin" / "python"] if os.name == "nt" else
                  [root / ".venv" / "bin" / "python",
                   root / ".venv" / "Scripts" / "python.exe"])
    python = next((item.resolve() for item in candidates
                   if item.is_file() and not item.is_symlink()), Path(sys.executable).resolve())
    checks = []
    pytest_version = _probe_version(python, ("-m", "pytest", "--version"), root)
    if pytest_version:
        checks.append(ValidationCheck("python-pytest", "Python pytest suite", str(python),
                                      _file_digest(python), ("-m", "pytest", "-q"),
                                      pytest_version))
    else:
        uses_unittest = False
        for relative in test_files[:256]:
            source = root.joinpath(*PurePosixPath(relative).parts)
            try:
                if source.stat().st_size <= 256 * 1024:
                    content = source.read_text(encoding="utf-8")
                    uses_unittest |= bool(re.search(
                        r"(?:unittest\.TestCase|from\s+unittest\s+import|import\s+unittest)", content))
            except (OSError, UnicodeError):
                continue
        unittest_version = (_probe_version(python, ("-m", "unittest", "--help"), root)
                            if uses_unittest else None)
        if uses_unittest and unittest_version:
            checks.append(ValidationCheck("python-unittest", "Python unittest discovery",
                                          str(python), _file_digest(python),
                                          ("-m", "unittest", "discover", "-v"),
                                          "stdlib unittest"))
    return checks


def _npm_runtime(root: Path) -> tuple[Path, tuple[str, ...], str] | None:
    node_found, npm_found = shutil.which("node"), shutil.which("npm")
    if not node_found or not npm_found:
        return None
    node = Path(node_found).resolve()
    npm = Path(npm_found).resolve()
    if not node.is_file() or not npm.is_file():
        return None
    if os.name == "nt" and npm.suffix.casefold() in {".cmd", ".bat"}:
        npm_cli = npm.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if not npm_cli.is_file() or npm_cli.is_symlink():
            return None
        prefix = (str(npm_cli),)
        executable = node
    else:
        prefix = ()
        executable = npm
    version = _probe_version(executable, (*prefix, "--version"), root)
    if not version:
        return None
    return executable, prefix, version


_KNOWN_NPM_SCRIPTS = ("test", "test:unit", "lint", "typecheck", "type-check", "build", "check")


def _package_checks(root: Path, files: tuple[str, ...]) -> list[ValidationCheck]:
    packages = [item for item in files if Path(item).name.casefold() == "package.json"]
    if not packages:
        return []
    npm_runtime = _npm_runtime(root)
    if npm_runtime is None:
        for package_path in packages[:32]:
            source = root.joinpath(*PurePosixPath(package_path).parts)
            try:
                package = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if isinstance(scripts, dict) and any(
                    type(scripts.get(name)) is str and scripts[name].strip()
                    for name in _KNOWN_NPM_SCRIPTS):
                raise InitialPlanningError("configured_npm_validation_scripts_but_runtime_unavailable")
        return []
    executable, prefix, npm_version = npm_runtime
    checks = []
    for package_path in packages[:32]:
        source = root.joinpath(*PurePosixPath(package_path).parts)
        if source.is_symlink() or source.stat().st_size > 1024 * 1024:
            continue
        try:
            package = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if not isinstance(scripts, dict):
            continue
        package_directory = PurePosixPath(package_path).parent.as_posix()
        if package_directory == ".":
            package_directory = ""
        for script in _KNOWN_NPM_SCRIPTS:
            definition = scripts.get(script)
            if type(definition) is not str or not definition.strip() or len(definition) > 4096:
                continue
            check_id = "npm-" + re.sub(r"[^a-z0-9-]+", "-", script.casefold()).strip("-")
            if package_directory:
                check_id += "-" + hashlib.sha256(package_directory.encode()).hexdigest()[:8]
            args = (*prefix, *( ("--prefix", package_directory) if package_directory else ()),
                    "--ignore-scripts", "run", script)
            checks.append(ValidationCheck(check_id, f"npm run {script}", str(executable),
                                          _file_digest(executable), tuple(args), npm_version,
                                          package_directory, script,
                                          "sha256:" + hashlib.sha256(
                                              definition.encode("utf-8")).hexdigest()))
            if len(checks) >= 32:
                return checks
    return checks


def detect_validation_strategy(root: Path) -> ValidationStrategy:
    files, technologies, _inventory_digest = _workspace_inventory(root)
    checks = _python_checks(root, files, technologies)
    checks.extend(_package_checks(root, files))
    platform_name = "windows" if os.name == "nt" else "linux"
    agentflow_exe = Path(sys.executable).resolve()
    agentflow_digest = _file_digest(agentflow_exe)
    labels = "+".join(item.check_id for item in checks) or "artifact-checks"
    version = "; ".join(f"{item.check_id}={item.version}" for item in checks) or "builtin artifacts"
    return ValidationStrategy(labels, str(agentflow_exe), version, str(agentflow_exe),
                              agentflow_digest, platform_name, tuple(checks))


def create_context(root: Path, objective: str) -> InitialPlanningContext:
    objective = objective.strip()
    if not objective or len(objective.encode("utf-8")) > 8192:
        raise InitialPlanningError("objective_length_invalid")
    root = root.resolve(strict=True)
    inventory, technologies, workspace_digest = _workspace_inventory(root)
    if not inventory:
        raise InitialPlanningError("workspace_has_no_safe_files_to_plan")
    strategy = detect_validation_strategy(root)
    allowed = {item.split("/", 1)[0] for item in inventory}
    if "python" in technologies:
        allowed.update(("src", "tests"))
    validation_scope = set()
    config_names = {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini",
                    "package.json", "package-lock.json", "npm-shrinkwrap.json",
                    "yarn.lock", "pnpm-lock.yaml", "tsconfig.json"}
    for item in inventory:
        path = PurePosixPath(item)
        if path.name.casefold() in config_names:
            validation_scope.add(path.parts[0])
        if ((path.name.casefold().startswith("test_") and path.suffix.casefold() == ".py")
                or path.name.casefold().endswith(("_test.py", ".test.js", ".test.jsx",
                                                  ".test.ts", ".test.tsx", ".spec.js",
                                                  ".spec.jsx", ".spec.ts", ".spec.tsx"))):
            validation_scope.add(path.parts[0])
    for check in strategy.checks:
        if check.package_directory:
            validation_scope.add(check.package_directory.split("/", 1)[0])
    validation_scope &= allowed
    allowed = {item for item in allowed if item and not _sensitive_path(item)}
    if not allowed:
        raise InitialPlanningError("workspace_has_no_safe_initial_scope")
    return InitialPlanningContext(objective, root, inventory, technologies,
                                  tuple(sorted(allowed)), workspace_digest, strategy,
                                  tuple(sorted(validation_scope)))


def _require_text(value: Any, where: str, *, max_bytes: int = 2048) -> str:
    if (type(value) is not str or not value.strip() or len(value.encode("utf-8")) > max_bytes
            or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise InitialPlanningError(f"initial_plan_{where}_invalid")
    return value.strip()


def _id(value: Any, where: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise InitialPlanningError(f"initial_plan_{where}_invalid")
    return value


def validate_initial_plan(data: Any, context: InitialPlanningContext) -> dict:
    expected_top = {"schema_version", "title", "rationale", "tasks", "completion_condition"}
    if not isinstance(data, dict) or set(data) != expected_top or data.get("schema_version") != 1:
        raise InitialPlanningError("initial_plan_schema_invalid")
    title = _require_text(data["title"], "title", max_bytes=160)
    rationale = _require_text(data["rationale"], "rationale", max_bytes=2048)
    completion = _require_text(data["completion_condition"], "completion_condition", max_bytes=1024)
    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 12:
        raise InitialPlanningError("initial_plan_task_count_invalid")
    tasks = []
    seen = set()
    for index, raw in enumerate(raw_tasks):
        required = {"id", "title", "description", "depends_on", "paths", "acceptance_criteria"}
        if (not isinstance(raw, dict) or not required <= set(raw)
                or set(raw) - (required | {"artifact_checks"})):
            raise InitialPlanningError(f"initial_plan_task_{index}_schema_invalid")
        task_id = _id(raw["id"], f"task_{index}_id")
        if task_id in seen:
            raise InitialPlanningError("initial_plan_task_id_duplicate")
        seen.add(task_id)
        task_title = _require_text(raw["title"], f"task_{task_id}_title", max_bytes=160)
        description = _require_text(raw["description"], f"task_{task_id}_description")
        dependencies = raw["depends_on"]
        paths = raw["paths"]
        criteria = raw["acceptance_criteria"]
        if (not isinstance(dependencies, list) or len(dependencies) > 12
                or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            raise InitialPlanningError(f"initial_plan_task_{task_id}_dependencies_invalid")
        if (not isinstance(paths, list) or not 1 <= len(paths) <= 32
                or any(type(item) is not str or item not in context.allowed_paths for item in paths)
                or len(set(paths)) != len(paths)):
            raise InitialPlanningError(f"initial_plan_task_{task_id}_scope_invalid")
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 8:
            raise InitialPlanningError(f"initial_plan_task_{task_id}_criteria_invalid")
        normalized_criteria = []
        criterion_ids = set()
        for criterion_index, criterion in enumerate(criteria):
            if not isinstance(criterion, dict) or set(criterion) != {"id", "description"}:
                raise InitialPlanningError(f"initial_plan_task_{task_id}_criterion_schema_invalid")
            criterion_id = _id(criterion["id"], f"task_{task_id}_criterion_{criterion_index}")
            if criterion_id in criterion_ids:
                raise InitialPlanningError(f"initial_plan_task_{task_id}_criterion_duplicate")
            criterion_ids.add(criterion_id)
            normalized_criteria.append((criterion_id,
                                       _require_text(criterion["description"],
                                                     f"task_{task_id}_criterion_description", max_bytes=1024)))
        artifacts = raw.get("artifact_checks", [])
        if not isinstance(artifacts, list) or len(artifacts) > 16:
            raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_checks_invalid")
        normalized_artifacts, artifact_criteria = [], set()
        for artifact_index, artifact in enumerate(artifacts):
            if (not isinstance(artifact, dict)
                    or set(artifact) != {"criterion_id", "path", "kind", "expected"}):
                raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_schema_invalid")
            criterion_id = _id(artifact["criterion_id"],
                               f"task_{task_id}_artifact_{artifact_index}_criterion")
            if criterion_id not in criterion_ids or criterion_id in artifact_criteria:
                raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_criterion_invalid")
            artifact_path = _require_text(artifact["path"],
                                          f"task_{task_id}_artifact_{artifact_index}_path", max_bytes=512)
            pure = PurePosixPath(artifact_path)
            if (pure.is_absolute() or not pure.parts or ".." in pure.parts
                    or "\\" in artifact_path or _sensitive_path(artifact_path)
                    or pure.parts[0] not in context.allowed_paths):
                raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_path_invalid")
            kind = artifact["kind"]
            if type(kind) is not str or kind not in {"exists", "nonempty", "json", "contains"}:
                raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_kind_invalid")
            expected = artifact["expected"]
            if type(expected) is not str or len(expected.encode("utf-8")) > 1024:
                raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_expected_invalid")
            if kind == "contains" and not expected:
                raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_expected_required")
            if kind != "contains" and expected:
                raise InitialPlanningError(f"initial_plan_task_{task_id}_artifact_unexpected_value")
            artifact_criteria.add(criterion_id)
            normalized_artifacts.append({"criterion_id": criterion_id, "path": artifact_path,
                                         "kind": kind, "expected": expected})
            paths = sorted(set(paths) | {pure.parts[0]})
        if artifact_criteria != criterion_ids:
            raise InitialPlanningError(f"initial_plan_task_{task_id}_objective_artifacts_required")
        paths = sorted(set(paths) | set(context.validation_scope_paths))
        if len(paths) > 32:
            raise InitialPlanningError(f"initial_plan_task_{task_id}_scope_limit_exceeded")
        tasks.append({"id": task_id, "title": task_title, "description": description,
                      "depends_on": sorted(dependencies), "paths": sorted(paths),
                      "criteria": normalized_criteria, "artifact_checks": normalized_artifacts})
    if any(dep not in seen for task in tasks for dep in task["depends_on"]):
        raise InitialPlanningError("initial_plan_dependency_missing")
    if any(dep == task["id"] for task in tasks for dep in task["depends_on"]):
        raise InitialPlanningError("initial_plan_dependency_self_reference")
    dependencies = {task["id"]: task["depends_on"] for task in tasks}
    visiting, visited = set(), set()

    def visit(task_id):
        if task_id in visiting:
            raise InitialPlanningError("initial_plan_dependency_cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in dependencies:
        visit(task_id)
    return {"title": title, "rationale": rationale, "completion_condition": completion,
            "tasks": tasks}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def plan_toml_documents(data: dict, context: InitialPlanningContext) -> tuple[str, str]:
    project_slug = re.sub(r"[^a-z0-9-]+", "-", context.workspace.name.casefold()).strip("-")
    project_id = (project_slug[:40] or "local-project")
    spec_lines = ["schema_version = 2", f"project_id = {_toml_string(project_id)}",
                  f"title = {_toml_string(data['title'])}",
                  f"objective = {_toml_string(context.objective)}", "",
                  "[[acceptance_criteria]]", 'id = "objective-complete"',
                  f"description = {_toml_string(data['completion_condition'])}", "",
                  "[validation]"]
    task_lines = ["schema_version = 3"]
    for task in data["tasks"]:
        command_id = f"verify-{task['id']}"
        criterion_ids = [item[0] for item in task["criteria"]]
        criteria_json = json.dumps(criterion_ids, separators=(",", ":"))
        checks_json = base64.urlsafe_b64encode(json.dumps(
            [item.to_mapping() for item in context.strategy.checks],
            sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii")
        artifacts_json = base64.urlsafe_b64encode(json.dumps(
            task["artifact_checks"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).decode("ascii")
        arguments = ["-m", "agentflow.validation.objective_gate", "--gate-id", "{gate_id}",
                     "--definition-revision", "{definition_revision}", "--snapshot-digest",
                     "{snapshot_digest}", "--criteria-json", criteria_json,
                     "--checks-b64", checks_json, "--artifacts-b64", artifacts_json]
        argument_text = ", ".join(_toml_string(item) for item in arguments)
        spec_lines.extend([
            "[[validation.commands]]", f"id = {_toml_string(command_id)}",
            f"platform = {_toml_string(context.strategy.host_platform)}",
            f"executable = {_toml_string(context.strategy.agentflow_executable)}",
            f"sha256 = {_toml_string(context.strategy.agentflow_executable_digest)}",
            f"arguments = [{argument_text}]", 'working_directory = "snapshot"',
            f"version = {_toml_string(platform.python_version())}", "",
        ])
        task_lines.extend([
            "", "[[tasks]]", f"id = {_toml_string(task['id'])}",
            f"title = {_toml_string(task['title'])}",
            f"description = {_toml_string(task['description'])}",
            "depends_on = [" + ", ".join(_toml_string(item) for item in task["depends_on"]) + "]",
            "versioned_paths = [" + ", ".join(_toml_string(item) for item in task["paths"]) + "]",
        ])
        for criterion_id, description in task["criteria"]:
            task_lines.extend(["", "[[tasks.acceptance_criteria]]",
                               f"id = {_toml_string(criterion_id)}",
                               f"description = {_toml_string(description)}"])
        task_lines.extend(["", "[[tasks.gates]]",
                           f"id = {_toml_string('gate-' + task['id'])}",
                           "version = 1",
                           "criterion_ids = [" + ", ".join(_toml_string(item) for item in criterion_ids) + "]",
                           f"command_id = {_toml_string(command_id)}", "required = true",
                           "timeout_seconds = 600", "output_limit_bytes = 8388608"])
    return "\n".join(spec_lines).rstrip() + "\n", "\n".join(task_lines).lstrip() + "\n"


def write_generated_plan(directory: Path, data: dict,
                         context: InitialPlanningContext):
    from agentflow.planning import load_plan
    from agentflow.planning.loader import PlanError
    from agentflow.planning.models import Criterion, GateDefinition, Task, AllowlistedCommand, Specification, Plan

    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise InitialPlanningError("generated_plan_directory_symlink_forbidden")
    spec_path, tasks_path = directory / "spec.toml", directory / "tasks.toml"
    if spec_path.exists() or tasks_path.exists():
        if spec_path.is_file() and tasks_path.is_file():
            try:
                current = load_plan(directory)
                if current.specification.objective == context.objective:
                    return current
            except (PlanError, OSError, ValueError):
                pass
        raise InitialPlanningError("generated_plan_destination_conflict_preserved")
    spec_text, tasks_text = plan_toml_documents(data, context)
    staging = directory / (".initial-plan-stage-" + hashlib.sha256(
        (context.objective + context.workspace_digest).encode()).hexdigest()[:12])
    if staging.exists() or staging.is_symlink():
        if staging.is_symlink() or not staging.is_dir():
            raise InitialPlanningError("generated_plan_staging_conflict_preserved")
        try:
            staged_plan = load_plan(staging)
        except (PlanError, OSError, ValueError) as exc:
            raise InitialPlanningError("generated_plan_staging_invalid_preserved") from exc
        if (staged_plan.specification.objective != context.objective
                or not _run_ready(staged_plan)
                or spec_path.exists() or tasks_path.exists()):
            raise InitialPlanningError("generated_plan_staging_conflict_preserved")
        with spec_path.open("xb") as stream:
            stream.write((staging / "spec.toml").read_bytes())
        with tasks_path.open("xb") as stream:
            stream.write((staging / "tasks.toml").read_bytes())
        return load_plan(directory)
    staging.mkdir(exist_ok=False)
    try:
        (staging / "spec.toml").write_text(spec_text, encoding="utf-8", newline="\n")
        (staging / "tasks.toml").write_text(tasks_text, encoding="utf-8", newline="\n")
        plan = load_plan(staging)
        # Publishing uses exclusive creation so interrupted or concurrent
        # writers never replace an existing project-owned definition.
        with spec_path.open("xb") as stream:
            stream.write(spec_text.encode("utf-8"))
        with tasks_path.open("xb") as stream:
            stream.write(tasks_text.encode("utf-8"))
        return load_plan(directory)
    except Exception:
        # Keep the staging directory as a local recovery artifact; no existing
        # project file is removed or overwritten.
        raise


def persist_plan_manifest(root: Path, request_id: str, *, status: str,
                          objective_digest: str, plan_directory: Path | None = None,
                          plan_digest: str = "", detail: str = "") -> Path:
    if status not in {"planning", "planned", "failed"}:
        raise InitialPlanningError("objective_plan_manifest_state_invalid")
    control = root / ".agentflow"
    if control.is_symlink():
        raise InitialPlanningError("agentflow_control_directory_symlink_forbidden")
    directory = control / "objective-runs"
    if directory.is_symlink():
        raise InitialPlanningError("objective_run_manifest_directory_symlink_forbidden")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{request_id}.json"
    if target.is_symlink():
        raise InitialPlanningError("objective_plan_manifest_symlink_forbidden")
    events = []
    if target.exists():
        try:
            previous = json.loads(target.read_text(encoding="utf-8"))
            if previous.get("request_id") != request_id or previous.get("objective_digest") != objective_digest:
                raise InitialPlanningError("objective_plan_manifest_conflict")
            events = previous.get("events", [])
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise InitialPlanningError("objective_plan_manifest_invalid_preserved") from exc
    events.append({"state": status, "detail": detail[:160],
                   "plan_digest": plan_digest,
                   "plan_directory": str(plan_directory.resolve()) if plan_directory else ""})
    payload = {"schema_version": 1, "request_id": request_id,
               "objective_digest": objective_digest, "status": status,
               "plan_directory": str(plan_directory.resolve()) if plan_directory else "",
               "plan_digest": plan_digest, "events": events[-32:]}
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return target


def load_plan_manifest(root: Path, request_id: str) -> dict | None:
    path = root / ".agentflow" / "objective-runs" / f"{request_id}.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InitialPlanningError("objective_plan_manifest_invalid_preserved") from exc
    if (data.get("schema_version") != 1 or data.get("request_id") != request_id
            or data.get("status") not in {"planning", "planned", "failed"}):
        raise InitialPlanningError("objective_plan_manifest_invalid_preserved")
    return data


def _plan_token(plan) -> str:
    return f"spec:{plan.spec_digest};tasks:{plan.tasks_digest}"


def _run_ready(plan) -> bool:
    platform_name = "windows" if os.name == "nt" else "linux"
    commands = {command.command_id for command in plan.specification.validation_commands
                if command.platform == platform_name}
    return bool(plan.tasks) and all(
        task.gates and task.acceptance_criteria and task.versioned_paths
        and {criterion.id for criterion in task.acceptance_criteria}
        <= {criterion for gate in task.gates for criterion in gate.criterion_ids}
        and all(gate.command_id in commands for gate in task.gates if gate.required)
        for task in plan.tasks)


def prepare_objective_plan(root: Path, objective: str, request_id: str,
                           planner) -> tuple[Any, Path, str]:
    """Recover or create one immutable plan without replacing workspace TOMLs."""
    control = root / ".agentflow"
    if control.is_symlink():
        raise InitialPlanningError("agentflow_control_directory_symlink_forbidden")
    control.mkdir(parents=True, exist_ok=True)
    objective_digest = hashlib.sha256(objective.encode("utf-8")).hexdigest()
    prior = load_plan_manifest(root, request_id)
    if prior and prior.get("status") == "planned":
        directory = Path(prior.get("plan_directory", "")).resolve(strict=True)
        if directory != control.resolve() and control.resolve() not in directory.parents:
            raise InitialPlanningError("objective_plan_directory_outside_agentflow_state")
        plan = load_plan(directory)
        if (plan.specification.objective != objective or not _run_ready(plan)
                or _plan_token(plan) != prior.get("plan_digest")):
            raise InitialPlanningError("persisted_objective_plan_integrity_mismatch")
        return plan, directory, request_id

    spec_path, tasks_path = control / "spec.toml", control / "tasks.toml"
    if spec_path.is_symlink() or tasks_path.is_symlink():
        raise InitialPlanningError("project_plan_symlink_forbidden")
    plan = None
    target = control
    if spec_path.is_file() and tasks_path.is_file():
        try:
            existing = load_plan(control)
        except Exception:
            existing = None
        if existing and existing.specification.objective == objective and _run_ready(existing):
            plan = existing
        else:
            target = control / "objective-plans" / request_id
    elif spec_path.exists() or tasks_path.exists():
        # Preserve a partial/manual configuration and isolate the new run plan.
        target = control / "objective-plans" / request_id

    context = create_context(root, objective) if plan is None else None
    persist_plan_manifest(root, request_id, status="planning",
                          objective_digest=objective_digest, detail="initial_plan_started")
    if plan is None:
        if not context.technologies or not context.allowed_paths:
            raise InitialPlanningError("initial_plan_workspace_context_insufficient")
        try:
            staging = target / (".initial-plan-stage-" + hashlib.sha256(
                (context.objective + context.workspace_digest).encode()).hexdigest()[:12])
            if staging.exists() or staging.is_symlink():
                if staging.is_symlink() or not staging.is_dir():
                    raise InitialPlanningError("generated_plan_staging_conflict_preserved")
                try:
                    staged = load_plan(staging)
                except Exception as exc:
                    raise InitialPlanningError("generated_plan_staging_invalid_preserved") from exc
                if staged.specification.objective != objective or not _run_ready(staged):
                    raise InitialPlanningError("generated_plan_staging_conflict_preserved")
                if (target / "spec.toml").exists() or (target / "tasks.toml").exists():
                    raise InitialPlanningError("generated_plan_partial_publication_preserved")
                with (target / "spec.toml").open("xb") as stream:
                    stream.write((staging / "spec.toml").read_bytes())
                with (target / "tasks.toml").open("xb") as stream:
                    stream.write((staging / "tasks.toml").read_bytes())
                plan = load_plan(target)
            if plan is None:
                proposed = planner.plan_initial(context)
                if (target / "spec.toml").exists() or (target / "tasks.toml").exists():
                    try:
                        existing = load_plan(target)
                        if (existing.specification.objective == objective and _run_ready(existing)):
                            plan = existing
                    except Exception:
                        # Keep the interrupted/invalid target; use a new attempt dir.
                        attempt = 2
                        candidate = target.with_name(target.name + f"-attempt-{attempt}")
                        while candidate.exists():
                            attempt += 1
                            candidate = target.with_name(target.name + f"-attempt-{attempt}")
                        target = candidate
                if plan is None:
                    plan = write_generated_plan(target, proposed, context)
        except Exception as exc:
            persist_plan_manifest(root, request_id, status="failed",
                                  objective_digest=objective_digest,
                                  plan_directory=target if target.exists() else None,
                                  detail=type(exc).__name__)
            raise
    if not _run_ready(plan):
        persist_plan_manifest(root, request_id, status="failed",
                              objective_digest=objective_digest,
                              plan_directory=target, plan_digest=_plan_token(plan),
                              detail="initial_plan_missing_acceptance_gate")
        raise InitialPlanningError("initial_plan_missing_independent_acceptance_gate")
    persist_plan_manifest(root, request_id, status="planned",
                          objective_digest=objective_digest, plan_directory=target,
                          plan_digest=_plan_token(plan), detail="initial_plan_persisted")
    return plan, target, request_id
