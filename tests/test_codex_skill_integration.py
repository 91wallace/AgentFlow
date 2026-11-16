import json
from pathlib import Path

import pytest

from agentflow.codex_skill import CodexSkillInstallError, install_codex_skill
from agentflow import cli as cli_module
from agentflow.cli import main
from agentflow.planning import load_plan
from agentflow.state.execution import ExecutionStore
from agentflow.apl import SQLiteCycleStore
from test_phase5_planning import SPEC_V2, TASKS_V3
from test_apl_initial_planning import proposed
from agentflow.apl.initial_planning import validate_initial_plan


def test_workspace_skill_install_is_non_destructive_and_resolves_python(tmp_path):
    (tmp_path / ".git").mkdir()
    result = install_codex_skill(workspace=tmp_path, python_executable=__import__("sys").executable)
    skill = Path(result["skill_path"])
    assert result["status"] == "installed"
    assert skill.is_file()
    assert "name: agentflow" in skill.read_text(encoding="utf-8")
    runtime = json.loads(Path(result["runtime_path"]).read_text(encoding="utf-8"))
    assert Path(runtime["python"]).is_file()
    assert runtime["module"] == "agentflow"
    assert install_codex_skill(workspace=tmp_path)["status"] == "unchanged"


def test_workspace_skill_install_refuses_unmanaged_destination(tmp_path):
    (tmp_path / ".git").mkdir()
    skill = tmp_path / ".agents" / "skills" / "agentflow"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("user content", encoding="utf-8")
    with pytest.raises(CodexSkillInstallError, match="unmanaged_content_preserved"):
        install_codex_skill(workspace=tmp_path)
    assert (skill / "SKILL.md").read_text(encoding="utf-8") == "user content"


def test_skill_update_refuses_user_edited_managed_file(tmp_path):
    (tmp_path / ".git").mkdir()
    install_codex_skill(workspace=tmp_path)
    skill = tmp_path / ".agents" / "skills" / "agentflow" / "SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    with pytest.raises(CodexSkillInstallError, match="modified_by_user"):
        install_codex_skill(workspace=tmp_path, update=True)


def test_global_skill_install_requires_explicit_option_and_does_not_edit_codex_config(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    result = install_codex_skill(workspace=tmp_path, global_install=True)
    assert result["scope"] == "global"
    assert Path(result["skill_path"]).is_relative_to(home / ".agents" / "skills")
    assert not (home / ".codex" / "config.toml").exists()


def test_codex_child_cannot_start_nested_agentflow_run(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_MANAGED_EXECUTION", "1")
    assert main(["run", "test objective", "--path", "."]) == 2


def test_objective_run_persists_cycle_and_delegates_to_existing_apl_runner(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "src").mkdir()
    control = root / ".agentflow"
    control.mkdir()
    (control / "spec.toml").write_text(
        SPEC_V2.replace('objective = "Validate a demo project"',
                        'objective = "Add a tiny feature"'), encoding="utf-8")
    tasks = TASKS_V3.replace('versioned_paths = []', 'versioned_paths = ["src"]')
    (control / "tasks.toml").write_text(tasks, encoding="utf-8")
    plan = load_plan(control)
    state_path = control / "state.sqlite3"
    with ExecutionStore(state_path) as state:
        state.import_plan(plan)
    delegated = []
    monkeypatch.setattr(cli_module, "main", lambda argv=None: delegated.append(argv) or 0)

    assert main(["run", "Add a tiny feature", "--path", str(root),
                 "--allow-unconfined-network"]) == 0
    first = delegated[-1]
    cycle_id = first[first.index("--cycle-id") + 1]
    operation_id = first[first.index("--operation-id") + 1]
    cycles = SQLiteCycleStore(state_path, initialize=False)
    snapshot = cycles.load(cycle_id)
    assert snapshot.objective == "Add a tiny feature"
    assert snapshot.jobs[0].required_scope == ("src",)
    assert "network:unconfined" in snapshot.authorized_scope

    # An identical objective and immutable plan resolve to the same operation/cycle.
    assert main(["run", "Add a tiny feature", "--path", str(root),
                 "--allow-unconfined-network"]) == 0
    assert delegated[-1][delegated[-1].index("--cycle-id") + 1] == cycle_id
    assert delegated[-1][delegated[-1].index("--operation-id") + 1] == operation_id


def test_objective_run_creates_initial_plan_without_workspace_agentflow_config(
        tmp_path, monkeypatch, capsys):
    root = tmp_path / "fresh-project"
    root.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_existing.py").write_text(
        "def test_marker():\n    assert True\n", encoding="utf-8")

    class FakeInitialPlanner:
        def __init__(self, *args, **kwargs):
            pass

        def plan_initial(self, context):
            return validate_initial_plan(proposed(context), context)

    monkeypatch.setattr("agentflow.apl.codex_planner.CodexCLIPlannerAdapter",
                        FakeInitialPlanner)
    delegated = []
    monkeypatch.setattr(cli_module, "main", lambda argv=None: delegated.append(argv) or 0)
    assert main(["run", "Add a first feature", "--path", str(root),
                 "--allow-unconfined-network"]) == 0
    assert (root / ".agentflow" / "spec.toml").is_file()
    assert (root / ".agentflow" / "tasks.toml").is_file()
    assert (root / ".agentflow" / "state.sqlite3").is_file()
    assert "--plan-dir" in delegated[0]
    assert "--allow-unconfined-network" not in delegated[0]
    out = capsys.readouterr().out
    assert '"event": "cycle_persisted"' in out

