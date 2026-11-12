from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from threading import Event

import pytest

from agentflow.apl.authorized_execution import (
    AuthorizedExecutionController, AuthorizedExecutionError, ExecutionLimits,
    NoEffectExecutionError, SQLiteAuthorizedExecutionStore,
    VerifiedExecutionGrant,
)
from agentflow.apl.contracts import APLState, CycleSnapshot, ExecutionOutcome, JobProposal
from agentflow.apl.persistence import SQLiteCycleStore


def _snapshot(jobs=None, *, state=APLState.READY):
    jobs = tuple(jobs or (
        JobProposal("first", "create file", (), ("workspace",), ("file exists",)),
        JobProposal("second", "verify file", ("first",), ("workspace",), ("check passes",)),
    ))
    return CycleSnapshot("cycle-1", "complete a synthetic math helper", ("workspace",),
                         state, jobs, (), 0, 0, plan_digest="sha256:" + "a" * 64,
                         plan_revision_id="revision-1")


def _grant(snapshot, *, operations=None, expiry=None, state="approved"):
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
        limits=ExecutionLimits(max_jobs=10, max_attempts_per_job=2, max_seconds=30,
                               max_estimated_budget=2), state=state,
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
    assert resolver.calls >= 3  # grant checked at session and immediately before each dispatch
    assert cycles.load("cycle-1").state is APLState.COMPLETED
    assert len(cycles.load("cycle-1").outcomes) == 2
    assert runs.load(result["run_id"])["status"] == "completed"
    assert len(result["events"]) >= 5


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
    with pytest.raises(AuthorizedExecutionError, match="unknown_execution_result"):
        runner.start("cycle-1", "grant-1")
    import sqlite3
    with sqlite3.connect(runs.database_path) as db:
        run_id = db.execute("SELECT run_id FROM apl_authorized_runs").fetchone()[0]
    with pytest.raises(AuthorizedExecutionError):
        runner.resume(run_id)
    assert len(executor.calls) == 1


def test_recover_inflight_marks_unknown_without_retry(tmp_path):
    path, cycles, runs, _, _, snapshot = _setup(tmp_path)
    grant = _grant(snapshot)
    run_id = runs.create(cycle_id="cycle-1", grant=grant, snapshot=snapshot)
    job = snapshot.jobs[0]
    runs.reserve_next(run_id, job, max_attempts=2, now=datetime.now(timezone.utc).isoformat())
    runs.mark_running(run_id, job.job_id, now=datetime.now(timezone.utc).isoformat())
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute("UPDATE apl_authorized_runs SET owner_pid=987654321,owner_marker='stale-process-marker' WHERE run_id=?", (run_id,))
    assert runs.recover_unfinished(now=datetime.now(timezone.utc).isoformat()) == (f"{run_id}:{job.job_id}",)
    assert runs.load(run_id)["tasks"][0]["status"] == "unknown"
    assert runs.load(run_id)["status"] == "blocked"


def test_retries_only_explicit_no_effect_failure_with_same_operation_id(tmp_path):
    executor = SyntheticExecutor(outcomes=[NoEffectExecutionError("spawn never occurred"), None, None])
    _, _, _, _, runner, _ = _setup(tmp_path, executor=executor)
    result = runner.start("cycle-1", "grant-1")
    assert result["status"] == "completed"
    assert executor.calls[0] == executor.calls[1]
    assert executor.calls[1][0] == "first"


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
