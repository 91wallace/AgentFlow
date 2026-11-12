"""CLI composition for reviewing one authoritative, persisted APL handoff."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import getpass
import re
import tomllib
from pathlib import Path
from typing import Callable, TextIO
import sys

from .authorization import (
    Ed25519LocalGrantVerifier,
    LocalAuthorizationAuthority,
    LocalAuthorizationOutcome,
    LocalAuthorizationPolicy,
    ResolvedLocalProposal,
    UnavailableLocalConsumerAuthenticator,
)
from .authoritative_consent import AuthoritativeConsentError, AuthoritativeConsentResolver
from .consent_ui import ConsentReviewError, LocalConsentReviewService, TerminalConsentReview
from .local_authentication import SQLiteLocalAuthenticationChallengeStore
from .local_passphrase import (
    LocalPassphraseGrantSigner,
    LocalPassphraseProofProvider,
    LocalPassphraseSigningSession,
    PassphraseLocalOperatorAuthenticator,
    SQLiteLocalOperatorCredentialStore,
    current_local_operator_principal,
)
from .profile import ConsentLimits, SecurityProfile


class LocalConsentConfigurationError(ValueError):
    """Local review policy is missing or ambiguous."""


@dataclass(frozen=True)
class LocalConsentRuntimeConfiguration:
    broker_database: Path
    project_id: str
    authorized_project_ids: tuple[str, ...]
    proposer_principal: str
    workspace_root: Path
    limits: ConsentLimits
    authorized_consumer_principals: tuple[str, ...]
    consent_ttl_seconds: int
    max_grant_ttl_seconds: int


def load_local_consent_configuration(path: Path, *, project_root: Path,
                                     state_database: Path) -> LocalConsentRuntimeConfiguration:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise LocalConsentConfigurationError("local_consent_configuration_unavailable") from exc
    required = {
        "schema_version", "broker_database", "project_id", "authorized_project_ids",
        "proposer_principal", "workspace_root", "max_jobs", "max_iterations",
        "max_timeout_seconds", "max_estimated_budget", "budget_currency",
        "authorized_consumer_principals", "consent_ttl_seconds", "max_grant_ttl_seconds",
    }
    if (type(raw) is not dict or set(raw) != required
            or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1):
        raise LocalConsentConfigurationError("local_consent_configuration_fields_invalid")
    string_fields = ("broker_database", "project_id", "proposer_principal",
                     "workspace_root", "max_estimated_budget", "budget_currency")
    if any(type(raw[name]) is not str or not raw[name] for name in string_fields):
        raise LocalConsentConfigurationError("local_consent_configuration_value_invalid")
    array_fields = ("authorized_project_ids", "authorized_consumer_principals")
    if any(type(raw[name]) is not list or not raw[name]
           or any(type(item) is not str or not item for item in raw[name])
           or len(set(raw[name])) != len(raw[name]) for name in array_fields):
        raise LocalConsentConfigurationError("local_consent_configuration_scope_invalid")
    integer_fields = ("max_jobs", "max_iterations", "max_timeout_seconds",
                      "consent_ttl_seconds", "max_grant_ttl_seconds")
    if any(type(raw[name]) is not int for name in integer_fields):
        raise LocalConsentConfigurationError("local_consent_configuration_limits_invalid")
    try:
        budget = Decimal(raw["max_estimated_budget"])
    except InvalidOperation as exc:
        raise LocalConsentConfigurationError("local_consent_budget_invalid") from exc
    if (not budget.is_finite() or budget < 0
            or not re.fullmatch(r"[A-Z]{3}", raw["budget_currency"])):
        raise LocalConsentConfigurationError("local_consent_budget_invalid")
    if raw["project_id"] not in raw["authorized_project_ids"]:
        raise LocalConsentConfigurationError("local_consent_project_not_allowlisted")
    try:
        broker = Path(raw["broker_database"])
        if not broker.is_absolute():
            broker = project_root / broker
        broker = broker.resolve(strict=True)
        workspace = Path(raw["workspace_root"])
        if not workspace.is_absolute():
            workspace = project_root / workspace
        workspace = workspace.resolve(strict=True)
        state_real = state_database.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LocalConsentConfigurationError("local_consent_store_or_workspace_unavailable") from exc
    if not broker.is_file() or not workspace.is_dir() or broker == state_real:
        raise LocalConsentConfigurationError("local_consent_store_or_workspace_invalid")
    try:
        limits = ConsentLimits(raw["max_jobs"], raw["max_iterations"],
                               raw["max_timeout_seconds"], budget, raw["budget_currency"])
    except ValueError as exc:
        raise LocalConsentConfigurationError("local_consent_limits_invalid") from exc
    if (not 1 <= raw["consent_ttl_seconds"] <= 120
            or not 1 <= raw["max_grant_ttl_seconds"] <= 300):
        raise LocalConsentConfigurationError("local_consent_ttl_invalid")
    return LocalConsentRuntimeConfiguration(
        broker, raw["project_id"], tuple(raw["authorized_project_ids"]),
        raw["proposer_principal"], workspace, limits,
        tuple(raw["authorized_consumer_principals"]),
        raw["consent_ttl_seconds"], raw["max_grant_ttl_seconds"],
    )


def review_persisted_handoff(*, cycle_store, state_database: Path, broker_store,
                             config: LocalConsentRuntimeConfiguration,
                             cycle_id: str, proposal_id: str,
                             input_stream: TextIO | None = None,
                             output_stream: TextIO | None = None,
                             hidden_prompt: Callable[[str], str] | None = None,
                             confirm_prompt: Callable[[str], str] | None = None,
                             now: datetime | None = None):
    """Resolve APL/Broker records, review explicitly, authenticate and persist a decision."""
    if not cycle_id or not proposal_id:
        raise ConsentReviewError("persisted_cycle_and_proposal_required")
    try:
        if (Path(cycle_store.path).resolve(strict=True) != state_database.resolve(strict=True)
                or Path(broker_store.path).resolve(strict=True)
                != config.broker_database.resolve(strict=True)):
            raise ConsentReviewError("consent_store_path_configuration_mismatch")
    except (AttributeError, OSError, RuntimeError) as exc:
        raise ConsentReviewError("consent_store_path_unavailable") from exc
    principal = current_local_operator_principal()
    credentials = SQLiteLocalOperatorCredentialStore(cycle_store)
    active = credentials.active(principal)

    resolver = AuthoritativeConsentResolver(
        apl_store=cycle_store, broker_store=broker_store,
        project_id=config.project_id, proposer_principal=config.proposer_principal,
        authorized_project_ids=config.authorized_project_ids,
        operator_identity=principal, limits=config.limits,
        workspace_root=config.workspace_root,
        consent_ttl_seconds=config.consent_ttl_seconds,
    )
    # Resolve in historical mode first so an expired consent window does not
    # hide a previously authenticated, durable decision. This mode never makes
    # an expired request approvable; only an existing matching audit record can
    # be returned from it.
    resolved = resolver.resolve(cycle_id, proposal_id, now=now,
                                 include_expired_for_history=True)
    prior = broker_store.get_local_consent_by_request(resolved.request.request_digest)
    if prior is not None:
        record, grant_id = prior
        if (record.request_digest != resolved.request.request_digest
                or record.decision.value in {"approve", "reject"}
                and (not record.explicit_action
                     or record.operator_identity != resolved.request.operator_identity
                     or record.operator_proof_digest is None)):
            raise ConsentReviewError("persisted_consent_decision_binding_invalid")
        request_expiry = datetime.fromisoformat(
            resolved.request.expires_at.replace("Z", "+00:00"))
        decision_time = datetime.fromisoformat(record.occurred_at.replace("Z", "+00:00"))
        if (record.decision.value in {"approve", "reject"}
                and decision_time >= request_expiry):
            raise ConsentReviewError("persisted_consent_decision_was_late")
        with broker_store._mutex:
            decision_count = broker_store.db.execute(
                "SELECT COUNT(*) FROM local_consent_decisions WHERE request_digest=?",
                (resolved.request.request_digest,),
            ).fetchone()[0]
            grants_for_request = tuple(row[0] for row in broker_store.db.execute(
                "SELECT grant_id FROM local_authorization_grants WHERE request_digest=?",
                (resolved.request.request_digest,),
            ).fetchall())
        if decision_count != 1:
            raise ConsentReviewError("persisted_consent_decision_not_unique")
        if grant_id is None:
            if grants_for_request:
                raise ConsentReviewError("persisted_decision_has_unmatched_grant")
            if record.decision.value == "approve":
                raise ConsentReviewError("approved_decision_missing_grant")
            state = "rejected" if record.decision.value == "reject" else "blocked"
            return LocalAuthorizationOutcome(record.decision, record.decision_id,
                                             None, None, state, "persisted_decision_recovered")
        grant, state = broker_store.get_local_grant(grant_id)
        if (len(grants_for_request) != 1 or grants_for_request[0] != grant_id
                or record.decision.value != "approve"
                or grant.consent_decision_id != record.decision_id
                or grant.request_digest != resolved.request.request_digest
                or grant.proposal_id != proposal_id
                or grant.plan_digest != resolved.request.proposal_digest
                or grant.revision != resolved.request.revision
                or grant.workspace_id != resolved.request.workspace_id
                or grant.allowed_operations != resolved.request.allowed_operations
                or grant.limits != resolved.request.limits
                or grant.operator_identity != resolved.request.operator_identity
                or grant.issued_at != record.occurred_at
                or datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
                > request_expiry):
            raise ConsentReviewError("persisted_grant_binding_invalid")
        credential_id = "local-credential-" + grant.key_id.removeprefix("local-operator-")
        public_record = credentials.public_key_record(credential_id)
        if public_record is None or public_record[0] != record.operator_identity:
            raise ConsentReviewError("persisted_grant_verifier_unavailable")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        verifier = Ed25519LocalGrantVerifier({
            grant.key_id: Ed25519PublicKey.from_public_bytes(public_record[1]),
        })
        if not grant.verify_signature(verifier):
            raise ConsentReviewError("persisted_grant_signature_invalid")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ConsentReviewError("aware_clock_required")
        grant_expiry = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
        if state == "approved" and grant_expiry <= instant.astimezone(timezone.utc):
            # Report effective expiry without mutating historical decision/grant rows.
            state = "expired"
        return LocalAuthorizationOutcome(record.decision, record.decision_id,
                                         grant.grant_id, grant.grant_digest,
                                         state, "persisted_decision_recovered")

    if not resolved.consent_window_open:
        raise AuthoritativeConsentError("proposal_expired")
    if not resolved.approvable_for_review:
        raise AuthoritativeConsentError("proposal_not_pending_or_not_current")
    if active is None:
        raise ConsentReviewError("local_operator_credential_not_enrolled")
    key_id = "local-operator-" + active[0].removeprefix("local-credential-")

    class PersistedProposalResolver:
        def resolve(self, candidate_id: str):
            if candidate_id != proposal_id:
                return None
            current = resolver.resolve(cycle_id, proposal_id, now=now)
            if not current.approvable_for_review:
                return None
            return ResolvedLocalProposal(
                current.apl_proposal_id, current.request.proposal_digest,
                current.request.revision, current.workspace_id,
                current.request.allowed_operations, config.proposer_principal,
            )

    non_operator = tuple(sorted(set((config.proposer_principal,)
                                    + config.authorized_consumer_principals)))
    policy = LocalAuthorizationPolicy(
        SecurityProfile.LOCAL_SUPERVISED, (principal,),
        config.authorized_consumer_principals, non_operator,
        config.limits.max_jobs, config.limits.max_iterations,
        config.limits.timeout_seconds, config.max_grant_ttl_seconds,
        config.limits.estimated_budget, config.limits.budget_currency,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    verifier = Ed25519LocalGrantVerifier({
        key_id: Ed25519PublicKey.from_public_bytes(active[2]),
    })
    session = LocalPassphraseSigningSession()
    signer = LocalPassphraseGrantSigner(key_id, active[0], session)
    challenge_store = SQLiteLocalAuthenticationChallengeStore(cycle_store)
    authority = LocalAuthorizationAuthority(
        store=broker_store, policy=policy, proposal_resolver=PersistedProposalResolver(),
        operator_authenticator=PassphraseLocalOperatorAuthenticator(credentials, principal),
        consumer_authenticator=UnavailableLocalConsumerAuthenticator(),
        signer=signer, verifier=verifier,
        authentication_challenge_store=challenge_store,
    )
    proof_provider = LocalPassphraseProofProvider(
        credentials, principal, prompt=hidden_prompt or getpass.getpass,
        confirm=confirm_prompt or input,
        clock=(lambda: now) if now is not None else (lambda: datetime.now(timezone.utc)),
        signing_session=session,
    )
    ui = TerminalConsentReview(
        input_stream=input_stream or sys.stdin, output_stream=output_stream or sys.stdout,
        authoritative_context_resolver=resolver.resolve_for_request,
        account_label=str(principal),
        clock=(lambda: now) if now is not None else (lambda: datetime.now(timezone.utc)),
    )
    return LocalConsentReviewService(ui, authority, proof_provider, challenge_store).review(
        resolved.request, now=now)
