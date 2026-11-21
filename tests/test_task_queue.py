import json

import pytest

from agentflow import cli
from agentflow.cli import main
from agentflow.planning import PlanError, load_plan
from agentflow.planning.task_queue import prepare_active_milestone
from agentflow.planning.task_source import read_task_source, TaskSourcePlanner, task_source_objective
from agentflow.apl.initial_planning import validate_initial_plan
from test_apl_initial_planning import workspace, proposed


MASTER = """# Plano Mestre
## Objetivo e limites
Implementar o comportamento local, sem serviços externos.
## Contratos e invariantes
SQLite guarda estado; schemas versionados.
## Marcos verticais
M0: primeiro fluxo completo. M1: expansão posterior.
## Saída dos marcos
M0: comportamento coberto por testes. M1: expansão demonstrada por testes.
"""


def queue():
    return {"schema_version": 1, "master_plan": "IMPLEMENTATION_PLAN.md", "active_milestone": "M0",
            "tasks": [
                {"id": "M0", "type": "milestone", "depends_on": [],
                 "goal": "Primeiro fluxo completo", "acceptance": ["Fluxo testado"]},
                {"id": "M1", "type": "milestone", "depends_on": ["M0"],
                 "goal": "Expansão", "acceptance": ["Expansão testada"]},
                {"id": "implementation", "type": "work", "parent_id": "M0", "depends_on": [],
                 "goal": "Implement the behavior and add focused tests.",
                 "acceptance": ["Focused tests verify the behavior."]},
                {"id": "verification", "type": "work", "parent_id": "M0",
                 "depends_on": ["implementation"],
                 "goal": "Run and review the focused project tests.",
                 "acceptance": ["The project test suite exits successfully."]},
            ]}


def write_queue(root, data=None):
    (root / "IMPLEMENTATION_PLAN.md").write_text(MASTER, encoding="utf-8")
    source = root / "TASKS.json"
    source.write_text(json.dumps(data or queue()), encoding="utf-8")
    return source


def test_typed_queue_reads_master_and_compiles_only_active_work(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    source = write_queue(root)
    originals = source.read_bytes(), (root / "IMPLEMENTATION_PLAN.md").read_bytes()
    contexts = []

    class Planner:
        def __init__(self, *args, **kwargs):
            pass

        def plan_initial(self, context):
            contexts.append(context)
            return validate_initial_plan(proposed(context), context)

    monkeypatch.setattr("agentflow.apl.codex_planner.CodexCLIPlannerAdapter", Planner)
    delegated = []
    monkeypatch.setattr(cli, "main", lambda args: delegated.append(args) or 0)
    assert main(["run", "--path", str(root)]) == 0
    compact = json.loads(contexts[0].master_plan)
    assert compact["path"] == "IMPLEMENTATION_PLAN.md"
    assert compact["toc"] and compact["sha256"].startswith("sha256:")
    assert MASTER not in contexts[0].to_mapping()["master_plan_markdown"]
    assert contexts[0].to_mapping()["master_plan_context"]["path"] == "IMPLEMENTATION_PLAN.md"
    assert "Expansão" not in contexts[0].objective
    assert {task.id for task in load_plan(root / ".agentflow").tasks} == {"implementation", "verification"}
    assert source.read_bytes() == originals[0]
    assert (root / "IMPLEMENTATION_PLAN.md").read_bytes() == originals[1]


def test_future_milestones_do_not_require_early_work_decomposition(tmp_path):
    root = workspace(tmp_path)
    data = queue()
    for index in range(2, 20):
        data["tasks"].append({"id": f"M{index}", "type": "milestone", "goal": f"Entrega {index}",
                              "depends_on": [f"M{index - 1}"], "acceptance": ["Testes passam"]})
    source = write_queue(root, data)
    active = prepare_active_milestone(root, read_task_source(source))
    assert len(active.source["tasks"]) == 2


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(schema_version=2),
    lambda data: data.update(active_milestone="missing"),
    lambda data: data["tasks"][2].update(parent_id="missing"),
    lambda data: data["tasks"][2].update(acceptance=[]),
    lambda data: data["tasks"][2].update(goal=""),
    lambda data: data["tasks"][2].update(depends_on=["M1"]),
    lambda data: data["tasks"][0].update(depends_on=["M1"]),
    lambda data: data.update(master_plan="missing.md"),
])
def test_invalid_queue_or_missing_master_blocks_before_writes(tmp_path, mutation):
    root = workspace(tmp_path)
    data = queue()
    mutation(data)
    write_queue(root, data)
    assert main(["run", "--path", str(root)]) == 2
    assert not (root / ".agentflow").exists()


def test_milestone_progress_requires_matching_persisted_run_not_json_status(tmp_path):
    root = workspace(tmp_path)
    data = queue()
    write_queue(root)
    original = prepare_active_milestone(root, data)
    expected = task_source_objective(original.source)
    data["active_milestone"] = "M1"
    data["tasks"][0].update(status="completed")
    data["tasks"].append({"id": "expansion", "type": "work", "parent_id": "M1", "depends_on": [],
                          "goal": "Implement expansion", "acceptance": ["Expansion tested"]})
    with pytest.raises(PlanError, match="completed_run_id"):
        prepare_active_milestone(root, data)
    data["tasks"][0]["completed_run_id"] = "run-previous"
    seen = []

    def completed(run_id, objective):
        seen.append((run_id, objective))
        return run_id == "run-previous" and objective == expected

    active = prepare_active_milestone(root, data, completed)
    assert [task["id"] for task in active.source["tasks"]] == ["expansion"]
    assert seen == [("run-previous", expected)]
    (root / "IMPLEMENTATION_PLAN.md").write_text(MASTER + "\nChanged invariant", encoding="utf-8")
    with pytest.raises(PlanError, match="completed_run_id"):
        prepare_active_milestone(root, data, completed)


def test_planner_must_preserve_work_goal_and_binary_acceptance(tmp_path):
    root = workspace(tmp_path)
    write_queue(root)
    active = prepare_active_milestone(root, queue())

    class Planner:
        def plan_initial(self, context):
            return {"tasks": [
                {"id": "implementation", "description": "Changed goal", "depends_on": [],
                 "criteria": [("check", "Focused tests verify the behavior.")]},
                {"id": "verification", "description": "Run and review the focused project tests.",
                 "depends_on": ["implementation"], "criteria": [("check", "Other acceptance")]},
            ]}

    planner = TaskSourcePlanner(active.source, Planner())
    with pytest.raises(PlanError, match="goal"):
        planner.plan_initial(None)
    active.source["tasks"][0]["goal"] = "Changed goal"
    with pytest.raises(PlanError, match="acceptance"):
        TaskSourcePlanner(active.source, Planner()).plan_initial(None)


def test_milestone_completion_reads_verified_run_without_initializing_store(tmp_path):
    from dataclasses import replace
    from test_apl_authorized_execution import _setup, _grant, _snapshot

    root = workspace(tmp_path)
    directory = root / ".agentflow"
    directory.mkdir()
    grant = replace(_grant(_snapshot()), workspace_path=str(root.resolve()))
    path, cycles, runs, resolver, runner, snapshot = _setup(directory, grant=grant)
    result = runner.start(snapshot.cycle_id, grant.grant_id)
    before = path.read_bytes()
    assert cli._completed_milestone_run(root, result["run_id"], snapshot.objective)
    assert not cli._completed_milestone_run(root, result["run_id"], "changed goal")
    assert path.read_bytes() == before
    other = root / "other"
    other.mkdir()
    assert not cli._completed_milestone_run(other, result["run_id"], snapshot.objective)
