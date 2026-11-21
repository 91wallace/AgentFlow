from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier, Event

import pytest

from agentflow.approval.persistence import BrokerConflict, BrokerStore
from agentflow.security import (
    AuthenticatedConsumer,
    AuthenticatedOperator,
    ConsentDecision,
    ConsentLimits,
    Ed25519LocalGrantSigner,
    Ed25519LocalGrantVerifier,
    LocalAuthorizationAuthority,
    LocalAuthorizationError,
    LocalAuthorizationPolicy,
    LocalConsentReceipt,
    LocalConsentRequest,
    LocalRevocationRequest,
    ResolvedLocalProposal,
    SecurityProfile,
    UnavailableLocalAuthorizationAuthority,
    local_consent_challenge_digest,
)


NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
PLAN_DIGEST = "sha256:" + "a" * 64
PROOF_DIGEST = "sha256:" + "b" * 64


class Resolver:
    def __init__(self, *, digest=PLAN_DIGEST, revision=2, operations=None, proposer="apl"):
        self.proposal = ResolvedLocalProposal(
            "proposal-1", digest, revision, "workspace-1",
            tuple(operations or ("write:src/math.py", "run:pytest")), proposer,
        )

    def resolve(self, proposal_id):
        return self.proposal if proposal_id == self.proposal.proposal_id else None


class OperatorAuth:
    def __init__(self, principal="operator-1"):
        self.principal = principal

    def authenticate(self, request_digest, proof, *, now):
        if proof != "operator-proof":
            return None
        return AuthenticatedOperator(self.principal, PROOF_DIGEST, "test-auth", now.isoformat())


class ConsumerAuth:
    def authenticate_consumer(self, grant_digest, proof, *, now):
        if proof != "executor-proof":
            return None
        return AuthenticatedConsumer("executor-1", PROOF_DIGEST, now.isoformat())


def setup_authority(tmp_path, *, resolver=None, operator_auth=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from agentflow.security import SQLiteLocalAuthenticationChallengeStore
    from agentflow.state.execution import ExecutionStore
    from agentflow.apl.persistence import SQLiteCycleStore

    store = BrokerStore(tmp_path / "broker.sqlite")
    state_path = tmp_path / "auth-state.sqlite"
    with ExecutionStore(state_path):
        pass
    challenge_store = SQLiteLocalAuthenticationChallengeStore(SQLiteCycleStore(state_path))
    private = Ed25519PrivateKey.generate()  # test-only ephemeral key
    public = private.public_key()
    signer = Ed25519LocalGrantSigner("test-key-1", private)
    verifier = Ed25519LocalGrantVerifier({"test-key-1": public})
    policy = LocalAuthorizationPolicy(
        SecurityProfile.LOCAL_SUPERVISED, ("operator-1",), ("executor-1",), ("apl",),
        5, 4, 600, 300, Decimal("10.00"), "USD",
    )
    authority = LocalAuthorizationAuthority(
        store=store, policy=policy, proposal_resolver=resolver or Resolver(),
        operator_authenticator=operator_auth or OperatorAuth(),
        consumer_authenticator=ConsumerAuth(), signer=signer, verifier=verifier,
        authentication_challenge_store=challenge_store,
    )
    return store, authority


def make_request(now=NOW):
    return LocalConsentRequest(
        profile=SecurityProfile.LOCAL_SUPERVISED,
        proposal_id="proposal-1", proposal_digest=PLAN_DIGEST, revision=2,
        workspace_id="workspace-1", allowed_operations=("write:src/math.py", "run:pytest"),
        limits=ConsentLimits(3, 2, 120, Decimal("1.00"), "USD"),
        expires_at=(now + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        operator_identity="operator-1",
    )


def make_receipt(request, decision=ConsentDecision.APPROVE, *, now=NOW):
    return LocalConsentReceipt(
        request.request_digest, decision, "operator-1", True,
        now.isoformat().replace("+00:00", "Z"), request.expires_at,
    )


def authorize(authority, request=None, receipt=None, *, challenge=None, now=NOW):
    request = request or make_request(now)
    receipt = receipt or make_receipt(request, now=now)
    challenge = challenge or authority.authentication_challenge_store.issue(request, receipt, now=now)
    return authority.authorize(request, receipt, "operator-proof",
                               authentication_challenge=challenge, now=now)


def test_local_consent_issues_narrow_signed_grant_atomically_and_never_dispatches(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        request = make_request()
        result = authorize(authority, request)
        grant, state = store.get_local_grant(result.grant_id)
        assert result.state == state == "approved"
        assert result.grant_digest == grant.grant_digest
        assert grant.verify_signature(authority.verifier)
        assert grant.proposal_id == request.proposal_id
        assert grant.plan_digest == request.proposal_digest
        assert grant.allowed_operations == request.allowed_operations
        assert grant.limits == request.limits
        assert result.reason == "local_grant_issued_not_dispatched"
        assert authority.validate_grant(grant.grant_id,
                                        expected_request_digest=request.request_digest,
                                        now=NOW).state == "approved"
        assert store.db.execute("SELECT COUNT(*) FROM local_consent_decisions").fetchone()[0] == 1
        assert store.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        assert store.local_grant_history(grant.grant_id)[0]["event_type"] == "local_grant.issued"
        store.verify_integrity()
    finally:
        store.close()


def test_denied_consent_persists_without_grant(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        request = make_request()
        result = authorize(authority, request, make_receipt(request, ConsentDecision.REJECT))
        assert result.state == "rejected" and result.grant_id is None
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        store.verify_integrity()
    finally:
        store.close()


def test_ui_cancellation_is_append_only_and_idempotent_without_grant(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        request = make_request()
        from io import StringIO
        from agentflow.security import LocalConsentReviewService, TerminalConsentReview
        terminal = TerminalConsentReview(
            input_stream=StringIO("cancel\n"), output_stream=StringIO(),
            objective_resolver=lambda _request: "test objective", account_label="operator",
            clock=lambda: NOW)
        first = LocalConsentReviewService(terminal, authority).review(request, now=NOW)
        second = authority.record_cancelled(request, now=NOW)
        assert first.state == second.state == "blocked"
        assert first.reason == "operator_cancelled"
        assert second.reason == "idempotent_cancel"
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        assert store.local_consent_history(first.consent_decision_id)[0]["event_type"] == \
            "local_consent.decision_recorded"
        store.verify_integrity()
        store.close()
        reopened = BrokerStore(tmp_path / "broker.sqlite")
        try:
            record, grant_id = reopened.get_local_consent_by_request(request.request_digest)
            assert record.reason_code == "operator_cancelled"
            assert record.explicit_action is False
            assert grant_id is None
            assert reopened.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        finally:
            reopened.close()
    finally:
        try:
            store.close()
        except Exception:
            pass


def test_terminal_approval_without_operational_auth_is_blocked_and_persisted(tmp_path):
    from io import StringIO
    from agentflow.security import (LocalConsentReviewService, TerminalConsentReview,
                                    UnavailableLocalConsentProofProvider,
                                    UnavailableLocalOperatorAuthenticator)
    store, authority = setup_authority(
        tmp_path, operator_auth=UnavailableLocalOperatorAuthenticator())
    try:
        request = make_request()
        terminal = TerminalConsentReview(
            input_stream=StringIO("approve\n"), output_stream=StringIO(),
            objective_resolver=lambda _request: "test objective", account_label="local-account",
            clock=lambda: NOW)
        outcome = LocalConsentReviewService(
            terminal, authority, UnavailableLocalConsentProofProvider()).review(request, now=NOW)
        assert outcome.state == "blocked"
        assert outcome.grant_id is None
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        record, grant_id = store.get_local_consent_by_request(request.request_digest)
        assert record.reason_code == "operator_authentication_failed"
        assert record.operator_identity is None
        assert grant_id is None
        assert store.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        with authority.authentication_challenge_store.apl_store._connect(readonly=True) as db:
            assert db.execute("SELECT event_type FROM apl_local_auth_challenge_events "
                              "ORDER BY event_id DESC LIMIT 1").fetchone()[0] == "failed"
    finally:
        store.close()


def test_authority_refuses_approval_without_persisted_unique_challenge(tmp_path):
    store, authority = setup_authority(tmp_path)
    request = make_request()
    receipt = make_receipt(request)
    authority.authentication_challenge_store = None
    try:
        with pytest.raises(LocalAuthorizationError, match="persistent_authentication_challenge_required"):
            authority.authorize(request, receipt, "operator-proof", now=NOW)
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
    finally:
        store.close()


def test_authenticator_challenge_changes_with_explicit_decision():
    request = make_request()
    approval = make_receipt(request, ConsentDecision.APPROVE)
    rejection = make_receipt(request, ConsentDecision.REJECT)
    assert local_consent_challenge_digest(request, approval) != local_consent_challenge_digest(
        request, rejection
    )


def test_invalid_operator_is_recorded_as_blocked_and_emits_no_grant(tmp_path):
    store, authority = setup_authority(tmp_path, operator_auth=OperatorAuth("other-user"))
    try:
        result = authorize(authority)
        assert result.state == "blocked" and result.grant_id is None
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        store.verify_integrity()
    finally:
        store.close()


@pytest.mark.parametrize("resolver", [
    Resolver(digest="sha256:" + "c" * 64),
    Resolver(revision=3),
    Resolver(operations=("run:pytest",)),
])
def test_digest_revision_and_scope_mismatches_are_denied(tmp_path, resolver):
    store, authority = setup_authority(tmp_path, resolver=resolver)
    try:
        with pytest.raises(LocalAuthorizationError):
            authorize(authority)
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
    finally:
        store.close()


def test_self_approval_is_recorded_as_blocked(tmp_path):
    store, authority = setup_authority(tmp_path, resolver=Resolver(proposer="operator-1"))
    try:
        result = authorize(authority)
        assert result.state == "blocked" and result.reason == "self_approval_forbidden"
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
    finally:
        store.close()


def test_replay_returns_same_grant_and_conflicting_decision_is_rejected(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        request = make_request()
        first = authorize(authority, request)
        replay = authorize(authority, request)
        assert replay.grant_id == first.grant_id
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 1
        with pytest.raises(LocalAuthorizationError, match="replay_conflicts"):
            authorize(authority, request, make_receipt(request, ConsentDecision.REJECT))
    finally:
        store.close()


def test_concurrent_replay_issues_one_grant(tmp_path):
    store, authority = setup_authority(tmp_path)
    request = make_request()
    receipt = make_receipt(request)
    challenge = authority.authentication_challenge_store.issue(request, receipt, now=NOW)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: authorize(authority, request, receipt,
                                                       challenge=challenge), range(2)))
        assert results[0].grant_id == results[1].grant_id
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 1
        store.verify_integrity()
    finally:
        store.close()


def test_consume_is_authenticated_one_shot_and_replay_fails(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        grant_id = authorize(authority).grant_id
        consumed = authority.consume(grant_id, proof="executor-proof", now=NOW)
        assert consumed.state == "consumed" and consumed.dispatch_enabled is False
        with pytest.raises(LocalAuthorizationError, match="grant_consumed"):
            authority.consume(grant_id, proof="executor-proof", now=NOW)
        assert [e["event_type"] for e in store.local_grant_history(grant_id)] == [
            "local_grant.issued", "local_grant.transitioned"
        ]
        store.verify_integrity()
    finally:
        store.close()


@pytest.mark.parametrize("schedule", ["both_verified", "second_reads_consumed"])
def test_concurrent_consumption_has_one_winner(tmp_path, monkeypatch, schedule):
    store, authority = setup_authority(tmp_path)
    try:
        grant_id = authorize(authority).grant_id
        first_finished = Event()
        if schedule == "both_verified":
            both_verified = Barrier(2)
            verify = authority._verified_grant

            def verified_before_transition(*args, **kwargs):
                result = verify(*args, **kwargs)
                both_verified.wait(timeout=10)
                return result

            monkeypatch.setattr(authority, "_verified_grant", verified_before_transition)

        def consume(index):
            if schedule == "second_reads_consumed" and index == 1:
                assert first_finished.wait(timeout=10)
            try:
                return authority.consume(grant_id, proof="executor-proof", now=NOW).state
            except BrokerConflict:
                return "replay_rejected"
            except LocalAuthorizationError as exc:
                # A later verifier observes the consumed state before the SQL CAS.
                if str(exc) != "grant_consumed":
                    raise
                return "replay_rejected"
            finally:
                if index == 0:
                    first_finished.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            states = list(pool.map(consume, range(2)))
        assert sorted(states) == ["consumed", "replay_rejected"]
        assert len([e for e in store.local_grant_history(grant_id)
                    if e["event_type"] == "local_grant.transitioned"]) == 1
        store.verify_integrity()
    finally:
        store.close()


def test_consumer_identity_and_grant_binding_are_checked(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        result = authorize(authority)
        grant, _ = store.get_local_grant(result.grant_id)
        with pytest.raises(LocalAuthorizationError, match="request_binding"):
            authority.validate_grant(grant.grant_id, expected_request_digest="sha256:" + "c" * 64,
                                     now=NOW)
        with pytest.raises(LocalAuthorizationError, match="consumer_authentication"):
            authority.consume(grant.grant_id, proof="wrong", now=NOW)
    finally:
        store.close()


def test_revocation_requires_allowlisted_authenticated_operator(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        result = authorize(authority)
        grant, _ = store.get_local_grant(result.grant_id)
        request = LocalRevocationRequest(
            grant.grant_id, grant.grant_digest, "intruder", "operator_request", True,
            (NOW + timedelta(minutes=1)).isoformat(),
        )
        with pytest.raises(LocalAuthorizationError, match="authentication_failed"):
            authority.revoke(request, "operator-proof", now=NOW)
        assert store.get_local_grant(grant.grant_id)[1] == "approved"
    finally:
        store.close()


def test_plan_change_invalidates_grant_before_use(tmp_path):
    resolver = Resolver()
    store, authority = setup_authority(tmp_path, resolver=resolver)
    try:
        result = authorize(authority)
        resolver.proposal = ResolvedLocalProposal(
            "proposal-1", "sha256:" + "d" * 64, 3, "workspace-1",
            ("write:src/math.py", "run:pytest"), "apl",
        )
        with pytest.raises(LocalAuthorizationError, match="obsolete"):
            authority.validate_grant(result.grant_id,
                                     expected_request_digest=make_request().request_digest,
                                     now=NOW)
        assert store.get_local_grant(result.grant_id)[1] == "revoked"
        store.verify_integrity()
    finally:
        store.close()


def test_authority_validation_persists_grant_expiration(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        request = make_request()
        result = authorize(authority, request)
        with pytest.raises(LocalAuthorizationError, match="expired"):
            authority.validate_grant(
                result.grant_id, expected_request_digest=request.request_digest,
                now=NOW + timedelta(minutes=6),
            )
        assert store.get_local_grant(result.grant_id)[1] == "expired"
        store.verify_integrity()
    finally:
        store.close()


def test_revocation_persists_and_survives_store_restart(tmp_path):
    store, authority = setup_authority(tmp_path)
    request = make_request()
    try:
        result = authorize(authority, request)
        grant, _ = store.get_local_grant(result.grant_id)
        revocation = LocalRevocationRequest(
            grant.grant_id, grant.grant_digest, "operator-1", "operator_request", True,
            (NOW + timedelta(minutes=1)).isoformat(),
        )
        assert authority.revoke(revocation, "operator-proof", now=NOW).state == "revoked"
        assert authority.revoke(revocation, "operator-proof", now=NOW).state == "revoked"
        conflicting = LocalRevocationRequest(
            grant.grant_id, grant.grant_digest, "operator-1", "security_concern", True,
            (NOW + timedelta(minutes=1)).isoformat(),
        )
        with pytest.raises(LocalAuthorizationError, match="replay_conflict"):
            authority.revoke(conflicting, "operator-proof", now=NOW)
        store.verify_integrity()
        verifier = authority.verifier
        store.close()
        store, authority = setup_authority(tmp_path)
        authority.verifier = verifier
        recovered, state = store.get_local_grant(grant.grant_id)
        assert state == "revoked" and recovered.grant_digest == grant.grant_digest
        with pytest.raises(LocalAuthorizationError, match="grant_revoked"):
            authority.validate_grant(grant.grant_id, expected_request_digest=request.request_digest,
                                     now=NOW)
        store.verify_integrity()
    finally:
        store.close()


def test_expired_grant_is_persisted_terminal(tmp_path):
    store, authority = setup_authority(tmp_path)
    try:
        result = authorize(authority)
        expired_at = NOW + timedelta(minutes=6)
        assert store.expire_local_grants(expired_at.isoformat().replace("+00:00", "Z")) == 1
        _, state = store.get_local_grant(result.grant_id)
        assert state == "expired"
        assert store.expire_local_grants(expired_at.isoformat().replace("+00:00", "Z")) == 0
        store.verify_integrity()
    finally:
        store.close()


def test_failed_transaction_rolls_back_decision_grant_and_events(tmp_path, monkeypatch):
    store, authority = setup_authority(tmp_path)
    original = store._append_event
    calls = 0

    def fail_on_grant(db, aggregate_type, aggregate_id, event_type, payload, occurred_at):
        nonlocal calls
        calls += 1
        if event_type == "local_grant.issued":
            raise RuntimeError("synthetic journal failure")
        return original(db, aggregate_type, aggregate_id, event_type, payload, occurred_at)

    monkeypatch.setattr(store, "_append_event", fail_on_grant)
    try:
        with pytest.raises(RuntimeError, match="journal failure"):
            authorize(authority)
        assert store.db.execute("SELECT COUNT(*) FROM local_consent_decisions").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM broker_events").fetchone()[0] == 0
    finally:
        store.close()


def test_default_authority_is_unavailable_and_never_dispatches():
    unavailable = UnavailableLocalAuthorizationAuthority()
    assert unavailable.status() == {
        "available": False, "grant_issuance_enabled": False,
        "dispatch_enabled": False, "reason": "local_authority_not_homologated",
    }
    with pytest.raises(LocalAuthorizationError, match="unavailable"):
        unavailable.authorize(None, None)
