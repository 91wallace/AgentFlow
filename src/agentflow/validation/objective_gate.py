"""Run a pinned set of project checks and objective artifact assertions."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath


def _json_list(value: str, name: str):
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name}_json_invalid") from exc
    if not isinstance(result, list):
        raise ValueError(f"{name}_must_be_list")
    return result


def _encoded_list(value: str, name: str):
    try:
        raw = base64.b64decode(value, altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{name}_base64_invalid") from exc
    return _json_list(raw, name)


def _validate_check(check: object) -> dict:
    fields = {"id", "label", "executable", "sha256", "arguments", "version",
              "package_directory", "script_name", "script_sha256"}
    if not isinstance(check, dict) or set(check) != fields:
        raise ValueError("project_check_schema_invalid")
    executable = check["executable"]
    path = Path(executable) if isinstance(executable, str) else Path()
    if (type(executable) is not str or not path.is_absolute() or not path.is_file()
            or path.is_symlink()):
        raise ValueError("project_check_executable_invalid")
    if (type(check["sha256"]) is not str or not check["sha256"].startswith("sha256:")
            or hashlib.sha256(path.read_bytes()).hexdigest() != check["sha256"][7:]):
        raise ValueError("project_check_executable_digest_mismatch")
    arguments = check["arguments"]
    if (not isinstance(arguments, list) or any(type(item) is not str or "\0" in item
                                               for item in arguments)
            or any(item in {"-c", "-e", "-Command", "/c", "/k"} for item in arguments)):
        raise ValueError("project_check_arguments_invalid")
    if any(type(check[key]) is not str or not check[key].strip()
           for key in ("id", "label", "version")):
        raise ValueError("project_check_identity_invalid")
    package_directory = check["package_directory"]
    if type(package_directory) is not str:
        raise ValueError("project_check_package_directory_invalid")
    if package_directory:
        package_path = PurePosixPath(package_directory)
        if package_path.is_absolute() or ".." in package_path.parts or "\\" in package_directory:
            raise ValueError("project_check_package_directory_invalid")
    script_name, script_digest = check["script_name"], check["script_sha256"]
    if (type(script_name) is not str or type(script_digest) is not str
            or bool(script_name) != bool(script_digest)):
        raise ValueError("project_check_script_binding_invalid")
    if script_name:
        if (script_name not in {"test", "test:unit", "lint", "typecheck", "type-check",
                                "build", "check"}
                or not script_digest.startswith("sha256:")
                or "--ignore-scripts" not in arguments
                or arguments[-2:] != ["run", script_name]):
            raise ValueError("project_check_script_binding_invalid")
    return {**check, "path": path}


def _verify_npm_script_binding(root: Path, check: dict) -> bool:
    if not check["script_name"]:
        return True
    package_path = root / PurePosixPath(check["package_directory"]) / "package.json"
    current = root
    for part in PurePosixPath(check["package_directory"]).parts + ("package.json",):
        current = current / part
        if current.is_symlink():
            return False
    try:
        if package_path.stat().st_size > 1024 * 1024:
            return False
        package = json.loads(package_path.read_text(encoding="utf-8"))
        script = package.get("scripts", {}).get(check["script_name"])
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return False
    return (type(script) is str and "sha256:" + hashlib.sha256(script.encode("utf-8")).hexdigest()
            == check["script_sha256"])


def _check_artifact(root: Path, artifact: object) -> tuple[str, str]:
    fields = {"criterion_id", "path", "kind", "expected"}
    if not isinstance(artifact, dict) or set(artifact) != fields:
        raise ValueError("artifact_check_schema_invalid")
    raw_path = artifact["path"]
    if type(raw_path) is not str or not raw_path or "\\" in raw_path:
        raise ValueError("artifact_check_path_invalid")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("artifact_check_path_invalid")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return "fail", f"artifact symlink rejected: {raw_path}"
    if not current.is_file():
        return "fail", f"artifact file missing: {raw_path}"
    if current.stat().st_size > 1024 * 1024:
        return "fail", f"artifact exceeds 1 MiB: {raw_path}"
    data = current.read_bytes()
    kind = artifact["kind"]
    expected = artifact["expected"]
    if kind == "exists":
        passed = True
    elif kind == "nonempty":
        passed = bool(data)
    elif kind == "json":
        try:
            json.loads(data.decode("utf-8"))
            passed = True
        except (UnicodeError, json.JSONDecodeError):
            passed = False
    elif kind == "contains" and type(expected) is str and expected:
        try:
            passed = expected in data.decode("utf-8")
        except UnicodeError:
            passed = False
    else:
        raise ValueError("artifact_check_kind_invalid")
    summary = f"artifact {kind} {'passed' if passed else 'failed'}: {raw_path}"
    return ("pass" if passed else "fail"), summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow-objective-gate")
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--definition-revision", required=True, type=int)
    parser.add_argument("--snapshot-digest", required=True)
    parser.add_argument("--criteria-json", required=True)
    parser.add_argument("--checks-json")
    parser.add_argument("--checks-b64")
    parser.add_argument("--artifacts-json")
    parser.add_argument("--artifacts-b64")
    args = parser.parse_args(argv)
    try:
        criteria = _json_list(args.criteria_json, "criteria")
        if bool(args.checks_json) == bool(args.checks_b64):
            raise ValueError("checks_argument_ambiguous")
        if bool(args.artifacts_json) == bool(args.artifacts_b64):
            raise ValueError("artifacts_argument_ambiguous")
        raw_checks = (_json_list(args.checks_json, "checks") if args.checks_json
                      else _encoded_list(args.checks_b64, "checks"))
        artifacts = (_json_list(args.artifacts_json, "artifacts") if args.artifacts_json
                     else _encoded_list(args.artifacts_b64, "artifacts"))
        if (not criteria or any(type(item) is not str or not item for item in criteria)
                or len(criteria) != len(set(criteria))):
            raise ValueError("criteria_invalid")
        checks = [_validate_check(item) for item in raw_checks]
        if len({item["id"] for item in checks}) != len(checks):
            raise ValueError("project_check_ids_duplicate")
        artifact_ids = [item.get("criterion_id") for item in artifacts if isinstance(item, dict)]
        if (len(artifact_ids) != len(artifacts) or len(set(artifact_ids)) != len(artifact_ids)
                or not set(artifact_ids) <= set(criteria)):
            raise ValueError("artifact_check_criteria_invalid")
        if set(artifact_ids) != set(criteria):
            raise ValueError("objective_artifact_check_required_for_every_criterion")
    except (ValueError, TypeError) as exc:
        print(f"objective_gate_invalid:{exc}", file=sys.stderr)
        return 64

    root = Path.cwd().resolve(strict=True)
    check_results = []
    for check in checks:
        if not _verify_npm_script_binding(root, check):
            return 65
        cwd = root
        if check["package_directory"]:
            try:
                cwd = (root / PurePosixPath(check["package_directory"])).resolve(strict=True)
            except OSError:
                return 69
            if root != cwd and root not in cwd.parents:
                return 65
        try:
            result = subprocess.run(
                [str(check["path"]), *check["arguments"]], cwd=cwd,
                stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
                timeout=540, check=False, shell=False)
        except subprocess.TimeoutExpired:
            return 70
        except OSError:
            return 69
        check_results.append((check["id"], result.returncode))
        if result.returncode != 0:
            return result.returncode or 1

    artifact_results = []
    for artifact in artifacts:
        try:
            status, summary = _check_artifact(root, artifact)
        except (OSError, RuntimeError, ValueError):
            return 65
        artifact_results.append((artifact["criterion_id"], status, summary))
        if status != "pass":
            return 1

    check_summary = ("general project checks passed: " + ", ".join(item["id"] for item in checks)
                     if checks else "no configured general project checks")
    artifact_summaries = {criterion_id: summary for criterion_id, _status, summary in artifact_results}
    evidence = []
    for criterion in criteria:
        objective_summary = artifact_summaries[criterion]
        summary = f"{check_summary}; objective criterion evidence: {objective_summary}"[:512]
        evidence.append({"criterion_id": criterion, "status": "declared", "summary": summary})
    report = {
        "schema_version": 1,
        "gate_id": args.gate_id,
        "definition_revision": args.definition_revision,
        "snapshot_digest": args.snapshot_digest,
        "status": "declared",
        "evidence": evidence,
    }
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
