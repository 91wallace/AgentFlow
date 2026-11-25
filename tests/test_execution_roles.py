from dataclasses import asdict, replace
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from agentflow import cli
from agentflow.agents.contracts import (AgentAvailability, AgentCapabilities, AgentResult,
    ExecutionError, ProcessSpec, normalize_execution_state)
from agentflow.agents.discovery import AgentDiscovery
from agentflow.agents.factory import SelectedAdapter
from agentflow.agents.registry import AdapterDescriptor, AgentRegistry
from agentflow.agents.roles import ExecutionRoles
from agentflow.apl.role_selection import binding_for, validate_roles
from agentflow.apl.role_orchestration import JournaledOrchestrator
from agentflow.config import AgentChoice
from agentflow.role_cli import choose_roles
from agentflow.state.contracts import ConflictError
from agentflow.state.roles import RoleConfigurationStore, RoleCallJournal, roles_for_cycle
from agentflow.state.execution import ExecutionStore
from test_apl_initial_planning import workspace as uncommitted_workspace, proposed


def workspace(tmp_path):
    from test_git_provider import git
    root = uncommitted_workspace(tmp_path)
    git(root, "config", "user.name", "AgentFlow Test")
    git(root, "config", "user.email", "agentflow-test@example.test")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "commit", "-qm", "synthetic baseline")
    return root


def report(agent="antigravity", status="available"):
    return AgentDiscovery(AdapterDescriptor(agent, "1", "1", sys.executable, ("windows", "linux")),
        AgentAvailability(agent, status, str(Path(sys.executable).resolve()), "1.0.0", "test",
                          "version_observed", "2026-11-23T12:00:00Z"))


def configuration():
    first, second = binding_for(report()), binding_for(report("gemini"))
    return ExecutionRoles(first, second, first)


def test_v8_migration_preserves_existing_state_and_makes_backup(tmp_path):
    from test_planning import files, TASKS, SPEC
    from agentflow.planning import load_plan
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path) as old:
        old.import_plan(load_plan(files(tmp_path / "plan", TASKS, SPEC)))
        original = old.db.execute("SELECT * FROM tasks").fetchall()
        assert old.db.execute("PRAGMA user_version").fetchone()[0] == 3
    with RoleConfigurationStore(path) as state:
        assert state.db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert state.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert state.db.execute("SELECT * FROM tasks").fetchall() == original
        assert state.db.execute("SELECT version FROM schema_migrations").fetchall() == [(i,) for i in range(1, 9)]
    assert list(tmp_path.glob("state.sqlite3.*.bak"))
    with ExecutionStore(path):
        pass  # Legacy execution reader also opens the additive schema.


def test_selection_persists_all_roles_and_resume_does_not_prompt(tmp_path):
    path = tmp_path / "state.sqlite3"
    reports = [report(), report("gemini")]
    requested = {"orchestrator": {"agent": "antigravity"}, "executor": {"agent": "gemini"}}
    with RoleConfigurationStore(path) as store:
        store.request("request", {"objective": "test"})
        roles = choose_roles(store, "request", reports, requested, input_stream=StringIO(), output_stream=StringIO())
        assert roles.reviewer == roles.orchestrator
        from agentflow.apl.persistence import SQLiteCycleStore
        from agentflow.apl.contracts import CycleSnapshot, APLState
        SQLiteCycleStore(path).create(CycleSnapshot("cycle", "test", ("tests",), APLState.READY, (), (), 0, 0))
        store.bind_cycle("request", "cycle")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.db.execute("DELETE FROM role_configurations")
    with RoleConfigurationStore(path) as store:
        assert store.request("request", {"objective": "test"}) == roles
        assert choose_roles(store, "request", reports, {}, input_stream=StringIO(), output_stream=StringIO()) == roles
        with pytest.raises(ConflictError):
            store.request("request", {"objective": "changed"})
        with pytest.raises(ValueError, match="immutable"):
            choose_roles(store, "request", reports, {"executor": {"agent": "antigravity"}},
                         input_stream=StringIO(), output_stream=StringIO())
    assert roles_for_cycle(path, "cycle") == ("request", roles)


def test_partial_configuration_is_awaiting_and_reuses_recorded_answer(tmp_path):
    with RoleConfigurationStore(tmp_path / "state.sqlite3") as store:
        store.request("request", {})
        reports = [report(), report("gemini")]
        assert choose_roles(store, "request", reports, {"orchestrator": {"agent": "antigravity"}},
                            input_stream=StringIO(), output_stream=StringIO()) is None
        assert store.configuration("request") is None
        assert "orchestrator" in store.draft_selections("request")
        roles = choose_roles(store, "request", reports, {"executor": {"agent": "gemini"}},
                             input_stream=StringIO(), output_stream=StringIO())
        assert roles.orchestrator.agent_id == "antigravity"


def test_unavailable_version_changed_or_capability_unavailable_blocks():
    with pytest.raises(ValueError, match="unavailable"):
        binding_for(report(status="unavailable"))
    with pytest.raises(ValueError, match="identity_changed"):
        validate_roles(configuration(), [replace(report(), availability=replace(report().availability, version="2.0.0")), report("gemini")])
    caps = AgentCapabilities("unavailable", (), False, False, False,
                             "agentflow.runtime.local", "agentflow.runtime.local",
                             "agentflow.state.execution", "agentflow.state.execution")
    with pytest.raises(ValueError, match="cannot_initialize"):
        binding_for(replace(report(), capabilities=caps))


def test_model_configuration_never_claims_attestation_and_native_is_allowed():
    configured = binding_for(report("gemini"), provider="native", model="configured-model")
    assert configured.model_status == "configured-unverified"
    assert configured.model_selection.model_id == "configured-model"
    assert binding_for(report("gemini")).model_status == "cli-default"


def test_unverified_model_path_accepts_unknown_but_rejects_mismatch():
    class Native:
        api_version = "1"
        executable = Path(sys.executable)
        def interpret(self, result):
            return result
    selected = SelectedAdapter(Native(), AgentChoice("gemini", "native", "chosen"), allow_unverified_model=True)
    assert selected.collect_result(AgentResult("process_succeeded", "done")).selection_status == "unknown"
    with pytest.raises(ExecutionError):
        selected.collect_result(AgentResult("process_succeeded", "done", effective_model_id="other", selection_status="mismatch"))


def test_journal_reuses_result_and_never_reissues_ambiguous_model_call(tmp_path):
    path = tmp_path / "state.sqlite3"
    with RoleConfigurationStore(path) as store:
        store.request("request", {})
        store.configure("request", configuration())
        result = store.call("request", "planning", {"input": 1}, lambda: {"tasks": []})
    journal = RoleCallJournal(path)
    assert journal.call("request", "planning", {"input": 1}, lambda: pytest.fail("replayed")) == result
    def interrupted():
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        journal.call("request", "planning", {"input": 2}, interrupted)
    with pytest.raises(ConflictError, match="unknown_or_running"):
        journal.call("request", "planning", {"input": 2}, lambda: pytest.fail("replayed"))


def test_noninteractive_start_waits_without_planner_or_executor(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    registry = AgentRegistry()
    monkeypatch.setattr("agentflow.agents.factory.create_registry", lambda: registry)
    monkeypatch.setattr("agentflow.apl.codex_planner.CodexCLIPlannerAdapter", lambda *a, **kw: pytest.fail("inference before configuration"))
    assert cli.main(["start", "Feature", "--path", str(root)]) == 2
    assert "AWAITING_CONFIGURATION" in capsys.readouterr().out
    with RoleConfigurationStore(root / ".agentflow/state.sqlite3") as store:
        assert store.db.execute("SELECT COUNT(*) FROM role_requests").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM role_calls").fetchone()[0] == 0


def fake_registry(plan_data, trace, *, decision="CONTINUE", executor_pass=True):
    class Native:
        api_version = "1"
        def __init__(self, agent):
            self.agent = self.adapter_id = agent
            self.executable = Path(sys.executable).resolve()
        def _native(self):
            return self
        def discover(self):
            return report(self.agent).availability
        def prepare(self, request):
            trace.append((self.agent, request.task_id))
            # Actual subprocesses use the existing LocalRuntime. They emulate
            # agent streams; they don't perform paid/model inference.
            if request.task_id in {"initial-plan", "plan-decision", "result-decision", "code-review"}:
                answer = ({"schema_version": 1, "decision": decision if request.task_id == "result-decision" else "CONTINUE", "reason": "verified evidence received"}
                          if request.task_id in {"result-decision", "plan-decision"} else plan_data)
                if request.task_id == "code-review":
                    answer = {"schema_version": 1, "state": "APPROVED", "findings": [], "context_requests": []}
                event = {"event": "result", "result": {"response": json.dumps(answer)}}
                program = "import sys; sys.stdin.read(); print(" + repr(json.dumps(event)) + ")"
            else:
                handoff = {"ok": True, "files": ["tests/test_requested.py"], "exports": [],
                           "decisions": [], "verified": ["independent gates required"], "next_notes": [], "blocker": None}
                event = {"type": "message", "role": "assistant", "content": json.dumps(handoff)}
                if self.agent == "antigravity":
                    event = {"event": "result", "result": {"response": json.dumps(handoff)}}
                program = ("import sys; from pathlib import Path; sys.stdin.read(); "
                    "Path('src').mkdir(exist_ok=True); Path('src/example.py').write_text('VALUE=1\\n'); "
                    "Path('tests/test_requested.py').write_text('def test_requested():\\n    assert True\\n'); "
                    "print(" + repr(json.dumps(event)) + ")")
                if not executor_pass:
                    program = program.replace("assert True", "assert False")
            return ProcessSpec(self.executable, ("-c", program), request.workspace, request.instruction,
                               (("AGENTFLOW_MANAGED_EXECUTION", "1"),), self.agent, "1.0.0", "unknown", "unknown")
        def interpret(self, result):
            return AgentResult(result.status, "structured result captured")
    registry = AgentRegistry()
    for agent in ("antigravity", "gemini"):
        registry.register(report(agent).descriptor, Native(agent))
    return registry


def test_start_plans_delegates_validates_returns_to_other_agent_and_recovers(tmp_path, monkeypatch, capsys):
    from agentflow.apl.initial_planning import create_context
    root = workspace(tmp_path)
    objective = "Implement requested behavior"
    data = proposed(create_context(root, objective))
    trace = []
    monkeypatch.setattr("agentflow.agents.factory.create_registry", lambda: fake_registry(data, trace))
    args = ["start", objective, "--path", str(root), "--orchestrator-agent", "antigravity",
            "--executor-agent", "gemini", "--operation-id", "multiagent-demo"]
    assert cli.main(args + ["--configure-only"]) == 0
    assert trace == []
    assert cli.main(args) == 0, capsys.readouterr()
    output = capsys.readouterr().out
    lines = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    result = lines[-1]
    assert result["status"] == "completed"
    assert trace[0][0] == "antigravity"
    executor_calls = [item for item in trace if item[0] == "gemini"]
    decisions = [item for item in trace if item == ("antigravity", "result-decision")]
    assert len(executor_calls) == len(decisions) == 2
    assert trace.index(decisions[0]) < trace.index(executor_calls[1])
    path = root / ".agentflow/state.sqlite3"
    with RoleConfigurationStore(path) as store:
        assert store.db.execute("SELECT COUNT(*) FROM role_configurations").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM role_call_results").fetchone()[0] == 6
        for call_id, kind, encoded in store.db.execute("SELECT call_id,kind,input_json FROM role_calls"):
            log = root / ".agentflow/runs/apl-planner" / ("role-" + call_id.removeprefix("sha256:") + "-1")
            assert (log / "metadata.json").is_file()
            if kind == "result_decision":
                envelope = json.loads(encoded)
                assert envelope["outcome"]["handoff"]["ok"] is True
                assert any(item["kind"] == "acceptance_evaluation" for item in envelope["outcome"]["evidence"])
    count = len(trace)
    assert cli.main(args) == 0
    assert len(trace) == count
    assert cli.main(["apl", "run-resume", "--path", str(root), "--run-id", result["run_id"]]) == 0
    assert len(trace) == count
    capsys.readouterr()
    assert cli.main(["apl", "run-status", "--path", str(root), "--run-id", result["run_id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["roles"]["orchestrator"]["agent_id"] == "antigravity"
    assert status["roles"]["executor"]["agent_id"] == "gemini"
    assert status["orchestrator_seconds"] > 0


@pytest.mark.parametrize("decision,executor_pass", [("BLOCK", True), ("INVALID", True), ("CONTINUE", False)])
def test_orchestrator_cannot_waive_gates_and_block_stops_next_executor(tmp_path, monkeypatch, capsys, decision, executor_pass):
    from agentflow.apl.initial_planning import create_context
    root = workspace(tmp_path)
    data = proposed(create_context(root, "Feature"))
    trace = []
    monkeypatch.setattr("agentflow.agents.factory.create_registry",
                        lambda: fake_registry(data, trace, decision=decision, executor_pass=executor_pass))
    args = ["start", "Feature", "--path", str(root), "--orchestrator-agent", "antigravity", "--executor-agent", "gemini"]
    assert cli.main(args) == 2
    assert len([item for item in trace if item[0] == "gemini"]) == 1
    assert ("antigravity", "result-decision") in trace
    with RoleConfigurationStore(root / ".agentflow/state.sqlite3") as store:
        rows = store.db.execute("SELECT status FROM apl_authorized_run_tasks ORDER BY rowid").fetchall()
        assert rows[0][0] == ("completed" if executor_pass else "failed")
        assert rows[1][0] == "ready"


def test_restart_between_task_checkpoint_and_decision_does_not_reexecute_task(tmp_path, monkeypatch, capsys):
    from agentflow.apl.initial_planning import create_context
    from agentflow.apl.authorized_execution import AuthorizedExecutionController
    root = workspace(tmp_path)
    trace = []
    data = proposed(create_context(root, "Feature"))
    monkeypatch.setattr("agentflow.agents.factory.create_registry", lambda: fake_registry(data, trace))
    original = AuthorizedExecutionController._observe_results
    def interrupt(self, run_id, snapshot, state):
        if any(task["status"] == "completed" for task in state["tasks"]):
            raise KeyboardInterrupt()
        return original(self, run_id, snapshot, state)
    monkeypatch.setattr(AuthorizedExecutionController, "_observe_results", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["start", "Feature", "--path", str(root), "--orchestrator-agent", "antigravity", "--executor-agent", "gemini"])
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    run_id = next(item["run_id"] for item in output if item.get("event") == "run_persisted")
    assert len([item for item in trace if item[0] == "gemini"]) == 1
    monkeypatch.setattr(AuthorizedExecutionController, "_observe_results", original)
    assert cli.main(["apl", "run-resume", "--path", str(root), "--run-id", run_id]) == 0, capsys.readouterr()
    assert len([item for item in trace if item[0] == "gemini"]) == 2
    assert len([item for item in trace if item == ("antigravity", "result-decision")]) == 2


def test_interactive_cli_asks_models_and_suggests_same_reviewer(tmp_path):
    class Terminal(StringIO):
        def isatty(self):
            return True
    with RoleConfigurationStore(tmp_path / "state.sqlite3") as store:
        store.request("request", {})
        output = StringIO()
        selected = choose_roles(store, "request", [report(), report("gemini")], {},
            input_stream=Terminal("antigravity\n\ngemini\n\n\n"), output_stream=output)
        assert selected.reviewer == selected.orchestrator
        assert "same" in output.getvalue()


def test_concurrent_clients_claim_one_orchestrator_call(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    path = tmp_path / "state.sqlite3"
    with RoleConfigurationStore(path) as store:
        store.request("request", {})
        store.configure("request", configuration())
    entered, release = Event(), Event()
    def invoke():
        entered.set()
        assert release.wait(10)
        return {"done": True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(RoleCallJournal(path).call, "request", "initial_plan", {}, invoke)
        try:
            assert entered.wait(10)
            with pytest.raises(ConflictError, match="unknown_or_running"):
                RoleCallJournal(path).call("request", "initial_plan", {}, lambda: pytest.fail("duplicate inference"))
        finally:
            release.set()
        assert first.result() == {"done": True}


def test_schema_v8_migration_failure_rolls_back_and_preserves_backup(tmp_path, monkeypatch):
    import agentflow.state.sqlite as state_module
    from agentflow.state.contracts import IntegrityError
    path = tmp_path / "state.sqlite3"
    with state_module.SQLiteStore(path, execution=True, git=True, validation=True, agent_models=True, sdd=True):
        pass
    monkeypatch.setattr(state_module, "ROLE_SCHEMA", state_module.ROLE_SCHEMA + ("INVALID SQL",))
    with pytest.raises(IntegrityError, match="v8"):
        RoleConfigurationStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("SELECT name FROM sqlite_master WHERE name='role_requests'").fetchone() is None
    assert list(tmp_path.glob("state.sqlite3.*.bak"))


def test_gemini_manual_model_is_forwarded_without_fabricating_provider(tmp_path):
    from test_gemini_adapter import adapter, request, result, events
    native = adapter(tmp_path)
    selected = SelectedAdapter(native, AgentChoice("gemini", "native", "chosen"), allow_unverified_model=True)
    spec = selected.prepare(request(tmp_path))
    assert spec.arguments[-2:] == ("--model", "chosen")
    output = result(tmp_path, events(), request_metadata=(("provider_id", "native"), ("model_id", "chosen")))
    interpreted = selected.collect_result(output)
    assert interpreted.selection_status == "unknown"
    assert interpreted.effective_provider_id == "unknown"


def test_same_agent_roles_use_distinct_processes_and_preserve_structured_results(tmp_path, monkeypatch, capsys):
    from agentflow.apl.initial_planning import create_context
    root = workspace(tmp_path)
    trace = []
    data = proposed(create_context(root, "Feature"))
    monkeypatch.setattr("agentflow.agents.factory.create_registry", lambda: fake_registry(data, trace))
    assert cli.main(["start", "Feature", "--path", str(root), "--orchestrator-agent", "antigravity",
                     "--executor-agent", "antigravity"]) == 0, capsys.readouterr()
    assert ("antigravity", "implementation") in trace
    assert ("antigravity", "result-decision") in trace
    with RoleConfigurationStore(root / ".agentflow/state.sqlite3") as store:
        for encoded, in store.db.execute("SELECT outcome_json FROM apl_authorized_run_tasks"):
            receipt = json.loads(encoded)["evidence"][0]
            assert "apl-planner" not in receipt["stdout_path"]
            assert Path(receipt["stdout_path"]).is_file()


def test_cancelled_orchestrator_does_not_claim_or_invoke_model(tmp_path):
    from threading import Event
    from types import SimpleNamespace
    from agentflow.apl.contracts import JobProposal, ExecutionOutcome
    path = tmp_path / "state.sqlite3"
    with RoleConfigurationStore(path) as store:
        store.request("request", {})
        store.configure("request", configuration())
    cancel = Event()
    cancel.set()
    orchestrator = JournaledOrchestrator(RoleCallJournal(path), "request", SimpleNamespace())
    with pytest.raises(InterruptedError):
        orchestrator.observe_result("cycle", JobProposal("task", "work"), ExecutionOutcome("succeeded", "done"), cancellation=cancel)
    with RoleConfigurationStore(path) as store:
        assert store.db.execute("SELECT COUNT(*) FROM role_calls").fetchone()[0] == 0


def test_completed_decisions_consume_time_and_cache_does_not_charge_model_twice(tmp_path):
    from types import SimpleNamespace
    from agentflow.apl.contracts import JobProposal, ExecutionOutcome
    path = tmp_path / "state.sqlite3"
    with RoleConfigurationStore(path) as store:
        store.request("request", {})
        store.configure("request", configuration())
    journal = RoleCallJournal(path)
    planner = SimpleNamespace(timeout_seconds=240, invoke_structured=lambda *args: {
        "schema_version": 1, "decision": "CONTINUE", "reason": "okay"})
    orchestrator = JournaledOrchestrator(journal, "request", planner)
    arguments = ("cycle", JobProposal("task", "work"), ExecutionOutcome("succeeded", "done"))
    orchestrator.observe_result(*arguments, remaining_seconds=30)
    consumed = journal.completed_decision_seconds("request")
    assert consumed > 0
    orchestrator.observe_result(*arguments, remaining_seconds=30)
    assert journal.completed_decision_seconds("request") == consumed
    with pytest.raises(ValueError, match="time_limit"):
        orchestrator.observe_result(*arguments, remaining_seconds=0)
