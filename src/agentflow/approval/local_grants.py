"""Pure local grant/audit contracts and their exact canonical serialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import base64
import hashlib
import json
import re
from typing import Protocol

from .consent_contracts import ConsentDecision, ConsentLimits, SecurityProfile


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


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


class LocalGrantVerifier(Protocol):
    def verify_digest(self, key_id: str, grant_digest: str, signature: bytes) -> bool: ...


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
    execution_binding: dict | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise LocalAuthorizationError("local_grant_schema_unsupported")
        if ((self.schema_version == 1 and self.execution_binding is not None)
                or (self.schema_version == 2
                    and (type(self.execution_binding) is not dict
                         or not self.execution_binding))):
            raise LocalAuthorizationError("local_grant_execution_binding_invalid")
        if self.schema_version == 2:
            try:
                from .consent_contracts import ExecutionPlanConsentBinding
                ExecutionPlanConsentBinding.from_payload(self.execution_binding)
            except Exception as exc:
                raise LocalAuthorizationError("local_grant_execution_binding_invalid") from exc
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
        payload = {
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
        if self.schema_version == 2:
            payload["execution_binding"] = self.execution_binding
        return payload

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
        if type(value) is dict and value.get("schema_version") == 2:
            expected.add("execution_binding")
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
                value.get("execution_binding"),
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


# Public validation entrypoints; legacy security helpers reexport these functions.
validate_grant_id = _id
validate_grant_digest = _digest
