import json
from pathlib import Path

import pytest

from agentflow import cli
from agentflow.cli import main
from agentflow.config import load_config
from agentflow.init import initialize
from agentflow.planning import PlanError, load_plan
from agentflow.planning.prompts import next_task_prompt, task_instruction
from agentflow.planning.task_source import read_task_objective, read_task_source, TaskSourcePlanner
from test_apl_initial_planning import workspace, proposed
from agentflow.apl.initial_planning import validate_initial_plan


def test_init_default_and_legacy_none_are_usable_without_rewriting(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("agentflow.agents.discovery.discover_agents", lambda *a, **kw: [])
    root = workspace(tmp_path)
    assert main(["init", "--path", str(root)]) == 0
    target = root / ".agentflow" / "config.toml"
    assert load_config(target).agent == "codex"
    target.write_text('schema_version = 1\nagent = "none"\n# preserve me\n')
    before = target.read_bytes()
    assert initialize(root, agent="codex") == (root.resolve(), False)
    assert load_config(target).effective_agent == "codex"
    assert target.read_bytes() == before
    assert not (root / ".agentflow" / "security-profile.toml").exists()


@pytest.mark.parametrize("explicit_file", [False, True])
def test_run_reads_backlog_without_authentication_and_preserves_source(
        tmp_path, monkeypatch, explicit_file):
    root = workspace(tmp_path)
    filename = "backlog.json" if explicit_file else "TASKS.json"
    source = root / filename
    source.write_text(json.dumps({"tasks": [
        {"id": "implementation", "description": "Implement requested behavior", "depends_on": []},
        {"id": "verification", "title": "Verify requested behavior", "depends_on": ["implementation"]},
    ]}), encoding="utf-8")
    original = source.read_bytes()
    contexts = []

    class Planner:
        def __init__(self, *args, **kwargs):
            pass

        def plan_initial(self, context):
            contexts.append(context)
            return validate_initial_plan(proposed(context), context)

    monkeypatch.setattr("agentflow.apl.codex_planner.CodexCLIPlannerAdapter", Planner)
    delegated = []
    monkeypatch.setattr(cli, "main", lambda argv: delegated.append(argv) or 0)
    args = ["run", "--path", str(root)]
    if explicit_file:
        args += ["--tasks-file", filename]
    assert main(args) == 0
    assert len(contexts) == 1
    assert "Implement requested behavior" in contexts[0].objective
    assert delegated[0][:2] == ["apl", "run"]
    assert source.read_bytes() == original
    assert not (root / ".agentflow" / "security-profile.toml").exists()
    assert not (root / ".agentflow" / "local-consent.toml").exists()
    plan = load_plan(root / ".agentflow")
    instruction = task_instruction(plan.tasks[0])
    prompt = next_task_prompt(plan.tasks[0].id, instruction, plan.tasks[0].versioned_paths)
    assert plan.tasks[0].acceptance_criteria[0].description in prompt
    assert plan.tasks[1].description not in prompt
    assert contexts[0].objective not in prompt
    assert main(args) == 0
    assert len(contexts) == 1  # Persistent plan is reused, not replanned.


@pytest.mark.parametrize("payload", [
    "{}", "[]", '[{"description":""}]', '[1]', '{"tasks":[],"tasks":[]}',
    '{"tasks":[{"id":"one","title":"A","depends_on":["missing"]}]}',
    '{"tasks":[{"id":"one","title":"A","depends_on":["one"]}]}',
    '{"tasks":[{"id":"one","title":"A"},{"id":"one","title":"B"}]}',
    '{"tasks":[{"title":"A"}],"value":NaN}', "x" * 8193,
])
def test_invalid_backlog_fails_before_planning_or_writes(tmp_path, payload, capsys):
    root = workspace(tmp_path)
    source = root / "TASKS.json"
    source.write_text(payload, encoding="utf-8")
    assert main(["run", "--path", str(root)]) == 2
    assert not (root / ".agentflow").exists()
    assert source.read_text(encoding="utf-8") == payload


def test_missing_backlog_and_ambiguous_input_are_controlled(tmp_path):
    root = workspace(tmp_path)
    assert main(["run", "--path", str(root)]) == 2
    assert main(["run", "objective", "--tasks-file", "TASKS.json", "--path", str(root)]) == 2
    assert not (root / ".agentflow").exists()


def test_simple_backlog_list_is_read_without_discarding_content(tmp_path):
    path = tmp_path / "TASKS.json"
    path.write_text('["Implement one feature", "Document that feature"]')
    objective = read_task_objective(path)
    assert "Implement one feature" in objective and "Document that feature" in objective


def test_simple_text_containing_type_does_not_require_typed_queue(tmp_path, monkeypatch):
    from test_apl_initial_planning import Planner

    root = workspace(tmp_path)
    (root / "TASKS.json").write_text('{"tasks":["Implement type inference"]}')
    monkeypatch.setattr("agentflow.apl.codex_planner.CodexCLIPlannerAdapter",
                        lambda *args, **kwargs: Planner())
    monkeypatch.setattr(cli, "main", lambda args: 0)
    assert main(["run", "--path", str(root)]) == 0


def test_planner_cannot_replace_declared_task_identity_or_dependencies(tmp_path):
    path = tmp_path / "TASKS.json"
    path.write_text('[{"id":"one","title":"Task","depends_on":[]}]')
    source = read_task_source(path)

    class Planner:
        def plan_initial(self, context):
            return {"tasks": [{"id": "two", "depends_on": []}]}

    with pytest.raises(PlanError, match="IDs"):
        TaskSourcePlanner(source, Planner()).plan_initial(None)

    class ChangedDependencies:
        def plan_initial(self, context):
            return {"tasks": [{"id": "one", "depends_on": ["two"]}]}

    with pytest.raises(PlanError, match="dependências"):
        TaskSourcePlanner(source, ChangedDependencies()).plan_initial(None)


def test_obsolete_enrollment_never_opens_authentication_provider(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("must not invoke authentication")

    monkeypatch.setattr("agentflow.security.windows_webauthn.WindowsWebAuthnApi", unexpected)
    assert main(["webauthn-enroll", "--broker-db", str(tmp_path / "broker.db")]) == 2
    assert main(["local-auth", "enroll", "--confirm-bootstrap", "--path", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []


def test_next_cli_prints_only_the_next_task_without_redundant_criteria(tmp_path, capsys):
    from agentflow.apl import SQLiteCycleStore, SupervisedCycleService
    from agentflow.apl.initial_planning import prepare_objective_plan
    from agentflow.state.execution import ExecutionStore
    from test_apl_initial_planning import Planner

    root = workspace(tmp_path)
    plan, _, _ = prepare_objective_plan(root, "Implement requested behavior", "next-test", Planner())
    state_path = root / ".agentflow" / "state.sqlite3"
    with ExecutionStore(state_path) as state:
        state.import_plan(plan)
    cycles = SQLiteCycleStore(state_path)
    scope = tuple(sorted({path for task in plan.tasks for path in task.versioned_paths}))
    cycle = SupervisedCycleService(cycles, state_path).start(plan, "Requested work", scope)
    assert main(["apl", "next", "--path", str(root), "--cycle-id", cycle.cycle_id]) == 0
    output = capsys.readouterr().out
    assert plan.tasks[0].description in output
    assert plan.tasks[1].description not in output
    assert output.count(plan.tasks[0].acceptance_criteria[0].id) == 1
    assert "Escopo:" in output
    assert "JSON minificado" in output
    assert not (root / ".agentflow" / "security-profile.toml").exists()
