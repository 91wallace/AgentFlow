"""Typed client identity and deterministic, consent-independent policy contracts.

An accepted policy decision only establishes that a technical client may propose an
operation. It never grants human consent or authorizes protected execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import secrets
from typing import ClassVar

from .protocol import (
    ACTION_CODES, PROTOCOL_VERSION, OperationIntent, ProtocolError, WireContract,
    _check_version, _digest, _enum, _identifier, _timestamp, canonical_json, digest_json,
)

_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCOPE = re.compile(r"^[a-z][a-z0-9]*(?::[a-z0-9_-]+)+$")


def certificate_fingerprint(certificate_der: bytes) -> str:
    """Fingerprint an X.509 leaf certificate without retaining certificate secrets."""
    if type(certificate_der) is not bytes or not certificate_der:
        raise ProtocolError("certificate DER must be non-empty bytes")
    return "sha256:" + hashlib.sha256(certificate_der).hexdigest()


def new_operation_nonce() -> str:
    """Return a cryptographically random 256-bit operation nonce as lowercase hex."""
    return secrets.token_hex(32)


def _fingerprint(value: str) -> None:
    if type(value) is not str or not _FINGERPRINT.fullmatch(value):
        raise ProtocolError("certificate fingerprint must be a lowercase SHA-256 digest")


def _scopes(values: tuple[str, ...], label: str) -> None:
    if type(values) is not tuple or not values or len(set(values)) != len(values):
        raise ProtocolError(f"{label} must be a non-empty tuple of unique scopes")
    for value in values:
        if type(value) is not str or not _SCOPE.fullmatch(value):
            raise ProtocolError(f"{label} contains an invalid scope")
    if values != tuple(sorted(values)):
        raise ProtocolError(f"{label} must be sorted for stable identity digests")


@dataclass(frozen=True)
class ClientIdentity(WireContract):
    protocol_version: int
    client_id: str
    principal_id: str
    scopes: tuple[str, ...]
    created_at: str
    expires_at: str
    identity_digest: str

    DIGEST_FIELD: ClassVar[str] = "identity_digest"

    def __post_init__(self) -> None:
        _check_version(self.protocol_version)
        _identifier(self.client_id, "client_id")
        _identifier(self.principal_id, "principal_id")
        _scopes(self.scopes, "client scopes")
        created = _timestamp(self.created_at, "created_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= created:
            raise ProtocolError("client identity expiry must follow creation")
        _digest(self.identity_digest, "identity_digest")
        if _contract_digest(self) != self.identity_digest:
            raise ProtocolError("client identity digest mismatch")


@dataclass(frozen=True)
class ClientCredential(WireContract):
    """Public certificate identity metadata. Private keys are never represented."""

    protocol_version: int
    client_id: str
    fingerprint: str
    issued_at: str
    not_before: str
    expires_at: str
    credential_digest: str

    DIGEST_FIELD: ClassVar[str] = "credential_digest"

    def __post_init__(self) -> None:
        _check_version(self.protocol_version)
        _identifier(self.client_id, "client_id")
        _fingerprint(self.fingerprint)
        issued = _timestamp(self.issued_at, "issued_at")
        not_before = _timestamp(self.not_before, "not_before")
        expires = _timestamp(self.expires_at, "expires_at")
        if not (issued < expires and not_before < expires):
            raise ProtocolError("credential validity interval is invalid")
        _digest(self.credential_digest, "credential_digest")
        if _contract_digest(self) != self.credential_digest:
            raise ProtocolError("credential digest mismatch")


@dataclass(frozen=True)
class ClientPolicy(WireContract):
    """Versioned least-privilege policy for one registered client."""

    protocol_version: int
    policy_id: str
    client_id: str
    version: int
    allowed_actions: tuple[str, ...]
    required_scopes: tuple[str, ...]
    allowed_targets: tuple[str, ...]
    max_request_ttl_seconds: int
    created_at: str
    policy_digest: str

    DIGEST_FIELD: ClassVar[str] = "policy_digest"

    def __post_init__(self) -> None:
        _check_version(self.protocol_version)
        for name in ("policy_id", "client_id"):
            _identifier(getattr(self, name), name)
        if type(self.version) is not int or self.version < 1:
            raise ProtocolError("policy version must be a positive integer")
        if (type(self.allowed_actions) is not tuple or not self.allowed_actions
                or len(set(self.allowed_actions)) != len(self.allowed_actions)):
            raise ProtocolError("allowed_actions must be a non-empty tuple of unique actions")
        for action in self.allowed_actions:
            _enum(action, ACTION_CODES, "allowed action")
        if self.allowed_actions != tuple(sorted(self.allowed_actions)):
            raise ProtocolError("allowed_actions must be sorted for stable policy digests")
        _scopes(self.required_scopes, "required_scopes")
        if (type(self.allowed_targets) is not tuple or not self.allowed_targets
                or len(set(self.allowed_targets)) != len(self.allowed_targets)):
            raise ProtocolError("allowed_targets must be a non-empty tuple of unique target IDs")
        for target in self.allowed_targets:
            _identifier(target, "allowed target")
        if self.allowed_targets != tuple(sorted(self.allowed_targets)):
            raise ProtocolError("allowed_targets must be sorted for stable policy digests")
        if type(self.max_request_ttl_seconds) is not int or not 1 <= self.max_request_ttl_seconds <= 3600:
            raise ProtocolError("max_request_ttl_seconds must be between 1 and 3600")
        _timestamp(self.created_at, "created_at")
        _digest(self.policy_digest, "policy_digest")
        if _contract_digest(self) != self.policy_digest:
            raise ProtocolError("client policy digest mismatch")


@dataclass(frozen=True)
class ClientRecord:
    identity: ClientIdentity
    status: str


@dataclass(frozen=True)
class CredentialRecord:
    credential: ClientCredential
    status: str


@dataclass(frozen=True)
class PolicyRecord:
    policy: ClientPolicy
    status: str


@dataclass(frozen=True)
class ClientAuthorizationDecision:
    """A technical policy result; consent is always required and never inferred."""

    permitted_to_propose: bool
    consent_required: bool
    reason: str
    client_id: str
    operation_id: str
    intent_digest: str
    policy_digest: str | None


def evaluate_client_operation(*, identity: ClientIdentity | None,
                              credential: ClientCredential | None,
                              policy: ClientPolicy | None,
                              intent: OperationIntent,
                              presented_fingerprint: str,
                              accepted_snapshot_digests: dict[str, str],
                              now: datetime) -> ClientAuthorizationDecision:
    """Evaluate client/policy/snapshot constraints without approving execution."""
    _fingerprint(presented_fingerprint)
    if identity is None or credential is None or policy is None:
        return _decision(False, "unknown_client_credential_or_policy", intent, policy)
    if now.tzinfo is None:
        return _decision(False, "invalid_clock", intent, policy)
    current = now.astimezone(timezone.utc).timestamp()
    def stamp(value: str) -> float:
        return datetime.fromisoformat(value[:-1] + "+00:00").timestamp()
    if identity.client_id != intent.client_id or credential.client_id != identity.client_id:
        return _decision(False, "client_identity_mismatch", intent, policy)
    if identity.principal_id != intent.principal_id:
        return _decision(False, "client_principal_mismatch", intent, policy)
    if credential.fingerprint != presented_fingerprint:
        return _decision(False, "certificate_identity_mismatch", intent, policy)
    if not (stamp(identity.created_at) <= current < stamp(identity.expires_at)):
        return _decision(False, "client_identity_inactive_or_expired", intent, policy)
    if not (stamp(credential.not_before) <= current < stamp(credential.expires_at)):
        return _decision(False, "client_credential_inactive_or_expired", intent, policy)
    if policy.client_id != identity.client_id:
        return _decision(False, "policy_client_mismatch", intent, policy)
    if intent.action_code not in policy.allowed_actions:
        return _decision(False, "action_not_allowed", intent, policy)
    if intent.target_id not in policy.allowed_targets:
        return _decision(False, "target_not_allowed", intent, policy)
    if not set(policy.required_scopes).issubset(set(identity.scopes)):
        return _decision(False, "required_scope_missing", intent, policy)
    if stamp(intent.expires_at) - stamp(intent.created_at) > policy.max_request_ttl_seconds:
        return _decision(False, "operation_ttl_exceeds_policy", intent, policy)
    expected = {
        "specification": intent.specification_digest,
        "plan": intent.plan_digest,
        "checkpoint": intent.checkpoint_digest,
        "payload": intent.payload_digest,
    }
    if accepted_snapshot_digests != expected:
        return _decision(False, "accepted_snapshot_digest_mismatch", intent, policy)
    return ClientAuthorizationDecision(
        True, True, "technical_policy_satisfied_human_consent_required",
        identity.client_id, intent.operation_id, intent.intent_digest, policy.policy_digest,
    )

def _decision(allowed: bool, reason: str, intent: OperationIntent,
              policy: ClientPolicy | None) -> ClientAuthorizationDecision:
    return ClientAuthorizationDecision(allowed, not allowed, reason, intent.client_id,
                                      intent.operation_id, intent.intent_digest,
                                      policy.policy_digest if policy else None)


def _contract_digest(instance: WireContract) -> str:
    payload = instance.to_dict()
    payload.pop(instance.DIGEST_FIELD)
    return digest_json(payload)
