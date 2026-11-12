from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from agentflow.apl import SQLiteCycleStore
from agentflow.security import (
    AuthenticatedConsumer,
    ConsentDecision,
    ConsentLimits,
    Ed25519LocalGrantSigner,
    Ed25519LocalGrantVerifier,
    LocalAuthorizationAuthority,
    LocalAuthorizationPolicy,
    LocalConsentRequest,
    LocalPassphraseError,
    LocalPassphraseProofProvider,
    PassphraseLocalOperatorAuthenticator,
    ResolvedLocalProposal,
    SQLiteLocalAuthenticationChallengeStore,
    SQLiteLocalOperatorCredentialStore,
    SecurityProfile,
    LocalConsentReviewService,
    TerminalConsentReview,
    current_local_operator_principal,
)
from agentflow.state.execution import ExecutionStore


NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
PRINCIPAL = "linux-uid-1000"


def _state(path: Path):
    with ExecutionStore(path):
        pass
    return SQLiteCycleStore(path)


def _request():
    return LocalConsentRequest(
        SecurityProfile.LOCAL_SUPERVISED, "proposal-local-auth", "sha256:" + "a" * 64,
        2, "workspace-local-auth", ("write:src/math.py", "run:pytest"),
        ConsentLimits(2, 2, 60, Decimal("0.00"), "USD"),
        (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"), PRINCIPAL,
    )


def _receipt(request, decision=ConsentDecision.APPROVE):
    from agentflow.security import LocalConsentReceipt
    return LocalConsentReceipt(
        request.request_digest, decision, PRINCIPAL, True,
        NOW.isoformat().replace("+00:00", "Z"),
        (NOW + timedelta(seconds=90)).isoformat().replace("+00:00", "Z"),
    )


def _enrolled(tmp_path):
    path = tmp_path / "state.sqlite3"
    apl = _state(path)
    credentials = SQLiteLocalOperatorCredentialStore(apl)
    credential_id = credentials._enroll(PRINCIPAL, "correct horse battery staple", "correct horse battery staple", now=NOW)
    return path, apl, credentials, credential_id


def test_platform_principal_comes_from_os_identity_not_argument():
    assert current_local_operator_principal(platform="linux", uid_provider=lambda: 1000) == "linux-uid-1000"
    principal = current_local_operator_principal(
        platform="win32", sid_provider=lambda: "S-1-5-21-test")
    assert principal.startswith("win-sid-") and len(principal) == len("win-sid-") + 32
    with pytest.raises(LocalPassphraseError, match="unsupported"):
        current_local_operator_principal(platform="darwin")
    from agentflow.security import inspect_local_authentication
    capability = inspect_local_authentication(platform="linux")
    assert capability.local_passphrase_available
    assert capability.authentication_operational is False


def test_enrollment_is_interactive_policy_checked_and_persisted(tmp_path):
    _path, _apl, store, credential_id = _enrolled(tmp_path)
    assert credential_id.startswith("local-credential-")
    row = store.active(PRINCIPAL)
    assert row is not None and row[0] == credential_id
    with pytest.raises(LocalPassphraseError, match="already_enrolled"):
        store._enroll(PRINCIPAL, "another passphrase", "another passphrase", now=NOW)
    with pytest.raises(LocalPassphraseError, match="confirmation_mismatch"):
        store._enroll("linux-uid-1001", "long-enough-passphrase", "different-passphrase", now=NOW)
    with pytest.raises(LocalPassphraseError, match="policy"):
        store._enroll("linux-uid-1001", "short", "short", now=NOW)
    with store.apl_store._connect(readonly=True) as db:
        columns = {row[1] for row in db.execute(
            "PRAGMA table_info(apl_local_operator_credentials)")}
        stored = db.execute("SELECT salt,public_key FROM apl_local_operator_credentials").fetchone()
    assert "verifier" not in columns and "private_key" not in columns
    assert b"correct horse battery staple" not in bytes(stored[0]) + bytes(stored[1])


def test_challenge_proof_authenticates_exact_nonce_and_rejects_bad_secret(tmp_path):
    _path, _apl, credentials, credential_id = _enrolled(tmp_path)
    request = _request()
    receipt = _receipt(request)
    challenge_store = SQLiteLocalAuthenticationChallengeStore(_apl)
    challenge = challenge_store.issue(request, receipt, now=NOW)
    provider = LocalPassphraseProofProvider(
        credentials, PRINCIPAL, prompt=lambda _prompt: "correct horse battery staple",
        confirm=lambda _prompt: "APROVAR aaaaaaaaaaaa", clock=lambda: NOW)
    proof = provider.prove(request, receipt, challenge.nonce)
    authenticator = PassphraseLocalOperatorAuthenticator(credentials, PRINCIPAL)
    operator = authenticator.authenticate(challenge.nonce, proof, now=NOW)
    assert operator is not None
    assert operator.principal_id == PRINCIPAL
    assert operator.authentication_method == "local_passphrase_scrypt"
    assert proof.signature not in repr(proof).encode()
    assert authenticator.authenticate(challenge.nonce + "x", proof, now=NOW) is None
    assert authenticator.authenticate(challenge.nonce, object(), now=NOW) is None
    bad = LocalPassphraseProofProvider(credentials, PRINCIPAL,
                                       prompt=lambda _prompt: "incorrect passphrase",
                                       confirm=lambda _prompt: "APROVAR aaaaaaaaaaaa",
                                       clock=lambda: NOW)
    with pytest.raises(LocalPassphraseError, match="authentication_failed"):
        bad.prove(request, receipt, challenge.nonce)


def test_consumed_challenge_rejects_replay_and_survives_store_reopen(tmp_path):
    path, apl, credentials, _credential = _enrolled(tmp_path)
    request = _request()
    receipt = _receipt(request)
    challenge_store = SQLiteLocalAuthenticationChallengeStore(apl)
    challenge = challenge_store.issue(request, receipt, now=NOW)
    proof = LocalPassphraseProofProvider(
        credentials, PRINCIPAL, prompt=lambda _prompt: "correct horse battery staple",
        confirm=lambda _prompt: "APROVAR aaaaaaaaaaaa",
        clock=lambda: NOW
    ).prove(request, receipt, challenge.nonce)
    operator = PassphraseLocalOperatorAuthenticator(credentials, PRINCIPAL).authenticate(
        challenge.nonce, proof, now=NOW)
    assert operator is not None
    assert challenge_store.consume(challenge, operator.proof_digest,
                                   operation_id="consent-auth-replay-test", now=NOW)
    reopened = SQLiteCycleStore(path, initialize=False)
    reopened_challenges = SQLiteLocalAuthenticationChallengeStore(reopened)
    assert reopened_challenges.consume(challenge, operator.proof_digest,
                                       operation_id="consent-auth-replay-test", now=NOW)
    with pytest.raises(ValueError, match="replay"):
        reopened_challenges.consume(challenge, "sha256:" + "f" * 64,
                                    operation_id="different-operation", now=NOW)


def test_local_authority_persists_explicit_approval_without_dispatch(tmp_path):
    from agentflow.approval.persistence import BrokerStore

    _path, apl, credentials, _credential = _enrolled(tmp_path)
    request = _request()
    challenge_store = SQLiteLocalAuthenticationChallengeStore(apl)

    class Resolver:
        def resolve(self, proposal_id):
            if proposal_id != request.proposal_id:
                return None
            return ResolvedLocalProposal(
                request.proposal_id, request.proposal_digest, request.revision,
                request.workspace_id, request.allowed_operations, "apl")

    class Consumer:
        def authenticate_consumer(self, grant_digest, proof, *, now):
            return AuthenticatedConsumer("executor", "sha256:" + "b" * 64,
                                         now.isoformat())

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private = Ed25519PrivateKey.generate()
    broker = BrokerStore(tmp_path / "broker.sqlite3")
    try:
        authority = LocalAuthorizationAuthority(
            store=broker,
            policy=LocalAuthorizationPolicy(
                SecurityProfile.LOCAL_SUPERVISED, (PRINCIPAL,), ("executor",), ("apl",),
                2, 2, 60, 60, Decimal("1.00"), "USD"),
            proposal_resolver=Resolver(),
            operator_authenticator=PassphraseLocalOperatorAuthenticator(credentials, PRINCIPAL),
            consumer_authenticator=Consumer(),
            signer=Ed25519LocalGrantSigner("local-test", private),
            verifier=Ed25519LocalGrantVerifier({"local-test": private.public_key()}),
            authentication_challenge_store=challenge_store,
        )
        review_ui = TerminalConsentReview(
            input_stream=StringIO("approve\n"),
            output_stream=StringIO(),
            objective_resolver=lambda _request: "Criar função matemática sintética",
            account_label="owner-local", clock=lambda: NOW)
        proof_provider = LocalPassphraseProofProvider(
            credentials, PRINCIPAL, prompt=lambda _prompt: "correct horse battery staple",
            confirm=lambda _prompt: "APROVAR aaaaaaaaaaaa",
            clock=lambda: NOW)
        outcome = LocalConsentReviewService(
            review_ui, authority, proof_provider, challenge_store).review(request, now=NOW)
        assert outcome.decision is ConsentDecision.APPROVE
        assert outcome.state == "approved"
        assert outcome.grant_id
        stored = broker.get_local_consent_by_request(request.request_digest)
        assert stored is not None and stored[0].decision is ConsentDecision.APPROVE
        grant, grant_state = broker.get_local_grant(outcome.grant_id)
        assert grant_state == "approved"
        assert not hasattr(grant, "dispatch_enabled")
        assert broker.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
    finally:
        broker.close()


def test_credential_revocation_is_append_only_and_blocks_authentication(tmp_path):
    _path, _apl, credentials, credential_id = _enrolled(tmp_path)
    request = _request()
    proof = LocalPassphraseProofProvider(
        credentials, PRINCIPAL, prompt=lambda _prompt: "correct horse battery staple",
        confirm=lambda _prompt: "APROVAR aaaaaaaaaaaa",
        clock=lambda: NOW
    ).prove(request, _receipt(request), "n" * 32)
    authenticator = PassphraseLocalOperatorAuthenticator(credentials, PRINCIPAL)
    assert authenticator.authenticate("n" * 32, proof, now=NOW) is not None
    assert credentials.revoke(PRINCIPAL, credential_id=credential_id, now=NOW)
    assert not credentials.revoke(PRINCIPAL, credential_id=credential_id, now=NOW)
    assert credentials.active(PRINCIPAL) is None
    assert authenticator.authenticate("n" * 32, proof, now=NOW) is None


def test_provider_rejects_receipt_for_different_request_and_tty_is_required(tmp_path):
    from agentflow.security.local_passphrase import enroll_local_operator_interactive
    from agentflow.security import LocalConsentReceipt

    _path, _apl, credentials, _credential = _enrolled(tmp_path)
    request = _request()
    changed = LocalConsentReceipt(
        "sha256:" + "9" * 64, ConsentDecision.APPROVE, PRINCIPAL, True,
        NOW.isoformat().replace("+00:00", "Z"),
        (NOW + timedelta(seconds=60)).isoformat().replace("+00:00", "Z"))
    provider = LocalPassphraseProofProvider(
        credentials, PRINCIPAL, prompt=lambda _prompt: "correct horse battery staple",
        confirm=lambda _prompt: "APROVAR aaaaaaaaaaaa",
        clock=lambda: NOW)
    with pytest.raises(Exception):
        provider.prove(request, changed, "a" * 32)
    not_confirmed = LocalPassphraseProofProvider(
        credentials, PRINCIPAL,
        prompt=lambda _prompt: pytest.fail("password must not be requested before explicit confirmation"),
        confirm=lambda _prompt: "approve", clock=lambda: NOW)
    with pytest.raises(LocalPassphraseError, match="explicit_challenge_confirmation_failed"):
        not_confirmed.prove(request, _receipt(request), "a" * 32)
    not_confirmed = LocalPassphraseProofProvider(
        credentials, PRINCIPAL,
        prompt=lambda _prompt: pytest.fail("password must not be requested before explicit confirmation"),
        confirm=lambda _prompt: "approve", clock=lambda: NOW)
    with pytest.raises(LocalPassphraseError, match="explicit_challenge_confirmation_failed"):
        not_confirmed.prove(request, _receipt(request), "a" * 32)
    uninteractive = lambda _prompt: None
    with pytest.raises(LocalPassphraseError):
        enroll_local_operator_interactive(credentials, "linux-uid-1001", prompt=uninteractive)


def test_posix_credential_database_is_restricted_to_owner(tmp_path):
    import os
    import stat

    if os.name == "nt":
        pytest.skip("Unix permission bits do not represent Windows ACLs")
    path, _apl, _credentials, _credential = _enrolled(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
