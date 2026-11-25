from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from agentflow import cli
from agentflow.agents.contracts import AgentResult, ProcessSpec
from agentflow.apl.initial_planning import create_context
from agentflow.apl.failure_recovery import QualityResult
from agentflow.git.change_tracker import GitChangeTracker
from agentflow.state.review import ReviewStore, inspect_review
from agentflow.state.roles import RoleConfigurationStore
from test_execution_roles import fake_registry, report, workspace
from test_apl_initial_planning import proposed


def install_agents(monkeypatch, root, trace, *, correction="VALUE=2\n", invented=False, expand=False, mutate=False):
    data = proposed(create_context(root, "Set VALUE to 2"))
    data["tasks"] = data["tasks"][:1]
    data["tasks"][0]["description"] = "Set src/example.py VALUE to 2 and retain all existing tests."
    registry = fake_registry(data, trace)
    executor = registry.resolve("gemini")
    original_prepare = executor.prepare
    def prepare(request):
        if request.task_id != "code-correction":
            return original_prepare(request)
        trace.append(("gemini", request.task_id))
        packet = json.loads(request.instruction.split("\n", 1)[1])
        content = packet["context"]["context"]["src/example.py"]
        answer = {"schema_version": 1, "edits": [{"path": "src/example.py",
                  "before_sha256": content["sha256"], "content": correction}]}
        event = {"type": "message", "role": "assistant", "content": json.dumps(answer)}
        program = "import sys; sys.stdin.read(); print(" + repr(json.dumps(event)) + ")"
        return ProcessSpec(Path(sys.executable).resolve(), ("-c", program), request.workspace,
            request.instruction, (), "gemini", "1.0.0", "unknown", "unknown")
    executor.prepare = prepare

    class Reviewer:
        adapter_id = "claude-code"
        api_version = "1"
        executable = Path(sys.executable).resolve()
        def _native(self):
            return self
        def discover(self):
            return report("claude-code").availability
        def prepare(self, request):
            assert request.task_id == "code-review"
            trace.append(("claude-code", request.task_id))
            packet = json.loads(request.instruction.split("\n", 1)[1])
            source = packet["context"]["src/example.py"]["text"]
            if expand and "tests/test_existing.py" not in packet["context"]:
                answer = {"schema_version": 1, "state": "INCOMPLETE", "findings": [], "context_requests": ["tests/test_existing.py"]}
            elif source == "VALUE=2\n":
                answer = {"schema_version": 1, "state": "APPROVED", "findings": [], "context_requests": []}
            else:
                answer = {"schema_version": 1, "state": "CORRECTIONS_REQUIRED", "context_requests": [], "findings": [{
                    "id": "model-id", "path": "src/example.py", "line": 1, "end_line": 1,
                    "severity": "high", "evidence": "INVENTED" if invented else "VALUE",
                    "description": "VALUE differs from the explicitly required value 2.",
                    "impact": "Consumers receive an incorrect value.", "suggestion": "Set VALUE to 2.",
                    "confidence": 0.99, "resolution": "open"}]}
            event = {"type": "result", "result": json.dumps(answer)}
            program = "import sys; sys.stdin.read(); "
            if mutate:
                program += "from pathlib import Path; Path(" + repr(str(root / "notes/user-note.txt")) + ").write_text('external edit'); "
            program += "print(" + repr(json.dumps(event)) + ")"
            return ProcessSpec(self.executable, ("-c", program), request.workspace,
                               request.instruction, (), self.adapter_id, "1.0.0", "unknown", "unknown")
        def interpret(self, result):
            return AgentResult(result.status, "review response")
    registry.register(report("claude-code").descriptor, Reviewer())
    monkeypatch.setattr("agentflow.agents.factory.create_registry", lambda: registry)
    return ["start", "Set VALUE to 2", "--path", str(root), "--orchestrator-agent", "antigravity",
            "--executor-agent", "gemini", "--reviewer-agent", "claude-code", "--operation-id", "review-demo"]


def session(root):
    path = root / ".agentflow/state.sqlite3"
    with ReviewStore(path) as store:
        operation = store.db.execute("SELECT operation_id FROM review_sessions").fetchone()[0]
        return operation, store.load(operation)


def test_real_runtime_end_to_end_review_correct_gates_re_review_and_orchestrator(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    (root / "notes").mkdir()
    (root / "notes/user-note.txt").write_text("pre-existing user note")
    trace = []
    args = install_agents(monkeypatch, root, trace)
    assert cli.main(args) == 0, capsys.readouterr()
    assert (root / "src/example.py").read_text() == "VALUE=2\n"
    assert (root / "notes/user-note.txt").read_text() == "pre-existing user note"
    assert trace.count(("claude-code", "code-review")) == 2
    assert trace.count(("gemini", "code-correction")) == 1
    assert trace.index(("gemini", "code-correction")) < trace.index(("antigravity", "result-decision"))
    operation, review = session(root)
    final = review["events"][-1]
    assert final["state"] == "APPROVED"
    assert final["payload"]["usage"]["calls"] == 3
    assert len([event for event in review["events"] if event["key"].startswith("gates-") and event["state"] == QualityResult.PASS.value]) == 2
    assert next(event for event in review["events"] if event["key"] == "resolution")["payload"]["resolved_findings"]
    initial_count = len(trace)
    assert cli.main(args) == 0
    assert len(trace) == initial_count
    assert cli.main(["review-status", "--path", str(root), "--operation-id", operation]) == 0
    assert inspect_review(root / ".agentflow/state.sqlite3", operation)["pending_or_unknown"] is False
    assert review["binding"]["roles"]["reviewer"]["agent_id"] == "claude-code"


@pytest.mark.parametrize("options", [
    ["--review-max-corrections", "0"], ["--review-max-calls", "1"],
    ["--review-max-payload-bytes", "1024"]])
def test_limits_block_without_looping_and_send_pending_to_orchestrator(tmp_path, monkeypatch, capsys, options):
    root = workspace(tmp_path)
    trace = []
    args = install_agents(monkeypatch, root, trace)
    assert cli.main(args + options) == 2
    assert (root / "src/example.py").read_text() == "VALUE=1\n"
    assert trace.count(("gemini", "code-correction")) == 0
    assert ("antigravity", "result-decision") in trace
    _, review = session(root)
    assert review["events"][-1]["state"] == "BLOCKED"
    assert "limit" in review["events"][-1]["payload"]["reason"]
    with RoleConfigurationStore(root / ".agentflow/state.sqlite3") as store:
        envelope = json.loads(store.db.execute("SELECT input_json FROM role_calls WHERE kind='result_decision'").fetchone()[0])
        receipt = next(item for item in envelope["outcome"]["evidence"] if item["kind"] == "git_review")
        assert receipt["state"] == "BLOCKED"
        assert "limit" in receipt["reason"]


def test_invented_evidence_never_triggers_correction(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    trace = []
    args = install_agents(monkeypatch, root, trace, invented=True)
    assert cli.main(args) == 2
    assert not any(task == "code-correction" for _, task in trace)
    assert (root / "src/example.py").read_text() == "VALUE=1\n"
    _, review = session(root)
    assert "evidence_not_in_snapshot" in review["events"][-1]["payload"]["reason"]


@pytest.mark.parametrize("correction", ["VALUE=1\n", "VALUE=3\n"])
def test_no_progress_stops_corrections(tmp_path, monkeypatch, capsys, correction):
    root = workspace(tmp_path)
    trace = []
    args = install_agents(monkeypatch, root, trace, correction=correction)
    assert cli.main(args) == 2
    assert trace.count(("gemini", "code-correction")) == 1
    _, review = session(root)
    assert "progress" in review["events"][-1]["payload"]["reason"]


def test_context_expansion_is_bounded_and_recovers_supported_context(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    trace = []
    args = install_agents(monkeypatch, root, trace, expand=True)
    assert cli.main(args) == 0, capsys.readouterr()
    assert trace.count(("claude-code", "code-review")) == 3


def test_external_writer_invalidates_review_without_overwriting_user_content(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    (root / "notes").mkdir()
    (root / "notes/user-note.txt").write_text("original user note")
    trace = []
    args = install_agents(monkeypatch, root, trace, mutate=True)
    assert cli.main(args) == 2
    assert trace.count(("gemini", "code-correction")) == 0
    assert (root / "notes/user-note.txt").read_text() == "external edit"
    _, review = session(root)
    assert "changed_during_analysis" in review["events"][-1]["payload"]["reason"]


def test_interruption_after_publication_never_reexecutes_or_republishes(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    trace = []
    args = install_agents(monkeypatch, root, trace)
    original = GitChangeTracker.publish
    def interrupted(self, *values):
        original(self, *values)
        raise KeyboardInterrupt()
    monkeypatch.setattr(GitChangeTracker, "publish", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli.main(args)
    assert (root / "src/example.py").read_text() == "VALUE=2\n"
    operation, review = session(root)
    count = len(trace)
    monkeypatch.setattr(GitChangeTracker, "publish", original)
    assert cli.main(["apl", "run-resume", "--path", str(root), "--run-id", review["run_id"]]) == 2
    assert len(trace) == count
    status = inspect_review(root / ".agentflow/state.sqlite3", operation)
    assert status["pending_or_unknown"] is True
    assert (root / "src/example.py").read_text() == "VALUE=2\n"


def test_v9_migration_rolls_back_and_keeps_history(tmp_path, monkeypatch):
    import agentflow.state.sqlite as state_module
    from agentflow.state.contracts import IntegrityError
    path = tmp_path / "state.sqlite3"
    with RoleConfigurationStore(path) as store:
        store.request("pending-request", {"objective": "preserve"})
    monkeypatch.setattr(state_module, "REVIEW_SCHEMA", state_module.REVIEW_SCHEMA + ("INVALID SQL",))
    with pytest.raises(IntegrityError, match="v9"):
        ReviewStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert db.execute("SELECT request_id FROM role_requests").fetchone()[0] == "pending-request"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='review_sessions'").fetchone() is None
    assert list(tmp_path.glob("state.sqlite3.*.bak"))


def test_approved_cache_is_invalidated_by_content_or_engine_identity(tmp_path, monkeypatch, capsys):
    from agentflow.apl.code_review import ReviewAcceptanceValidator, _outcome
    from agentflow.git.provider import GitLocalProvider
    from agentflow.state.review import ReviewJournal
    from agentflow.apl.contracts import JobProposal
    root = workspace(tmp_path)
    args = install_agents(monkeypatch, root, [])
    assert cli.main(args) == 0, capsys.readouterr()
    _, review = session(root)
    final = review["events"][-1]["payload"]
    outcome = _outcome(final["outcome"])
    job = JobProposal(review["job_id"], "work")
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        journal = ReviewJournal(root / ".agentflow/state.sqlite3")
        validator = ReviewAcceptanceValidator(None, tracker, journal, implementation=review["binding"]["implementation"])
        assert validator.evaluate(review["cycle_id"], job, outcome).quality is QualityResult.PASS
        changed_contract = ReviewAcceptanceValidator(None, tracker, journal, implementation="different-contract")
        assert changed_contract.evaluate(review["cycle_id"], job, outcome).quality is QualityResult.INCONCLUSIVE
        (root / "src/example.py").write_text("VALUE=999\n")
        assert validator.evaluate(review["cycle_id"], job, outcome).quality is QualityResult.INCONCLUSIVE


def test_existing_v8_request_keeps_immutable_policy_and_does_not_enable_review_silently(tmp_path, monkeypatch, capsys):
    import hashlib
    from agentflow.init import initialize
    from test_execution_roles import configuration
    from test_apl_initial_planning import workspace as uncommitted_workspace
    root = uncommitted_workspace(tmp_path)
    initialize(root)
    objective = "Legacy behavior"
    operation = "legacy-v8-request"
    request_id = hashlib.sha256((str(root.resolve()) + "\0" + operation).encode()).hexdigest()[:32]
    data = {"workspace": str(root.resolve()), "objective": objective, "ready_plan": None, "master_plan": None,
        "limits": {"max_seconds": 3600, "max_estimated_cost": 0.10, "max_attempts_per_job": 3,
                   "task_timeout": 1800, "planner_timeout": 240, "planner_max_plan_reads": 8, "idle_seconds": 300}}
    with RoleConfigurationStore(root / ".agentflow/state.sqlite3") as store:
        store.request(request_id, data)
        store.configure(request_id, configuration())
    trace = []
    plan = proposed(create_context(root, objective))
    monkeypatch.setattr("agentflow.agents.factory.create_registry", lambda: fake_registry(plan, trace))
    assert cli.main(["start", objective, "--path", str(root), "--operation-id", operation]) == 0, capsys.readouterr()
    assert not any(task in {"code-review", "code-correction"} for _, task in trace)
    with RoleConfigurationStore(root / ".agentflow/state.sqlite3") as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert store.input_data(request_id) == data


def test_dirty_user_file_in_scope_blocks_before_executor_and_preserves_content(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    target = root / "tests/test_existing.py"
    target.write_text("# user change\ndef test_marker():\n    assert True\n")
    before = target.read_bytes()
    trace = []
    args = install_agents(monkeypatch, root, trace)
    assert cli.main(args) == 2
    assert not any(agent == "gemini" for agent, _ in trace)
    assert target.read_bytes() == before
    _, review = session(root)
    assert review["events"][-1]["payload"]["reason"] == "preexisting_changes_in_task_scope"


def test_ignored_user_source_in_scope_blocks_before_executor(tmp_path, monkeypatch, capsys):
    root = workspace(tmp_path)
    (root / ".gitignore").write_text("src/private.py\n")
    (root / "src").mkdir()
    target = root / "src/private.py"
    target.write_text("# existing ignored user source\n")
    before = target.read_bytes()
    trace = []
    args = install_agents(monkeypatch, root, trace)
    assert cli.main(args) == 2
    assert not any(agent == "gemini" for agent, _ in trace)
    assert target.read_bytes() == before
    _, review = session(root)
    assert "ignored_source" in review["events"][-1]["payload"]["reason"]
