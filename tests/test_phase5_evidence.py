import pytest

from agentflow.state import ConflictError
from agentflow.state.sqlite import current_verified_gate_criteria
from agentflow.validation import SnapshotManager, ValidationScheduler
from test_phase5_scheduler import FakeRuntime, FakeValidator, make_validation_project, request_for


def test_verified_evidence_is_vigente_idempotent_and_bound_to_gate(tmp_path):
    repository, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    request = request_for(plan, store, workspace, checkpoint)
    result = ValidationScheduler(
        store, provider, FakeRuntime(), FakeValidator(), SnapshotManager(provider),
        tmp_path / "snapshots", tmp_path / "outputs",
    ).run(request, workspace, checkpoint, plan)
    assert result.status.value == "passed"
    evidence_id = store.record_verified_gate_evidence(
        result.validation_run_id, "gate-a", "works", "evidence-op-1", author="reviewer")
    assert evidence_id > 0
    assert store.record_verified_gate_evidence(
        result.validation_run_id, "gate-a", "works", "evidence-op-1", author="reviewer") == evidence_id
    row = store.db.execute(
        "SELECT criterion_id,kind,payload,source,passed FROM evidence WHERE evidence_id=?",
        (evidence_id,),
    ).fetchone()
    assert row[0:2] == ("works", "verified")
    assert '"snapshot_digest"' in row[2] and "gate-a" in row[3]
    assert row[4] == 1
    assert current_verified_gate_criteria(
        store.db, plan.specification.project_id, "task-one", 1,
        request.definition_digest) == {"works"}
    store.db.execute("UPDATE workspaces SET current_head_oid=? WHERE workspace_id=?",
                     ("0" * 40, workspace.workspace_id))
    assert current_verified_gate_criteria(
        store.db, plan.specification.project_id, "task-one", 1,
        request.definition_digest) == set(), "checkpoint drift invalidates earlier evidence"
    store.db.execute("UPDATE workspaces SET current_head_oid=? WHERE workspace_id=?",
                     (checkpoint.commit_oid, workspace.workspace_id))
    with pytest.raises(ConflictError, match="divergentemente"):
        store.record_verified_gate_evidence(
            result.validation_run_id, "gate-a", "works", "evidence-op-1", author="different")
    with pytest.raises(ConflictError, match="vigente/verificada"):
        store.record_verified_gate_evidence(
            result.validation_run_id, "gate-a", "not-a-criterion", "evidence-op-2",
            author="reviewer")


def test_failed_or_inconclusive_gate_cannot_create_verified_evidence(tmp_path):
    repository, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    request = request_for(plan, store, workspace, checkpoint)
    result = ValidationScheduler(
        store, provider, FakeRuntime({"gate-a": {"exit_code": 5}}), FakeValidator(),
        SnapshotManager(provider), tmp_path / "snapshots", tmp_path / "outputs",
    ).run(request, workspace, checkpoint, plan)
    assert result.status.value == "failed"
    with pytest.raises(ConflictError, match="vigente/verificada"):
        store.record_verified_gate_evidence(
            result.validation_run_id, "gate-a", "works", "evidence-op-fail", author="reviewer")
    assert store.db.execute("SELECT count(*) FROM evidence WHERE kind='verified'").fetchone()[0] == 0
