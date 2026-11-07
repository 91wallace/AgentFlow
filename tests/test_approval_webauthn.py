import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agentflow.approval.identity import certificate_fingerprint
from agentflow.approval.persistence import BrokerConflict, BrokerStore, BrokerStoreError, SCHEMA_VERSION
from agentflow.approval.protocol import ApprovalRequest, build_contract, digest_json, utc_now
from agentflow.approval.webauthn import (
    WebAuthnAssertion, WebAuthnVerificationError, b64url_encode, build_credential,
)
from test_approval_identity import _identity_bundle, _intent, _stamp


ORIGIN = "https://approver.example.test"
RP_ID = "example.test"


def _challenge_bundle(store, operation_id="webauthn-op", *, now=None, ttl=120, credential_material=None,
                      include_approval_context=True, provider_payload=None):
    existing_client = store.get_client("agentflow-client")
    if existing_client:
        identity = existing_client.identity
        client_credential = store.get_client_credential(
            certificate_fingerprint(b"ephemeral-test-certificate")).credential
        policy = store.get_client_policy("agentflow-client", "policy-ci").policy
    else:
        identity, client_credential, policy = _identity_bundle(store)
    intent = _intent(store, operation_id, include_approval_context=include_approval_context,
                     provider_payload=provider_payload)
    store.authorize_client_operation(
        client_id=identity.client_id, fingerprint=client_credential.fingerprint,
        policy_id=policy.policy_id, intent=intent,
        nonce_digest=digest_json({"operation": operation_id}),
    )
    request = build_contract(
        ApprovalRequest, protocol_version=1, request_id=f"request-{operation_id}",
        operation_id=operation_id, action_code=intent.action_code, intent_digest=intent.intent_digest,
        requester_principal=intent.principal_id, nonce_digest=digest_json({"request": operation_id}),
        created_at=_stamp(-1), expires_at=_stamp(240), status="pending",
    )
    request_record = store.create_request(request)
    if credential_material is None:
        from agentflow.approval.webauthn import WebAuthnCredential
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        credential = build_credential(
            WebAuthnCredential, protocol_version=1,
            credential_id=b64url_encode(b"credential-id-" + operation_id.encode()),
            principal_id="approver-human", rp_id=RP_ID, allowed_origins=(ORIGIN,), algorithm=-7,
            public_key_spki_b64url=b64url_encode(public_der), created_at=_stamp(-1), expires_at=_stamp(3600),
        )
        store._store_webauthn_credential(credential)
    else:
        private_key, credential = credential_material
    presentation = store.build_consent_presentation(request_record.request.request_id, now=now)
    challenge = store._create_webauthn_challenge(
        request_record.request.request_id, "approver-human", RP_ID, (ORIGIN,),
        presentation_digest=presentation.presentation_digest, now=now, ttl_seconds=ttl,
    )
    return private_key, credential, challenge


def _assertion(private_key, credential, challenge, *, origin=ORIGIN, flags=0x05, counter=0,
               challenge_value=None, rp_hash=None):
    data = json.dumps({
        "type": "webauthn.get", "challenge": challenge_value or challenge.challenge_b64url,
        "origin": origin,
    }, separators=(",", ":")).encode("utf-8")
    auth_data = (rp_hash or hashlib.sha256(RP_ID.encode("ascii")).digest()) + bytes([flags]) + counter.to_bytes(4, "big")
    signature = private_key.sign(auth_data + hashlib.sha256(data).digest(), ec.ECDSA(hashes.SHA256()))
    return WebAuthnAssertion(1, challenge.binding.challenge_id, credential.credential_id,
                            data, auth_data, signature)


def test_schema_v3_migration_rollback_and_integrity(tmp_path, monkeypatch):
    import agentflow.approval.persistence as persistence

    path = tmp_path / "broker.sqlite"
    store = BrokerStore(path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
    store.rollback_webauthn_schema_if_empty()
    store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='webauthn_credentials'").fetchone() is None

    original = persistence._SCHEMA_V3
    monkeypatch.setattr(persistence, "_SCHEMA_V3", ("CREATE TABLE partial_v3(id TEXT)", "INVALID SQL"))
    with pytest.raises(BrokerStoreError, match="rolled back"):
        BrokerStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='partial_v3'").fetchone() is None
    monkeypatch.setattr(persistence, "_SCHEMA_V3", original)
    with BrokerStore(path) as reopened:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        reopened.verify_integrity()


def test_valid_assertion_is_one_shot_authentication_not_consent(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        private_key, credential, challenge = _challenge_bundle(store)
        binding = challenge.binding
        assert binding.operation_id == "webauthn-op"
        assert binding.action_code == "provider_call"
        assert binding.target_id == "trusted-provider-gateway"
        assert binding.approver_principal == "approver-human"
        assert binding.requester_principal == "principal-ci"
        assert binding.presentation_version == 1
        assert binding.presentation_digest == store.build_consent_presentation(binding.request_id).presentation_digest
        assert binding.allowed_origins == (ORIGIN,)
        assert binding.specification_digest and binding.plan_digest and binding.checkpoint_digest
        with pytest.raises(BrokerConflict, match="presentation changed"):
            store._create_webauthn_challenge(binding.request_id, "approver-human", RP_ID, (ORIGIN,),
                                             presentation_digest="sha256:" + "0" * 64)
        assertion = _assertion(private_key, credential, challenge)
        result = store.verify_webauthn_assertion(assertion)
        assert result.authenticated_principal == "approver-human"
        assert result.operation_id == "webauthn-op"
        assert result.user_present and result.user_verified
        assert result.consent_granted is False and result.approval_created is False
        assert store.db.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        counter_mode = store.db.execute("SELECT counter_mode FROM webauthn_credential_state WHERE credential_id=?",
                                        (credential.credential_id,)).fetchone()[0]
        assert counter_mode == "zero"
        with pytest.raises(WebAuthnVerificationError, match="replay"):
            store.verify_webauthn_assertion(assertion)
        store.verify_integrity()


@pytest.mark.parametrize(("origin", "flags", "challenge_value", "bad_signature"), [
    ("https://evil.example.test", 0x05, None, False),
    (ORIGIN, 0x01, None, False),
    (ORIGIN, 0x04, None, False),
    (ORIGIN, 0x05, "wrong-challenge", False),
    (ORIGIN, 0x05, None, True),
])
def test_invalid_assertions_are_rejected_and_consumed(tmp_path, origin, flags, challenge_value, bad_signature):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        private_key, credential, challenge = _challenge_bundle(store)
        assertion = _assertion(private_key, credential, challenge, origin=origin, flags=flags,
                               challenge_value=challenge_value)
        if bad_signature:
            assertion = WebAuthnAssertion(1, assertion.challenge_id, assertion.credential_id,
                                          assertion.client_data_json, assertion.authenticator_data, b"bad")
        with pytest.raises(WebAuthnVerificationError):
            store.verify_webauthn_assertion(assertion)
        row = store.db.execute("SELECT status FROM webauthn_challenge_state WHERE challenge_id=?",
                               (challenge.binding.challenge_id,)).fetchone()
        assert row[0] == "rejected"
        assert store.db.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0


def test_rp_id_hash_mismatch_is_rejected(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        private_key, credential, challenge = _challenge_bundle(store)
        wrong_hash = hashlib.sha256(b"attacker.example").digest()
        assertion = _assertion(private_key, credential, challenge, rp_hash=wrong_hash)
        with pytest.raises(WebAuthnVerificationError, match="rp_id_hash"):
            store.verify_webauthn_assertion(assertion)


def test_revocation_expiry_and_unknown_credential_fail_closed(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        private_key, credential, challenge = _challenge_bundle(store, "revoked-op")
        store.revoke_webauthn_credential(credential.credential_id)
        with pytest.raises(WebAuthnVerificationError, match="revoked"):
            store.verify_webauthn_assertion(_assertion(private_key, credential, challenge))

    with BrokerStore(tmp_path / "expired.sqlite") as store:
        base = datetime.now(timezone.utc)
        private_key, credential, challenge = _challenge_bundle(store, "expired-op", now=base, ttl=1)
        assertion = _assertion(private_key, credential, challenge)
        with pytest.raises(WebAuthnVerificationError, match="expired"):
            store.verify_webauthn_assertion(assertion, now=base + timedelta(seconds=2))
        state = store.db.execute("SELECT status FROM webauthn_challenge_state WHERE challenge_id=?",
                                 (challenge.binding.challenge_id,)).fetchone()
        assert state[0] == "expired"

    with BrokerStore(tmp_path / "unknown.sqlite") as store:
        private_key, credential, challenge = _challenge_bundle(store, "unknown-op")
        assertion = _assertion(private_key, credential, challenge)
        unknown = WebAuthnAssertion(1, assertion.challenge_id, b64url_encode(b"not-registered"),
                                    assertion.client_data_json, assertion.authenticator_data, assertion.signature)
        with pytest.raises(WebAuthnVerificationError, match="unknown"):
            store.verify_webauthn_assertion(unknown)


def test_counter_monotonicity_and_zero_counter_authenticators(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        private_key, credential, challenge = _challenge_bundle(store, "counter-one")
        store.verify_webauthn_assertion(_assertion(private_key, credential, challenge, counter=1))
        # A second challenge for another operation can verify only a larger counter.
        _, _, challenge2 = _challenge_bundle(store, "counter-two", credential_material=(private_key, credential))
        with pytest.raises(WebAuthnVerificationError, match="counter"):
            store.verify_webauthn_assertion(_assertion(private_key, credential, challenge2, counter=1))


def test_concurrent_replay_has_single_winner_and_interrupted_verification_recovers_unknown(tmp_path):
    path = tmp_path / "broker.sqlite"
    store = BrokerStore(path)
    private_key, credential, challenge = _challenge_bundle(store, "concurrent-op")
    assertion = _assertion(private_key, credential, challenge)

    def attempt():
        try:
            return store.verify_webauthn_assertion(assertion)
        except WebAuthnVerificationError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    _, _, interrupted = _challenge_bundle(store, "interrupted-op", credential_material=(private_key, credential))
    with store.transaction() as db:
        store._change_webauthn_challenge(db, interrupted.binding.challenge_id, "pending", "verifying",
                                         utc_now(), "webauthn_challenge.verification_started", None)
    store.close()
    with BrokerStore(path) as reopened:
        state = reopened.db.execute("SELECT status FROM webauthn_challenge_state WHERE challenge_id=?",
                                    (interrupted.binding.challenge_id,)).fetchone()
        assert state[0] == "unknown"
        reopened.verify_integrity()
        assert reopened.db.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0


def test_failure_after_crypto_leaves_verification_unknown_on_reopen(tmp_path, monkeypatch):
    path = tmp_path / "broker.sqlite"
    store = BrokerStore(path)
    private_key, credential, challenge = _challenge_bundle(store)
    assertion = _assertion(private_key, credential, challenge, counter=1)

    def fail_final_persist(*_args, **_kwargs):
        raise BrokerStoreError("injected final transaction failure")

    monkeypatch.setattr(store, "_revalidate_webauthn_binding", fail_final_persist)
    with pytest.raises(BrokerStoreError, match="injected"):
        store.verify_webauthn_assertion(assertion)
    store.close()
    with BrokerStore(path) as reopened:
        state = reopened.db.execute("SELECT status FROM webauthn_challenge_state WHERE challenge_id=?",
                                    (challenge.binding.challenge_id,)).fetchone()[0]
        count = reopened.db.execute("SELECT sign_count FROM webauthn_credential_state WHERE credential_id=?",
                                    (credential.credential_id,)).fetchone()[0]
        assert state == "unknown"
        assert count == 0
        with pytest.raises(WebAuthnVerificationError, match="replay"):
            reopened.verify_webauthn_assertion(assertion)


def test_raw_challenge_assertion_and_private_key_never_persisted(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        private_key, credential, challenge = _challenge_bundle(store)
        assertion = _assertion(private_key, credential, challenge)
        store.verify_webauthn_assertion(assertion)
        assert assertion.client_data_json.decode() not in repr(assertion)
        assert challenge.challenge_b64url not in repr(challenge)
        persisted = "\n".join(str(row) for row in store.db.execute(
            "SELECT credential_json FROM webauthn_credentials").fetchall())
        persisted += "\n" + "\n".join(str(row) for row in store.db.execute(
            "SELECT binding_json FROM webauthn_challenges").fetchall())
        persisted += "\n" + "\n".join(str(row) for row in store.db.execute(
            "SELECT payload_json FROM broker_events").fetchall())
        assert challenge.challenge_b64url not in persisted
        assert assertion.client_data_json.decode() not in persisted
        assert assertion.signature.hex() not in persisted
        assert b"PRIVATE KEY" not in persisted.encode()
