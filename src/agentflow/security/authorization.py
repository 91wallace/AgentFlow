"""LOCAL_SUPERVISED consent and grant contracts.

The classes here are not wired to the APL, executor, CLI, or dispatch. An
authority instance requires explicit trusted collaborators; defaults are
unavailable. In-process tests do not prove OS/process isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Protocol
from uuid import uuid4

from agentflow.approval.local_grants import (
    _DIGEST,
    _IDENTIFIER,
    LocalAuthorizationError,
    _id,
    _digest,
    _time,
    _stamp,
    _canonical,
    _hash,
    _b64,
    _unb64,
    LocalGrantVerifier,
    _limits_payload,
    LocalGrant,
    LocalConsentAuditRecord,
)

from .profile import (
    ConsentDecision,
    ConsentLimits,
    LocalExecutionConsentRequest,
    LocalConsentReceipt,
    LocalConsentRequest,
    ProfileConfigurationError,
    SecurityProfile,
    inspect_consent_receipt,
)

_REASONS = frozenset({"operator_request", "plan_changed", "scope_changed", "security_concern"})


@dataclass(frozen=True)
class ResolvedLocalProposal:
    """Read-only proposal facts resolved by the authority, not supplied as claims."""

    proposal_id: str
    plan_digest: str
    revision: int
    workspace_id: str
    allowed_operations: tuple[str, ...]
    proposer_principal: str

    def __post_init__(self) -> None:
        _id(self.proposal_id, "proposal_id")
        _digest(self.plan_digest, "plan_digest")
        if type(self.revision) is not int or self.revision < 1:
            raise LocalAuthorizationError("proposal_revision_invalid")
        _id(self.workspace_id, "workspace_id")
        _id(self.proposer_principal, "proposal_proposer")
        if (type(self.allowed_operations) is not tuple or not self.allowed_operations
                or len(set(self.allowed_operations)) != len(self.allowed_operations)):
            raise LocalAuthorizationError("proposal_operations_invalid")
        for operation in self.allowed_operations:
            _id(operation, "proposal_operation")


class LocalProposalResolver(Protocol):
    def resolve(self, proposal_id: str) -> ResolvedLocalProposal | None: ...


class UnavailableLocalProposalResolver:
    def resolve(self, proposal_id: str) -> ResolvedLocalProposal | None:
        return None


@dataclass(frozen=True)
class AuthenticatedOperator:
    principal_id: str
    proof_digest: str
    authentication_method: str
    authenticated_at: str

    def __post_init__(self) -> None:
        _id(self.principal_id, "operator_principal")
        _digest(self.proof_digest, "operator_proof_digest")
        _id(self.authentication_method, "authentication_method")
        _time(self.authenticated_at, "authenticated_at")


class LocalOperatorAuthenticator(Protocol):
    """Trusted OS/UI authenticator; identity is returned by verifier, never by APL."""

    def authenticate(self, challenge_nonce: str, proof: object,
                     *, now: datetime) -> AuthenticatedOperator | None: ...


class UnavailableLocalOperatorAuthenticator:
    def authenticate(self, challenge_nonce: str, proof: object,
                     *, now: datetime) -> AuthenticatedOperator | None:
        return None


class LocalConsumerAuthenticator(Protocol):
    def authenticate_consumer(self, grant_digest: str, proof: object,
                              *, now: datetime) -> "AuthenticatedConsumer | None": ...


@dataclass(frozen=True)
class AuthenticatedConsumer:
    principal_id: str
    proof_digest: str
    authenticated_at: str

    def __post_init__(self) -> None:
        _id(self.principal_id, "consumer_principal")
        _digest(self.proof_digest, "consumer_proof_digest")
        _time(self.authenticated_at, "consumer_authenticated_at")


class UnavailableLocalConsumerAuthenticator:
    def authenticate_consumer(self, grant_digest: str, proof: object,
                              *, now: datetime) -> AuthenticatedConsumer | None:
        return None


class LocalGrantSigner(Protocol):
    key_id: str

    def sign_digest(self, grant_digest: str) -> bytes: ...


class Ed25519LocalGrantSigner:
    """Signer wrapper; callers provide a protected key, this class creates none."""

    def __init__(self, key_id: str, private_key):
        _id(key_id, "grant_key_id")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        if not isinstance(private_key, Ed25519PrivateKey):
            raise LocalAuthorizationError("ed25519_private_key_required")
        self.key_id = key_id
        self._private_key = private_key

    def sign_digest(self, grant_digest: str) -> bytes:
        _digest(grant_digest, "grant_digest")
        return self._private_key.sign(grant_digest.encode("ascii"))


class Ed25519LocalGrantVerifier:
    def __init__(self, public_keys: dict[str, object]):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        if (type(public_keys) is not dict or not public_keys
                or any(type(key_id) is not str or not isinstance(key, Ed25519PublicKey)
                       for key_id, key in public_keys.items())):
            raise LocalAuthorizationError("ed25519_public_key_registry_invalid")
        self._public_keys = dict(public_keys)

    def verify_digest(self, key_id: str, grant_digest: str, signature: bytes) -> bool:
        key = self._public_keys.get(key_id)
        if key is None or type(signature) is not bytes:
            return False
        try:
            key.verify(signature, grant_digest.encode("ascii"))
            return True
        except ValueError:
            return False
        except Exception:
            return False


def _build_signed_grant(*, signer: LocalGrantSigner, **fields) -> LocalGrant:
    schema_version = fields.pop("schema_version", 1)
    payload = {
        "schema_version": schema_version,
        "profile": SecurityProfile.LOCAL_SUPERVISED.value,
        **fields,
        "limits": _limits_payload(fields["limits"]),
        "key_id": signer.key_id,
    }
    grant_digest = _hash(payload)
    signature = signer.sign_digest(grant_digest)
    return LocalGrant(
        schema_version, SecurityProfile.LOCAL_SUPERVISED, fields["grant_id"], fields["operation_id"],
        fields["consent_decision_id"], fields["request_digest"], fields["proposal_id"],
        fields["plan_digest"], fields["revision"], fields["workspace_id"],
        fields["allowed_operations"], fields["limits"], fields["operator_identity"],
        fields["issued_at"], fields["expires_at"], signer.key_id, grant_digest, _b64(signature),
        fields.get("execution_binding"),
    )


@dataclass(frozen=True)
class LocalAuthorityPolicy:
    profile: SecurityProfile
    allowed_operator_principals: tuple[str, ...]
    allowed_consumer_principals: tuple[str, ...]
    non_operator_principals: tuple[str, ...]
    max_jobs: int
    max_iterations: int
    max_timeout_seconds: int
    max_grant_ttl_seconds: int
    max_estimated_budget: Decimal
    budget_currency: str

    def __post_init__(self) -> None:
        if self.profile is not SecurityProfile.LOCAL_SUPERVISED:
            raise LocalAuthorizationError("local_authority_requires_local_supervised_profile")
        if (type(self.allowed_operator_principals) is not tuple or not self.allowed_operator_principals
                or len(set(self.allowed_operator_principals)) != len(self.allowed_operator_principals)):
            raise LocalAuthorizationError("trusted_operator_allowlist_required")
        for principal in self.allowed_operator_principals:
            _id(principal, "trusted_operator")
        for field_name in ("allowed_consumer_principals", "non_operator_principals"):
            values = getattr(self, field_name)
            if type(values) is not tuple or len(set(values)) != len(values):
                raise LocalAuthorizationError(f"{field_name}_invalid")
            for principal in values:
                _id(principal, field_name)
        if not self.allowed_consumer_principals:
            raise LocalAuthorizationError("trusted_consumer_allowlist_required")
        for name in ("max_jobs", "max_iterations", "max_timeout_seconds", "max_grant_ttl_seconds"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise LocalAuthorizationError(f"authority_{name}_invalid")
        if (not isinstance(self.max_estimated_budget, Decimal)
                or not self.max_estimated_budget.is_finite() or self.max_estimated_budget < 0):
            raise LocalAuthorizationError("authority_budget_limit_invalid")
        if not re.fullmatch(r"[A-Z]{3}", self.budget_currency):
            raise LocalAuthorizationError("authority_budget_currency_invalid")

    def validate_limits(self, limits: ConsentLimits) -> None:
        if (limits.max_jobs > self.max_jobs or limits.max_iterations > self.max_iterations
                or limits.timeout_seconds > self.max_timeout_seconds
                or limits.budget_currency != self.budget_currency
                or limits.estimated_budget > self.max_estimated_budget):
            raise LocalAuthorizationError("consent_limits_exceed_authority_policy")


@dataclass(frozen=True)
class LocalRevocationRequest:
    grant_id: str
    grant_digest: str
    operator_identity: str
    reason_code: str
    explicit_action: bool
    expires_at: str

    def __post_init__(self) -> None:
        _id(self.grant_id, "grant_id")
        _digest(self.grant_digest, "grant_digest")
        _id(self.operator_identity, "operator_identity")
        if self.reason_code not in _REASONS:
            raise LocalAuthorizationError("revocation_reason_invalid")
        if type(self.explicit_action) is not bool or not self.explicit_action:
            raise LocalAuthorizationError("explicit_revocation_action_required")
        _time(self.expires_at, "revocation_expiry")

    @property
    def request_digest(self) -> str:
        return _hash({"profile": SecurityProfile.LOCAL_SUPERVISED.value,
                      "grant_id": self.grant_id, "grant_digest": self.grant_digest,
                      "operator_identity": self.operator_identity,
                      "reason_code": self.reason_code, "explicit_action": self.explicit_action,
                      "expires_at": self.expires_at})


@dataclass(frozen=True)
class LocalAuthorizationOutcome:
    decision: ConsentDecision
    consent_decision_id: str
    grant_id: str | None
    grant_digest: str | None
    state: str
    reason: str


@dataclass(frozen=True)
class LocalGrantStatus:
    grant_id: str
    grant_digest: str
    state: str
    expires_at: str
    dispatch_enabled: bool = False


def local_consent_challenge_digest(request: LocalConsentRequest,
                                   receipt: LocalConsentReceipt) -> str:
    """Canonical authenticator challenge binding proof to one explicit decision."""
    if not isinstance(request, LocalConsentRequest) or not isinstance(receipt, LocalConsentReceipt):
        raise LocalAuthorizationError("typed_consent_contracts_required")
    return _hash({
        "request_digest": request.request_digest,
        "decision": receipt.decision.value,
        "operator_identity": receipt.operator_identity,
        "explicit_action": receipt.explicit_action,
        "recorded_at": receipt.recorded_at,
        "expires_at": receipt.expires_at,
    })


class LocalAuthorizationAuthority:
    """Explicit LOCAL_SUPERVISED signer; never installed by default."""

    def __init__(self, *, store, policy: LocalAuthorityPolicy,
                 proposal_resolver: LocalProposalResolver,
                 operator_authenticator: LocalOperatorAuthenticator,
                 consumer_authenticator: LocalConsumerAuthenticator,
                 signer: LocalGrantSigner, verifier: LocalGrantVerifier,
                 authentication_challenge_store=None,
                 execution_binding_resolver=None):
        from agentflow.approval.persistence import BrokerStore
        if not isinstance(store, BrokerStore):
            raise LocalAuthorizationError("BrokerStore_required")
        for collaborator in (proposal_resolver, operator_authenticator,
                            consumer_authenticator, signer, verifier):
            if collaborator is None:
                raise LocalAuthorizationError("trusted_authority_collaborator_required")
        self.store = store
        self.policy = policy
        self.proposal_resolver = proposal_resolver
        self.operator_authenticator = operator_authenticator
        self.consumer_authenticator = consumer_authenticator
        self.signer = signer
        self.verifier = verifier
        self.authentication_challenge_store = authentication_challenge_store
        self.execution_binding_resolver = execution_binding_resolver

    def _now(self, now: datetime | None) -> datetime:
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise LocalAuthorizationError("aware_utc_clock_required")
        return instant.astimezone(timezone.utc)

    def _operator(self, challenge_nonce: str, proof: object, now: datetime,
                  claimed_identity: str | None) -> AuthenticatedOperator | None:
        if type(challenge_nonce) is not str or not 32 <= len(challenge_nonce) <= 256:
            raise LocalAuthorizationError("operator_challenge_invalid")
        result = self.operator_authenticator.authenticate(challenge_nonce, proof, now=now)
        if (not isinstance(result, AuthenticatedOperator)
                or result.principal_id not in self.policy.allowed_operator_principals
                or (claimed_identity is not None and result.principal_id != claimed_identity)
                or _time(result.authenticated_at, "operator_authenticated_at") > now):
            return None
        return result

    @staticmethod
    def _consent_challenge(request: LocalConsentRequest,
                           receipt: LocalConsentReceipt) -> str:
        """Bind authenticating proof to this exact decision and displayed request."""
        return local_consent_challenge_digest(request, receipt)

    def authorize(self, request: LocalConsentRequest, receipt: LocalConsentReceipt,
                  proof: object, *, authentication_challenge=None,
                  now: datetime | None = None) -> LocalAuthorizationOutcome:
        if not isinstance(request, LocalConsentRequest) or not isinstance(receipt, LocalConsentReceipt):
            raise LocalAuthorizationError("typed_consent_contracts_required")
        instant = self._now(now)
        if (self.authentication_challenge_store is None or authentication_challenge is None):
            raise LocalAuthorizationError("persistent_authentication_challenge_required")
        try:
            self.authentication_challenge_store.validate(
                authentication_challenge, request, receipt, now=instant)
        except Exception as exc:
            raise LocalAuthorizationError("authentication_challenge_invalid") from exc
        proposal = self.proposal_resolver.resolve(request.proposal_id)
        if not isinstance(proposal, ResolvedLocalProposal):
            raise LocalAuthorizationError("authoritative_proposal_unavailable")
        if (proposal.proposal_id != request.proposal_id or proposal.plan_digest != request.proposal_digest
                or proposal.plan_digest != getattr(request, "proposal_digest")
                or proposal.revision != request.revision or proposal.workspace_id != request.workspace_id
                or not set(request.allowed_operations).issubset(set(proposal.allowed_operations))):
            raise LocalAuthorizationError("consent_proposal_binding_mismatch")
        execution_binding = None
        if isinstance(request, LocalExecutionConsentRequest):
            if not callable(self.execution_binding_resolver):
                raise LocalAuthorizationError("authoritative_execution_binding_unavailable")
            try:
                current_binding = self.execution_binding_resolver(request.proposal_id)
            except Exception as exc:
                raise LocalAuthorizationError("authoritative_execution_binding_unavailable") from exc
            if current_binding != request.execution_binding:
                raise LocalAuthorizationError("execution_plan_binding_changed")
            execution_binding = request.execution_binding.to_payload()
        self.policy.validate_limits(request.limits)
        if request.operator_identity not in self.policy.allowed_operator_principals:
            raise LocalAuthorizationError("requested_operator_not_allowlisted")
        checked = inspect_consent_receipt(request, receipt, now=instant)
        if (not checked.request_digest_matches or not checked.operator_identity_matches
                or not checked.explicit_action_present or not checked.within_validity):
            raise LocalAuthorizationError("consent_receipt_binding_or_expiry_invalid")
        operator = self._operator(authentication_challenge.nonce, proof,
                                  instant, request.operator_identity)
        if operator is None:
            self.authentication_challenge_store.fail(
                authentication_challenge, reason="operator_authentication_failed", now=instant)
            return self._record_denial(request, receipt, instant, "operator_authentication_failed")
        if (operator.principal_id == proposal.proposer_principal
                or operator.principal_id in self.policy.non_operator_principals):
            return self._record_denial(request, receipt, instant, "self_approval_forbidden")
        operation_id = "local-consent-" + request.request_digest[7:39]
        decision_id = "local-decision-" + request.request_digest[7:39]
        try:
            self.authentication_challenge_store.consume(
                authentication_challenge, operator.proof_digest,
                operation_id=operation_id + ":auth:" + authentication_challenge.challenge_id,
                now=instant)
        except Exception as exc:
            raise LocalAuthorizationError("authentication_challenge_consumption_failed") from exc
        prior = self.store.get_local_consent_by_request(request.request_digest)
        if prior is not None:
            prior_record, prior_grant_id = prior
            if prior_record.decision is not receipt.decision:
                raise LocalAuthorizationError("consent_request_replay_conflicts_with_recorded_decision")
            if prior_grant_id is None:
                state = "rejected" if prior_record.decision is ConsentDecision.REJECT else "blocked"
                return LocalAuthorizationOutcome(prior_record.decision, prior_record.decision_id,
                                                 None, None, state, "idempotent_replay")
            prior_grant, prior_state = self.store.get_local_grant(prior_grant_id)
            return LocalAuthorizationOutcome(ConsentDecision.APPROVE, prior_record.decision_id,
                                             prior_grant.grant_id, prior_grant.grant_digest,
                                             prior_state, "idempotent_replay")
        if receipt.decision is not ConsentDecision.APPROVE:
            record = LocalConsentAuditRecord.create(
                decision_id=decision_id, operation_id=operation_id,
                request_digest=request.request_digest, decision=receipt.decision,
                operator_identity=operator.principal_id,
                operator_proof_digest=operator.proof_digest, explicit_action=True,
                occurred_at=_stamp(instant), reason_code="operator_rejected",
            )
            self.store._record_local_consent(record, None)
            return LocalAuthorizationOutcome(receipt.decision, decision_id, None, None,
                                             "rejected", "operator_rejected")
        if (_time(receipt.expires_at, "receipt_expiry") > _time(request.expires_at, "request_expiry")
                or _time(request.expires_at, "request_expiry") <= instant):
            raise LocalAuthorizationError("consent_expired")
        issued = instant
        expires = min(_time(request.expires_at, "request_expiry"),
                      issued + timedelta(seconds=self.policy.max_grant_ttl_seconds))
        grant_id = "local-grant-" + uuid4().hex
        fields = {
            "schema_version": 2 if execution_binding is not None else 1,
            "grant_id": grant_id, "operation_id": operation_id,
            "consent_decision_id": decision_id, "request_digest": request.request_digest,
            "proposal_id": proposal.proposal_id, "plan_digest": proposal.plan_digest,
            "revision": proposal.revision, "workspace_id": proposal.workspace_id,
            "allowed_operations": request.allowed_operations, "limits": request.limits,
            "operator_identity": operator.principal_id, "issued_at": _stamp(issued),
            "expires_at": _stamp(expires),
        }
        if execution_binding is not None:
            fields["execution_binding"] = execution_binding
        grant = _build_signed_grant(signer=self.signer, **fields)
        record = LocalConsentAuditRecord.create(
            decision_id=decision_id, operation_id=operation_id,
            request_digest=request.request_digest, decision=ConsentDecision.APPROVE,
            operator_identity=operator.principal_id,
            operator_proof_digest=operator.proof_digest, explicit_action=True,
            occurred_at=_stamp(instant), reason_code="operator_approved",
        )
        persisted_grant_id = self.store._record_local_consent(record, grant)
        if persisted_grant_id != grant.grant_id:
            persisted, persisted_state = self.store.get_local_grant(persisted_grant_id)
            return LocalAuthorizationOutcome(ConsentDecision.APPROVE, decision_id,
                                             persisted.grant_id, persisted.grant_digest,
                                             persisted_state, "idempotent_concurrent_replay")
        return LocalAuthorizationOutcome(ConsentDecision.APPROVE, decision_id,
                                         grant.grant_id, grant.grant_digest,
                                         "approved", "local_grant_issued_not_dispatched")

    def authorize_execution_plan(self, request: LocalExecutionConsentRequest,
                                 receipt: LocalConsentReceipt, proof: object, *,
                                 authentication_challenge=None,
                                 now: datetime | None = None) -> LocalAuthorizationOutcome:
        """Authenticate and persist one grant bound to the entire immutable plan."""
        if not isinstance(request, LocalExecutionConsentRequest):
            raise LocalAuthorizationError("execution_plan_consent_request_required")
        return self.authorize(request, receipt, proof,
                              authentication_challenge=authentication_challenge, now=now)

    def record_cancelled(self, request: LocalConsentRequest, *,
                         now: datetime | None = None) -> LocalAuthorizationOutcome:
        """Persist a UI cancellation as an unauthenticated, non-authorizing audit event."""
        if not isinstance(request, LocalConsentRequest):
            raise LocalAuthorizationError("typed_consent_request_required")
        instant = self._now(now)
        operation_id = "local-consent-" + request.request_digest[7:39]
        decision_id = "local-cancel-" + request.request_digest[7:39]
        prior = self.store.get_local_consent_by_request(request.request_digest)
        if prior is not None:
            record, grant_id = prior
            if record.decision is ConsentDecision.UNAVAILABLE and record.reason_code == "operator_cancelled":
                return LocalAuthorizationOutcome(ConsentDecision.UNAVAILABLE, record.decision_id,
                                                 grant_id, None, "blocked", "idempotent_cancel")
            raise LocalAuthorizationError("consent_cancellation_conflicts_with_recorded_decision")
        record = LocalConsentAuditRecord.create(
            decision_id=decision_id, operation_id=operation_id,
            request_digest=request.request_digest, decision=ConsentDecision.UNAVAILABLE,
            operator_identity=None, operator_proof_digest=None, explicit_action=False,
            occurred_at=_stamp(instant), reason_code="operator_cancelled",
        )
        self.store._record_local_consent(record, None)
        return LocalAuthorizationOutcome(ConsentDecision.UNAVAILABLE, decision_id,
                                         None, None, "blocked", "operator_cancelled")

    def _record_denial(self, request: LocalConsentRequest, receipt: LocalConsentReceipt,
                       now: datetime, reason: str) -> LocalAuthorizationOutcome:
        operation_id = "local-consent-" + request.request_digest[7:39]
        decision_id = "local-denial-" + request.request_digest[7:39]
        record = LocalConsentAuditRecord.create(
            decision_id=decision_id, operation_id=operation_id,
            request_digest=request.request_digest, decision=ConsentDecision.UNAVAILABLE,
            operator_identity=None, operator_proof_digest=None,
            explicit_action=receipt.explicit_action, occurred_at=_stamp(now), reason_code=reason,
        )
        self.store._record_local_consent(record, None)
        return LocalAuthorizationOutcome(ConsentDecision.UNAVAILABLE, decision_id,
                                         None, None, "blocked", reason)

    def _verified_grant(self, grant_id: str, *, now: datetime) -> tuple[LocalGrant, str]:
        grant, state = self.store.get_local_grant(grant_id)
        if not grant.verify_signature(self.verifier):
            raise LocalAuthorizationError("grant_signature_invalid")
        proposal = self.proposal_resolver.resolve(grant.proposal_id)
        if not isinstance(proposal, ResolvedLocalProposal):
            raise LocalAuthorizationError("authoritative_proposal_unavailable")
        if (proposal.plan_digest != grant.plan_digest
                or proposal.revision != grant.revision
                or proposal.workspace_id != grant.workspace_id
                or not set(grant.allowed_operations).issubset(set(proposal.allowed_operations))):
            if state == "approved":
                self.store._transition_local_grant(grant_id, "revoked", "plan_changed")
            raise LocalAuthorizationError("grant_proposal_obsolete_or_binding_changed")
        if _time(grant.expires_at, "grant_expiry") <= now:
            if state == "approved":
                self.store._transition_local_grant(grant_id, "expired", "grant_expiry")
            raise LocalAuthorizationError("grant_expired")
        if state != "approved":
            raise LocalAuthorizationError(f"grant_{state}")
        return grant, state

    def validate_grant(self, grant_id: str, *, expected_request_digest: str,
                       now: datetime | None = None) -> LocalGrantStatus:
        instant = self._now(now)
        _digest(expected_request_digest, "expected_request_digest")
        grant, _state = self._verified_grant(grant_id, now=instant)
        if grant.request_digest != expected_request_digest:
            raise LocalAuthorizationError("grant_request_binding_mismatch")
        return LocalGrantStatus(grant.grant_id, grant.grant_digest, "approved", grant.expires_at)

    def consume(self, grant_id: str, *, proof: object, now: datetime | None = None) -> LocalGrantStatus:
        instant = self._now(now)
        grant, _state = self._verified_grant(grant_id, now=instant)
        consumer = self.consumer_authenticator.authenticate_consumer(
            grant.grant_digest, proof, now=instant)
        if (not isinstance(consumer, AuthenticatedConsumer)
                or consumer.principal_id not in self.policy.allowed_consumer_principals
                or _time(consumer.authenticated_at, "consumer_authenticated_at") > instant):
            raise LocalAuthorizationError("consumer_authentication_failed")
        self.store._transition_local_grant(grant_id, "consumed", "consumer_consumed",
                                           actor_proof_digest=consumer.proof_digest)
        return LocalGrantStatus(grant.grant_id, grant.grant_digest, "consumed", grant.expires_at)

    def revoke(self, request: LocalRevocationRequest, proof: object,
               *, now: datetime | None = None) -> LocalGrantStatus:
        if not isinstance(request, LocalRevocationRequest):
            raise LocalAuthorizationError("typed_revocation_request_required")
        instant = self._now(now)
        grant, state = self.store.get_local_grant(request.grant_id)
        if grant.grant_digest != request.grant_digest:
            raise LocalAuthorizationError("revocation_grant_digest_mismatch")
        if not grant.verify_signature(self.verifier):
            raise LocalAuthorizationError("grant_signature_invalid")
        if state == "approved" and _time(grant.expires_at, "grant_expiry") <= instant:
            self.store._transition_local_grant(request.grant_id, "expired", "grant_expiry")
            raise LocalAuthorizationError("grant_expired")
        checked_operator = self._operator(request.request_digest, proof, instant,
                                          request.operator_identity)
        if checked_operator is None:
            raise LocalAuthorizationError("revocation_operator_authentication_failed")
        if _time(request.expires_at, "revocation_expiry") <= instant:
            raise LocalAuthorizationError("revocation_request_expired")
        if state == "approved":
            self.store._transition_local_grant(
                request.grant_id, "revoked", request.reason_code,
                operation_id="local-revoke-" + request.request_digest[7:39],
                actor_proof_digest=checked_operator.proof_digest,
            )
        elif state == "revoked":
            history = self.store.local_grant_history(grant.grant_id)
            transition = next((event["payload"] for event in history
                               if event["event_type"] == "local_grant.transitioned"
                               and event["payload"].get("to") == "revoked"), None)
            if (transition is None
                    or transition.get("transition_id") != "local-revoke-" + request.request_digest[7:39]
                    or transition.get("reason_code") != request.reason_code
                    or transition.get("actor_proof_digest") != checked_operator.proof_digest):
                raise LocalAuthorizationError("grant_revocation_replay_conflict")
        else:
            raise LocalAuthorizationError(f"grant_{state}")
        return LocalGrantStatus(grant.grant_id, grant.grant_digest, "revoked", grant.expires_at)

    def expire(self, *, now: datetime | None = None) -> int:
        return self.store.expire_local_grants(_stamp(self._now(now)))


class UnavailableLocalAuthorizationAuthority:
    """Default authority: no grants, consent, or dispatch are available."""

    def status(self) -> dict[str, object]:
        return {"available": False, "grant_issuance_enabled": False,
                "dispatch_enabled": False, "reason": "local_authority_not_homologated"}

    def authorize(self, *args, **kwargs):
        raise LocalAuthorizationError("local_authority_unavailable")


LocalAuthorizationPolicy = LocalAuthorityPolicy
