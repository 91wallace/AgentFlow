from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import subprocess
import sys
from threading import Barrier
from pathlib import Path

import pytest

from agentflow.agents import ExecutionPolicy, ExecutionRequest, ProcessIdentity, ProcessResult, ProcessSpec
from agentflow.planning import load_plan
from agentflow.state import ConflictError, EvidenceInput, TransitionError
from agentflow.state.execution import ExecutionStore
from test_planning import files


def setup(tmp_path):
    plan = load_plan(files(tmp_path / "plan"))
    path = tmp_path / "state.sqlite3"
    store = ExecutionStore(path)
    store.import_plan(plan)
    row = store.db.execute("SELECT definition_digest FROM task_definitions WHERE task_id='first'").fetchone()
    request = ExecutionRequest("sample", "first", 1, row[0], "run-1", tmp_path, "Implement", ExecutionPolicy())
    spec = ProcessSpec(Path("fake"), (), tmp_path, "Implement", (), "fake", "1", "inherited", "inherited")
    return store, request, spec


def test_prepare_is_atomic_idempotent_and_revision_bound(tmp_path):
    store, request, spec = setup(tmp_path)
    with store:
        first = store.prepare(request, spec, "owner")
        assert first.status == "prepared"
        assert store.prepare(request, spec, "owner") == first
        assert store.list_tasks()[0].attempt_count == 1
        with pytest.raises(ConflictError, match="parâmetros diferentes"):
            store.prepare(replace(request, instruction="changed"), spec, "owner")
        with pytest.raises(TransitionError):
            store.prepare(replace(request, operation_id="run-2"), spec, "owner")
        assert store.db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0] == 1


def test_preflight_failure_has_no_reservation(tmp_path):
    store, request, spec = setup(tmp_path)
    with store:
        with pytest.raises(TransitionError):
            store.prepare(replace(request, definition_digest="wrong"), spec, "owner")
        assert store.list_tasks()[0].status == "ready"


def test_operation_id_cannot_collide_with_phase_two_operation(tmp_path):
    store, request, spec = setup(tmp_path)
    with store:
        store.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)",
                         (request.operation_id, "other-digest", "event", 1, "{}", "now"))
        with pytest.raises(ConflictError, match="outra operação"):
            store.prepare(request, spec, "owner")
        assert store.db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0] == 0


def test_retry_requires_explicit_reconciliation_note(tmp_path):
    store, request, spec = setup(tmp_path)
    with store:
        first = store.prepare(request, spec, "owner")
        store.finish(first.attempt_id, ProcessResult("process_failed", 1, tmp_path / "o", tmp_path / "e",
                                                       "0" * 64, "0" * 64, 0, 0, "failed"))
        second_request = replace(request, operation_id="run-2")
        with pytest.raises(TransitionError):
            store.prepare(second_request, spec, "owner")
        second = store.prepare(second_request, spec, "owner", retry_reason="workspace inspected; no effects to repeat")
        assert second.created and second.attempt_id != first.attempt_id
        assert store.list_tasks()[0].attempt_count == 2
        store.finish(second.attempt_id, ProcessResult("process_failed", 1, tmp_path / "o2", tmp_path / "e2",
                                                       "0" * 64, "0" * 64, 0, 0, "failed"))
        third = store.prepare(replace(request, operation_id="run-3"), spec, "owner",
                             retry_reason="workspace inspected again")
        assert store.list_tasks()[0].attempt_count == 3
        store.finish(third.attempt_id, ProcessResult("process_failed", 1, tmp_path / "o3", tmp_path / "e3",
                                                      "0" * 64, "0" * 64, 0, 0, "failed"))
        with pytest.raises(TransitionError, match="nova tentativa"):
            store.prepare(replace(request, operation_id="run-4"), spec, "owner",
                          retry_reason="fourth attempt")


def test_process_success_does_not_complete_without_verified_evidence(tmp_path):
    store, request, spec = setup(tmp_path)
    with store:
        attempt = store.prepare(request, spec, "owner")
        result = ProcessResult("process_succeeded", 0, tmp_path / "out", tmp_path / "err",
                               hashlib.sha256(b"").hexdigest(), hashlib.sha256(b"").hexdigest(), 0, 0, "ok")
        store.finish(attempt.attempt_id, result)
        assert store.list_tasks()[0].status == "blocked"
        with pytest.raises(TransitionError, match="evidências verificadas"):
            store.set_validation(attempt.attempt_id, True, (), "tests passed")
        assert store.attempt(attempt.attempt_id).validation_status == "pending"


def test_only_verified_criterion_evidence_allows_completion(tmp_path):
    store, request, spec = setup(tmp_path)
    with store:
        attempt = store.prepare(request, spec, "owner")
        store.finish(attempt.attempt_id, ProcessResult("process_succeeded", 0, tmp_path / "out", tmp_path / "err",
                          "0" * 64, "0" * 64, 0, 0, "ok"))
        declaration = store.add_evidence(EvidenceInput("first", "ok", "declared", "done", "user", "declared"))
        verified = store.add_evidence(EvidenceInput("first", "ok", "verified", "checked", "test", "verified",
                                                    declaration, "pytest", "validator", True))
        updated = store.set_validation(attempt.attempt_id, True, (verified,), "criterion passed")
        assert updated.validation_status == "validation_passed"
        assert store.list_tasks()[0].status == "completed"


def test_recovery_is_conservative_and_idempotent(tmp_path):
    store, request, spec = setup(tmp_path)
    with store:
        attempt = store.prepare(request, spec, "owner")
        assert store.reconcile_interrupted() == (attempt.attempt_id,)
        assert store.reconcile_interrupted() == ()
        assert store.attempt(attempt.attempt_id).status == "interrupted"
        assert store.list_tasks()[0].status == "blocked"


def test_explicit_log_retention_preserves_recent_and_removes_old(tmp_path):
    store, request, spec = setup(tmp_path)
    log_root = tmp_path / "logs"
    log_root.mkdir()
    with store:
        attempt = store.prepare(request, spec, "owner")
        store.finish(attempt.attempt_id, ProcessResult("process_failed", 1, tmp_path / "o", tmp_path / "e",
                          "0" * 64, "0" * 64, 0, 0, "failed"))
        attempt_dir = log_root / attempt.attempt_id
        attempt_dir.mkdir()
        (attempt_dir / "stdout.log").write_text("old")
        store.db.execute("UPDATE execution_attempts SET finished_at=? WHERE attempt_id=?",
                         ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), attempt.attempt_id))
        assert store.cleanup_logs(log_root) == (attempt.attempt_id,)
        assert not attempt_dir.exists()


def test_concurrent_identical_operation_creates_one_attempt(tmp_path):
    store, request, spec = setup(tmp_path)
    path = store.path
    store.close()
    barrier = Barrier(2)
    def worker():
        with ExecutionStore(path) as concurrent_store:
            barrier.wait(timeout=5)
            return concurrent_store.prepare(request, spec, "owner")
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(lambda _: worker(), range(2)))
    assert attempts[0].attempt_id == attempts[1].attempt_id
    assert sorted(item.created for item in attempts) == [False, True]
    with ExecutionStore(path) as check:
        assert check.db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0] == 1


def test_recovery_keeps_verified_live_process_blocking(tmp_path):
    store, request, spec = setup(tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], cwd=tmp_path,
                               start_new_session=(sys.platform != "win32"))
    try:
        from agentflow.runtime.local import LocalRuntime
        with store:
            attempt = store.prepare(request, spec, "pid:1")
            parent_marker = LocalRuntime._self_creation_marker()
            identity = ProcessIdentity(process.pid, LocalRuntime._creation_marker(process), str(process.pid),
                                       os.getpid(), attempt.attempt_id, "opaque", parent_marker)
            assert LocalRuntime.identity_matches(identity)
            assert not LocalRuntime.identity_matches(replace(identity, started_at="stale-marker"))
            store.started(attempt.attempt_id, identity)
            assert store.db.execute("SELECT parent_pid,parent_started_at FROM execution_attempts WHERE attempt_id=?", (attempt.attempt_id,)).fetchone() == (os.getpid(), parent_marker)
            assert store.reconcile_interrupted() == ()
            assert store.attempt(attempt.attempt_id).status == "running"
            assert store.db.execute("SELECT count(*) FROM execution_events WHERE kind='recovery_process_alive'").fetchone()[0] == 1
    finally:
        process.kill()
        process.wait()
