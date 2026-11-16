from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from agentflow.agents import AgentResult, ProcessIdentity, ProcessResult, ProcessSpec
from agentflow.apl.authorized_execution import (
    AuthorizedExecutionController, AuthorizedExecutionError, ExecutionLimits,
    FinancialBudgetMode, FinancialBudgetPolicy, SQLiteAuthorizedExecutionStore,
    TaskExecutionLimits, VerifiedExecutionGrant,
)
from agentflow.apl.codex_execution import (
    CodexAcceptanceEvidenceValidator, CodexAuthorizedJobExecutor, CodexExecutionError,
    PinnedWorkspace,
)
from agentflow.apl.contracts import APLState, CycleSnapshot, ExecutionOutcome, JobProposal
from agentflow.apl.persistence import SQLiteCycleStore


class Adapter:
    def prepare_task(self, request):
        self.request = request
        return ProcessSpec(Path("C:/bin/codex.exe"), ("exec",), request.workspace,
                           request.instruction, (), "codex", "0.166.0",
                           "unknown", request.policy.approval)

    def collect_result(self, result):
        return AgentResult(result.status, "Codex turn completed", (), "codex", "test", "selected")

    @staticmethod
    def normalize_state(status):
        from agentflow.agents import normalize_execution_state
        return normalize_execution_state(status)


class Runtime:
    def __init__(self, logs: Path, *, status="process_succeeded"):
        self.logs, self.status = logs, status
        self.calls = []

    def run(self, spec, attempt_id, policy, on_started, cancel, before_spawn=None):
        self.calls.append((spec, attempt_id, policy, cancel))
        if before_spawn is not None:
            before_spawn()
        on_started(ProcessIdentity(321, "start-token", "321", 12, attempt_id, "opaque"))
        out = self.logs / f"{attempt_id}.out"
        err = self.logs / f"{attempt_id}.err"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('{"type":"turn.completed"}\n', encoding="utf-8")
        err.write_text("", encoding="utf-8")
        return ProcessResult(self.status, 0 if self.status == "process_succeeded" else 1,
                             out, err, "sha256:" + "a" * 64, "sha256:" + "b" * 64,
                             out.stat().st_size, err.stat().st_size, self.status)


class WorkspaceResolver:
    def __init__(self, root, workspace_id, digest=None):
        self.root, self.workspace_id, self.digest = root, workspace_id, digest

    def resolve(self, cycle_id):
        canonical = self.root.resolve(strict=True)
        info = canonical.stat()
        identity = f"{info.st_dev}:{info.st_ino}"
        digest = "sha256:" + hashlib.sha256(
            f"{str(canonical).casefold()}\0{identity}".encode()).hexdigest()
        # Use the platform's same normcase convention as the consent resolver.
        import os
        digest = "sha256:" + hashlib.sha256(
            f"{os.path.normcase(str(canonical))}\0{identity}".encode()).hexdigest()
        return PinnedWorkspace(canonical, "synthetic-project", self.workspace_id,
                               self.digest or digest)


class GrantResolver:
    def __init__(self, grant):
        self.grant = grant

    def resolve_and_validate(self, grant_id, cycle_id):
        if grant_id != self.grant.grant_id or self.grant.state != "approved":
            raise ValueError("unknown grant")
        return self.grant


class Accept:
    def validate(self, cycle_id, job, outcome):
        return job.acceptance_criteria == ("file exists",) and len(outcome.evidence) == 1


def test_authorized_controller_uses_existing_codex_adapter_runtime_and_persists_receipt(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = JobProposal("create-note", "Create note.txt containing hello", (), ("workspace",),
                      ("file exists",))
    snapshot = CycleSnapshot("cycle-codex", "create a note", ("workspace",), APLState.READY,
                             (job,), (), 0, 0, plan_digest="sha256:" + "c" * 64,
                             plan_revision_id="revision-codex")
    info = workspace.stat()
    import os
    identity_digest = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(workspace.resolve()))}\0{info.st_dev}:{info.st_ino}".encode()
    ).hexdigest()
    grant = VerifiedExecutionGrant(
        "grant-codex", "sha256:" + "b" * 64, "proposal-codex", "sha256:" + "a" * 64,
        1, snapshot.plan_revision_id, snapshot.plan_digest, "workspace-codex", str(workspace),
        identity_digest, ("job:create-note",), snapshot.authorized_scope,
        (datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1)).isoformat(),
        ExecutionLimits(1, 1, 120),
        operator_identity="operator:synthetic",
        executor_id="codex", executor_version="0.166.0",
        task_limits_by_job=(("create-note", TaskExecutionLimits(1, 120, 1)),),
        budget_policy=FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, 1, "USD"),
    )
    adapter, runtime = Adapter(), Runtime(tmp_path / "logs")
    executor = CodexAuthorizedJobExecutor(
        adapter=adapter, runtime=runtime,
        workspace_resolver=WorkspaceResolver(workspace, grant.workspace_id),
    )
    from agentflow.apl.persistence import SQLiteCycleStore
    from agentflow.state.execution import ExecutionStore
    db_path = tmp_path / "state.sqlite3"
    with ExecutionStore(db_path):
        pass
    cycles = SQLiteCycleStore(db_path)
    cycles.create(snapshot)
    runs = SQLiteAuthorizedExecutionStore(str(db_path))
    controller = AuthorizedExecutionController(
        cycle_store=cycles, execution_store=runs,
        grant_resolver=GrantResolver(grant), executor=executor,
        evidence_validator=CodexAcceptanceEvidenceValidator(Accept()),
    )

    result = controller.start(snapshot.cycle_id, grant.grant_id)

    assert result["status"] == "completed"
    spec, operation_id, policy, cancellation = runtime.calls[0]
    assert spec.cwd == workspace.resolve()
    assert spec.effective_sandbox == "unknown"
    assert spec.effective_approval == "never"
    assert 1 <= policy.timeout_seconds <= 120
    assert operation_id.startswith("apl-attempt-")
    assert cancellation.is_set() is False
    task = runs.load(result["run_id"])["tasks"][0]
    assert task["status"] == "completed"
    assert task["process_identity_json"]
    assert json.loads(task["outcome_json"])["evidence"][0]["kind"] == "codex_process_receipt"
    assert json.loads(task["outcome_json"])["evidence"][0]["operation_id"].startswith("apl-op-")


def test_grant_revocation_at_last_pre_spawn_gate_prevents_started_process(tmp_path):
    from dataclasses import replace

    workspace = tmp_path / "project"
    workspace.mkdir()
    job = JobProposal("create-note", "Create note.txt", (), ("workspace",), ("file exists",))
    snapshot = CycleSnapshot("cycle-codex-revoke", "create note", ("workspace",),
                             APLState.READY, (job,), (), 0, 0,
                             plan_digest="sha256:" + "c" * 64,
                             plan_revision_id="revision-codex-revoke")
    info = workspace.stat()
    import os
    identity_digest = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(workspace.resolve()))}\0{info.st_dev}:{info.st_ino}".encode()
    ).hexdigest()
    grant = VerifiedExecutionGrant(
        "grant-codex-revoke", "sha256:" + "b" * 64, "proposal-codex-revoke",
        "sha256:" + "a" * 64, 1, snapshot.plan_revision_id, snapshot.plan_digest,
        "workspace-codex-revoke", str(workspace), identity_digest,
        ("job:create-note",), snapshot.authorized_scope, "2099-01-01T00:00:00+00:00",
        ExecutionLimits(1, 1, 120), operator_identity="operator:synthetic",
        executor_id="codex", executor_version="0.166.0",
        task_limits_by_job=(("create-note", TaskExecutionLimits(1, 120, 1)),),
        budget_policy=FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, 1, "USD"),
    )
    resolver = GrantResolver(grant)

    class RevokingAdapter(Adapter):
        def prepare_task(self, request):
            spec = super().prepare_task(request)
            resolver.grant = replace(resolver.grant, state="revoked")
            return spec

    runtime = Runtime(tmp_path / "logs")
    executor = CodexAuthorizedJobExecutor(
        adapter=RevokingAdapter(), runtime=runtime,
        workspace_resolver=WorkspaceResolver(workspace, grant.workspace_id),
    )
    from agentflow.apl.persistence import SQLiteCycleStore
    from agentflow.state.execution import ExecutionStore
    db_path = tmp_path / "revoke.sqlite3"
    with ExecutionStore(db_path):
        pass
    cycles = SQLiteCycleStore(db_path)
    cycles.create(snapshot)
    runs = SQLiteAuthorizedExecutionStore(str(db_path))
    controller = AuthorizedExecutionController(
        cycle_store=cycles, execution_store=runs, grant_resolver=resolver,
        executor=executor, evidence_validator=CodexAcceptanceEvidenceValidator(Accept()))

    with pytest.raises(AuthorizedExecutionError, match="ifr_intervention_required"):
        controller.start(snapshot.cycle_id, grant.grant_id)
    import sqlite3
    with sqlite3.connect(db_path) as db:
        run_id = db.execute("SELECT run_id FROM apl_authorized_runs").fetchone()[0]
    persisted = runs.load(run_id)
    assert runtime.calls[0][1] is not None
    assert all(event["event_type"] != "task.process_started" for event in persisted["events"])
    assert any(event["event_type"] == "run.revocation_cancel_requested"
               for event in persisted["events"])


def test_codex_bridge_rejects_changed_workspace_identity_before_runtime(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    job = JobProposal("one", "do thing", required_scope=("workspace",))
    info = workspace.stat()
    import os, json
    digest = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(workspace.resolve()))}\0{info.st_dev}:{info.st_ino}".encode()
    ).hexdigest()
    grant = VerifiedExecutionGrant(
        "g", "sha256:" + "b" * 64, "p", "sha256:" + "a" * 64, 1, "rev",
        "sha256:" + "c" * 64, "wid", str(workspace), "sha256:" + "d" * 64,
        ("job:one",), ("workspace",), "2099-01-01T00:00:00+00:00", ExecutionLimits(1, 1, 10),
        operator_identity="operator:synthetic",
        executor_id="codex", executor_version="0.166.0",
        task_limits_by_job=(("one", TaskExecutionLimits(1, 10, 1)),),
        budget_policy=FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, 1, "USD"))
    runtime = Runtime(tmp_path / "logs")
    executor = CodexAuthorizedJobExecutor(
        adapter=Adapter(), runtime=runtime,
        workspace_resolver=WorkspaceResolver(workspace, "wid", digest),
    )
    with pytest.raises(CodexExecutionError, match="workspace_binding_changed"):
        executor.execute_authorized("cycle", job, grant, "apl-op-" + "1" * 32,
                                    cancellation=object(), on_started=lambda _: None)
    assert runtime.calls == []


def test_codex_bridge_rejects_job_without_explicit_operation_coverage(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    import os
    info = workspace.stat()
    digest = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(workspace.resolve()))}\0{info.st_dev}:{info.st_ino}".encode()
    ).hexdigest()
    job = JobProposal("extra", "run extra", required_scope=("workspace",))
    grant = VerifiedExecutionGrant(
        "g", "sha256:" + "b" * 64, "p", "sha256:" + "a" * 64, 1, "rev",
        "sha256:" + "c" * 64, "wid", str(workspace), digest,
        ("job:other",), ("workspace",), "2099-01-01T00:00:00+00:00", ExecutionLimits(1, 1, 10),
        operator_identity="operator:synthetic",
        executor_id="codex", executor_version="0.166.0",
        task_limits_by_job=(("extra", TaskExecutionLimits(1, 10, 1)),),
        budget_policy=FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, 1, "USD"))
    runtime = Runtime(tmp_path / "logs")
    executor = CodexAuthorizedJobExecutor(
        adapter=Adapter(), runtime=runtime,
        workspace_resolver=WorkspaceResolver(workspace, "wid"),
    )
    with pytest.raises(CodexExecutionError, match="job_not_authorized_by_grant"):
        executor.execute_authorized("cycle", job, grant, "apl-op-" + "1" * 32,
                                    cancellation=object(), on_started=lambda _: None)
    assert runtime.calls == []


def test_codex_acceptance_requires_process_receipt_and_acceptance_verifier():
    from agentflow.apl.codex_execution import CodexAcceptanceEvidenceValidator

    job = JobProposal("one", "do", acceptance_criteria=("passes",))
    validator = CodexAcceptanceEvidenceValidator(Accept())
    no_receipt = ExecutionOutcome("succeeded", "ok", ())
    assert validator.validate("cycle", job, no_receipt) is False
    receipt = ExecutionOutcome("succeeded", "ok", ({
        "kind": "codex_process_receipt", "process_status": "process_succeeded",
        "exit_code": 0, "stdout_digest": "sha256:x", "stderr_digest": "sha256:y",
    },))
    assert validator.validate("cycle", job, receipt) is False


def test_plan_gate_acceptance_verifier_requires_independent_gate_and_persists_evidence(tmp_path):
    import os
    from agentflow.apl.codex_execution import PlanGateAcceptanceVerifier
    from agentflow.apl.failure_recovery import QualityResult
    from agentflow.planning.models import (AllowlistedCommand, Criterion, GateDefinition,
                                           Plan, Specification, Task)
    from agentflow.validation import (CapabilityState, RuntimeCapabilities,
                                     RuntimeCapability, RuntimeProfile, RuntimeResult)

    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text("def answer(): return 42\n", encoding="utf-8")
    criteria = (Criterion("criterion-a", "answer returns 42"),)
    gate = GateDefinition("gate-a", 1, ("criterion-a",), "verify-a")
    command = AllowlistedCommand(
        "verify-a", "windows" if os.name == "nt" else "linux",
        str(tmp_path / "synthetic-validator.exe"), "sha256:" + "a" * 64,
        ("--gate", "{gate_id}", "--revision", "{definition_revision}",
         "--snapshot", "{snapshot_digest}"), "snapshot", "test-v1")
    task = Task("job-a", "verify", "", criteria, gates=(gate,))
    plan = Plan(Specification("project", "title", "objective", criteria,
                              (command,)), (task,), ("job-a",), "spec", "tasks",
                gate_order=("gate-a",))

    class Runtime:
        def inspect_capabilities(self, profile):
            return RuntimeCapabilities(profile, "synthetic-runtime", "1", (
                RuntimeCapability("network_none", CapabilityState.ENFORCED),))

        def run(self, request, cancellation, on_spawn, on_probe):
            gate_id, revision, snapshot_digest = (
                request.arguments[1], request.arguments[3], request.arguments[5])
            report = {"schema_version": 1, "gate_id": gate_id,
                      "definition_revision": int(revision), "snapshot_digest": snapshot_digest,
                      "status": "declared", "evidence": [{
                          "criterion_id": "criterion-a", "status": "declared",
                          "summary": "Independent gate checked the expected result."}]}
            stdout_path = request.output_directory / "stdout.log"
            stderr_path = request.output_directory / "stderr.log"
            raw = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
            stdout_path.write_bytes(raw)
            stderr_path.write_bytes(b"")
            return RuntimeResult(
                0, False, False, False, "synthetic-process", stdout_path, stderr_path,
                "sha256:" + hashlib.sha256(raw).hexdigest(),
                "sha256:" + hashlib.sha256(b"").hexdigest(),
                len(raw), 0, False, self.inspect_capabilities(RuntimeProfile.LOCAL_EXPLICIT),
                process_stopped=True, logs_sanitized=True)

    verifier = PlanGateAcceptanceVerifier(
        plan=plan, workspace=root, authorized_scope=("src",),
        evidence_directory=tmp_path / "persistent-evidence", runtime=Runtime())
    job = JobProposal("job-a", "verify", (), ("src",), ("criterion-a",))
    outcome = ExecutionOutcome("succeeded", "executor says complete", ({
        "kind": "codex_process_receipt", "operation_id": "apl-op-" + "a" * 32,
        "attempt_id": "apl-attempt-" + "b" * 32,
        "process_status": "process_succeeded", "exit_code": 0,
    },))
    result = verifier.evaluate("cycle-a", job, outcome)
    assert result.quality is QualityResult.PASS
    proof = result.evidence[0]
    assert proof["executor_finished"] and proof["executor_command_succeeded"]
    assert proof["artifacts_status"] == "verified_by_acceptance_gates"
    assert proof["criteria_status"] == "PASS" and proof["work_status"] == "approved"
    assert proof["gate_receipts"][0]["snapshot_digest"].startswith("sha256:")
