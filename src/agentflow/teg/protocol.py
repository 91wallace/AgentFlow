"""Versioned, non-bearer protocol contracts for Broker/TEG development flows.

These records authenticate no peer and issue no authority by themselves. A real
Broker transport and its trust boundary remain unavailable; only test fixtures may
implement the authority interface until a later security approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, ClassVar

from agentflow.approval.protocol import (
    canonical_json, digest_json, parse_canonical_json, _digest as _check_digest,
    _identifier, _timestamp,
)

from .contracts import GatewayContractError, GatewaySubmission, TEG_PROTOCOL_VERSION


def _self_digest(instance: Any, field: str) -> str:
    values = asdict(instance)
    values.pop(field)
    return digest_json(values)


def _check_self(instance: Any, field: str) -> None:
    actual = getattr(instance, field)
    _check_digest(actual, field)
    if _self_digest(instance, field) != actual:
        raise GatewayContractError(f"{field}_mismatch")


def build_message(contract_type: type, **values):
    """Construct a test/protocol message by deriving its canonical digest."""
    field = contract_type.DIGEST_FIELD
    encoded = {key: (value.to_dict() if isinstance(value, _Wire) else value)
               for key, value in values.items()}
    encoded.pop(field, None)
    return contract_type(**values, **{field: digest_json(encoded)})


class _Wire:
    DIGEST_FIELD: ClassVar[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, raw: str):
        value = parse_canonical_json(raw)
        if type(value) is not dict:
            raise GatewayContractError("protocol_message_must_be_object")
        if set(value) != {field.name for field in fields(cls)}:
            raise GatewayContractError("protocol_message_fields_mismatch")
        if "binding" in value and type(value["binding"]) is dict:
            value["binding"] = ExecutionBinding(**value["binding"])
        if "reservation" in value and type(value["reservation"]) is dict:
            value["reservation"] = ConsumptionReservation(**value["reservation"])
        try:
            return cls(**value)
        except TypeError as exc:
            raise GatewayContractError("protocol_message_fields_invalid") from exc


@dataclass(frozen=True)
class ExecutionBinding(_Wire):
    """Digest-bound identity of the exact operation authorized for one Gateway."""

    protocol_version: int
    operation_id: str
    gateway_id: str
    gateway_identity_digest: str
    intent_digest: str
    snapshot_set_digest: str
    target_id: str
    provider_id: str
    model_id: str
    scope_digest: str
    budget_digest: str
    binding_digest: str

    DIGEST_FIELD: ClassVar[str] = "binding_digest"

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != TEG_PROTOCOL_VERSION:
            raise GatewayContractError("binding_protocol_unsupported")
        for name in ("operation_id", "gateway_id", "target_id", "provider_id", "model_id"):
            _identifier(getattr(self, name), name)
        for name in ("gateway_identity_digest", "intent_digest", "snapshot_set_digest",
                     "scope_digest", "budget_digest", "binding_digest"):
            _check_digest(getattr(self, name), name)
        _check_self(self, self.DIGEST_FIELD)

    @classmethod
    def from_submission(cls, submission: GatewaySubmission, gateway) -> "ExecutionBinding":
        snapshots = submission.snapshots
        payload = parse_canonical_json(next(item.content_json for item in snapshots if item.kind == "payload"))
        context = payload["approval_context"]
        fields = {
            "protocol_version": TEG_PROTOCOL_VERSION,
            "operation_id": submission.operation_id,
            "gateway_id": gateway.gateway_id,
            "gateway_identity_digest": gateway.identity_digest,
            "intent_digest": submission.intent_digest,
            "snapshot_set_digest": digest_json([item.to_dict() for item in snapshots]),
            "target_id": submission.intent.target_id,
            "provider_id": submission.intent.provider_id,
            "model_id": submission.intent.model_id,
            "scope_digest": digest_json(context["scope"]),
            "budget_digest": digest_json(parse_canonical_json(submission.intent.budget_json)),
        }
        return cls(**fields, binding_digest=digest_json(fields))


@dataclass(frozen=True)
class AuthorizationQuery(_Wire):
    protocol_version: int
    request_id: str
    binding: ExecutionBinding
    expires_at: str
    query_digest: str

    DIGEST_FIELD: ClassVar[str] = "query_digest"

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        _binding(self.binding)
        _timestamp(self.expires_at, "expires_at")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class AuthorizationQueryResult(_Wire):
    protocol_version: int
    request_id: str
    binding_digest: str
    status: str
    authorization_digest: str | None
    expires_at: str
    result_digest: str

    DIGEST_FIELD: ClassVar[str] = "result_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"approved", "pending", "rejected", "expired", "revoked", "unknown"}
    )

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        _digest(self.binding_digest, "binding_digest")
        if self.status not in self.STATUSES:
            raise GatewayContractError("authorization_query_status_invalid")
        if self.authorization_digest is not None:
            _digest(self.authorization_digest, "authorization_digest")
        if self.status == "approved" and self.authorization_digest is None:
            raise GatewayContractError("approved_query_requires_authorization_digest")
        _timestamp(self.expires_at, "expires_at")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class ConsumptionReservationRequest(_Wire):
    protocol_version: int
    request_id: str
    binding: ExecutionBinding
    query_result_digest: str
    expires_at: str
    request_digest: str

    DIGEST_FIELD: ClassVar[str] = "request_digest"

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        _binding(self.binding)
        _digest(self.query_result_digest, "query_result_digest")
        _timestamp(self.expires_at, "expires_at")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class ConsumptionReservation(_Wire):
    protocol_version: int
    reservation_id: str
    request_id: str
    binding_digest: str
    request_digest: str
    status: str
    expires_at: str
    reservation_digest: str

    DIGEST_FIELD: ClassVar[str] = "reservation_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"reserved", "rejected", "expired", "revoked", "unknown"}
    )

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        for name in ("reservation_id", "request_id"):
            _id(getattr(self, name), name)
        for name in ("binding_digest", "request_digest"):
            _digest(getattr(self, name), name)
        if self.status not in self.STATUSES:
            raise GatewayContractError("reservation_status_invalid")
        _timestamp(self.expires_at, "expires_at")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class ConsumptionConfirmationRequest(_Wire):
    protocol_version: int
    request_id: str
    reservation: ConsumptionReservation
    binding: ExecutionBinding
    idempotency_key: str
    request_digest: str

    DIGEST_FIELD: ClassVar[str] = "request_digest"

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        if not isinstance(self.reservation, ConsumptionReservation) or not isinstance(self.binding, ExecutionBinding):
            raise GatewayContractError("confirmation_binding_invalid")
        expected = digest_json({"operation_id": self.binding.operation_id,
                                "gateway_id": self.binding.gateway_id,
                                "reservation_digest": self.reservation.reservation_digest,
                                "binding_digest": self.binding.binding_digest})
        if self.reservation.binding_digest != self.binding.binding_digest or self.idempotency_key != expected:
            raise GatewayContractError("confirmation_binding_or_idempotency_key_mismatch")
        _digest(self.idempotency_key, "idempotency_key")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class ConsumptionConfirmation(_Wire):
    protocol_version: int
    confirmation_id: str
    request_id: str
    reservation_digest: str
    binding_digest: str
    status: str
    consumed_at: str | None
    expires_at: str
    confirmation_digest: str

    DIGEST_FIELD: ClassVar[str] = "confirmation_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"confirmed", "rejected", "expired", "revoked", "already_consumed", "unknown"}
    )

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        for name in ("confirmation_id", "request_id"):
            _id(getattr(self, name), name)
        for name in ("reservation_digest", "binding_digest"):
            _digest(getattr(self, name), name)
        if self.status not in self.STATUSES:
            raise GatewayContractError("confirmation_status_invalid")
        _timestamp(self.expires_at, "expires_at")
        if self.status == "confirmed":
            if self.consumed_at is None:
                raise GatewayContractError("confirmed_requires_consumed_at")
            consumed = _timestamp(self.consumed_at, "consumed_at")
            if consumed > _timestamp(self.expires_at, "expires_at"):
                raise GatewayContractError("consumption_after_expiry")
        elif self.consumed_at is not None:
            raise GatewayContractError("unconfirmed_must_not_have_consumed_at")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class AuthorizationStateQuery(_Wire):
    protocol_version: int
    request_id: str
    binding: ExecutionBinding
    reservation_digest: str | None
    query_digest: str

    DIGEST_FIELD: ClassVar[str] = "query_digest"

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        _binding(self.binding)
        if self.reservation_digest is not None:
            _digest(self.reservation_digest, "reservation_digest")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class AuthorizationState(_Wire):
    protocol_version: int
    request_id: str
    binding_digest: str
    status: str
    reservation_digest: str | None
    confirmation_digest: str | None
    expires_at: str | None
    state_digest: str

    DIGEST_FIELD: ClassVar[str] = "state_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"not_found", "reserved", "consumed", "rejected", "expired", "revoked", "unknown"}
    )

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        _digest(self.binding_digest, "binding_digest")
        if self.status not in self.STATUSES:
            raise GatewayContractError("authorization_state_invalid")
        for name in ("reservation_digest", "confirmation_digest"):
            value = getattr(self, name)
            if value is not None:
                _digest(value, name)
        if self.expires_at is not None:
            _timestamp(self.expires_at, "expires_at")
        if self.status == "consumed" and self.confirmation_digest is None:
            raise GatewayContractError("consumed_state_requires_confirmation")
        if self.status in {"reserved", "consumed"} and self.reservation_digest is None:
            raise GatewayContractError("reserved_state_requires_reservation")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class ReconciliationRequest(_Wire):
    protocol_version: int
    request_id: str
    binding: ExecutionBinding
    local_state: str
    reservation_digest: str | None
    confirmation_digest: str | None
    receipt_digest: str | None
    request_digest: str

    DIGEST_FIELD: ClassVar[str] = "request_digest"
    LOCAL_STATES: ClassVar[frozenset[str]] = frozenset(
        {"awaiting_authorization", "reserved", "prepared", "confirming", "effect_possible", "unknown"}
    )

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        _binding(self.binding)
        if self.local_state not in self.LOCAL_STATES:
            raise GatewayContractError("reconciliation_local_state_invalid")
        for name in ("reservation_digest", "confirmation_digest", "receipt_digest"):
            value = getattr(self, name)
            if value is not None:
                _digest(value, name)
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class ReconciliationResult(_Wire):
    protocol_version: int
    request_id: str
    binding_digest: str
    status: str
    reservation_digest: str | None
    confirmation_digest: str | None
    observed_at: str
    result_digest: str

    DIGEST_FIELD: ClassVar[str] = "result_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"not_found", "reserved", "consumed", "rejected", "expired", "revoked", "unknown"}
    )

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.request_id, "request_id")
        _digest(self.binding_digest, "binding_digest")
        if self.status not in self.STATUSES:
            raise GatewayContractError("reconciliation_status_invalid")
        for name in ("reservation_digest", "confirmation_digest"):
            value = getattr(self, name)
            if value is not None:
                _digest(value, name)
        if self.status == "consumed" and self.confirmation_digest is None:
            raise GatewayContractError("consumed_reconciliation_requires_confirmation")
        _timestamp(self.observed_at, "observed_at")
        _check_self(self, self.DIGEST_FIELD)


@dataclass(frozen=True)
class GatewayExecutionReceipt(_Wire):
    """Execution result bound to the exact reservation, confirmation and snapshots."""

    protocol_version: int
    receipt_id: str
    binding: ExecutionBinding
    reservation_digest: str
    confirmation_digest: str
    status: str
    started_at: str
    completed_at: str | None
    effect_idempotency: str
    effect_key: str | None
    result_digest: str | None
    receipt_digest: str

    DIGEST_FIELD: ClassVar[str] = "receipt_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"succeeded", "failed_before_effect", "failed_verified", "unknown"}
    )
    IDEMPOTENCY: ClassVar[frozenset[str]] = frozenset(
        {"applied_verified", "estimated", "unavailable"}
    )

    def __post_init__(self) -> None:
        _version(self.protocol_version)
        _id(self.receipt_id, "receipt_id")
        _binding(self.binding)
        _digest(self.reservation_digest, "reservation_digest")
        _digest(self.confirmation_digest, "confirmation_digest")
        if self.status not in self.STATUSES or self.effect_idempotency not in self.IDEMPOTENCY:
            raise GatewayContractError("execution_receipt_status_invalid")
        start = _timestamp(self.started_at, "started_at")
        end = _timestamp(self.completed_at, "completed_at") if self.completed_at else None
        if end is not None and end < start:
            raise GatewayContractError("receipt_completion_precedes_start")
        if end is None:
            raise GatewayContractError("execution_receipt_requires_completion_time")
        if self.status == "succeeded" and self.result_digest is None:
            raise GatewayContractError("successful_receipt_requires_result_digest")
        if self.result_digest is not None:
            _digest(self.result_digest, "result_digest")
        if self.effect_key is not None:
            _id(self.effect_key, "effect_key")
        _check_self(self, self.DIGEST_FIELD)


def _version(value: int) -> None:
    if type(value) is not int or value != TEG_PROTOCOL_VERSION:
        raise GatewayContractError("broker_teg_protocol_unsupported")


def _id(value: str, name: str) -> None:
    _identifier(value, name)


def _digest(value: str, name: str) -> None:
    _check_digest(value, name)


def _binding(value: ExecutionBinding) -> None:
    if not isinstance(value, ExecutionBinding):
        raise GatewayContractError("execution_binding_required")
