"""Non-destructive installer for the AgentFlow Codex CLI skill."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


class CodexSkillInstallError(ValueError):
    pass


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_symlink_path(path: Path, *, stop: Path) -> None:
    current = stop
    for part in path.relative_to(stop).parts:
        current = current / part
        if current.is_symlink():
            raise CodexSkillInstallError("skill_install_symlink_destination_refused")


def install_codex_skill(*, workspace: Path, global_install: bool = False,
                        update: bool = False, python_executable: str | None = None) -> dict:
    """Install workspace-scoped by default; never replaces unmanaged user content."""
    if type(global_install) is not bool or type(update) is not bool:
        raise CodexSkillInstallError("skill_install_options_invalid")
    if global_install:
        root = Path.home().resolve(strict=True)
        target = root / ".agents" / "skills" / "agentflow"
    else:
        root = workspace.resolve(strict=True)
        if not (root / ".git").exists():
            raise CodexSkillInstallError("skill_workspace_must_be_git_root")
        target = root / ".agents" / "skills" / "agentflow"
    _reject_symlink_path(target, stop=root)
    source = Path(__file__).parent / "integrations" / "codex_skill" / "SKILL.md"
    if not source.is_file():
        raise CodexSkillInstallError("bundled_codex_skill_missing")
    target.parent.mkdir(parents=True, exist_ok=True)
    skill_target = target / "SKILL.md"
    manifest_target = target / ".agentflow-managed.json"
    desired_digest = _digest(source)

    if target.exists() or target.is_symlink():
        if not target.is_dir() or not manifest_target.is_file() or not skill_target.is_file():
            raise CodexSkillInstallError("skill_destination_conflict_unmanaged_content_preserved")
        if manifest_target.is_symlink() or skill_target.is_symlink():
            raise CodexSkillInstallError("skill_install_symlink_destination_refused")
        try:
            manifest = json.loads(manifest_target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexSkillInstallError("skill_management_manifest_invalid") from exc
        current_digest = _digest(skill_target)
        if manifest.get("managed_file") != "SKILL.md" or manifest.get("sha256") != current_digest:
            raise CodexSkillInstallError("skill_modified_by_user_update_refused")
        if current_digest != desired_digest and not update:
            raise CodexSkillInstallError("skill_update_requires_--update")
        if current_digest == desired_digest:
            status = "unchanged"
        else:
            status = "updated"
    else:
        target.mkdir()
        status = "installed"

    runtime_path = target / "agentflow-runtime.json"
    if runtime_path.is_symlink():
        raise CodexSkillInstallError("skill_install_symlink_destination_refused")
    python_path = str(Path(python_executable or sys.executable).resolve())
    if not Path(python_path).is_file():
        raise CodexSkillInstallError("agentflow_python_runtime_missing")
    # Write managed files atomically. Runtime path is machine-local and is ignored
    # by the repository's normal Git rules when installing into a workspace.
    temporary = skill_target.with_suffix(".tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, skill_target)
    runtime_tmp = runtime_path.with_suffix(".tmp")
    runtime_tmp.write_text(json.dumps({"python": python_path, "module": "agentflow"},
                                      sort_keys=True) + "\n", encoding="utf-8")
    os.replace(runtime_tmp, runtime_path)
    manifest_tmp = manifest_target.with_suffix(".tmp")
    manifest_tmp.write_text(json.dumps({"managed_file": "SKILL.md",
                                        "sha256": desired_digest}, sort_keys=True) + "\n",
                            encoding="utf-8")
    os.replace(manifest_tmp, manifest_target)
    return {"status": status, "scope": "global" if global_install else "workspace",
            "skill_path": str(skill_target), "runtime_path": str(runtime_path),
            "python": python_path}
