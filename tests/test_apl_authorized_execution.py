from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Lock
from threading import Event

import pytest

from agentflow.apl.authorized_execution import (
    AuthorizedExecutionController, AuthorizedExecutionError, ExecutionLimits,
    FinancialBudgetMode, FinancialBudgetPolicy, NoEffectExecutionError, SQLiteAuthorizedExecutionStore,
    TaskExecutionLimits, VerifiedExecutionGrant, LocalExecutionContractResolver,
    LocalAuthorizedExecutionService, create_local_execution_contract,
)
from agentflow.apl.contracts import APLState, CycleSnapshot, ExecutionOutcome, JobProposal
from agentflow.apl.persistence import SQLiteCycleStore
from agentflow.apl.failure_recovery import (EffectState, FailureDiagnosis, FailureKind,
                                            QualityResult, Recoverability)


def _snapshot(jobs=None, *, state=APLState.READY):
    jobs = tuple(jobs or (
        JobProposal("first", "create file", (), ("workspace",), ("file exists",)),
        JobProposal("second", "verify file", ("first",), ("workspace",), ("check passes",)),
    ))
    return CycleSnapshot("cycle-1", "complete a synthetic math helper", ("workspace",),
                         state, jobs, (), 0, 0, plan_digest="sha256:" + "a" * 64,
                         plan_revision_id="revision-1")


def _grant(snapshot, *, operations=None, expiry=None, state="approved"):
    limits = ExecutionLimits(max_jobs=10, max_attempts_per_job=2, max_seconds=30,
                             max_estimated_budget=2)
    return VerifiedExecutionGrant(
        grant_id="grant-1", request_digest="sha256:" + "b" * 64,
        proposal_id="proposal-1", approved_plan_digest="sha256:" + "c" * 64,
        revision=1, plan_revision_id=snapshot.plan_revision_id,
        accepted_plan_digest=snapshot.plan_digest, workspace_id="workspace-id",
        workspace_path="C:/synthetic/workspace",
        workspace_identity_digest="sha256:" + "d" * 64,
        allowed_operations=tuple(operations or (f"job:{job.job_id}" for job in snapshot.jobs)),
        authorized_scope=snapshot.authorized_scope,
        expires_at=expiry or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        limits=limits, state=state, operator_identity="operator:synthetic",
        executor_id="synthetic", executor_version="test-v1",
        task_limits_by_job=tuple((job.job_id, TaskExecutionLimits(2, 30, 2))
                                 for job in snapshot.jobs),
        budget_policy=FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED,
                                            2, "USD"),
    )


class GrantResolver:
    def __init__(self, grant):
        self.grant = grant
        self.calls = 0

    def resolve_and_validate(self, grant_id, cycle_id):
        self.calls += 1
        if grant_id != self.grant.grant_id:
            raise AuthorizedExecutionError("grant_unknown")
        if self.grant.state != "approved":
            raise AuthorizedExecutionError("grant_revoked")
        if datetime.fromisoformat(self.grant.expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise AuthorizedExecutionError("grant_expired")
        return self.grant


class SyntheticExecutor:
    def __init__(self, *, outcomes=None):
        self.calls = []
        self._lock = Lock()
        self.outcomes = list(outcomes or [])

    def execute(self, cycle_id, job, scope, operation_id):
        with self._lock:
            self.calls.append((job.job_id, operation_id))
            outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or ExecutionOutcome("succeeded", f"done {job.job_id}",
                                            ({"evidence_id": len(self.calls), "passed": True},),
                                            progress_key=operation_id)


class EvidenceValidator:
    def validate(self, cycle_id, job, outcome):
        return bool(outcome.evidence) and all(item.get("passed") is True for item in outcome.evidence)


def _setup(tmp_path, jobs=None, *, executor=None, grant=None):
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    cycles = SQLiteCycleStore(path)
    snapshot = _snapshot(jobs)
    cycles.create(snapshot)
    grants = GrantResolver(grant or _grant(snapshot))
    runs = SQLiteAuthorizedExecutionStore(str(path))
    runner = AuthorizedExecutionController(
        cycle_store=cycles, execution_store=runs, grant_resolver=grants,
        executor=executor or SyntheticExecutor(), evidence_validator=EvidenceValidator())
    return path, cycles, runs, grants, runner, snapshot


def test_one_grant_runs_sequential_plan_to_completion_and_persists(tmp_path):
    executor = SyntheticExecutor()
    path, cycles, runs, resolver, runner, _ = _setup(tmp_path, executor=executor)
    result = runner.start("cycle-1", "grant-1")
    assert result["status"] == "completed"
    assert [item[0] for item in executor.calls] == ["first", "second"]
    assert executor.calls[0][1] != executor.calls[1][1]
    assert len(set(item[1] for item in executor.calls)) == 2
    state = runs.load(result["run_id"])
    assert state["reserved_seconds"] == 0
    assert all(task["reserved_seconds"] == 0 for task in state["tasks"])
    assert resolver.calls >= 3  # grant checked at session and immediately before each dispatch
    assert cycles.load("cycle-1").state is APLState.COMPLETED
    assert len(cycles.load("cycle-1").outcomes) == 2
    assert runs.load(result["run_id"])["status"] == "completed"
    assert len(result["events"]) >= 5


def test_passwordless_local_contract_runs_full_plan_from_sqlite_without_authenticator(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os

    executor = SyntheticExecutor()
    path, cycles, runs, _legacy_resolver, _legacy_runner, snapshot = _setup(
        tmp_path, executor=executor)
    workspace_path = tmp_path / "local-workspace"
    workspace_path.mkdir()
    real = workspace_path.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-local", identity_digest=identity)
    original = _grant(snapshot)
    contract = create_local_execution_contract(
        snapshot=snapshot, workspace=workspace, executor_id="synthetic",
        executor_version="test-v1", limits=original.limits,
        task_limits=TaskExecutionLimits(2, 30, 2),
        financial_policy=original.budget_policy,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        operation_id="operator-start-001")
    assert contract.authorization.operator_identity == ""
    assert contract.authorization.authorization_mode == "local_execution_contract"
    runs.persist_local_contract(contract)
    reopened = SQLiteAuthorizedExecutionStore(str(path))
    loaded, state = reopened.load_local_contract(contract.contract_id)
    assert state == "active" and loaded == contract

    class WorkspaceResolver:
        def resolve(self, cycle_id):
            return workspace if cycle_id == snapshot.cycle_id else None

    resolver = LocalExecutionContractResolver(
        contract_store=reopened, cycle_store=cycles, workspace_resolver=WorkspaceResolver(),
        executor_id="synthetic", executor_version="test-v1")
    runner = AuthorizedExecutionController(
        cycle_store=cycles, execution_store=reopened, grant_resolver=resolver,
        executor=executor, evidence_validator=EvidenceValidator())
    result = runner.start(snapshot.cycle_id, contract.contract_id)
    assert result["status"] == "completed"
    assert runs.load_local_contract(contract.contract_id)[1] == "completed"
    assert [item[0] for item in executor.calls] == ["first", "second"]
    assert cycles.load(snapshot.cycle_id).state is APLState.COMPLETED


def test_passwordless_local_contract_rejects_plan_workspace_executor_and_revocation(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os
    from dataclasses import replace

    _path, cycles, runs, _resolver, _runner, snapshot = _setup(tmp_path)
    workspace_path = tmp_path / "workspace-contract"
    workspace_path.mkdir()
    real = workspace_path.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-contract", identity_digest=identity)
    grant = _grant(snapshot)
    contract = create_local_execution_contract(
        snapshot=snapshot, workspace=workspace, executor_id="synthetic",
        executor_version="test-v1", limits=grant.limits,
        task_limits=TaskExecutionLimits(2, 30, 2), financial_policy=grant.budget_policy,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        operation_id="operator-start-002")
    runs.persist_local_contract(contract)

    class WorkspaceResolver:
        def resolve(self, cycle_id):
            return workspace

    resolver = LocalExecutionContractResolver(
        contract_store=runs, cycle_store=cycles, workspace_resolver=WorkspaceResolver(),
        executor_id="synthetic", executor_version="test-v1")
    changed = replace(snapshot, objective="tampered objective")
    cycles.save(changed, expected_iterations=snapshot.iterations,
                expected_revision=snapshot.revision)
    with pytest.raises(AuthorizedExecutionError, match="binding_changed"):
        resolver.resolve_and_validate(contract.contract_id, snapshot.cycle_id)
    assert runs.revoke_local_contract(contract.contract_id)
    with pytest.raises(AuthorizedExecutionError, match="contract_revoked"):
        resolver.resolve_and_validate(contract.contract_id, snapshot.cycle_id)


def test_hard_financial_cap_requirement_blocks_synthetic_dispatch(tmp_path):
    from dataclasses import replace
    snapshot = _snapshot()
    grant = replace(_grant(snapshot), budget_policy=FinancialBudgetPolicy(
        FinancialBudgetMode.HARD_CAP_REQUIRED))
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, executor=executor, grant=grant)
    with pytest.raises(AuthorizedExecutionError, match="financial_hard_cap_unavailable"):
        runner.start("cycle-1", grant.grant_id)
    assert executor.calls == []


def test_passwordless_local_service_starts_and_replays_two_dependent_tasks_once(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os

    executor = SyntheticExecutor()
    path, cycles, _runs, _resolver, _runner, snapshot = _setup(tmp_path, executor=executor)
    root = tmp_path / "operator-workspace"
    root.mkdir()
    real = root.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-cli", identity_digest=identity)

    class Workspaces:
        def resolve(self, cycle_id):
            return workspace

    store = SQLiteAuthorizedExecutionStore(str(path))
    resolver = LocalExecutionContractResolver(
        contract_store=store, cycle_store=cycles, workspace_resolver=Workspaces(),
        executor_id="synthetic", executor_version="test-v1")
    controller = AuthorizedExecutionController(
        cycle_store=cycles, execution_store=store, grant_resolver=resolver,
        executor=executor, evidence_validator=EvidenceValidator())
    service = LocalAuthorizedExecutionService(
        cycle_store=cycles, execution_store=store, resolver=resolver,
        controller=controller, workspace_resolver=Workspaces())
    kwargs = dict(
        cycle_id=snapshot.cycle_id, operation_id="operator-cli-start-01",
        limits=ExecutionLimits(2, 1, 60, 1, max_concurrency=1),
        task_limits=TaskExecutionLimits(1, 30, 0.5),
        financial_policy=FinancialBudgetPolicy(
            FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, 1, "USD"),
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    announced = []
    contract, result = service.start(
        **kwargs, on_run_persisted=lambda run_id: announced.append(
            (run_id, len(executor.calls))))
    assert result["status"] == "completed"
    assert announced == [(result["run_id"], 0)]
    assert [item[0] for item in executor.calls] == ["first", "second"]
    assert contract.authorization.operator_identity == ""
    assert contract.authorization.authorization_mode == "local_execution_contract"
    contract_again, result_again = service.start(**kwargs)
    assert contract_again.contract_id == contract.contract_id
    assert result_again["run_id"] == result["run_id"]
    assert len(executor.calls) == 2
    assert service.status(result["run_id"])["status"] == "completed"

    from dataclasses import replace
    resumed_snapshot = replace(snapshot, cycle_id="cycle-resumed",
                               plan_revision_id="revision-resumed")
    cycles.create(resumed_snapshot)
    resumed_contract = create_local_execution_contract(
        snapshot=resumed_snapshot, workspace=workspace, executor_id="synthetic",
        executor_version="test-v1", limits=kwargs["limits"],
        task_limits=kwargs["task_limits"], financial_policy=kwargs["financial_policy"],
        expires_at=kwargs["expires_at"], operation_id="operator-cli-resume-01")
    store.persist_local_contract(resumed_contract)
    resumed_run_id = store.create(cycle_id=resumed_snapshot.cycle_id,
                                  grant=resumed_contract.authorization,
                                  snapshot=resumed_snapshot)
    resumed = service.resume(resumed_run_id)
    assert resumed["status"] == "completed"
    assert [item[0] for item in executor.calls[2:]] == ["first", "second"]


def test_local_contract_rejects_agent_initiation_without_preconfigured_policy(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os

    _, _, _, _, _, snapshot = _setup(tmp_path)
    root = tmp_path / "agent-workspace"
    root.mkdir()
    real = root.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-agent", identity_digest=identity)
    original = _grant(snapshot)
    with pytest.raises(AuthorizedExecutionError, match="initiator_not_allowed"):
        create_local_execution_contract(
            snapshot=snapshot, workspace=workspace, executor_id="synthetic",
            executor_version="test-v1", limits=original.limits,
            task_limits=TaskExecutionLimits(1, 20, 0.5),
            financial_policy=original.budget_policy,
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            operation_id="agent-start-01", initiated_by="preconfigured_agent")


def test_local_service_recovers_after_contract_persisted_before_run_creation(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os

    path, cycles, _runs, _resolver, _runner, snapshot = _setup(tmp_path)
    root = tmp_path / "recovery-workspace"
    root.mkdir()
    real = root.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-recovery",
                                identity_digest=identity)

    class Workspaces:
        def resolve(self, cycle_id):
            return workspace if cycle_id == snapshot.cycle_id else None

    executor = SyntheticExecutor()
    store = SQLiteAuthorizedExecutionStore(str(path))
    resolver = LocalExecutionContractResolver(
        contract_store=store, cycle_store=cycles, workspace_resolver=Workspaces(),
        executor_id="synthetic", executor_version="test-v1")
    service = LocalAuthorizedExecutionService(
        cycle_store=cycles, execution_store=store, resolver=resolver,
        controller=AuthorizedExecutionController(
            cycle_store=cycles, execution_store=store, grant_resolver=resolver,
            executor=executor, evidence_validator=EvidenceValidator()),
        workspace_resolver=Workspaces())
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    limits = ExecutionLimits(2, 1, 60, 1, max_concurrency=1)
    task_limits = TaskExecutionLimits(1, 30, 0.5)
    budget = FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, 1, "USD")
    contract = create_local_execution_contract(
        snapshot=snapshot, workspace=workspace, executor_id="synthetic",
        executor_version="test-v1", limits=limits, task_limits=task_limits,
        financial_policy=budget, expires_at=expires_at,
        operation_id="contract-persisted-before-run")
    store.persist_local_contract(contract)
    assert store.run_for_contract(contract.contract_id) is None

    recovered, result = service.start(
        cycle_id=snapshot.cycle_id, operation_id=contract.operation_id,
        limits=limits, task_limits=task_limits, financial_policy=budget,
        expires_at=expires_at)
    assert recovered.contract_id == contract.contract_id
    assert result["status"] == "completed"
    assert [call[0] for call in executor.calls] == ["first", "second"]


def test_local_contract_uses_cryptographic_digest_for_spec_reference_plan(tmp_path):
    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os

    path, cycles, _, _, _, snapshot = _setup(tmp_path)
    snapshot = replace(snapshot, plan_digest="spec:spec-digest;tasks:tasks-digest")
    cycles.save(snapshot, expected_iterations=0, expected_revision=0)
    root = tmp_path / "canonical-workspace"
    root.mkdir()
    real = root.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-canonical", identity_digest=identity)
    original = _grant(_snapshot())
    contract = create_local_execution_contract(
        snapshot=snapshot, workspace=workspace, executor_id="synthetic",
        executor_version="test-v1", limits=original.limits,
        task_limits=TaskExecutionLimits(1, 20, 0.5),
        financial_policy=original.budget_policy,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        operation_id="canonical-plan-01")
    assert contract.authorization.accepted_plan_digest.startswith("sha256:")
    store = SQLiteAuthorizedExecutionStore(str(path))
    store.persist_local_contract(contract)

    class Workspaces:
        def resolve(self, cycle_id):
            return workspace

    resolver = LocalExecutionContractResolver(
        contract_store=store, cycle_store=cycles, workspace_resolver=Workspaces(),
        executor_id="synthetic", executor_version="test-v1")
    assert resolver.resolve_and_validate(contract.contract_id, snapshot.cycle_id)


def test_local_execution_cancel_revokes_contract_and_requests_run_cancellation(tmp_path):
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os

    executor = SyntheticExecutor()
    path, cycles, _runs, _resolver, _runner, snapshot = _setup(tmp_path, executor=executor)
    root = tmp_path / "cancel-workspace"
    root.mkdir()
    real = root.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-cancel", identity_digest=identity)

    class Workspaces:
        def resolve(self, cycle_id):
            return workspace

    store = SQLiteAuthorizedExecutionStore(str(path))
    original = _grant(snapshot)
    contract = create_local_execution_contract(
        snapshot=snapshot, workspace=workspace, executor_id="synthetic",
        executor_version="test-v1", limits=original.limits,
        task_limits=TaskExecutionLimits(1, 30, 1),
        financial_policy=original.budget_policy,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        operation_id="operator-cancel-01")
    store.persist_local_contract(contract)
    resolver = LocalExecutionContractResolver(
        contract_store=store, cycle_store=cycles, workspace_resolver=Workspaces(),
        executor_id="synthetic", executor_version="test-v1")
    controller = AuthorizedExecutionController(
        cycle_store=cycles, execution_store=store, grant_resolver=resolver,
        executor=executor, evidence_validator=EvidenceValidator())
    run_id = store.create(cycle_id=snapshot.cycle_id, grant=contract.authorization,
                          snapshot=snapshot)
    service = LocalAuthorizedExecutionService(
        cycle_store=cycles, execution_store=store, resolver=resolver,
        controller=controller, workspace_resolver=Workspaces())
    service.cancel(run_id)
    assert store.load_local_contract(contract.contract_id)[1] == "revoked"
    assert store.cancel_requested(run_id)
    assert executor.calls == []


def test_local_execution_cli_status_and_cancel_use_persisted_contract(tmp_path, monkeypatch, capsys):
    from datetime import timedelta
    from types import SimpleNamespace
    import hashlib
    import os
    import agentflow.cli as cli

    root = tmp_path / "cli-repo"
    (root / ".agentflow").mkdir(parents=True)
    executor = SyntheticExecutor()
    path, cycles, _runs, _resolver, _runner, snapshot = _setup(
        root / ".agentflow", executor=executor)
    workspace_root = root / "workspace"
    workspace_root.mkdir()
    real = workspace_root.resolve(strict=True)
    info = real.stat()
    identity = "sha256:" + hashlib.sha256(
        f"{os.path.normcase(str(real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    workspace = SimpleNamespace(path=real, workspace_id="workspace-cli", identity_digest=identity)

    class Workspaces:
        def resolve(self, cycle_id):
            return workspace

    original = _grant(snapshot)
    contract = create_local_execution_contract(
        snapshot=snapshot, workspace=workspace, executor_id="synthetic",
        executor_version="test-v1", limits=original.limits,
        task_limits=TaskExecutionLimits(1, 20, 1),
        financial_policy=original.budget_policy,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        operation_id="cli-status-cancel-01")
    store = SQLiteAuthorizedExecutionStore(str(path))
    store.persist_local_contract(contract)
    run_id = store.create(cycle_id=snapshot.cycle_id,
                          grant=contract.authorization, snapshot=snapshot)
    monkeypatch.setattr(cli, "repository_root", lambda _path: root)
    assert cli.main(["apl", "run-status", "--path", str(root), "--run-id", run_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == run_id and payload["status"] == "ready"
    assert cli.main(["apl", "run-cancel", "--path", str(root), "--run-id", run_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_revoked"] is True and payload["cancel_requested"] is True
    assert executor.calls == []


@pytest.mark.parametrize("policy", [None, object()])
def test_missing_or_malformed_financial_policy_blocks_implicit_unlimited_execution(tmp_path, policy):
    from dataclasses import replace
    snapshot = _snapshot()
    grant = replace(_grant(snapshot), budget_policy=policy)
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, executor=executor, grant=grant)
    with pytest.raises(AuthorizedExecutionError, match="explicit_financial_budget_policy_required"):
        runner.start("cycle-1", grant.grant_id)
    assert executor.calls == []


@pytest.mark.parametrize("amount", [0, -1, float("inf"), float("nan")])
def test_estimated_cost_policy_rejects_unbounded_or_invalid_amount(amount):
    with pytest.raises(ValueError, match="explicit_finite_cost_estimate_required"):
        FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, amount, "USD")


def test_estimated_cost_is_labeled_and_estimate_limit_blocks_next_task(tmp_path):
    import sqlite3
    executor = SyntheticExecutor(outcomes=[ExecutionOutcome(
        "succeeded", "first complete", ({"passed": True},), cost=2.01)])
    path, _, _, _, runner, _ = _setup(tmp_path, executor=executor)
    with pytest.raises(AuthorizedExecutionError, match="estimated_budget_limit"):
        runner.start("cycle-1", "grant-1")
    with sqlite3.connect(path) as db:
        payload, = db.execute("SELECT payload_json FROM apl_authorized_run_events WHERE event_type='quota.settled'").fetchone()
    event = json.loads(payload)
    assert event["cost_estimated_currency"] == "USD"
    assert event["cost_observed"] is None
    assert event["financial_hard_cap"] is False
    assert [job for job, _ in executor.calls] == ["first"]


@pytest.mark.parametrize("grant_changes, error", [
    ({"state": "revoked"}, "grant_revoked"),
    ({"expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}, "grant_expired"),
])
def test_missing_revoked_or_expired_authorization_dispatches_nothing(tmp_path, grant_changes, error):
    from dataclasses import replace
    snapshot = _snapshot()
    grant = replace(_grant(snapshot), **grant_changes)
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, executor=executor, grant=grant)
    with pytest.raises(AuthorizedExecutionError, match=error):
        runner.start("cycle-1", "grant-1")
    assert executor.calls == []


def test_out_of_scope_or_extra_plan_operation_fails_before_dispatch(tmp_path):
    snapshot = _snapshot([JobProposal("bad", "touch outside", required_scope=("outside",))])
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, jobs=snapshot.jobs, executor=executor,
                                  grant=_grant(snapshot))
    with pytest.raises(AuthorizedExecutionError, match="plan_job_outside_authorized_scope"):
        runner.start("cycle-1", "grant-1")
    assert executor.calls == []


@pytest.mark.parametrize("jobs", [
    (JobProposal("one", "orphan", ("missing",), ("workspace",), ("ok",)),),
    (JobProposal("one", "cycle", ("two",), ("workspace",), ("ok",)),
     JobProposal("two", "cycle", ("one",), ("workspace",), ("ok",))),
])
def test_invalid_dependency_graph_is_rejected_before_first_effect(tmp_path, jobs):
    snapshot = _snapshot(jobs)
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, jobs=jobs, executor=executor,
                                  grant=_grant(snapshot))
    with pytest.raises(AuthorizedExecutionError, match="dependency_(missing|cycle)"):
        runner.start("cycle-1", "grant-1")
    assert executor.calls == []


def test_digest_or_revision_change_blocks_before_dispatch(tmp_path):
    from dataclasses import replace
    snapshot = _snapshot()
    grant = replace(_grant(snapshot), accepted_plan_digest="sha256:" + "e" * 64)
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, executor=executor, grant=grant)
    with pytest.raises(AuthorizedExecutionError, match="approved_plan_binding_changed"):
        runner.start("cycle-1", "grant-1")
    assert executor.calls == []


def test_dependency_order_is_enforced_even_if_plan_order_is_wrong(tmp_path):
    jobs = (
        JobProposal("second", "second", ("first",), ("workspace",), ("ok",)),
        JobProposal("first", "first", (), ("workspace",), ("ok",)),
    )
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, jobs=jobs, executor=executor,
                                  grant=_grant(_snapshot(jobs)))
    result = runner.start("cycle-1", "grant-1")
    assert result["status"] == "completed"
    assert [item[0] for item in executor.calls] == ["first", "second"]


def test_unknown_result_is_persisted_and_never_redispatched_on_resume(tmp_path):
    executor = SyntheticExecutor(outcomes=[ExecutionOutcome("unknown", "lost response")])
    _, _, runs, _, runner, _ = _setup(tmp_path, executor=executor)
    with pytest.raises(AuthorizedExecutionError, match="ifr_intervention_required"):
        runner.start("cycle-1", "grant-1")
    import sqlite3
    with sqlite3.connect(runs.database_path) as db:
        run_id = db.execute("SELECT run_id FROM apl_authorized_runs").fetchone()[0]
    assert not any(event["event_type"] == "ifr.quality_result"
                   for event in runs.load(run_id)["events"])
    with pytest.raises(AuthorizedExecutionError):
        runner.resume(run_id)
    assert len(executor.calls) == 1
    assert runs.load(run_id)["reserved_seconds"] > 0


def test_operator_interrupt_marks_effect_unknown_before_releasing_run(tmp_path):
    class InterruptedExecutor(SyntheticExecutor):
        def execute(self, cycle_id, job, scope, operation_id):
            self.calls.append((job.job_id, operation_id))
            raise KeyboardInterrupt()

    executor = InterruptedExecutor()
    _, _, runs, _, runner, _ = _setup(
        tmp_path, jobs=[JobProposal("only", "synthetic", (), ("workspace",), ("ok",))],
        executor=executor)
    with pytest.raises(KeyboardInterrupt):
        runner.start("cycle-1", "grant-1")
    run_id = _run_id(runs)
    state = runs.load(run_id)
    assert state["status"] == "blocked"
    assert state["tasks"][0]["status"] == "unknown"
    with pytest.raises(AuthorizedExecutionError):
        runner.resume(run_id)
    assert len(executor.calls) == 1


def test_recover_inflight_marks_unknown_without_retry(tmp_path):
    path, cycles, runs, _, _, snapshot = _setup(tmp_path)
    grant = _grant(snapshot)
    run_id = runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot)
    job = snapshot.jobs[0]
    runs.reserve_next(run_id, job, grant=grant, now=datetime.now(timezone.utc).isoformat())
    runs.mark_running(run_id, job.job_id, now=datetime.now(timezone.utc).isoformat())
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute("UPDATE apl_authorized_runs SET owner_pid=987654321,owner_marker='stale-process-marker' WHERE run_id=?", (run_id,))
    assert runs.recover_unfinished(now=datetime.now(timezone.utc).isoformat()) == (f"{run_id}:{job.job_id}",)
    assert runs.load(run_id)["tasks"][0]["status"] == "unknown"
    assert runs.load(run_id)["status"] == "blocked"


def test_quota_reservation_is_atomic_across_concurrent_reservers(tmp_path):
    snapshot = _snapshot([JobProposal("only", "synthetic", (), ("workspace",), ("ok",))])
    _, _, runs, resolver, _, _ = _setup(tmp_path, jobs=snapshot.jobs)
    run_id = runs.create(cycle_id="cycle-1", grant=resolver.grant, snapshot=snapshot)
    now = datetime.now(timezone.utc).isoformat()

    def reserve():
        try:
            return runs.reserve_next(run_id, snapshot.jobs[0], grant=resolver.grant, now=now)
        except AuthorizedExecutionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    accepted = [item for item in results if item is not None]
    assert len(accepted) == 1
    state = runs.load(run_id)
    assert state["reserved_seconds"] == accepted[0]["reserved_seconds"]
    assert state["tasks"][0]["reserved_seconds"] == accepted[0]["reserved_seconds"]
    assert state["tasks"][0]["attempts"] == 1


def test_no_effect_without_authorized_useful_strategy_escalates_without_retry(tmp_path):
    diagnosis = FailureDiagnosis(FailureKind.TRANSIENT, Recoverability.POTENTIALLY_RECOVERABLE,
                                 EffectState.NO_EFFECT_PROVEN, "spawn_temporarily_failed",
                                 recommended_strategy_id="default")
    executor = SyntheticExecutor(outcomes=[NoEffectExecutionError("spawn never occurred",
                                                                  diagnosis=diagnosis)])
    _, _, runs, _, runner, _ = _setup(tmp_path, executor=executor)
    with pytest.raises(AuthorizedExecutionError, match="ifr_intervention_required"):
        runner.start("cycle-1", "grant-1")
    assert len(executor.calls) == 1
    import sqlite3
    with sqlite3.connect(runs.database_path) as db:
        run_id = db.execute("SELECT run_id FROM apl_authorized_runs").fetchone()[0]
    state = runs.load(run_id)
    assert state["status"] == "blocked"
    assert state["reason"] == "ifr_intervention_required"
    assert any(event["event_type"] == "ifr.escalated" for event in state["events"])


def test_authorized_alternate_strategy_retries_same_operation_as_new_attempt(tmp_path):
    from dataclasses import replace
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    grant = replace(_grant(snapshot), limits=ExecutionLimits(1, 3, 30, 2),
                    strategies_by_job=(("only", ("default", "alternate")),))
    diagnosis = FailureDiagnosis(FailureKind.TRANSIENT, Recoverability.POTENTIALLY_RECOVERABLE,
                                 EffectState.NO_EFFECT_PROVEN, "spawn_transient",
                                 recommended_strategy_id="alternate")

    class AlternateExecutor(SyntheticExecutor):
        def __init__(self):
            super().__init__(outcomes=[NoEffectExecutionError("no effect", diagnosis=diagnosis)])
            self.recovery_calls = []

        def execute_recovery(self, cycle_id, job, scope, operation_id, attempt_id, strategy_id,
                             remaining_seconds):
            self.recovery_calls.append((job.job_id, operation_id, attempt_id, strategy_id))
            return ExecutionOutcome("succeeded", "recovered", ({"passed": True},),
                                    progress_key="recovered")

    executor = AlternateExecutor()
    _, _, runs, _, runner, _ = _setup(tmp_path, jobs=snapshot.jobs, executor=executor, grant=grant)
    result = runner.start("cycle-1", "grant-1")
    assert result["status"] == "completed"
    assert executor.recovery_calls[0][1] == executor.calls[0][1]
    assert executor.recovery_calls[0][2].startswith("apl-attempt-")
    assert executor.recovery_calls[0][3] == "alternate"
    events = runs.load(result["run_id"])["events"]
    assert [event["event_type"] for event in events].count("ifr.diagnosis") == 1
    assert [event["event_type"] for event in events].count("ifr.retry_decision") == 1
    recovered = SQLiteAuthorizedExecutionStore(runs.database_path).load(result["run_id"])
    assert recovered["status"] == "completed"
    assert recovered["tasks"][0]["attempts"] == 2
    assert recovered["reserved_seconds"] == 0
    assert recovered["tasks"][0]["reserved_seconds"] == 0


def test_equivalent_alternate_failure_escalates_instead_of_looping(tmp_path):
    from dataclasses import replace
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    grant = replace(_grant(snapshot), limits=ExecutionLimits(1, 3, 30, 2),
                    strategies_by_job=(("only", ("default", "alternate")),))
    diagnosis = FailureDiagnosis(FailureKind.TRANSIENT, Recoverability.POTENTIALLY_RECOVERABLE,
                                 EffectState.NO_EFFECT_PROVEN, "same_error",
                                 recommended_strategy_id="alternate", progress_key="no-change")

    class AlwaysFails(SyntheticExecutor):
        def execute(self, *args):
            self.calls.append((args[1].job_id, args[3]))
            raise NoEffectExecutionError("safe no effect", diagnosis=diagnosis)

        def execute_recovery(self, *args):
            self.calls.append((args[1].job_id, args[3]))
            raise NoEffectExecutionError("same error, no progress", diagnosis=diagnosis)

    executor = AlwaysFails()
    _, _, runs, _, runner, _ = _setup(tmp_path, jobs=snapshot.jobs, executor=executor, grant=grant)
    with pytest.raises(AuthorizedExecutionError, match="ifr_intervention_required"):
        runner.start("cycle-1", grant.grant_id)
    state = runs.load(_run_id(runs))
    assert len(executor.calls) == 2
    escalated = next(json.loads(event["payload_json"]) for event in state["events"]
                     if event["event_type"] == "ifr.escalated")
    assert escalated["decision"]["reason_code"] == "equivalent_retry_without_progress"


def test_ifr_limits_are_immutable_and_survive_store_reopen(tmp_path):
    import sqlite3
    from agentflow.apl.failure_recovery import RecoveryLimits
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    _, _, runs, _, _, _ = _setup(tmp_path, jobs=snapshot.jobs)
    grant = _grant(snapshot)
    run_id = runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot,
                         recovery_limits=RecoveryLimits(max_attempts_total=3,
                                                        max_equivalent_repeats=0,
                                                        max_duration_seconds=45,
                                                        max_estimated_budget=1.25))
    recovered = SQLiteAuthorizedExecutionStore(runs.database_path).load(run_id)
    assert json.loads(recovered["recovery_limits_json"])["max_attempts_total"] == 3
    with sqlite3.connect(runs.database_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE apl_authorized_runs SET recovery_limits_json='{}' WHERE run_id=?", (run_id,))


def test_persisted_safe_retry_resumes_after_controller_restart(tmp_path):
    from dataclasses import replace
    from agentflow.apl.failure_recovery import RecoveryLimits
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    grant = replace(_grant(snapshot), limits=ExecutionLimits(1, 3, 30, 2),
                    strategies_by_job=(("only", ("default", "alternate")),))
    path, cycles, runs, resolver, _, _ = _setup(tmp_path, jobs=snapshot.jobs, grant=grant)
    run_id = runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot,
                         recovery_limits=RecoveryLimits())
    first = runs.reserve_next(run_id, snapshot.jobs[0], grant=grant,
                              now=datetime.now(timezone.utc).isoformat())
    runs.mark_running(run_id, "only", now=datetime.now(timezone.utc).isoformat())
    diagnosis = FailureDiagnosis(FailureKind.TRANSIENT, Recoverability.POTENTIALLY_RECOVERABLE,
                                 EffectState.NO_EFFECT_PROVEN, "transient_before_restart",
                                 recommended_strategy_id="alternate")
    runs.record_retryable_ifr(
        run_id, "only", "alternate",
        {"failure_signature": diagnosis.failure_signature, "strategy_id": "default",
         "effect_state": EffectState.NO_EFFECT_PROVEN.value,
         "progress_key": ""},
        {"retry": True, "reason_code": "safe_retry_authorized"},
        elapsed_seconds=1.0, now=datetime.now(timezone.utc).isoformat())

    class RecoveryExecutor:
        def __init__(self):
            self.calls = []

        def execute(self, *_):
            raise AssertionError("the persisted alternative strategy must be used")

        def execute_recovery(self, cycle_id, job, scope, operation_id, attempt_id, strategy_id,
                             remaining_seconds):
            self.calls.append((operation_id, attempt_id, strategy_id))
            return ExecutionOutcome("succeeded", "resumed", ({"passed": True},),
                                    progress_key="after-restart")

    executor = RecoveryExecutor()
    reopened = SQLiteAuthorizedExecutionStore(path)
    controller = AuthorizedExecutionController(
        cycle_store=cycles, execution_store=reopened, grant_resolver=resolver,
        executor=executor, evidence_validator=EvidenceValidator())
    result = controller.resume(run_id)
    assert result["status"] == "completed"
    assert executor.calls[0][0] == first["operation_id"]
    assert executor.calls[0][1] != first["attempt_id"]
    assert executor.calls[0][2] == "alternate"


@pytest.mark.parametrize("quality", [QualityResult.PASS, QualityResult.FAIL,
                                      QualityResult.INCONCLUSIVE])
def test_quality_result_is_separate_and_persisted(tmp_path, quality):
    class QualityValidator:
        def validate(self, *_):
            return quality

        def evaluate(self, *_):
            from agentflow.apl.codex_execution import AcceptanceEvaluation
            proof = {"kind": "acceptance_evaluation", "criteria_status": quality.value,
                     "work_status": "approved" if quality is QualityResult.PASS else "not_approved"}
            return AcceptanceEvaluation(quality, (proof,))

    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    _, _, runs, _, _, _ = _setup(tmp_path, jobs=snapshot.jobs)
    grant = _grant(snapshot)
    executor = SyntheticExecutor()
    runner = AuthorizedExecutionController(
        cycle_store=SQLiteCycleStore(runs.database_path), execution_store=runs,
        grant_resolver=GrantResolver(grant), executor=executor,
        evidence_validator=QualityValidator())
    if quality is QualityResult.PASS:
        result = runner.start("cycle-1", grant.grant_id)
        state = runs.load(result["run_id"])
        assert state["status"] == "completed"
    else:
        with pytest.raises(AuthorizedExecutionError, match="ifr_intervention_required"):
            runner.start("cycle-1", grant.grant_id)
        import sqlite3
        with sqlite3.connect(runs.database_path) as db:
            run_id = db.execute("SELECT run_id FROM apl_authorized_runs").fetchone()[0]
        state = runs.load(run_id)
        assert state["status"] == "blocked"
    quality_events = [event for event in state["events"]
                      if event["event_type"] == "ifr.quality_result"]
    assert json.loads(quality_events[-1]["payload_json"])["quality_result"] == quality.value
    if quality is QualityResult.PASS:
        outcome = json.loads(state["tasks"][0]["outcome_json"])
        assert outcome["evidence"][-1]["kind"] == "acceptance_evaluation"
        assert outcome["evidence"][-1]["criteria_status"] == "PASS"


def test_ifr_event_replay_is_idempotent_and_journal_integrity_survives_reopen(tmp_path):
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    _, _, runs, _, _, _ = _setup(tmp_path, jobs=snapshot.jobs)
    run_id = runs.create(cycle_id="cycle-1", grant=_grant(snapshot), snapshot=snapshot)
    payload = {"failure_code": "synthetic", "attempt_id": "attempt-1"}
    now = datetime.now(timezone.utc).isoformat()
    runs.record_ifr_event(run_id, "only", "ifr.diagnostic", payload, now=now)
    count = len(runs.load(run_id)["events"])
    runs.record_ifr_event(run_id, "only", "ifr.diagnostic", payload, now=now)
    reopened = SQLiteAuthorizedExecutionStore(runs.database_path).load(run_id)
    assert len(reopened["events"]) == count


def test_ifr_retry_persistence_failure_rolls_back_task_transition(tmp_path):
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    _, _, runs, _, _, _ = _setup(tmp_path, jobs=snapshot.jobs)
    grant = _grant(snapshot)
    run_id = runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot)
    now = datetime.now(timezone.utc).isoformat()
    runs.reserve_next(run_id, snapshot.jobs[0], grant=grant, now=now)
    runs.mark_running(run_id, "only", now=now)
    with pytest.raises(Exception):
        runs.record_retryable_ifr(run_id, "only", "alternate", {"bad": object()},
                                  {"retry": True}, elapsed_seconds=1, now=now)
    state = runs.load(run_id)
    assert state["tasks"][0]["status"] == "running"
    assert not any(event["event_type"] == "ifr.diagnosis" for event in state["events"])


def test_grant_revocation_after_safe_retry_prevents_second_dispatch(tmp_path):
    from dataclasses import replace
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    grant = replace(_grant(snapshot), limits=ExecutionLimits(1, 3, 30, 2),
                    strategies_by_job=(("only", ("default", "alternate")),))
    diagnosis = FailureDiagnosis(FailureKind.TRANSIENT, Recoverability.POTENTIALLY_RECOVERABLE,
                                 EffectState.NO_EFFECT_PROVEN, "spawn_transient",
                                 recommended_strategy_id="alternate")

    class RevokeBeforeRetry(GrantResolver):
        def resolve_and_validate(self, grant_id, cycle_id):
            # start validation, then pre-dispatch validation, then retry pre-dispatch
            if len(executor.calls) >= 1:
                self.grant = replace(self.grant, state="revoked")
            return super().resolve_and_validate(grant_id, cycle_id)

    class FailsNoEffect(SyntheticExecutor):
        def execute(self, *args):
            with self._lock:
                self.calls.append((args[1].job_id, args[3]))
            raise NoEffectExecutionError("confirmed no effect", diagnosis=diagnosis)

        def execute_recovery(self, *args):
            raise AssertionError("revoked grant must stop before recovery dispatch")

    executor = FailsNoEffect()
    _, _, runs, _, _, _ = _setup(tmp_path, jobs=snapshot.jobs, executor=executor, grant=grant)
    resolver = RevokeBeforeRetry(grant)
    runner = AuthorizedExecutionController(
        cycle_store=SQLiteCycleStore(runs.database_path), execution_store=runs,
        grant_resolver=resolver, executor=executor, evidence_validator=EvidenceValidator())
    with pytest.raises(AuthorizedExecutionError, match="dispatch_suspended"):
        runner.start("cycle-1", grant.grant_id)
    assert len(executor.calls) == 1
    assert runs.load(_run_id(runs))["status"] == "blocked"


def test_revocation_during_active_authorized_execution_requests_cancellation(tmp_path):
    from dataclasses import replace
    snapshot = _snapshot([JobProposal("only", "synthetic", (), ("workspace",), ("ok",))])
    grant = _grant(snapshot)
    resolver = GrantResolver(grant)

    class ActiveExecutor:
        def execute(self, *_):
            raise AssertionError("authorized executor path required")

        def execute_authorized(self, cycle_id, job, binding, operation_id, *, cancellation,
                               on_started, **kwargs):
            resolver.grant = replace(resolver.grant, state="revoked")
            assert cancellation.is_set()
            raise RuntimeError("active process observed revocation and stopped")

    executor = ActiveExecutor()
    _, _, runs, _, _, _ = _setup(tmp_path, jobs=snapshot.jobs, executor=executor, grant=grant)
    controller = AuthorizedExecutionController(
        cycle_store=SQLiteCycleStore(runs.database_path), execution_store=runs,
        grant_resolver=resolver, executor=executor, evidence_validator=EvidenceValidator())
    with pytest.raises(AuthorizedExecutionError, match="unknown_execution_result"):
        controller.start("cycle-1", grant.grant_id)
    state = runs.load(_run_id(runs))
    assert any(event["event_type"] == "run.revocation_cancel_requested"
               for event in state["events"])
    assert state["tasks"][0]["status"] == "unknown"


def _run_id(runs):
    import sqlite3
    with sqlite3.connect(runs.database_path) as db:
        return db.execute("SELECT run_id FROM apl_authorized_runs").fetchone()[0]


def test_grant_cannot_be_reused_for_another_session(tmp_path):
    snapshot = _snapshot()
    grant = _grant(snapshot)
    _, _, runs, _, _, _ = _setup(tmp_path, snapshot.jobs, grant=grant)
    first = runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot)
    assert runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot) == first
    other = _snapshot([JobProposal("other", "different", (), ("workspace",), ("ok",))])
    with pytest.raises(AuthorizedExecutionError, match="grant_already_bound_to_different_execution"):
        runs.create(cycle_id="cycle-1", grant=_grant(other), snapshot=other)


def test_journal_checkpoint_digest_detects_tampering(tmp_path):
    import sqlite3
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    _, _, runs, _, runner, _ = _setup(tmp_path, jobs=snapshot.jobs, grant=_grant(snapshot))
    result = runner.start("cycle-1", "grant-1")
    run_id = result["run_id"]
    with sqlite3.connect(runs.database_path) as db:
        db.execute("DROP TRIGGER apl_authorized_run_events_no_update")
        db.execute("UPDATE apl_authorized_run_events SET payload_json='{}' WHERE run_id=? AND event_id=(SELECT MIN(event_id) FROM apl_authorized_run_events WHERE run_id=?)", (run_id, run_id))
    with pytest.raises(AuthorizedExecutionError, match="journal_integrity"):
        runs.load(run_id)


def test_executor_cannot_self_authorize_without_resolver(tmp_path):
    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    path, cycles, runs, _, _, _ = _setup(tmp_path)
    executor = SyntheticExecutor()
    with pytest.raises(ValueError, match="contract_unavailable"):
        AuthorizedExecutionController(cycle_store=cycles, execution_store=runs,
            grant_resolver=object(), executor=executor, evidence_validator=EvidenceValidator())
    assert executor.calls == []


def test_completion_replay_is_idempotent_and_no_second_dispatch(tmp_path):
    executor = SyntheticExecutor()
    _, _, _, _, runner, _ = _setup(tmp_path, executor=executor)
    complete = runner.start("cycle-1", "grant-1")
    assert runner.resume(complete["run_id"])["status"] == "completed"
    assert len(executor.calls) == 2


def test_concurrent_worker_cannot_claim_same_run(tmp_path):
    entered, release = Event(), Event()

    class BlockingExecutor(SyntheticExecutor):
        def execute(self, *args):
            entered.set()
            assert release.wait(5)
            return super().execute(*args)

    snapshot = _snapshot([JobProposal("only", "single", (), ("workspace",), ("ok",))])
    executor = BlockingExecutor()
    grant = _grant(snapshot)
    _, _, runs, _, runner, _ = _setup(tmp_path, jobs=snapshot.jobs, executor=executor,
                                      grant=grant)
    run_id = runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(runner.resume, run_id)
        assert entered.wait(5)
        with pytest.raises(AuthorizedExecutionError, match="live_process"):
            runner.resume(run_id)
        release.set()
        assert active.result(timeout=5)["status"] == "completed"
    assert len(executor.calls) == 1


def test_revocation_between_jobs_blocks_next_dispatch(tmp_path):
    snapshot = _snapshot()
    grant = _grant(snapshot)
    resolver = GrantResolver(grant)

    class RevokeAfterFirst(SyntheticExecutor):
        def execute(self, *args):
            outcome = super().execute(*args)
            if len(self.calls) == 1:
                from dataclasses import replace
                resolver.grant = replace(resolver.grant, state="revoked")
            return outcome

    executor = RevokeAfterFirst()
    _, _, runs, resolver, runner, _ = _setup(tmp_path, executor=executor, grant=grant)
    with pytest.raises(AuthorizedExecutionError, match="dispatch_suspended"):
        runner.start("cycle-1", "grant-1")
    assert [item[0] for item in executor.calls] == ["first"]
    import sqlite3
    with sqlite3.connect(runs.database_path) as db:
        assert db.execute("SELECT status FROM apl_authorized_runs").fetchone()[0] == "blocked"


def test_cancellation_between_tasks_stops_continuation(tmp_path):
    snapshot = _snapshot()
    runs_holder = {}

    class CancelAfterFirst(SyntheticExecutor):
        def execute(self, *args):
            outcome = super().execute(*args)
            if len(self.calls) == 1:
                runs_holder["runs"].request_cancel(runs_holder["run_id"],
                                                   now=datetime.now(timezone.utc).isoformat())
            return outcome

    executor = CancelAfterFirst()
    _, _, runs, resolver, runner, _ = _setup(tmp_path, executor=executor)
    runs_holder["runs"] = runs
    runs_holder["run_id"] = runs.create(cycle_id="cycle-1", grant=resolver.grant, snapshot=snapshot)
    result = runner.resume(runs_holder["run_id"])
    assert result["status"] == "cancelled"
    assert [item[0] for item in executor.calls] == ["first"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_execution_limits_reject_non_finite_values(value):
    with pytest.raises(ValueError, match="execution_limits_invalid"):
        ExecutionLimits(1, 1, value)
    with pytest.raises(ValueError, match="execution_limits_invalid"):
        ExecutionLimits(1, 1, 10, max_estimated_budget=value)
    with pytest.raises(ValueError, match="task_execution_limits_invalid"):
        TaskExecutionLimits(1, value)
    with pytest.raises(ValueError, match="task_execution_limits_invalid"):
        TaskExecutionLimits(1, 10, value)


def test_execution_grant_cannot_claim_unsupported_parallel_concurrency():
    with pytest.raises(ValueError, match="execution_limits_invalid"):
        ExecutionLimits(2, 1, 10, max_concurrency=2)
