"""LOCAL_SUPERVISED consent and grant contracts.

The classes here are not wired to the APL, executor, CLI, or dispatch. An
authority instance requires explicit trusted collaborators; defaults are
unavailable. In-process tests do not prove OS/process isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import base64
import hashlib
import json
import re
from typing import Protocol
from uuid import uuid4

from .profile import (
    ConsentDecision,
    ConsentLimits,
    LocalConsentReceipt,
    LocalConsentRequest,
    ProfileConfigurationError,
    SecurityProfile,
    inspect_consent_receipt,
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_REASONS = frozenset({"operator_request", "plan_changed", "scope_changed", "security_concern"})


class LocalAuthorizationError(RuntimeError):
    """Local authorization was unavailable, invalid, expired, revoked or replayed."""


def _id(value: str, name: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise LocalAuthorizationError(f"{name}_invalid")


def _digest(value: str, name: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise LocalAuthorizationError(f"{name}_invalid")


def _time(value: str, name: str) -> datetime:
    if type(value) is not str:
        raise LocalAuthorizationError(f"{name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LocalAuthorizationError(f"{name}_invalid") from exc
    if parsed.tzinfo is None:
        raise LocalAuthorizationError(f"{name}_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise LocalAuthorizationError("aware_utc_clock_required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{86}", value):
        raise LocalAuthorizationError("grant_signature_encoding_invalid")
    try:
        return base64.b64decode(value + "==", altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise LocalAuthorizationError("grant_signature_encoding_invalid") from exc


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


class LocalGrantVerifier(Protocol):
    def verify_digest(self, key_id: str, grant_digest: str, signature: bytes) -> bool: ...


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


def _limits_payload(limits: ConsentLimits) -> dict:
    return {
        "max_jobs": limits.max_jobs,
        "max_iterations": limits.max_iterations,
        "timeout_seconds": limits.timeout_seconds,
        "estimated_budget": str(limits.estimated_budget),
        "budget_currency": limits.budget_currency,
    }


@dataclass(frozen=True)
class LocalGrant:
    schema_version: int
    profile: SecurityProfile
    grant_id: str
    operation_id: str
    consent_decision_id: str
    request_digest: str
    proposal_id: str
    plan_digest: str
    revision: int
    workspace_id: str
    allowed_operations: tuple[str, ...]
    limits: ConsentLimits
    operator_identity: str
    issued_at: str
    expires_at: str
    key_id: str
    grant_digest: str
    signature_b64: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise LocalAuthorizationError("local_grant_schema_unsupported")
        if self.profile is not SecurityProfile.LOCAL_SUPERVISED:
            raise LocalAuthorizationError("local_grant_profile_invalid")
        for name in ("grant_id", "operation_id", "consent_decision_id", "proposal_id",
                     "workspace_id", "operator_identity", "key_id"):
            _id(getattr(self, name), name)
        for name in ("request_digest", "plan_digest", "grant_digest"):
            _digest(getattr(self, name), name)
        if type(self.revision) is not int or self.revision < 1:
            raise LocalAuthorizationError("grant_revision_invalid")
        if (type(self.allowed_operations) is not tuple or not self.allowed_operations
                or len(set(self.allowed_operations)) != len(self.allowed_operations)):
            raise LocalAuthorizationError("grant_operations_invalid")
        for operation in self.allowed_operations:
            _id(operation, "grant_operation")
        if not isinstance(self.limits, ConsentLimits):
            raise LocalAuthorizationError("grant_limits_invalid")
        issued, expiry = _time(self.issued_at, "grant_issued_at"), _time(self.expires_at, "grant_expiry")
        if expiry <= issued:
            raise LocalAuthorizationError("grant_expiry_invalid")
        signature = _unb64(self.signature_b64)
        if len(signature) != 64 or self.digest_payload_hash() != self.grant_digest:
            raise LocalAuthorizationError("grant_digest_or_signature_invalid")

    def signing_payload(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "profile": self.profile.value,
            "grant_id": self.grant_id,
            "operation_id": self.operation_id,
            "consent_decision_id": self.consent_decision_id,
            "request_digest": self.request_digest,
            "proposal_id": self.proposal_id,
            "plan_digest": self.plan_digest,
            "revision": self.revision,
            "workspace_id": self.workspace_id,
            "allowed_operations": list(self.allowed_operations),
            "limits": _limits_payload(self.limits),
            "operator_identity": self.operator_identity,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
        }

    def digest_payload_hash(self) -> str:
        return _hash(self.signing_payload())

    def verify_signature(self, verifier: LocalGrantVerifier) -> bool:
        return verifier.verify_digest(self.key_id, self.grant_digest, _unb64(self.signature_b64))

    def to_dict(self) -> dict:
        return {**self.signing_payload(), "grant_digest": self.grant_digest,
                "signature_b64": self.signature_b64}

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, value: dict) -> "LocalGrant":
        expected = {"schema_version", "profile", "grant_id", "operation_id", "consent_decision_id",
                    "request_digest", "proposal_id", "plan_digest", "revision", "workspace_id",
                    "allowed_operations", "limits", "operator_identity", "issued_at", "expires_at",
                    "key_id", "grant_digest", "signature_b64"}
        if type(value) is not dict or set(value) != expected:
            raise LocalAuthorizationError("local_grant_fields_mismatch")
        limits = value["limits"]
        if type(limits) is not dict or set(limits) != {
            "max_jobs", "max_iterations", "timeout_seconds", "estimated_budget", "budget_currency"
        }:
            raise LocalAuthorizationError("grant_limits_fields_mismatch")
        try:
            return cls(
                value["schema_version"], SecurityProfile(value["profile"]), value["grant_id"],
                value["operation_id"], value["consent_decision_id"], value["request_digest"],
                value["proposal_id"], value["plan_digest"], value["revision"], value["workspace_id"],
                tuple(value["allowed_operations"]),
                ConsentLimits(limits["max_jobs"], limits["max_iterations"],
                              limits["timeout_seconds"], Decimal(limits["estimated_budget"]),
                              limits["budget_currency"]),
                value["operator_identity"], value["issued_at"], value["expires_at"],
                value["key_id"], value["grant_digest"], value["signature_b64"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, LocalAuthorizationError):
                raise
            raise LocalAuthorizationError("local_grant_invalid") from exc

    @classmethod
    def from_json(cls, raw: str) -> "LocalGrant":
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LocalAuthorizationError("local_grant_json_invalid") from exc
        return cls.from_dict(value)


def _build_signed_grant(*, signer: LocalGrantSigner, **fields) -> LocalGrant:
    payload = {
        "schema_version": 1,
        "profile": SecurityProfile.LOCAL_SUPERVISED.value,
        **fields,
        "limits": _limits_payload(fields["limits"]),
        "key_id": signer.key_id,
    }
    grant_digest = _hash(payload)
    signature = signer.sign_digest(grant_digest)
    return LocalGrant(
        1, SecurityProfile.LOCAL_SUPERVISED, fields["grant_id"], fields["operation_id"],
        fields["consent_decision_id"], fields["request_digest"], fields["proposal_id"],
        fields["plan_digest"], fields["revision"], fields["workspace_id"],
        fields["allowed_operations"], fields["limits"], fields["operator_identity"],
        fields["issued_at"], fields["expires_at"], signer.key_id, grant_digest, _b64(signature),
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
class LocalConsentAuditRecord:
    decision_id: str
    operation_id: str
    request_digest: str
    decision: ConsentDecision
    operator_identity: str | None
    operator_proof_digest: str | None
    explicit_action: bool
    occurred_at: str
    reason_code: str
    record_digest: str

    def payload(self) -> dict:
        return {
            "decision_id": self.decision_id, "operation_id": self.operation_id,
            "request_digest": self.request_digest, "decision": self.decision.value,
            "operator_identity": self.operator_identity,
            "operator_proof_digest": self.operator_proof_digest,
            "explicit_action": self.explicit_action, "occurred_at": self.occurred_at,
            "reason_code": self.reason_code,
        }

    def __post_init__(self) -> None:
        for name in ("decision_id", "operation_id"):
            _id(getattr(self, name), name)
        _digest(self.request_digest, "consent_request_digest")
        if not isinstance(self.decision, ConsentDecision):
            raise LocalAuthorizationError("consent_decision_invalid")
        if self.operator_identity is not None:
            _id(self.operator_identity, "operator_identity")
        if self.operator_proof_digest is not None:
            _digest(self.operator_proof_digest, "operator_proof_digest")
        if type(self.explicit_action) is not bool:
            raise LocalAuthorizationError("explicit_action_invalid")
        _time(self.occurred_at, "consent_occurred_at")
        _id(self.reason_code, "consent_reason_code")
        if _hash(self.payload()) != self.record_digest:
            raise LocalAuthorizationError("consent_audit_digest_mismatch")

    def to_json(self) -> str:
        return _canonical({**self.payload(), "record_digest": self.record_digest}).decode("utf-8")

    @classmethod
    def create(cls, *, decision_id: str, operation_id: str, request_digest: str,
               decision: ConsentDecision, operator_identity: str | None,
               operator_proof_digest: str | None, explicit_action: bool,
               occurred_at: str, reason_code: str) -> "LocalConsentAuditRecord":
        payload = {
            "decision_id": decision_id, "operation_id": operation_id,
            "request_digest": request_digest, "decision": decision.value,
            "operator_identity": operator_identity,
            "operator_proof_digest": operator_proof_digest,
            "explicit_action": explicit_action, "occurred_at": occurred_at,
            "reason_code": reason_code,
        }
        return cls(decision_id, operation_id, request_digest, decision, operator_identity,
                   operator_proof_digest, explicit_action, occurred_at, reason_code, _hash(payload))

    @classmethod
    def from_json(cls, raw: str) -> "LocalConsentAuditRecord":
        value = json.loads(raw)
        expected = {"decision_id", "operation_id", "request_digest", "decision",
                    "operator_identity", "operator_proof_digest", "explicit_action",
                    "occurred_at", "reason_code", "record_digest"}
        if type(value) is not dict or set(value) != expected:
            raise LocalAuthorizationError("consent_audit_fields_mismatch")
        try:
            return cls(value["decision_id"], value["operation_id"], value["request_digest"],
                       ConsentDecision(value["decision"]), value["operator_identity"],
                       value["operator_proof_digest"], value["explicit_action"],
                       value["occurred_at"], value["reason_code"], value["record_digest"])
        except (ValueError, TypeError) as exc:
            raise LocalAuthorizationError("consent_audit_invalid") from exc


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
    """Explicit local authority object; never installed/wired by the app by default."""

    def __init__(self, *, store, policy: LocalAuthorityPolicy,
                 proposal_resolver: LocalProposalResolver,
                 operator_authenticator: LocalOperatorAuthenticator,
                 consumer_authenticator: LocalConsumerAuthenticator,
                 signer: LocalGrantSigner, verifier: LocalGrantVerifier,
                 authentication_challenge_store=None):
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
            "grant_id": grant_id, "operation_id": operation_id,
            "consent_decision_id": decision_id, "request_digest": request.request_digest,
            "proposal_id": proposal.proposal_id, "plan_digest": proposal.plan_digest,
            "revision": proposal.revision, "workspace_id": proposal.workspace_id,
            "allowed_operations": request.allowed_operations, "limits": request.limits,
            "operator_identity": operator.principal_id, "issued_at": _stamp(issued),
            "expires_at": _stamp(expires),
        }
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
