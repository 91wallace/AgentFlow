from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest

from agentflow.security import (
    ConsentDecision,
    ConsentLimits,
    LocalConsentRequest,
    LocalConsentReviewService,
    LocalAuthorizationError,
    SecurityProfile,
    TerminalConsentReview,
    UnavailableLocalAuthorizationAuthority,
    UnavailableLocalConsentProofProvider,
    format_consent_request,
    review_authentication_challenge,
    SQLiteLocalAuthenticationChallengeStore,
    inspect_local_authentication,
)


NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def request():
    return LocalConsentRequest(
        SecurityProfile.LOCAL_SUPERVISED, "proposal-1", "sha256:" + "a" * 64, 3,
        "workspace-1", ("write:src/math.py", "run:pytest"),
        ConsentLimits(3, 2, 90, Decimal("1.25"), "USD"),
        (NOW + timedelta(minutes=2)).isoformat().replace("+00:00", "Z"), "operator-1",
    )


def ui(answer):
    return TerminalConsentReview(
        input_stream=StringIO(answer), output_stream=StringIO(),
        objective_resolver=lambda _request: "Implementar soma inteira com testes",
        account_label="local-account", clock=lambda: NOW)


@pytest.mark.parametrize(("answer", "decision"), [
    ("approve\n", ConsentDecision.APPROVE), ("reject\n", ConsentDecision.REJECT),
])
def test_terminal_review_collects_explicit_bound_decision(answer, decision):
    consent_request = request()
    terminal = ui(answer)
    receipt = terminal.request_consent(consent_request)
    assert receipt.decision is decision
    assert receipt.explicit_action is True
    assert receipt.request_digest == consent_request.request_digest
    assert receipt.operator_identity == consent_request.operator_identity
    assert receipt.expires_at <= consent_request.expires_at
    assert "Implementar soma inteira" in terminal._output.getvalue()
    assert "Digest: " + consent_request.proposal_digest in terminal._output.getvalue()
    assert "não autenticado" in terminal._output.getvalue()


def test_cancel_or_eof_does_not_create_receipt():
    assert ui("cancel\n").request_consent(request()) is None
    assert ui("").request_consent(request()) is None


def test_review_challenge_is_bound_to_exact_action_and_request():
    consent_request = request()
    approved = ui("approve\n").request_consent(consent_request)
    rejected = ui("reject\n").request_consent(consent_request)
    assert review_authentication_challenge(consent_request, approved) != \
        review_authentication_challenge(consent_request, rejected)


def test_invalid_decision_or_missing_authoritative_objective_fails_closed():
    with pytest.raises(ValueError, match="decision_input_invalid"):
        ui("yes\n").request_consent(request())
    with pytest.raises(ValueError, match="authoritative_objective_required"):
        format_consent_request(request(), objective="", account_label="local")


def test_missing_or_failing_authoritative_resolver_fails_before_prompt():
    output = StringIO()
    terminal = TerminalConsentReview(
        input_stream=StringIO("approve\n"), output_stream=output,
        authoritative_context_resolver=lambda _request: None,
        account_label="local-account", clock=lambda: NOW)
    with pytest.raises(ValueError, match="authoritative_consent_context_unavailable"):
        terminal.request_consent(request())
    assert output.getvalue() == ""

    terminal = TerminalConsentReview(
        input_stream=StringIO("approve\n"), output_stream=StringIO(),
        objective_resolver=None, authoritative_context_resolver=lambda _request: None,
        account_label="local-account", clock=lambda: NOW)
    with pytest.raises(ValueError, match="authoritative_consent_context_unavailable"):
        terminal.request_consent(request())

    terminal = TerminalConsentReview(
        input_stream=StringIO("approve\n"), output_stream=StringIO(),
        objective_resolver=lambda _request: None,
        account_label="local-account", clock=lambda: NOW)
    with pytest.raises(ValueError, match="authoritative_objective_required"):
        terminal.request_consent(request())


def test_invalid_authoritative_context_resolver_is_rejected_at_construction():
    with pytest.raises(ValueError, match="authoritative_context_resolver_invalid"):
        TerminalConsentReview(
            input_stream=StringIO("approve\n"), output_stream=StringIO(),
            objective_resolver=lambda _request: "objective",
            authoritative_context_resolver=object())


def test_authoritative_context_missing_required_presentation_data_fails_closed():
    from types import SimpleNamespace

    consent_request = request()
    context = SimpleNamespace(
        request=consent_request, approvable_for_review=True,
        authorized_scope=("workspace",), task_summaries=(),
        broker_status="pending", workspace_path="C:/workspace")
    terminal = TerminalConsentReview(
        input_stream=StringIO("approve\n"), output_stream=StringIO(),
        authoritative_context_resolver=lambda _request: context,
        account_label="local-account", clock=lambda: NOW)
    with pytest.raises(ValueError, match="authoritative_consent_context_invalid"):
        terminal.request_consent(consent_request)


def test_terminal_render_escapes_controls_and_bounds_untrusted_text():
    rendered = format_consent_request(
        request(), objective="goal\x1b[31m\r\nFAKE APPROVED", account_label="operator\x1b[2J",
        task_summaries=("task\x00 forged line",))
    assert "\x1b" not in rendered and "\r" not in rendered
    assert "\\x1b[31m\\x0d\\x0aFAKE APPROVED" in rendered
    assert "\\x00 forged line" in rendered
    with pytest.raises(ValueError, match="size_limit"):
        format_consent_request(request(), objective="goal", account_label="local",
                               task_summaries=tuple("x" * 4096 for _ in range(20)))


def test_unavailable_proof_and_authority_never_issue_grant_or_dispatch():
    proof_provider = UnavailableLocalConsentProofProvider()
    consent_request = request()
    receipt = ui("approve\n").request_consent(consent_request)
    challenge = review_authentication_challenge(consent_request, receipt)
    assert proof_provider.prove(consent_request, receipt, challenge) is None
    from agentflow.apl import SQLiteCycleStore
    from agentflow.state.execution import ExecutionStore
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "state.sqlite3"
        with ExecutionStore(state_path):
            pass
        challenge_store = SQLiteLocalAuthenticationChallengeStore(SQLiteCycleStore(state_path))
        service = LocalConsentReviewService(
            ui("approve\n"), UnavailableLocalAuthorizationAuthority(), proof_provider,
            challenge_store=challenge_store)
        with pytest.raises(LocalAuthorizationError, match="local_authority_unavailable"):
            service.review(consent_request, now=NOW)
        with SQLiteCycleStore(state_path, initialize=False)._connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM apl_local_auth_challenges").fetchone()[0] == 1


def test_persisted_challenge_is_unique_bound_and_one_time(tmp_path):
    from agentflow.apl import SQLiteCycleStore
    from agentflow.state.execution import ExecutionStore
    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    store = SQLiteLocalAuthenticationChallengeStore(SQLiteCycleStore(state_path))
    consent_request = request()
    receipt = ui("approve\n").request_consent(consent_request)
    challenge = store.issue(consent_request, receipt, now=NOW)
    assert challenge.nonce
    assert challenge.nonce not in repr(challenge)
    assert challenge.challenge_digest.startswith("sha256:")
    store.validate(challenge, consent_request, receipt, now=NOW)
    proof_digest = "sha256:" + "c" * 64
    assert store.consume(challenge, proof_digest, operation_id="test-consume", now=NOW)
    assert store.consume(challenge, proof_digest, operation_id="test-consume", now=NOW)
    with pytest.raises(ValueError, match="replay"):
        store.consume(challenge, "sha256:" + "d" * 64,
                      operation_id="different-consume", now=NOW)
    another_challenge = store.issue(consent_request, receipt, now=NOW)
    assert another_challenge.challenge_id != challenge.challenge_id
    assert another_challenge.nonce != challenge.nonce
    with SQLiteCycleStore(state_path, initialize=False)._connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM apl_local_auth_challenge_events").fetchone()[0] == 3
        row = db.execute("SELECT nonce_digest FROM apl_local_auth_challenges").fetchone()
        assert challenge.nonce not in row[0]
    reopened = SQLiteLocalAuthenticationChallengeStore(SQLiteCycleStore(state_path, initialize=False))
    reopened.validate(challenge, consent_request, receipt, now=NOW)
    from dataclasses import replace
    with pytest.raises(ValueError, match="binding_mismatch"):
        reopened.validate(replace(challenge, nonce="other-nonce"), consent_request,
                          receipt, now=NOW)


def test_authentication_challenge_rejects_expiry_and_receipt_divergence(tmp_path):
    from agentflow.apl import SQLiteCycleStore
    from agentflow.state.execution import ExecutionStore
    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    store = SQLiteLocalAuthenticationChallengeStore(SQLiteCycleStore(state_path))
    consent_request = request()
    receipt = ui("approve\n").request_consent(consent_request)
    with pytest.raises(ValueError, match="binding_invalid"):
        store.issue(consent_request, receipt, now=NOW + timedelta(minutes=3))
    challenge = store.issue(consent_request, receipt, now=NOW)
    altered = ui("reject\n").request_consent(consent_request)
    with pytest.raises(ValueError, match="binding_mismatch"):
        store.validate(challenge, consent_request, altered, now=NOW)
    with pytest.raises(ValueError, match="expired"):
        store.validate(challenge, consent_request, receipt,
                       now=NOW + timedelta(minutes=3))


def test_platform_auth_probe_does_not_claim_operational_authentication():
    class FakeWebAuthnApi:
        def WebAuthNGetApiVersionNumber(self):
            return 4
        WebAuthNAuthenticatorGetAssertion = object()
        WebAuthNFreeAssertion = object()

    api = inspect_local_authentication(platform="win32", library_loader=lambda _name: FakeWebAuthnApi())
    assert api.native_api_available is True
    assert api.ceremony_integrated is False
    assert api.authentication_operational is False
    linux = inspect_local_authentication(platform="linux")
    assert not linux.authentication_operational
