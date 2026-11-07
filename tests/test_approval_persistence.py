from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

import agentflow
from agentflow.approval.broker import ApprovalBroker
from agentflow.approval.persistence import (
    APPLICATION_ID,
    BrokerConflict,
    BrokerIntegrityError,
    BrokerStore,
    BrokerStoreError,
    BrokerStoreLocked,
)
from agentflow.approval.protocol import (
    ApprovalDecision,
    ApprovalRequest,
    ExecutionAuthorization,
    ExecutionReceipt,
    OperationIntent,
    SnapshotProposal,
    build_contract,
    canonical_json,
    digest_json,
    utc_now,
)


def _times(offset=600):
    start = datetime.now(timezone.utc).replace(microsecond=0)
    end = start + timedelta(seconds=offset)
    fmt = lambda value: value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return fmt(start), fmt(end)


def _digest(value):
    return digest_json(value)


def _bundle(store, operation_id="protected-op-1", *, create_request=True):
    snapshot_map = {}
    for kind in ("specification", "plan", "checkpoint", "payload"):
        snapshot_id = f"snap-{operation_id}-{kind}"
        content = {"kind": kind, "project_id": "demo", "body": f"fixture-{kind}"}
        started, _ = _times()
        proposal = SnapshotProposal(
            protocol_version=1, snapshot_id=snapshot_id, kind=kind, project_id="demo",
            revision_id=f"rev-{kind}", schema_version=1, content_json=canonical_json(content),
            canonical_digest=_digest(content), provenance_json=canonical_json({"origin": "test-fixture"}),
            proposed_by="test-principal", operation_id=f"proposal-{operation_id}-{kind}",
            proposed_at=started, parent_digest=None,
        )
        store.propose_snapshot(proposal)
        accepted_at = _times()[0]
        store._transition_snapshot(snapshot_id, "accepted", "test-authority", accepted_at,
                                   proof_digest=_digest({"test-only": snapshot_id}))
        snapshot_map[kind] = (snapshot_id, proposal.canonical_digest)
    created, expires = _times()
    fields = dict(
        protocol_version=1, operation_id=operation_id, action_code="provider_call",
        principal_id="requested-user", client_id="agentflow-client", project_id="demo",
        task_id="task-1", attempt_id="attempt-1",
        specification_snapshot_id=snapshot_map["specification"][0],
        specification_revision_id=f"rev-specification",
        specification_digest=snapshot_map["specification"][1],
        plan_snapshot_id=snapshot_map["plan"][0], plan_revision_id="rev-plan",
        plan_digest=snapshot_map["plan"][1],
        checkpoint_snapshot_id=snapshot_map["checkpoint"][0],
        checkpoint_digest=snapshot_map["checkpoint"][1], target_id="trusted-provider-gateway",
        executor_version="1.0", provider_id="provider-a", model_id="model-a",
        payload_snapshot_id=snapshot_map["payload"][0], payload_digest=snapshot_map["payload"][1],
        parameters_json=canonical_json({"temperature": "0"}),
        budget_json=canonical_json({"cost_status": "estimated", "max_cost": "2.00"}),
        policy_digest=_digest({"policy": 1}), catalog_digest=_digest({"catalog": 1}),
        created_at=created, expires_at=expires,
    )
    intent = build_contract(OperationIntent, **fields)
    store.store_intent(intent)
    _, request_expiry = _times(300)
    request = build_contract(
        ApprovalRequest, protocol_version=1, request_id=f"request-{operation_id}",
        operation_id=operation_id, action_code=intent.action_code,
        intent_digest=intent.intent_digest, requester_principal=intent.principal_id,
        nonce_digest=_digest({"nonce": operation_id}), created_at=created,
        expires_at=request_expiry, status="pending",
    )
    return intent, store.create_request(request).request if create_request else request


def _decision(request, result="approved"):
    started, expires = _times(300)
    return build_contract(
        ApprovalDecision, protocol_version=1, decision_id=f"decision-{request.request_id}-{result}",
        request_id=request.request_id, operation_id=request.operation_id,
        request_digest=request.request_digest, intent_digest=request.intent_digest,
        decision=result, approver_principal="independent-human", assurance="test-webauthn-uv",
        challenge_digest=_digest({"challenge": request.request_id}), decided_at=started,
        expires_at=expires,
    )


def _authorization(request, intent):
    _, expires = _times(240)
    return build_contract(
        ExecutionAuthorization, protocol_version=1,
        authorization_id=f"authorization-{request.request_id}", request_id=request.request_id,
        operation_id=request.operation_id, intent_digest=intent.intent_digest,
        audience="trusted-gateway", target_id=intent.target_id,
        payload_digest=intent.payload_digest, nonce_digest=_digest({"auth": intent.operation_id}),
        issued_at=_times(60)[0], expires_at=expires, status="approved",
    )


def _receipt(intent, authorization, status="succeeded"):
    started = datetime.now(timezone.utc).replace(microsecond=0)
    completed = started + timedelta(seconds=1)
    fmt = lambda value: value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return build_contract(
        ExecutionReceipt, protocol_version=1, receipt_id=f"receipt-{intent.operation_id}-{status}",
        operation_id=intent.operation_id, intent_digest=intent.intent_digest,
        authorization_digest=authorization.authorization_digest, target_id=intent.target_id,
        executor_id="gateway-test", executor_version="1.0", payload_digest=intent.payload_digest,
        status=status, started_at=fmt(started), completed_at=fmt(completed) if status != "pending" else None,
        effect_key=f"effect-{intent.operation_id}" if status != "pending" else None,
        result_digest=_digest({"result": "ok"}) if status == "succeeded" else None,
    )


def test_broker_store_uses_isolated_schema_and_never_opens_agentflow_state(tmp_path):
    broker_path = tmp_path / "broker" / "authority.sqlite"
    with BrokerStore(broker_path) as store:
        assert store.db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.verify_integrity() is None
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
    with pytest.raises(BrokerStoreError, match=".agentflow"):
        BrokerStore(tmp_path / ".agentflow" / "state.sqlite3")

    existing = tmp_path / "state.sqlite3"
    with sqlite3.connect(existing) as db:
        db.execute("CREATE TABLE projects(id TEXT PRIMARY KEY)")
        db.execute("PRAGMA user_version=7")
    with pytest.raises(BrokerStoreError, match="non-empty"):
        BrokerStore(existing)
    assert not existing.with_name(existing.name + ".lock").exists()
    with sqlite3.connect(existing) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone()
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7


def test_initial_migration_rolls_back_as_a_unit_on_failure(tmp_path, monkeypatch):
    import agentflow.approval.persistence as persistence

    monkeypatch.setattr(persistence, "_SCHEMA", (
        "CREATE TABLE temporary_table(id INTEGER PRIMARY KEY)", "INVALID SQL STATEMENT"
    ))
    path = tmp_path / "broken" / "broker.sqlite"
    with pytest.raises(BrokerStoreError, match="rolled back"):
        BrokerStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("PRAGMA application_id").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall() == []


def test_schema_rollback_is_allowed_only_while_empty(tmp_path):
    path = tmp_path / "broker.sqlite"
    store = BrokerStore(path)
    store.rollback_empty_schema()
    store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    with BrokerStore(path) as reopened:
        proposal = _bundle_snapshot_proposal()
        reopened.propose_snapshot(proposal)
        with pytest.raises(BrokerStoreError, match="history"):
            reopened.rollback_empty_schema()


def _bundle_snapshot_proposal():
    started, _ = _times()
    content = {"purpose": "test"}
    return SnapshotProposal(
        protocol_version=1, snapshot_id="snapshot-one", kind="specification", project_id="demo",
        revision_id="revision-one", schema_version=1, content_json=canonical_json(content),
        canonical_digest=_digest(content), provenance_json=canonical_json({"source": "test"}),
        proposed_by="test", operation_id="proposal-one", proposed_at=started, parent_digest=None,
    )


def test_snapshots_are_immutable_pending_by_default_and_idempotent(tmp_path):
    proposal = _bundle_snapshot_proposal()
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        first = store.propose_snapshot(proposal)
        second = store.propose_snapshot(proposal)
        assert first == second
        assert first.state == "pending"
        assert store.get_snapshot(proposal.snapshot_id) == proposal
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.db.execute("UPDATE broker_snapshots SET revision_id='changed' WHERE snapshot_id=?",
                             (proposal.snapshot_id,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            store.db.execute("DELETE FROM broker_snapshots WHERE snapshot_id=?", (proposal.snapshot_id,))
        changed_content = {"changed": True}
        changed = SnapshotProposal(**{
            **proposal.__dict__, "content_json": canonical_json(changed_content),
            "canonical_digest": _digest(changed_content),
        })
        with pytest.raises(BrokerConflict):
            store.propose_snapshot(changed)
        assert store.verify_integrity() is None


def test_intent_requires_existing_accepted_snapshots_and_digest_matches(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        intent, request = _bundle(store)
        assert intent.intent_digest
        assert store.get_request(request.request_id).state == "pending"
        events = store.list_events("operation", intent.operation_id)
        assert [event["event_type"] for event in events] == ["operation.intent_recorded"]
        assert store.verify_integrity() is None


def test_request_idempotency_and_divergent_parameters_conflict(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        intent, request = _bundle(store)
        assert store.create_request(request) == store.create_request(request)
        changed = build_contract(
            ApprovalRequest, protocol_version=1, request_id="request-different",
            operation_id=intent.operation_id, action_code=request.action_code,
            intent_digest=intent.intent_digest, requester_principal=request.requester_principal,
            nonce_digest=_digest({"different": True}), created_at=request.created_at,
            expires_at=request.expires_at, status="pending",
        )
        with pytest.raises(BrokerConflict, match="divergent"):
            store.create_request(changed)
        divergent_intent = build_contract(
            OperationIntent, **{**intent.to_dict(), "model_id": "different-model"}
        )
        with pytest.raises(BrokerConflict, match="divergent"):
            store.store_intent(divergent_intent)


def test_nonce_and_challenge_replay_are_rejected(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        _, first_request = _bundle(store, "replay-op-1")
        _, second_request = _bundle(store, "replay-op-2")
        _, third_request = _bundle(store, "replay-op-3", create_request=False)
        replayed_request = build_contract(
            ApprovalRequest,
            **{**third_request.to_dict(), "nonce_digest": first_request.nonce_digest},
        )
        with pytest.raises(BrokerConflict, match="nonce_digest"):
            store.create_request(replayed_request)
        store._record_decision(_decision(first_request))  # simulated verifier is test-only
        second_decision = _decision(second_request)
        replayed_decision = build_contract(
            ApprovalDecision,
            **{**second_decision.to_dict(), "challenge_digest": _decision(first_request).challenge_digest},
        )
        with pytest.raises(BrokerConflict, match="challenge digest"):
            store._record_decision(replayed_decision)


def test_unverified_request_cannot_be_created_from_missing_or_pending_snapshot(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        proposal = _bundle_snapshot_proposal()
        store.propose_snapshot(proposal)
        _, missing = _times()
        with pytest.raises(BrokerIntegrityError, match="snapshot"):
            store.store_intent(build_contract(
                OperationIntent, protocol_version=1, operation_id="bad-op", action_code="provider_call",
                principal_id="user", client_id="client", project_id="demo", task_id="task",
                attempt_id="attempt", specification_snapshot_id=proposal.snapshot_id,
                specification_digest=proposal.canonical_digest, plan_snapshot_id=proposal.snapshot_id,
                specification_revision_id=proposal.revision_id, plan_revision_id=proposal.revision_id,
                plan_digest=proposal.canonical_digest, checkpoint_snapshot_id=proposal.snapshot_id,
                checkpoint_digest=proposal.canonical_digest, target_id="gateway", executor_version="1",
                provider_id="provider", model_id="model", payload_snapshot_id=proposal.snapshot_id,
                payload_digest=proposal.canonical_digest, parameters_json="{}", budget_json="{}",
                policy_digest=_digest({"p": 1}), catalog_digest=_digest({"c": 1}),
                created_at=utc_now(), expires_at=missing,
            ))


def test_two_store_instances_cannot_acquire_single_instance_lock(tmp_path):
    path = tmp_path / "broker.sqlite"
    first = BrokerStore(path)
    try:
        with pytest.raises(BrokerStoreLocked):
            BrokerStore(path)
    finally:
        first.close()
    with BrokerStore(path) as second:
        assert second.verify_integrity() is None


def test_single_instance_lock_blocks_a_second_process(tmp_path):
    path = tmp_path / "broker.sqlite"
    package_root = str(Path(agentflow.__file__).resolve().parent.parent)
    child = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from agentflow.approval.persistence import BrokerStore,BrokerStoreLocked; "
        "\ntry: BrokerStore(sys.argv[2])\n"
        "except BrokerStoreLocked: print('LOCKED')\n"
        "else: print('UNEXPECTEDLY_ACQUIRED')"
    )
    with BrokerStore(path):
        result = subprocess.run([sys.executable, "-c", child, package_root, str(path)],
                                capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "LOCKED"


def test_abandoned_process_lock_and_sqlite_state_recover_after_crash(tmp_path):
    path = tmp_path / "broker.sqlite"
    package_root = str(Path(agentflow.__file__).resolve().parent.parent)
    child = (
        "import os,sys; sys.path.insert(0, sys.argv[1]); "
        "from agentflow.approval.persistence import BrokerStore; "
        "BrokerStore(sys.argv[2]); os._exit(0)"
    )
    result = subprocess.run([sys.executable, "-c", child, package_root, str(path)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    with BrokerStore(path) as recovered:
        assert recovered.verify_integrity() is None


def test_concurrent_operations_are_serialized_without_duplicate_ids(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        def create(index):
            intent, request = _bundle(store, f"concurrent-op-{index}")
            return store.create_request(request).request.request_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            identifiers = list(pool.map(create, range(20)))
        assert len(set(identifiers)) == 20
        assert store.db.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0] == 20
        assert store.verify_integrity() is None


def test_private_test_authority_decision_and_states_survive_restart(tmp_path):
    path = tmp_path / "broker.sqlite"
    store = BrokerStore(path)
    _, request = _bundle(store)
    store._record_decision(_decision(request, "approved"))  # simulated authority is test-only
    assert store.get_request(request.request_id).state == "approved"
    store._transition_request(request.request_id, "revoked", "test-revoker")
    assert store.get_request(request.request_id).state == "revoked"
    store.close()
    with BrokerStore(path) as reopened:
        assert reopened.get_request(request.request_id).state == "revoked"
        assert reopened.verify_integrity() is None
        assert not hasattr(reopened, "approve")
        assert not hasattr(reopened, "issue_grant")
    assert ApprovalBroker().status().available is False


def test_unknown_is_conservative_and_persists_after_reopen(tmp_path):
    path = tmp_path / "broker.sqlite"
    store = BrokerStore(path)
    _, request = _bundle(store)
    store._transition_request(request.request_id, "unknown", "test-recovery")
    store.close()
    with BrokerStore(path) as reopened:
        assert reopened.get_request(request.request_id).state == "unknown"
        with pytest.raises(BrokerConflict):
            reopened._transition_request(request.request_id, "approved", "test-recovery",
                                         proof_digest=_digest({"proof": True}))
        assert reopened.verify_integrity() is None


def test_expiry_is_deterministic_and_cannot_be_reversed(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        _, request = _bundle(store)
        expiry = datetime.fromisoformat(request.expires_at[:-1] + "+00:00")
        as_of = (expiry + timedelta(seconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        assert store.expire_pending_requests(as_of) == 1
        assert store.get_request(request.request_id).state == "expired"
        assert store.expire_pending_requests(as_of) == 0
        with pytest.raises(BrokerConflict):
            store._transition_request(request.request_id, "approved", "test",
                                     proof_digest=_digest({"proof": True}))
        assert store.verify_integrity() is None


def test_authorization_consumption_and_receipt_are_bound_and_append_only(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        intent, request = _bundle(store)
        decision = _decision(request)
        store._record_decision(decision)  # simulated verifier is test-only
        authorization = _authorization(request, intent)
        store._record_authorization(authorization)
        store._transition_authorization(authorization.authorization_id, "consumed", "test-teg")
        receipt = _receipt(intent, authorization)
        store._record_receipt(receipt)
        assert store.db.execute("SELECT status FROM authorization_state").fetchone()[0] == "consumed"
        assert store.db.execute("SELECT status FROM execution_receipt_state").fetchone()[0] == "succeeded"
        assert store.verify_integrity() is None


def test_failed_event_write_rolls_back_snapshot_and_history(tmp_path, monkeypatch):
    proposal = _bundle_snapshot_proposal()
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        def fail(*_args, **_kwargs):
            raise sqlite3.IntegrityError("injected event failure")

        monkeypatch.setattr(store, "_append_event", fail)
        with pytest.raises(sqlite3.IntegrityError):
            store.propose_snapshot(proposal)
        monkeypatch.undo()
        assert store.db.execute("SELECT COUNT(*) FROM broker_snapshots").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM broker_events").fetchone()[0] == 0
        assert store.verify_integrity() is None


def test_event_hash_chain_detects_tampering_and_fails_closed(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        store.propose_snapshot(_bundle_snapshot_proposal())
        store.db.execute("DROP TRIGGER broker_events_no_update")
        store.db.execute("UPDATE broker_events SET payload_json='{}' WHERE sequence=1")
        with pytest.raises(BrokerIntegrityError, match="hash chain"):
            store.verify_integrity()


def test_materialized_state_cannot_diverge_from_append_only_event_history(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        proposal = _bundle_snapshot_proposal()
        store.propose_snapshot(proposal)
        store.db.execute("UPDATE snapshot_state SET status='accepted' WHERE snapshot_id=?",
                         (proposal.snapshot_id,))
        with pytest.raises(BrokerIntegrityError, match="materialized state diverges"):
            store.verify_integrity()


def test_backup_restore_preserves_verified_history_and_never_overwrites(tmp_path):
    source = tmp_path / "broker.sqlite"
    backup = tmp_path / "backups" / "broker.bak"
    restored = tmp_path / "restore" / "broker.sqlite"
    with BrokerStore(source) as store:
        proposal = _bundle_snapshot_proposal()
        store.propose_snapshot(proposal)
        assert store.backup_to(backup) == backup.resolve()
        with pytest.raises(BrokerStoreError, match="new and distinct"):
            store.backup_to(backup)
    assert BrokerStore.restore_backup(backup, restored) == restored.resolve()
    with BrokerStore(restored) as copy:
        assert copy.get_snapshot("snapshot-one") == proposal
        assert copy.verify_integrity() is None
    with pytest.raises(BrokerStoreError, match="new and distinct"):
        BrokerStore.restore_backup(backup, restored)


def test_corrupt_backup_is_rejected_without_restore(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a sqlite database")
    target = tmp_path / "restored.sqlite"
    with pytest.raises(BrokerIntegrityError):
        BrokerStore.restore_backup(bad, target)
    assert not target.exists()
