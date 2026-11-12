import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from agentflow.approval.persistence import BrokerStore
from agentflow.approval.webauthn import WebAuthnAssertion, WebAuthnCredential, b64url_encode, build_credential
from agentflow.security import (
    ConsentDecision, ConsentLimits,
    LocalConsentReceipt, LocalConsentRequest, SecurityProfile,
    SQLiteLocalAuthenticationChallengeStore,
)
from agentflow.security import windows_webauthn as windows


def _cbor_head(major, number):
    if number < 24:
        return bytes([(major << 5) | number])
    if number <= 0xff:
        return bytes([(major << 5) | 24, number])
    if number <= 0xffff:
        return bytes([(major << 5) | 25]) + number.to_bytes(2, "big")
    if number <= 0xffffffff:
        return bytes([(major << 5) | 26]) + number.to_bytes(4, "big")
    return bytes([(major << 5) | 27]) + number.to_bytes(8, "big")


def _cbor(value):
    if type(value) is int:
        return _cbor_head(0, value) if value >= 0 else _cbor_head(1, -1 - value)
    if type(value) is bytes:
        return _cbor_head(2, len(value)) + value
    if type(value) is str:
        raw = value.encode()
        return _cbor_head(3, len(raw)) + raw
    if type(value) is dict:
        return _cbor_head(5, len(value)) + b"".join(_cbor(k) + _cbor(v) for k, v in value.items())
    raise TypeError(value)


def _registration_fixture(*, rp_id="localhost", flags=0x45):
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()
    cose = {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big")}
    credential_id = b"local-credential-001"
    auth_data = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + bytes(4)
                 + bytes(16) + len(credential_id).to_bytes(2, "big") + credential_id + _cbor(cose))
    attestation = _cbor({"fmt": "none", "authData": auth_data, "attStmt": {}})
    return private, credential_id, attestation


def _consent(now):
    request = LocalConsentRequest(
        SecurityProfile.LOCAL_SUPERVISED, "proposal-webauthn", "sha256:" + "a" * 64, 1,
        "workspace-webauthn", ("write:src/math.py",),
        ConsentLimits(1, 1, 60, Decimal("0"), "USD"),
        (now + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"), "test-operator")
    receipt = LocalConsentReceipt(request.request_digest, ConsentDecision.APPROVE,
                                  request.operator_identity, True,
                                  now.isoformat().replace("+00:00", "Z"), request.expires_at)
    return request, receipt


def test_registration_parser_extracts_only_public_key_and_requires_up_uv():
    _private, credential_id, attestation = _registration_fixture()
    encoded_id, algorithm, public_key, flags = windows._registration_material(
        attestation, challenge=b64url_encode(bytes(range(32))), rp_id="localhost")
    assert encoded_id == b64url_encode(credential_id)
    assert algorithm == -7
    key = serialization.load_der_public_key(
        __import__("base64").urlsafe_b64decode(public_key + "=" * ((4 - len(public_key) % 4) % 4)))
    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert flags & 0x05 == 0x05
    with pytest.raises(windows.WindowsWebAuthnError, match="up_uv"):
        windows._registration_material(_registration_fixture(flags=0x41)[2],
                                       challenge=b64url_encode(bytes(range(32))), rp_id="localhost")
    with pytest.raises(windows.WindowsWebAuthnError, match="rp_id_mismatch"):
        windows._registration_material(_registration_fixture(rp_id="wrong.local")[2],
                                       challenge=b64url_encode(bytes(range(32))), rp_id="localhost")


def test_cbor_decoder_rejects_indefinite_and_noncanonical_lengths():
    with pytest.raises(windows.WindowsWebAuthnError, match="indefinite"):
        windows._read_cbor(b"\x9f\xff")
    with pytest.raises(windows.WindowsWebAuthnError, match="noncanonical"):
        windows._read_cbor(b"\x18\x01")


def test_native_enrollment_refuses_noninteractive_bootstrap(tmp_path, monkeypatch):
    class FakeApi:
        called = False

        def make_credential(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("ceremony must not run in this test")

    monkeypatch.setattr(windows, "windows_operator_principal", lambda: "test-operator")
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        api = FakeApi()
        enrollment = windows.WindowsHelloCredentialEnrollment(store, api)
        with pytest.raises(windows.WindowsWebAuthnError, match="interactive_terminal"):
            enrollment.enroll_interactive(input_stream=StringIO("ENROLL WINDOWS HELLO\n"))
        assert not api.called
        assert store.db.execute("SELECT COUNT(*) FROM webauthn_credentials").fetchone()[0] == 0


def test_real_contracts_store_and_existing_crypto_verifier_accept_fixture_assertion(tmp_path, monkeypatch):
    from agentflow.apl.persistence import SQLiteCycleStore
    from agentflow.state.execution import ExecutionStore

    now = datetime.now(timezone.utc)
    request, receipt = _consent(now)
    principal = "test-operator"
    private = ec.generate_private_key(ec.SECP256R1())
    public_der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    credential = build_credential(
        WebAuthnCredential,
        protocol_version=1, credential_id=b64url_encode(b"synthetic-platform-credential"),
        principal_id=principal, rp_id=windows.RP_ID, allowed_origins=(windows.ORIGIN,),
        algorithm=-7, public_key_spki_b64url=b64url_encode(public_der),
        created_at=now.isoformat().replace("+00:00", "Z"), expires_at=None)
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        broker._store_webauthn_credential(credential)
        state_path = tmp_path / "apl.sqlite"
        with ExecutionStore(state_path):
            pass
        challenge_store = SQLiteLocalAuthenticationChallengeStore(SQLiteCycleStore(state_path))
        challenge = challenge_store.issue(request, receipt, now=now)

        class FixtureApi:
            def get_assertion(self, nonce, credential_ids):
                assert credential_ids == (b"synthetic-platform-credential",)
                raw, _buffer, _data = windows.WindowsWebAuthnApi._client_data("webauthn.get", nonce)
                auth_data = hashlib.sha256(windows.RP_ID.encode()).digest() + b"\x05" + (1).to_bytes(4, "big")
                signature = private.sign(auth_data + hashlib.sha256(raw).digest(), ec.ECDSA(hashes.SHA256()))
                challenge_id = "local-" + hashlib.sha256(nonce.encode("ascii")).hexdigest()[:32]
                return WebAuthnAssertion(1, challenge_id, credential.credential_id,
                                         raw, auth_data, signature)

        monkeypatch.setattr(windows, "windows_operator_principal", lambda: principal)
        proof = windows.WindowsWebAuthnProofProvider(broker, FixtureApi()).prove(
            request, receipt, challenge.nonce)
        challenge_store.validate(challenge, request, receipt, now=now)
        result = windows.WindowsWebAuthnOperatorAuthenticator(broker).authenticate(
            challenge.nonce, proof, now=now)
        assert result is not None
        assert result.principal_id == principal
        assert result.authentication_method == "windows_webauthn_uv"
        assert broker.list_active_webauthn_credentials(principal, windows.RP_ID)[0].sign_count == 1
        assert challenge_store.consume(challenge, result.proof_digest,
                                       operation_id="consent-auth-op", now=now)
        assert windows.WindowsWebAuthnOperatorAuthenticator(broker).authenticate(
            challenge.nonce, proof, now=now) is None


def test_windows_capability_probe_does_not_claim_operational_authentication():
    if __import__("sys").platform != "win32":
        pytest.skip("Windows-only API capability probe")
    api = windows.WindowsWebAuthnApi()
    assert api.version >= 1
    # Availability does not imply a configured platform UV authenticator or a completed ceremony.
    assert isinstance(api.platform_uv_available(), bool)


@pytest.mark.parametrize(("case", "expected"), [
    ("unknown", None), ("uv_missing", None), ("bad_signature", None),
    ("expired", None), ("revoked", None),
])
def test_assertion_rejects_unknown_uv_missing_bad_signature_and_expired(tmp_path, monkeypatch, case, expected):
    now = datetime.now(timezone.utc)
    principal = "test-operator"
    private = ec.generate_private_key(ec.SECP256R1())
    public_der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    credential = build_credential(
        WebAuthnCredential, protocol_version=1, credential_id=b64url_encode(b"known-credential"),
        principal_id=principal, rp_id=windows.RP_ID, allowed_origins=(windows.ORIGIN,),
        algorithm=-7, public_key_spki_b64url=b64url_encode(public_der),
        created_at=now.isoformat().replace("+00:00", "Z"), expires_at=None)
    monkeypatch.setattr(windows, "windows_operator_principal", lambda: principal)
    with BrokerStore(tmp_path / f"{case}.sqlite") as broker:
        broker._store_webauthn_credential(credential)
        if case == "revoked":
            broker.revoke_webauthn_credential(credential.credential_id)
        challenge = b64url_encode(bytes(range(32)))
        client_data, _buffer, _native_data = windows.WindowsWebAuthnApi._client_data(
            "webauthn.get", challenge)
        flags = 0x01 if case == "uv_missing" else 0x05
        auth_data = hashlib.sha256(windows.RP_ID.encode()).digest() + bytes([flags]) + (1).to_bytes(4, "big")
        signature = private.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        credential_id = b64url_encode(b"unknown-credential") if case == "unknown" else credential.credential_id
        if case == "bad_signature":
            signature = b"invalid-signature"
        assertion = WebAuthnAssertion(
            1, "local-" + hashlib.sha256(challenge.encode("ascii")).hexdigest()[:32],
            credential_id, client_data, auth_data, signature)
        expiry = now - timedelta(seconds=1) if case == "expired" else now + timedelta(seconds=30)
        proof = windows.LocalWebAuthnAssertionProof(
            assertion, expiry.isoformat().replace("+00:00", "Z"))
        result = windows.WindowsWebAuthnOperatorAuthenticator(broker).authenticate(
            challenge, proof, now=now)
        assert result is expected
        active = broker.list_active_webauthn_credentials(principal, windows.RP_ID)
        if case == "revoked":
            assert active == ()
        else:
            assert active[0].sign_count == 0


def test_cancel_hresult_is_a_cancelled_outcome_not_success():
    assert str(windows._native_error("assertion", 0x800704C7)) == \
        "windows_webauthn_assertion_cancelled"
    assert str(windows._native_error("enrollment", 0x800705B4)) != \
        "windows_webauthn_enrollment_cancelled"


def test_cli_enrollment_preflight_does_not_create_store_when_platform_uv_is_unavailable(tmp_path, monkeypatch):
    class UnavailableApi:
        def platform_uv_available(self):
            return False

    monkeypatch.setattr(windows, "WindowsWebAuthnApi", UnavailableApi)
    from agentflow.cli import main
    broker_path = tmp_path / "must-not-be-created.sqlite"
    assert main(["webauthn-enroll", "--broker-db", str(broker_path)]) == 2
    assert not broker_path.exists()


def test_public_credential_survives_broker_restart(tmp_path):
    now = datetime.now(timezone.utc)
    private = ec.generate_private_key(ec.SECP256R1())
    public_der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    credential = build_credential(
        WebAuthnCredential, protocol_version=1, credential_id=b64url_encode(b"recovery-credential"),
        principal_id="operator-recovery", rp_id=windows.RP_ID, allowed_origins=(windows.ORIGIN,),
        algorithm=-7, public_key_spki_b64url=b64url_encode(public_der),
        created_at=now.isoformat().replace("+00:00", "Z"), expires_at=None)
    path = tmp_path / "broker-restart.sqlite"
    with BrokerStore(path) as first:
        first._store_webauthn_credential(credential)
    with BrokerStore(path) as recovered:
        active = recovered.list_active_webauthn_credentials("operator-recovery", windows.RP_ID)
        assert len(active) == 1
        assert active[0].credential == credential
        assert active[0].status == "active"
