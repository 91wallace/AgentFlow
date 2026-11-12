"""Human-readable local consent review, deliberately without authority.

The terminal captures an explicit decision but does not authenticate its user.
Callers must pass the resulting receipt and proof over a persisted, one-time
challenge to the MVP-17 LocalAuthorizationAuthority. The terminal text prompt is
not authentication. This module has no job dispatch path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import getpass
from typing import Protocol, TextIO

from .authorization import local_consent_challenge_digest
from .profile import (
    ConsentDecision,
    LocalConsentReceipt,
    LocalConsentRequest,
)


class ConsentReviewError(ValueError):
    """The review could not be safely completed."""


def _terminal_text(value: str, *, maximum: int = 4096) -> str:
    """Escape terminal control characters and bound untrusted persisted text."""
    if type(value) is not str:
        raise ConsentReviewError("presentation_text_invalid")
    encoded = []
    for char in value:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            encoded.append(f"\\u{code:04x}" if code > 0xFF else f"\\x{code:02x}")
        else:
            encoded.append(char)
    rendered = "".join(encoded)
    if len(rendered) > maximum:
        rendered = rendered[:maximum] + "…[truncated]"
    return rendered


class LocalConsentAuthority(Protocol):
    def authorize(self, request: LocalConsentRequest, receipt: LocalConsentReceipt,
                  proof: object, *, authentication_challenge=None,
                  now: datetime | None = None): ...


class LocalConsentProofProvider(Protocol):
    def prove(self, request: LocalConsentRequest, receipt: LocalConsentReceipt,
              challenge_nonce: str) -> object | None: ...


class UnavailableLocalConsentProofProvider:
    """Default until a platform authenticator is operationally integrated."""

    def prove(self, request: LocalConsentRequest, receipt: LocalConsentReceipt,
              challenge_nonce: str) -> object | None:
        return None


def format_consent_request(request: LocalConsentRequest, *, objective: str,
                           account_label: str,
                           authorized_scope: tuple[str, ...] = (),
                           task_summaries: tuple[str, ...] = (),
                           proposal_state: str = "pending",
                           workspace_path: str | None = None) -> str:
    """Render all authorization-relevant facts; account_label is unverified."""
    if not isinstance(request, LocalConsentRequest):
        raise ConsentReviewError("typed_local_consent_request_required")
    if type(objective) is not str or not objective.strip():
        raise ConsentReviewError("authoritative_objective_required")
    if type(account_label) is not str or not account_label.strip():
        raise ConsentReviewError("local_account_label_unavailable")
    limits = request.limits
    lines = (
        "AgentFlow — revisão de consentimento LOCAL_SUPERVISED",
        f"Proposta: {_terminal_text(request.proposal_id)}",
        f"Objetivo: {_terminal_text(objective)}",
        f"Workspace: {_terminal_text(request.workspace_id)}",
        f"Caminho canônico do workspace: {_terminal_text(workspace_path)}"
        if workspace_path else "Caminho canônico do workspace: indisponível",
        f"Estado persistido: {_terminal_text(proposal_state)}",
        "Escopo autorizado:",
        *(f"  - {_terminal_text(item)}" for item in authorized_scope),
        f"Digest: {_terminal_text(request.proposal_digest)}",
        f"Revisão: {request.revision}",
        "Operações solicitadas:",
        *(f"  - {_terminal_text(operation)}" for operation in request.allowed_operations),
        "Tarefas persistidas da proposta:",
        *(f"  - {_terminal_text(item)}" for item in task_summaries),
        "Limites:",
        f"  jobs={limits.max_jobs}; iterações={limits.max_iterations}; timeout={limits.timeout_seconds}s",
        f"  orçamento estimado={limits.estimated_budget} {limits.budget_currency} (não é teto financeiro do provider)",
        f"Validade do pedido: até {_terminal_text(request.expires_at)}",
        f"Identidade solicitada: {_terminal_text(request.operator_identity)}",
        f"Conta local apresentada pelo SO: {_terminal_text(account_label)} (rótulo não autenticado)",
        "Ação digitada não autentica identidade e, por si só, não aprova nem autoriza execução.",
    )
    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode("utf-8")) > 64 * 1024:
        raise ConsentReviewError("consent_presentation_exceeds_size_limit")
    return rendered


class TerminalConsentReview:
    """Cross-platform review prompt; its receipt is untrusted until authenticated."""

    def __init__(self, *, input_stream: TextIO, output_stream: TextIO,
                 objective_resolver=None, authoritative_context_resolver=None,
                 account_label: str | None = None, clock=None,
                 receipt_ttl_seconds: int = 120):
        if type(receipt_ttl_seconds) is not int or not 1 <= receipt_ttl_seconds <= 120:
            raise ConsentReviewError("receipt_ttl_out_of_range")
        self._input = input_stream
        self._output = output_stream
        if not callable(objective_resolver) and not callable(authoritative_context_resolver):
            raise ConsentReviewError("authoritative_objective_resolver_required")
        if authoritative_context_resolver is not None and not callable(authoritative_context_resolver):
            raise ConsentReviewError("authoritative_context_resolver_invalid")
        self._objective_resolver = objective_resolver
        self._authoritative_context_resolver = authoritative_context_resolver
        self._account_label = account_label if account_label is not None else getpass.getuser()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ttl = receipt_ttl_seconds

    def request_consent(self, request: LocalConsentRequest) -> LocalConsentReceipt | None:
        context = (self._authoritative_context_resolver(request)
                   if self._authoritative_context_resolver is not None else None)
        if context is not None:
            if getattr(context, "request", None) != request:
                raise ConsentReviewError("authoritative_request_binding_mismatch")
            if not getattr(context, "approvable_for_review", False):
                self._output.write(f"Proposta não aprovável neste estado: {context.broker_status}.\n")
                raise ConsentReviewError("proposal_not_approvable_in_current_state")
            objective = context.objective
            scope = context.authorized_scope
            summaries = context.task_summaries
            persisted_status = context.broker_status
            workspace_path = context.workspace_path
        else:
            objective = self._objective_resolver(request)
            scope, summaries, persisted_status = (), (), "pending (não revalidado)"
            workspace_path = None
        self._output.write(format_consent_request(
            request, objective=objective, account_label=self._account_label,
            authorized_scope=scope, task_summaries=summaries,
            proposal_state=persisted_status, workspace_path=workspace_path))
        self._output.write("Digite approve, reject ou cancel: ")
        self._output.flush()
        answer = self._input.readline()
        if answer == "":
            return None
        decision = {"approve": ConsentDecision.APPROVE,
                    "reject": ConsentDecision.REJECT,
                    "cancel": None}.get(answer.strip().casefold(), ...)
        if decision is ...:
            raise ConsentReviewError("decision_input_invalid")
        if decision is None:
            return None
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ConsentReviewError("aware_clock_required")
        now = now.astimezone(timezone.utc)
        request_expiry = datetime.fromisoformat(request.expires_at.replace("Z", "+00:00"))
        expiry = min(request_expiry, now + timedelta(seconds=self._ttl))
        if expiry <= now:
            raise ConsentReviewError("consent_request_expired")
        return LocalConsentReceipt(
            request_digest=request.request_digest,
            decision=decision,
            operator_identity=request.operator_identity,
            explicit_action=True,
            recorded_at=now.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            expires_at=expiry.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )


def review_authentication_challenge(request: LocalConsentRequest,
                                    receipt: LocalConsentReceipt) -> str:
    """Legacy deterministic decision digest; not the one-time OS challenge.

    Operational review uses ``SQLiteLocalAuthenticationChallengeStore`` and a
    random persisted nonce. This helper remains for compatibility and tests.
    """
    return local_consent_challenge_digest(request, receipt)


class LocalConsentReviewService:
    """Connect explicit UI action to MVP-17; no default positive collaborators."""

    def __init__(self, interface: TerminalConsentReview, authority: LocalConsentAuthority,
                 proof_provider: LocalConsentProofProvider | None = None,
                 challenge_store=None):
        self.interface = interface
        self.authority = authority
        self.proof_provider = proof_provider or UnavailableLocalConsentProofProvider()
        self.challenge_store = challenge_store or getattr(
            authority, "authentication_challenge_store", None)

    def review(self, request: LocalConsentRequest, *, now: datetime | None = None):
        receipt = self.interface.request_consent(request)
        if receipt is None:
            record_cancelled = getattr(self.authority, "record_cancelled", None)
            if callable(record_cancelled):
                return record_cancelled(request, now=now)
            return None
        if self.challenge_store is None:
            raise ConsentReviewError("persistent_authentication_challenge_store_required")
        try:
            challenge = self.challenge_store.issue(request, receipt, now=now)
        except Exception as exc:
            raise ConsentReviewError("authentication_challenge_unavailable") from exc
        try:
            proof = self.proof_provider.prove(request, receipt, challenge.nonce)
        except Exception as exc:
            self.challenge_store.fail(challenge, reason="authenticator_error", now=now)
            raise ConsentReviewError("local_authentication_failed") from exc
        try:
            return self.authority.authorize(request, receipt, proof,
                                            authentication_challenge=challenge, now=now)
        finally:
            clear_session = getattr(self.proof_provider, "clear_session", None)
            if callable(clear_session):
                clear_session()
