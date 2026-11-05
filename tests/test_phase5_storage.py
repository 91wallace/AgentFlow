from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentflow.validation import (SnapshotManager, ValidationScheduler,
                                  cleanup_validation_storage)
from test_phase5_scheduler import FakeRuntime, FakeValidator, make_validation_project, request_for


def test_retention_cleanup_preserves_audit_and_unrelated_files(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    request = request_for(plan, store, workspace, checkpoint)
    result = ValidationScheduler(
        store, provider, FakeRuntime(), FakeValidator(), SnapshotManager(provider),
        tmp_path / "snapshots", tmp_path / "outputs",
    ).run(request, workspace, checkpoint, plan)
    assert result.status.value == "passed"
    output = tmp_path / "outputs" / request.validation_run_id
    for gate in result.results:
        gate_dir = output / gate.gate_id
        (gate_dir / "stdout.log").write_bytes(b"retained log")
        (gate_dir / "stderr.log").write_bytes(b"")
        (gate_dir / "keep.txt").write_text("user data", encoding="utf-8")
    snapshot_path = store.db.execute(
        "SELECT storage_path FROM validation_snapshots WHERE validation_run_id=?",
        (request.validation_run_id,),
    ).fetchone()[0]
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    store.db.execute("UPDATE validation_runs SET finished_at=? WHERE validation_run_id=?",
                     (old, request.validation_run_id))
    removed = cleanup_validation_storage(store, tmp_path / "snapshots", tmp_path / "outputs",
                                         tmp_path / "artifacts")
    assert any(item.startswith("snapshot:") for item in removed)
    assert any(item.startswith("log:") for item in removed)
    assert not Path(snapshot_path).exists()
    assert (output / "gate-a" / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert store.db.execute(
        "SELECT state FROM validation_snapshots WHERE validation_run_id=?",
        (request.validation_run_id,),).fetchone()[0] == "expired"
    rows = store.db.execute(
        "SELECT stdout_digest,stdout_bytes,logs_retained FROM validation_gate_results "
        "WHERE validation_run_id=?", (request.validation_run_id,)).fetchall()
    assert all(row[0] and row[1] >= 0 and row[2] == 0 for row in rows)
    assert cleanup_validation_storage(store, tmp_path / "snapshots", tmp_path / "outputs",
                                     tmp_path / "artifacts") == ()
