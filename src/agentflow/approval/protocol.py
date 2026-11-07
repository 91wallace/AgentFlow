"""Versioned, strict wire contracts for the dormant remote approval protocol.

This module defines data only. It does not authenticate principals, approve requests,
issue grants, contact a broker, or execute operations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, ClassVar, Mapping, TypeVar, get_origin, get_type_hints


PROTOCOL_VERSION = 1
MAX_CANONICAL_BYTES = 64 * 1024
ACTION_CODES = frozenset({"provider_call", "provider_model_change", "model_escalation"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$")
_SENSITIVE_KEYS = frozenset({
    "password", "passwd", "secret", "clientsecret", "privatekey", "apikey",
    "accesskey", "accesstoken", "refreshtoken", "bearertoken", "authorization",
    "authorizationheader", "cookie", "setcookie", "assertion", "webauthnassertion",
})
_SENSITIVE_TEXT = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}"),
    re.compile(r"(?i)\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}"),
)


class ProtocolError(ValueError):
    """Invalid, ambiguous, oversized, or unsupported protocol data."""


def _validate_json_value(value: Any, path: str = "$") -> None:
    if type(value) is str:
        if any(pattern.search(value) for pattern in _SENSITIVE_TEXT):
            raise ProtocolError(f"credential-like text is forbidden at {path}")
        return
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        raise ProtocolError(f"floating point values are forbidden at {path}")
    if type(value) in (list, tuple):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProtocolError(f"object keys must be strings at {path}")
            normalized_key = re.sub(r"[^a-z]", "", key.casefold())
            if normalized_key in _SENSITIVE_KEYS:
                raise ProtocolError(f"credential or assertion field is forbidden at {path}.{key}")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ProtocolError(f"non-JSON value at {path}")


def canonical_json(value: Any) -> str:
    """Return ADR 0002 compatible canonical JSON, rejecting ambiguous numbers."""
    _validate_json_value(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CANONICAL_BYTES:
        raise ProtocolError("canonical JSON exceeds the 64 KiB protocol limit")
    return encoded


def digest_json(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_canonical_json(value: str) -> Any:
    if type(value) is not str or len(value.encode("utf-8")) > MAX_CANONICAL_BYTES:
        raise ProtocolError("canonical JSON must be a string no larger than 64 KiB")

    def pairs(pairs_value):
        result = {}
        for key, item in pairs_value:
            if key in result:
                raise ProtocolError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(constant):
        raise ProtocolError(f"invalid JSON constant: {constant}")

    try:
        parsed = json.loads(value, object_pairs_hook=pairs, parse_constant=reject_constant,
                            parse_float=lambda _: (_ for _ in ()).throw(
                                ProtocolError("floating point values are forbidden")))
    except ProtocolError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProtocolError("invalid JSON") from exc
    if canonical_json(parsed) != value:
        raise ProtocolError("JSON input is not in canonical form")
    return parsed


def _identifier(value: str, label: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ProtocolError(f"{label} is invalid")


def _digest(value: str, label: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ProtocolError(f"{label} must be a lowercase SHA-256 digest")


def _enum(value: str, values: set[str] | frozenset[str], label: str) -> None:
    if type(value) is not str or value not in values:
        raise ProtocolError(f"{label} is invalid or unknown")


def _timestamp(value: str, label: str, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    if type(value) is not str or not _TIME.fullmatch(value):
        raise ProtocolError(f"{label} must be an RFC 3339 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProtocolError(f"{label} is not a valid timestamp") from exc
    return parsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


T = TypeVar("T", bound="WireContract")


class WireContract:
    PROTOCOL_VERSION: ClassVar[int] = PROTOCOL_VERSION
    DIGEST_FIELD: ClassVar[str | None] = None
    DIGEST_EXCLUDE: ClassVar[tuple[str, ...]] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)  # type: ignore[arg-type]

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls: type[T], value: Mapping[str, Any]) -> T:
        if type(value) is not dict:
            raise ProtocolError("contract must be a JSON object")
        expected = {item.name for item in fields(cls)}
        actual = set(value)
        if actual != expected:
            missing, extra = sorted(expected - actual), sorted(actual - expected)
            raise ProtocolError(f"contract fields mismatch; missing={missing}, unknown={extra}")
        version = value.get("protocol_version")
        if type(version) is not int or version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol_version: {version!r}")
        normalized = dict(value)
        hints = get_type_hints(cls)
        for item in fields(cls):
            if get_origin(hints.get(item.name)) is tuple and type(normalized.get(item.name)) is list:
                normalized[item.name] = tuple(normalized[item.name])
        try:
            return cls(**normalized)  # type: ignore[arg-type,call-arg]
        except TypeError as exc:
            raise ProtocolError("contract field types are invalid") from exc

    @classmethod
    def from_json(cls: type[T], value: str) -> T:
        parsed = parse_canonical_json(value)
        return cls.from_dict(parsed)


def _contract_digest(instance: WireContract) -> str:
    data = instance.to_dict()
    field_name = instance.DIGEST_FIELD
    if field_name:
        data.pop(field_name)
    for excluded in instance.DIGEST_EXCLUDE:
        data.pop(excluded)
    return digest_json(data)


@dataclass(frozen=True)
class AcceptedSnapshot(WireContract):
    protocol_version: int
    snapshot_id: str
    kind: str
    project_id: str
    revision_id: str
    schema_version: int
    content_json: str
    canonical_digest: str
    provenance_json: str
    accepted_by: str
    accepted_at: str
    parent_digest: str | None

    def __post_init__(self):
        _check_version(self.protocol_version)
        for name in ("snapshot_id", "project_id", "revision_id", "accepted_by"):
            _identifier(getattr(self, name), name)
        _enum(self.kind, {"specification", "plan", "checkpoint", "payload"}, "snapshot kind")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ProtocolError("schema_version must be a positive integer")
        content = parse_canonical_json(self.content_json)
        parse_canonical_json(self.provenance_json)
        _digest(self.canonical_digest, "canonical_digest")
        if digest_json(content) != self.canonical_digest:
            raise ProtocolError("snapshot content digest mismatch")
        if self.parent_digest is not None:
            _digest(self.parent_digest, "parent_digest")
        _timestamp(self.accepted_at, "accepted_at")


@dataclass(frozen=True)
class SnapshotProposal(WireContract):
    """Untrusted candidate snapshot; this contract cannot represent acceptance."""

    protocol_version: int
    snapshot_id: str
    kind: str
    project_id: str
    revision_id: str
    schema_version: int
    content_json: str
    canonical_digest: str
    provenance_json: str
    proposed_by: str
    operation_id: str
    proposed_at: str
    parent_digest: str | None

    def __post_init__(self):
        _check_version(self.protocol_version)
        for name in ("snapshot_id", "project_id", "revision_id", "proposed_by", "operation_id"):
            _identifier(getattr(self, name), name)
        _enum(self.kind, {"specification", "plan", "checkpoint", "payload"}, "snapshot kind")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ProtocolError("schema_version must be a positive integer")
        content = parse_canonical_json(self.content_json)
        parse_canonical_json(self.provenance_json)
        _digest(self.canonical_digest, "canonical_digest")
        if digest_json(content) != self.canonical_digest:
            raise ProtocolError("snapshot content digest mismatch")
        if self.parent_digest is not None:
            _digest(self.parent_digest, "parent_digest")
        _timestamp(self.proposed_at, "proposed_at")


@dataclass(frozen=True)
class OperationIntent(WireContract):
    protocol_version: int
    operation_id: str
    action_code: str
    principal_id: str
    client_id: str
    project_id: str
    task_id: str
    attempt_id: str
    specification_snapshot_id: str
    specification_revision_id: str
    specification_digest: str
    plan_snapshot_id: str
    plan_revision_id: str
    plan_digest: str
    checkpoint_snapshot_id: str
    checkpoint_digest: str
    target_id: str
    executor_version: str
    provider_id: str
    model_id: str
    payload_snapshot_id: str
    payload_digest: str
    parameters_json: str
    budget_json: str
    policy_digest: str
    catalog_digest: str
    created_at: str
    expires_at: str
    intent_digest: str

    DIGEST_FIELD: ClassVar[str] = "intent_digest"

    def __post_init__(self):
        _check_version(self.protocol_version)
        for name in ("operation_id", "action_code", "principal_id", "client_id", "project_id",
                     "task_id", "attempt_id", "specification_snapshot_id", "plan_snapshot_id",
                     "specification_revision_id", "plan_revision_id",
                     "checkpoint_snapshot_id", "target_id", "executor_version", "provider_id",
                     "model_id", "payload_snapshot_id"):
            _identifier(getattr(self, name), name)
        _enum(self.action_code, ACTION_CODES, "action_code")
        for name in ("specification_digest", "plan_digest", "checkpoint_digest", "payload_digest",
                     "policy_digest", "catalog_digest", "intent_digest"):
            _digest(getattr(self, name), name)
        parse_canonical_json(self.parameters_json)
        parse_canonical_json(self.budget_json)
        created = _timestamp(self.created_at, "created_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= created:
            raise ProtocolError("intent expiry must be after creation")
        if _contract_digest(self) != self.intent_digest:
            raise ProtocolError("intent digest mismatch")


@dataclass(frozen=True)
class ApprovalRequest(WireContract):
    protocol_version: int
    request_id: str
    operation_id: str
    action_code: str
    intent_digest: str
    requester_principal: str
    nonce_digest: str
    created_at: str
    expires_at: str
    status: str
    request_digest: str

    DIGEST_FIELD: ClassVar[str] = "request_digest"
    DIGEST_EXCLUDE: ClassVar[tuple[str, ...]] = ("status",)
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"pending", "approved", "rejected", "expired", "revoked", "unknown"}
    )

    def __post_init__(self):
        _check_version(self.protocol_version)
        for name in ("request_id", "operation_id", "action_code", "requester_principal"):
            _identifier(getattr(self, name), name)
        _enum(self.action_code, ACTION_CODES, "action_code")
        for name in ("intent_digest", "nonce_digest", "request_digest"):
            _digest(getattr(self, name), name)
        created = _timestamp(self.created_at, "created_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= created:
            raise ProtocolError("request expiry must be after creation")
        _enum(self.status, self.STATUSES, "approval request status")
        if _contract_digest(self) != self.request_digest:
            raise ProtocolError("request digest mismatch")


@dataclass(frozen=True)
class ApprovalDecision(WireContract):
    protocol_version: int
    decision_id: str
    request_id: str
    operation_id: str
    request_digest: str
    intent_digest: str
    decision: str
    approver_principal: str
    assurance: str
    challenge_digest: str
    decided_at: str
    expires_at: str
    decision_digest: str

    DIGEST_FIELD: ClassVar[str] = "decision_digest"

    def __post_init__(self):
        _check_version(self.protocol_version)
        for name in ("decision_id", "request_id", "operation_id", "approver_principal", "assurance"):
            _identifier(getattr(self, name), name)
        for name in ("request_digest", "intent_digest", "challenge_digest", "decision_digest"):
            _digest(getattr(self, name), name)
        _enum(self.decision, {"approved", "rejected", "expired", "revoked"}, "decision")
        decided = _timestamp(self.decided_at, "decided_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= decided:
            raise ProtocolError("decision expiry must be after decision time")
        if _contract_digest(self) != self.decision_digest:
            raise ProtocolError("decision digest mismatch")


@dataclass(frozen=True)
class ExecutionAuthorization(WireContract):
    protocol_version: int
    authorization_id: str
    request_id: str
    operation_id: str
    intent_digest: str
    audience: str
    target_id: str
    payload_digest: str
    nonce_digest: str
    issued_at: str
    expires_at: str
    status: str
    authorization_digest: str

    DIGEST_FIELD: ClassVar[str] = "authorization_digest"
    DIGEST_EXCLUDE: ClassVar[tuple[str, ...]] = ("status",)
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"pending", "approved", "rejected", "expired", "revoked", "consumed", "unknown"}
    )

    def __post_init__(self):
        _check_version(self.protocol_version)
        for name in ("authorization_id", "request_id", "operation_id", "audience", "target_id"):
            _identifier(getattr(self, name), name)
        for name in ("intent_digest", "payload_digest", "nonce_digest", "authorization_digest"):
            _digest(getattr(self, name), name)
        issued = _timestamp(self.issued_at, "issued_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= issued:
            raise ProtocolError("authorization expiry must follow issue time")
        _enum(self.status, self.STATUSES, "authorization status")
        if _contract_digest(self) != self.authorization_digest:
            raise ProtocolError("authorization digest mismatch")


@dataclass(frozen=True)
class ExecutionReceipt(WireContract):
    protocol_version: int
    receipt_id: str
    operation_id: str
    intent_digest: str
    authorization_digest: str
    target_id: str
    executor_id: str
    executor_version: str
    payload_digest: str
    status: str
    started_at: str
    completed_at: str | None
    effect_key: str | None
    result_digest: str | None
    receipt_digest: str

    DIGEST_FIELD: ClassVar[str] = "receipt_digest"
    STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"pending", "succeeded", "failed_before_effect", "failed_verified", "unknown"}
    )

    def __post_init__(self):
        _check_version(self.protocol_version)
        for name in ("receipt_id", "operation_id", "target_id", "executor_id", "executor_version"):
            _identifier(getattr(self, name), name)
        for name in ("intent_digest", "authorization_digest", "payload_digest", "receipt_digest"):
            _digest(getattr(self, name), name)
        if self.result_digest is not None:
            _digest(self.result_digest, "result_digest")
        if self.effect_key is not None:
            _identifier(self.effect_key, "effect_key")
        _enum(self.status, self.STATUSES, "execution receipt status")
        started = _timestamp(self.started_at, "started_at")
        completed = _timestamp(self.completed_at, "completed_at", optional=True)
        if completed and completed < started:
            raise ProtocolError("receipt completion precedes start")
        if self.status == "pending" and self.completed_at is not None:
            raise ProtocolError("pending receipt cannot have completed_at")
        if self.status != "pending" and self.completed_at is None:
            raise ProtocolError("terminal receipt requires completed_at")
        if _contract_digest(self) != self.receipt_digest:
            raise ProtocolError("receipt digest mismatch")


def _check_version(value: int) -> None:
    if type(value) is not int or value != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol_version: {value!r}")


def build_contract(contract_type: type[T], **values: Any) -> T:
    """Build a contract by deriving its self digest; useful for trusted producers."""
    digest_field = contract_type.DIGEST_FIELD
    if not digest_field:
        raise ProtocolError("contract has no derived digest")
    values[digest_field] = "sha256:" + "0" * 64
    digest_payload = dict(values)
    digest_payload.pop(digest_field)
    for excluded in contract_type.DIGEST_EXCLUDE:
        digest_payload.pop(excluded)
    values[digest_field] = digest_json(digest_payload)
    return contract_type(**values)
