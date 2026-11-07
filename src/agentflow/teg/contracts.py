"""Versioned contracts for the dormant, local Trusted Execution Gateway core.

These contracts do not authenticate a Broker response or execute an operation. The
eventual authority transport must provide that trust; a digest alone is not a signature.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, ClassVar

from agentflow.approval.protocol import (
    AcceptedSnapshot, OperationIntent,
    ProtocolError, canonical_json, digest_json, parse_canonical_json,
    _digest as _check_digest, _identifier, _timestamp,
)

TEG_PROTOCOL_VERSION = 1
MAX_SUBMISSION_BYTES = 512 * 1024
MAX_PREPARATION_BYTES = 256 * 1024
_SCOPE = re.compile(r"^[a-z][a-z0-9]*(?::[a-z0-9_-]+)+$")
_MONEY = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
_DIMENSION_STATUS = {"applied_verified", "estimated", "unavailable"}
_BUDGET_FIELDS = {"status", "currency", "cost_estimate", "token_limit",
                  "time_limit_seconds", "constraints"}
_CONTEXT_FIELDS = {"context_version", "operation_id", "project_id", "task_id", "attempt_id",
                   "specification_revision_id", "specification_digest", "plan_revision_id",
                   "plan_digest", "checkpoint_digest", "scope", "impact"}
_IMPACT_FIELDS = {"kind", "affected_requirement_ids", "affected_task_ids", "impact_digest"}


class GatewayContractError(ValueError):
    """Invalid or ambiguous TEG contract."""


class GatewayExecutionError(RuntimeError):
    """Gateway operation is blocked, unavailable, or in an unreconciled state."""


def _canonical(value: Any) -> str:
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise GatewayContractError("gateway_json_invalid") from exc
    if len(result.encode("utf-8")) > MAX_SUBMISSION_BYTES:
        raise GatewayContractError("gateway_contract_exceeds_size_limit")
    return result


def _digest(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_json(raw: str, *, limit: int = MAX_SUBMISSION_BYTES) -> Any:
    if type(raw) is not str or len(raw.encode("utf-8")) > limit:
        raise GatewayContractError("gateway_json_size_invalid")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GatewayContractError("gateway_json_duplicate_field")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               GatewayContractError("gateway_json_invalid")))
    except GatewayContractError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise GatewayContractError("gateway_json_invalid") from exc
    if _canonical(value) != raw:
        raise GatewayContractError("gateway_json_not_canonical")
    return value


def _sorted_identifiers(values: tuple[str, ...], label: str, *, nonempty: bool = True) -> None:
    if (type(values) is not tuple or (nonempty and not values)
            or any(type(value) is not str for value in values)
            or len(set(values)) != len(values) or values != tuple(sorted(values))):
        raise GatewayContractError(f"{label}_invalid_or_not_canonical")
    for value in values:
        _identifier(value, label)


@dataclass(frozen=True)
class GatewayIdentity:
    protocol_version: int
    gateway_id: str
    gateway_version: str
    created_at: str
    identity_digest: str

    DIGEST_FIELD: ClassVar[str] = "identity_digest"

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("gateway_protocol_version_unsupported")
        for field in ("gateway_id", "gateway_version"):
            _identifier(getattr(self, field), field)
        _timestamp(self.created_at, "created_at")
        _check_digest(self.identity_digest, "identity_digest")
        if _digest({k: v for k, v in self.to_dict().items() if k != self.DIGEST_FIELD}) != self.identity_digest:
            raise GatewayContractError("gateway_identity_digest_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(cls, gateway_id: str, gateway_version: str, created_at: str) -> "GatewayIdentity":
        fields = {"protocol_version": TEG_PROTOCOL_VERSION, "gateway_id": gateway_id,
                  "gateway_version": gateway_version, "created_at": created_at}
        return cls(**fields, identity_digest=_digest(fields))


@dataclass(frozen=True)
class GatewaySubmission:
    """Exact immutable operation request plus four Broker-provided snapshots."""

    protocol_version: int
    request_id: str
    intent_json: str
    snapshots_json: str
    operation_id: str
    intent_digest: str
    submission_digest: str

    DIGEST_FIELD: ClassVar[str] = "submission_digest"

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("gateway_protocol_version_unsupported")
        _identifier(self.request_id, "request_id")
        intent = OperationIntent.from_json(self.intent_json)
        snapshots = self.snapshots
        if (intent.operation_id != self.operation_id or intent.intent_digest != self.intent_digest
                or type(snapshots) is not tuple
                or tuple(item.kind for item in snapshots) !=
                ("specification", "plan", "checkpoint", "payload")):
            raise GatewayContractError("submission_identity_or_snapshot_set_invalid")
        _check_digest(self.intent_digest, "intent_digest")
        _check_digest(self.submission_digest, "submission_digest")
        if _digest(self._digest_payload()) != self.submission_digest:
            raise GatewayContractError("submission_digest_mismatch")

    @property
    def intent(self) -> OperationIntent:
        return OperationIntent.from_json(self.intent_json)

    @property
    def snapshots(self) -> tuple[AcceptedSnapshot, ...]:
        records = _strict_json(self.snapshots_json)
        if type(records) is not list:
            raise GatewayContractError("snapshots_must_be_array")
        try:
            return tuple(AcceptedSnapshot.from_dict(item) for item in records)
        except (TypeError, ProtocolError) as exc:
            raise GatewayContractError("accepted_snapshot_contract_invalid") from exc

    def _digest_payload(self) -> dict[str, Any]:
        return {"protocol_version": self.protocol_version, "request_id": self.request_id,
                "intent_json": self.intent_json, "snapshots_json": self.snapshots_json,
                "operation_id": self.operation_id, "intent_digest": self.intent_digest}

    def to_dict(self) -> dict[str, Any]:
        return {**self._digest_payload(), "submission_digest": self.submission_digest}

    def to_json(self) -> str:
        return _canonical(self.to_dict())

    @classmethod
    def from_json(cls, raw: str) -> "GatewaySubmission":
        value = _strict_json(raw)
        expected = {"protocol_version", "request_id", "intent_json", "snapshots_json",
                    "operation_id", "intent_digest", "submission_digest"}
        if type(value) is not dict or set(value) != expected:
            raise GatewayContractError("submission_fields_mismatch")
        return cls(**value)

    @classmethod
    def create(cls, request_id: str, intent: OperationIntent,
               snapshots: tuple[AcceptedSnapshot, ...]) -> "GatewaySubmission":
        snapshots_json = _canonical([item.to_dict() for item in snapshots])
        fields = {"protocol_version": TEG_PROTOCOL_VERSION, "request_id": request_id,
                  "intent_json": intent.to_json(), "snapshots_json": snapshots_json,
                  "operation_id": intent.operation_id, "intent_digest": intent.intent_digest}
        return cls(**fields, submission_digest=_digest(fields))


@dataclass(frozen=True)
class GatewayHandlerDescriptor:
    protocol_version: int
    handler_id: str
    handler_version: str
    action_codes: tuple[str, ...]
    target_ids: tuple[str, ...]
    provider_ids: tuple[str, ...]
    model_ids: tuple[str, ...]
    allowed_scopes: tuple[str, ...]
    required_scopes: tuple[str, ...]
    allowed_budget_statuses: tuple[str, ...]
    required_budget_fields: tuple[str, ...]
    currency: str | None
    max_cost_estimate: str | None
    max_tokens: int | None
    max_time_seconds: int | None
    allowed_parameter_keys: tuple[str, ...]
    required_parameter_keys: tuple[str, ...]
    max_payload_bytes: int
    provider_idempotency: str
    descriptor_digest: str

    DIGEST_FIELD: ClassVar[str] = "descriptor_digest"

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("handler_protocol_version_unsupported")
        for field in ("handler_id", "handler_version"):
            _identifier(getattr(self, field), field)
        for field in ("action_codes", "target_ids", "provider_ids", "model_ids",
                      "allowed_scopes", "allowed_budget_statuses", "allowed_parameter_keys"):
            _sorted_identifiers(getattr(self, field), field, nonempty=field not in {"provider_ids", "model_ids"})
        _sorted_identifiers(self.required_scopes, "required_scopes")
        _sorted_identifiers(self.required_parameter_keys, "required_parameter_keys", nonempty=False)
        if not set(self.required_scopes) <= set(self.allowed_scopes):
            raise GatewayContractError("required_scopes_not_allowed")
        if not set(self.required_parameter_keys) <= set(self.allowed_parameter_keys):
            raise GatewayContractError("required_parameters_not_allowed")
        if not set(self.action_codes) <= {"provider_call", "provider_model_change", "model_escalation"}:
            raise GatewayContractError("handler_action_unknown")
        if (not self.allowed_budget_statuses
                or not set(self.allowed_budget_statuses) <= _DIMENSION_STATUS
                or type(self.required_budget_fields) is not tuple
                or any(type(item) is not str for item in self.required_budget_fields)
                or self.required_budget_fields != tuple(sorted(set(self.required_budget_fields)))
                or not set(self.required_budget_fields) <= _BUDGET_FIELDS):
            raise GatewayContractError("handler_budget_policy_invalid")
        if self.currency is not None and (type(self.currency) is not str or not re.fullmatch(r"[A-Z]{3}", self.currency)):
            raise GatewayContractError("handler_currency_invalid")
        if self.max_cost_estimate is not None:
            _money(self.max_cost_estimate, "max_cost_estimate")
            if self.currency is None:
                raise GatewayContractError("max_cost_requires_currency")
        for name in ("max_tokens", "max_time_seconds"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise GatewayContractError(f"{name}_invalid")
        if type(self.max_payload_bytes) is not int or not 1 <= self.max_payload_bytes <= MAX_SUBMISSION_BYTES:
            raise GatewayContractError("max_payload_bytes_invalid")
        if self.provider_idempotency not in {"applied_verified", "estimated", "unavailable"}:
            raise GatewayContractError("provider_idempotency_status_invalid")
        _check_digest(self.descriptor_digest, "descriptor_digest")
        if _digest({k: v for k, v in self.to_dict().items() if k != self.DIGEST_FIELD}) != self.descriptor_digest:
            raise GatewayContractError("handler_descriptor_digest_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _money(value: Any, label: str) -> Decimal:
    if type(value) is not str or not _MONEY.fullmatch(value):
        raise GatewayContractError(f"{label}_invalid")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise GatewayContractError(f"{label}_invalid") from exc


@dataclass(frozen=True)
class GatewayPreparation:
    protocol_version: int
    operation_id: str
    intent_digest: str
    handler_id: str
    handler_version: str
    descriptor_digest: str
    target_id: str
    payload_digest: str
    parameters_digest: str
    budget_digest: str
    scope_digest: str
    expected_effect_code: str
    preparation_digest: str

    DIGEST_FIELD: ClassVar[str] = "preparation_digest"

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("preparation_version_unsupported")
        for name in ("operation_id", "handler_id", "handler_version", "target_id", "expected_effect_code"):
            _identifier(getattr(self, name), name)
        for name in ("intent_digest", "descriptor_digest", "payload_digest", "parameters_digest",
                     "budget_digest", "scope_digest", "preparation_digest"):
            _check_digest(getattr(self, name), name)
        if _digest({k: v for k, v in self.to_dict().items() if k != self.DIGEST_FIELD}) != self.preparation_digest:
            raise GatewayContractError("preparation_digest_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GatewayEffectResult:
    """Minimal result record; never stores provider output or secrets."""

    protocol_version: int
    operation_id: str
    status: str
    result_code: str
    effect_key: str | None
    result_digest: str | None

    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"succeeded", "failed_before_effect", "failed_verified", "unknown"}
    )

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("effect_result_version_unsupported")
        _identifier(self.operation_id, "operation_id")
        _identifier(self.result_code, "result_code")
        if self.status not in self.STATUSES:
            raise GatewayContractError("effect_result_status_invalid")
        if self.effect_key is not None:
            _identifier(self.effect_key, "effect_key")
        if self.result_digest is not None:
            _check_digest(self.result_digest, "result_digest")
        if self.status == "succeeded" and self.result_digest is None:
            raise GatewayContractError("successful_effect_requires_result_digest")
        if self.status in {"failed_before_effect", "failed_verified"} and self.result_digest is None:
            raise GatewayContractError("verified_failure_requires_result_digest")


@dataclass(frozen=True)
class GatewayAuthorityStatus:
    protocol_version: int
    available: bool
    reason_code: str
    verified_capabilities: tuple[str, ...]

    REQUIRED_CAPABILITIES: ClassVar[frozenset[str]] = frozenset({
        "accepted_snapshot_lookup", "atomic_confirmation", "atomic_reservation",
        "current_expiry_revocation", "exact_intent_digest", "gateway_audience",
        "idempotent_confirmation", "idempotent_reservation", "reconciliation_query",
    })

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("authority_protocol_version_unsupported")
        if type(self.available) is not bool:
            raise GatewayContractError("authority_availability_invalid")
        _identifier(self.reason_code, "reason_code")
        _sorted_identifiers(self.verified_capabilities, "authority_capabilities", nonempty=False)
        if self.available and not self.REQUIRED_CAPABILITIES <= set(self.verified_capabilities):
            raise GatewayContractError("authority_required_capability_unverified")


@dataclass(frozen=True)
class AuthorizationConsumption:
    """Proof returned by a trusted authority's atomic consume operation.

    Authentication of this record belongs to the external authority transport. The
    canonical digest detects mismatch but does not authenticate a response by itself.
    """

    protocol_version: int
    consumption_id: str
    authorization_digest: str
    request_id: str
    operation_id: str
    intent_digest: str
    audience: str
    target_id: str
    payload_digest: str
    status: str
    reason_code: str
    consumed_at: str | None
    expires_at: str
    consumption_digest: str

    DIGEST_FIELD: ClassVar[str] = "consumption_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"consumed", "rejected", "expired", "revoked", "already_consumed", "unknown"}
    )

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("authorization_response_version_unsupported")
        for name in ("consumption_id", "request_id", "operation_id", "audience", "target_id", "reason_code"):
            _identifier(getattr(self, name), name)
        for name in ("authorization_digest", "intent_digest", "payload_digest", "consumption_digest"):
            _check_digest(getattr(self, name), name)
        if self.status not in self.STATUSES:
            raise GatewayContractError("authorization_response_status_invalid")
        _timestamp(self.expires_at, "authorization_expiry")
        if self.consumed_at is not None:
            _timestamp(self.consumed_at, "authorization_consumed_at")
        if self.status == "consumed" and self.consumed_at is None:
            raise GatewayContractError("consumed_authorization_requires_timestamp")
        if self.status != "consumed" and self.consumed_at is not None:
            raise GatewayContractError("non_consumed_authorization_has_timestamp")
        if _digest({k: v for k, v in self.to_dict().items() if k != self.DIGEST_FIELD}) != self.consumption_digest:
            raise GatewayContractError("authorization_response_digest_mismatch")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_contract(contract_type: type, **values: Any):
    """Build local protocol fixtures/contracts by deriving the self digest."""
    field = contract_type.DIGEST_FIELD
    values[field] = "sha256:" + "0" * 64
    digest_fields = dict(values)
    digest_fields.pop(field)
    return contract_type(**{**values, field: _digest(digest_fields)})
