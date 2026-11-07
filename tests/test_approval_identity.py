from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from agentflow.approval.identity import (
    ClientCredential, ClientIdentity, ClientPolicy, certificate_fingerprint,
    evaluate_client_operation,
)
from agentflow.approval.persistence import BrokerConflict, BrokerStore, SCHEMA_VERSION
from agentflow.approval.protocol import (
    OperationIntent, SnapshotProposal, build_contract, canonical_json, digest_json,
)


def _stamp(delta=0):
    value = datetime.now(timezone.utc) + timedelta(seconds=delta)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _identity_bundle(store, *, client_id="agentflow-client", fingerprint=None):
    fingerprint = fingerprint or certificate_fingerprint(b"ephemeral-test-certificate")
    identity = build_contract(
        ClientIdentity, protocol_version=1, client_id=client_id, principal_id="principal-ci",
        scopes=("execution:propose",), created_at=_stamp(-1), expires_at=_stamp(3600),
    )
    credential = build_contract(
        ClientCredential, protocol_version=1, client_id=client_id, fingerprint=fingerprint,
        issued_at=_stamp(-1), not_before=_stamp(-1), expires_at=_stamp(1800),
    )
    policy = build_contract(
        ClientPolicy, protocol_version=1, policy_id="policy-ci", client_id=client_id,
        version=1, allowed_actions=("provider_call",), required_scopes=("execution:propose",),
        allowed_targets=("trusted-provider-gateway",), max_request_ttl_seconds=600,
        created_at=_stamp(-1),
    )
    store.register_client(identity, credential, policy)
    return identity, credential, policy


def _intent(store, operation_id="client-op-1", *, include_approval_context=False, provider_payload=None,
            unexpected_context_field=False, mismatched_approval_revision=False,
            budget_json=None):
    refs = {}
    for kind in ("specification", "plan", "checkpoint", "payload"):
        content = {"kind": kind, "approved": True}
        if kind == "payload" and include_approval_context:
            impact = {"kind": "task_attempt", "affected_requirement_ids": ["req-ci"],
                      "affected_task_ids": ["task-ci"]}
            impact["impact_digest"] = digest_json(impact)
            context = {
                "context_version": 1, "operation_id": operation_id, "project_id": "project-ci",
                "task_id": "task-ci", "attempt_id": "attempt-ci",
                "specification_revision_id": refs["specification"].revision_id,
                "specification_digest": refs["specification"].canonical_digest,
                "plan_revision_id": refs["plan"].revision_id,
                "plan_digest": refs["plan"].canonical_digest,
                "checkpoint_digest": refs["checkpoint"].canonical_digest,
                "scope": ["execution:propose"], "impact": impact,
            }
            if mismatched_approval_revision:
                context["specification_revision_id"] = "revision-divergent"
            if unexpected_context_field:
                context["description"] = "agent supplied text"
            content = {"provider_payload": provider_payload if provider_payload is not None else content,
                       "approval_context": context}
        proposal = SnapshotProposal(
            1, f"snapshot-{operation_id}-{kind}", kind, "project-ci", f"revision-{kind}",
            1, canonical_json(content), digest_json(content), canonical_json({"source": "test"}),
            "operator-fixture", f"propose-{operation_id}-{kind}", _stamp(-1), None,
        )
        store.propose_snapshot(proposal)
        store._transition_snapshot(proposal.snapshot_id, "accepted", "test-authority", _stamp(-1),
                                   proof_digest=digest_json({"test-only": kind}))
        refs[kind] = proposal
    intent = build_contract(
        OperationIntent, protocol_version=1, operation_id=operation_id, action_code="provider_call",
        principal_id="principal-ci", client_id="agentflow-client", project_id="project-ci",
        task_id="task-ci", attempt_id="attempt-ci",
        specification_snapshot_id=refs["specification"].snapshot_id,
        specification_revision_id=refs["specification"].revision_id,
        specification_digest=refs["specification"].canonical_digest,
        plan_snapshot_id=refs["plan"].snapshot_id, plan_revision_id=refs["plan"].revision_id,
        plan_digest=refs["plan"].canonical_digest,
        checkpoint_snapshot_id=refs["checkpoint"].snapshot_id,
        checkpoint_digest=refs["checkpoint"].canonical_digest,
        target_id="trusted-provider-gateway", executor_version="1.0",
        provider_id="provider-ci", model_id="model-ci",
        payload_snapshot_id=refs["payload"].snapshot_id,
        payload_digest=refs["payload"].canonical_digest,
        parameters_json=canonical_json({"mode": "test"}),
        budget_json=budget_json or canonical_json({"cost_status": "estimated"}),
        policy_digest=digest_json({"router": 1}), catalog_digest=digest_json({"catalog": 1}),
        created_at=_stamp(-1), expires_at=_stamp(300),
    )
    store.store_intent(intent)
    return intent


def test_identity_contracts_roundtrip_and_reject_unknown_scope_and_version():
    identity = build_contract(
        ClientIdentity, protocol_version=1, client_id="client-a", principal_id="principal-a",
        scopes=("execution:propose",), created_at=_stamp(-1), expires_at=_stamp(60),
    )
    assert ClientIdentity.from_json(identity.to_json()) == identity
    changed = identity.to_dict()
    changed["protocol_version"] = 999
    with pytest.raises(ValueError, match="unsupported protocol_version"):
        ClientIdentity.from_dict(changed)
    with pytest.raises(ValueError, match="scope"):
        build_contract(
            ClientIdentity, protocol_version=1, client_id="client-a", principal_id="principal-a",
            scopes=("*",), created_at=_stamp(-1), expires_at=_stamp(60),
        )


def test_operation_policy_rejects_missing_scope_action_and_target(tmp_path):
    fingerprint = certificate_fingerprint(b"least-privilege-cert")
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        identity, credential, policy = _identity_bundle(store, fingerprint=fingerprint)
        intent = _intent(store)
        snapshots = {
            "specification": intent.specification_digest,
            "plan": intent.plan_digest,
            "checkpoint": intent.checkpoint_digest,
            "payload": intent.payload_digest,
        }
        now = datetime.now(timezone.utc)
        under_scoped = build_contract(
            ClientIdentity, protocol_version=1, client_id=identity.client_id,
            principal_id=identity.principal_id, scopes=("execution:read",),
            created_at=identity.created_at, expires_at=identity.expires_at,
        )
        scope_result = evaluate_client_operation(
            identity=under_scoped, credential=credential, policy=policy, intent=intent,
            presented_fingerprint=fingerprint, accepted_snapshot_digests=snapshots, now=now,
        )
        assert scope_result.reason == "required_scope_missing"
        action_policy = build_contract(
            ClientPolicy, protocol_version=1, policy_id="policy-other-action",
            client_id=identity.client_id, version=1, allowed_actions=("model_escalation",),
            required_scopes=("execution:propose",), allowed_targets=("trusted-provider-gateway",),
            max_request_ttl_seconds=600, created_at=_stamp(-1),
        )
        action_result = evaluate_client_operation(
            identity=identity, credential=credential, policy=action_policy, intent=intent,
            presented_fingerprint=fingerprint, accepted_snapshot_digests=snapshots, now=now,
        )
        assert action_result.reason == "action_not_allowed"
        changed_target = build_contract(
            OperationIntent, **{**intent.to_dict(), "target_id": "unlisted-target"}
        )
        target_result = evaluate_client_operation(
            identity=identity, credential=credential, policy=policy, intent=changed_target,
            presented_fingerprint=fingerprint, accepted_snapshot_digests=snapshots, now=now,
        )
        assert target_result.reason == "target_not_allowed"


def test_client_registration_rotation_revocation_and_reopen_are_audited(tmp_path):
    path = tmp_path / "broker.sqlite"
    first_fp = certificate_fingerprint(b"test-leaf-one")
    replacement_fp = certificate_fingerprint(b"test-leaf-two")
    with BrokerStore(path) as store:
        identity, credential, policy = _identity_bundle(store, fingerprint=first_fp)
        assert store.get_client(identity.client_id).status == "active"
        assert store.get_client_credential(first_fp).status == "active"
        assert store.get_client_policy(identity.client_id, policy.policy_id).policy == policy
        replacement = build_contract(
            ClientCredential, protocol_version=1, client_id=identity.client_id,
            fingerprint=replacement_fp, issued_at=_stamp(1), not_before=_stamp(1),
            expires_at=_stamp(1200),
        )
        store.rotate_client_credential(identity.client_id, first_fp, replacement)
        assert store.get_client_credential(first_fp).status == "rotated"
        assert store.get_client_credential(replacement_fp).status == "active"
        store.revoke_client_credential(identity.client_id, replacement_fp)
        assert store.get_client_credential(replacement_fp).status == "revoked"
        store.verify_integrity()
    with BrokerStore(path) as reopened:
        assert reopened.get_client(identity.client_id).identity == identity
        assert reopened.get_client_credential(first_fp).status == "rotated"
        reopened.revoke_client(identity.client_id)
        assert reopened.get_client(identity.client_id).status == "revoked"
        reopened.verify_integrity()


def test_policy_update_is_versioned_append_only_and_replacement_revokes_old(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        identity, _, policy = _identity_bundle(store)
        replacement = build_contract(
            ClientPolicy, protocol_version=1, policy_id=policy.policy_id,
            client_id=identity.client_id, version=2, allowed_actions=("provider_call",),
            required_scopes=("execution:propose",), allowed_targets=("trusted-provider-gateway",),
            max_request_ttl_seconds=300, created_at=_stamp(),
        )
        store.register_client_policy(replacement)
        assert store.get_client_policy(identity.client_id, policy.policy_id).policy == replacement
        assert store.get_client_policy(identity.client_id, policy.policy_id, 1).status == "revoked"
        with pytest.raises(BrokerConflict, match="monotonically"):
            store.register_client_policy(policy)
        store.verify_integrity()


def test_authorization_is_technical_only_binds_snapshots_and_rejects_replay(tmp_path):
    fingerprint = certificate_fingerprint(b"authorized-cert")
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        identity, _, policy = _identity_bundle(store, fingerprint=fingerprint)
        intent = _intent(store)
        result = store.authorize_client_operation(
            client_id=identity.client_id, fingerprint=fingerprint, policy_id=policy.policy_id,
            intent=intent, nonce_digest=digest_json({"nonce": 1}),
        )
        assert result.permitted_to_propose is True
        assert result.consent_required is True
        assert result.reason == "technical_policy_satisfied_human_consent_required"
        assert store.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        replay = store.authorize_client_operation(
            client_id=identity.client_id, fingerprint=fingerprint, policy_id=policy.policy_id,
            intent=intent, nonce_digest=digest_json({"nonce": 1}),
        )
        assert replay.permitted_to_propose is False
        assert replay.reason == "operation_replay_detected"
        denied_events = store.db.execute(
            "SELECT payload_json FROM broker_events WHERE event_type='client_authorization.denied'"
        ).fetchall()
        assert len(denied_events) == 1
        assert "nonce" not in denied_events[0][0]
        assert fingerprint not in denied_events[0][0]
        assert store.authorize_client_operation(
            client_id="Bearer test-secret-value-1234567890", fingerprint=fingerprint, policy_id=policy.policy_id,
            intent=intent, nonce_digest=digest_json({"nonce": 2}),
        ).permitted_to_propose is False
        denial_json = store.db.execute(
            "SELECT payload_json FROM broker_events WHERE event_type='client_authorization.denied' "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        assert "Bearer" not in denial_json and "test-secret" not in denial_json
        store.verify_integrity()
    with BrokerStore(tmp_path / "broker.sqlite") as reopened:
        replay = reopened.authorize_client_operation(
            client_id=identity.client_id, fingerprint=fingerprint, policy_id=policy.policy_id,
            intent=intent, nonce_digest=digest_json({"nonce": 1}),
        )
        assert replay.reason == "operation_replay_detected"


def test_policy_mismatch_expiry_revocation_and_digest_mismatch_fail_closed(tmp_path):
    fingerprint = certificate_fingerprint(b"policy-cert")
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        identity, _, policy = _identity_bundle(store, fingerprint=fingerprint)
        intent = _intent(store)
        denied = store.authorize_client_operation(
            client_id=identity.client_id, fingerprint=fingerprint, policy_id="unknown-policy",
            intent=intent, nonce_digest=digest_json({"nonce": 10}),
        )
        assert denied.permitted_to_propose is False
        assert denied.reason == "unknown_client_credential_or_policy"
        expired = store.authorize_client_operation(
            client_id=identity.client_id, fingerprint=fingerprint, policy_id=policy.policy_id,
            intent=intent, nonce_digest=digest_json({"nonce": 11}),
            now=datetime.now(timezone.utc) + timedelta(seconds=2000),
        )
        assert expired.permitted_to_propose is False
        assert expired.reason == "client_credential_inactive_or_expired"
        changed = build_contract(
            OperationIntent, **{**intent.to_dict(), "specification_digest": digest_json({"different": 1})}
        )
        mismatch = store.authorize_client_operation(
            client_id=identity.client_id, fingerprint=fingerprint, policy_id=policy.policy_id,
            intent=changed, nonce_digest=digest_json({"nonce": 12}),
        )
        assert mismatch.permitted_to_propose is False
        assert mismatch.reason == "operation_intent_missing_or_divergent"
        store.revoke_client_credential(identity.client_id, fingerprint)
        revoked = store.authorize_client_operation(
            client_id=identity.client_id, fingerprint=fingerprint, policy_id=policy.policy_id,
            intent=intent, nonce_digest=digest_json({"nonce": 13}),
        )
        assert revoked.permitted_to_propose is False
        store.verify_integrity()


def test_concurrent_same_operation_claim_has_single_winner(tmp_path):
    fingerprint = certificate_fingerprint(b"concurrency-cert")
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        identity, _, policy = _identity_bundle(store, fingerprint=fingerprint)
        intent = _intent(store, "concurrent-client-operation")

        def attempt():
            return store.authorize_client_operation(
                client_id=identity.client_id, fingerprint=fingerprint,
                policy_id=policy.policy_id, intent=intent,
                nonce_digest=digest_json({"shared": "nonce"}),
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: attempt(), range(8)))
        assert sum(result.permitted_to_propose for result in results) == 1
        assert sum(result.reason == "operation_replay_detected" for result in results) == 7
        assert store.db.execute("SELECT COUNT(*) FROM broker_client_operations").fetchone()[0] == 1
        store.verify_integrity()


def test_broker_v1_database_migrates_transactionally_to_v3(tmp_path):
    import sqlite3
    import agentflow.approval.persistence as persistence

    path = tmp_path / "v1.sqlite"
    with sqlite3.connect(path) as db:
        for statement in persistence._SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={persistence.APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
    with BrokerStore(path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        assert store.db.execute("SELECT name FROM sqlite_master WHERE name='broker_clients'").fetchone()
        assert store.db.execute("SELECT name FROM sqlite_master WHERE name='webauthn_credentials'").fetchone()
        store.verify_integrity()


def test_failed_v1_v2_migration_rolls_back(tmp_path, monkeypatch):
    import sqlite3
    import agentflow.approval.persistence as persistence

    path = tmp_path / "v1.sqlite"
    with sqlite3.connect(path) as db:
        for statement in persistence._SCHEMA:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={persistence.APPLICATION_ID}")
        db.execute("PRAGMA user_version=1")
    monkeypatch.setattr(persistence, "_SCHEMA_V2", ("CREATE TABLE migration_partial(id TEXT)",
                                                        "CREATE TABLE broken syntax"))
    with pytest.raises(Exception, match="rolled back"):
        BrokerStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='migration_partial'").fetchone() is None


def test_identity_migration_can_rollback_only_before_any_broker_history(tmp_path):
    path = tmp_path / "empty-identity-store.sqlite"
    with BrokerStore(path) as store:
        store.rollback_identity_schema_if_empty()
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert store.db.execute("SELECT name FROM sqlite_master WHERE name='broker_clients'").fetchone() is None
    with BrokerStore(path) as store:
        identity, _, _ = _identity_bundle(store)
        with pytest.raises(Exception, match="after Broker history"):
            store.rollback_identity_schema_if_empty()
        assert store.get_client(identity.client_id).status == "active"
