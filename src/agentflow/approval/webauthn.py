"""Strict WebAuthn assertion contracts and verification helpers.

This module verifies authenticator proofs only. It does not enroll authenticators,
create ApprovalDecision records, issue grants, or treat WebAuthn as informed consent.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
import base64
import binascii
import hashlib
import hmac
import json
import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from .protocol import (
    ACTION_CODES, PROTOCOL_VERSION, ProtocolError, WireContract, _check_version,
    _digest, _identifier, _timestamp, canonical_json, digest_json,
)

SUPPORTED_ALGORITHMS = frozenset({-7, -8, -257})  # ES256, EdDSA/Ed25519, RS256
MAX_CREDENTIAL_ID_BYTES = 1024
MAX_PUBLIC_KEY_DER_BYTES = 8192
MAX_CLIENT_DATA_BYTES = 8192
MAX_SIGNATURE_BYTES = 8192
MAX_CHALLENGE_TTL_SECONDS = 120
WEBAUTHN_BINDING_VERSION = 2
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_RP_ID = re.compile(r"^(?:localhost|[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)$")


class WebAuthnError(ValueError):
    """Malformed or policy-invalid WebAuthn data."""


class WebAuthnVerificationError(WebAuthnError):
    """Assertion rejected; the message contains only a stable reason code."""


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(value: str, *, maximum: int, label: str) -> bytes:
    if (type(value) is not str or not value or len(value) > ((maximum + 2) // 3) * 4
            or not _B64URL.fullmatch(value)):
        raise WebAuthnError(f"{label}_encoding_invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
    except (ValueError, binascii.Error) as exc:
        raise WebAuthnError(f"{label}_encoding_invalid") from exc
    if not decoded or len(decoded) > maximum or b64url_encode(decoded) != value:
        raise WebAuthnError(f"{label}_encoding_invalid")
    return decoded


def _normalize_rp_id(value: str) -> str:
    if type(value) is not str or not _RP_ID.fullmatch(value) or value != value.lower():
        raise WebAuthnError("rp_id_invalid")
    try:
        ascii_id = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebAuthnError("rp_id_invalid") from exc
    if ascii_id != value or value.endswith(".") or ".." in value:
        raise WebAuthnError("rp_id_invalid")
    return value


def _normalize_origin(origin: str, rp_id: str) -> str:
    if type(origin) is not str or len(origin) > 512:
        raise WebAuthnError("origin_invalid")
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise WebAuthnError("origin_invalid") from exc
    if (parsed.scheme not in {"https", "http"} or not host or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment):
        raise WebAuthnError("origin_invalid")
    host = host.lower()
    if parsed.scheme == "http" and host != "localhost":
        raise WebAuthnError("origin_requires_https")
    if host != rp_id and not host.endswith("." + rp_id):
        raise WebAuthnError("origin_outside_rp_id")
    default_port = (parsed.scheme == "https" and port == 443) or (parsed.scheme == "http" and port == 80)
    expected = f"{parsed.scheme}://{host}"
    if port is not None and not default_port:
        expected += f":{port}"
    if expected != origin:
        raise WebAuthnError("origin_not_canonical")
    return origin


def _public_key_for_algorithm(algorithm: int, der: bytes):
    if type(algorithm) is not int or algorithm not in SUPPORTED_ALGORITHMS:
        raise WebAuthnError("algorithm_unsupported")
    if type(der) is not bytes or not der or len(der) > MAX_PUBLIC_KEY_DER_BYTES:
        raise WebAuthnError("public_key_size_invalid")
    try:
        key = serialization.load_der_public_key(der)
    except (ValueError, TypeError) as exc:
        raise WebAuthnError("public_key_der_invalid") from exc
    if algorithm == -7 and not (
        isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1)
    ):
        raise WebAuthnError("public_key_algorithm_mismatch")
    if algorithm == -8 and not isinstance(key, ed25519.Ed25519PublicKey):
        raise WebAuthnError("public_key_algorithm_mismatch")
    if algorithm == -257 and not (isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048):
        raise WebAuthnError("public_key_algorithm_mismatch")
    return key


def _contract_digest(instance: WireContract) -> str:
    value = instance.to_dict()
    value.pop(instance.DIGEST_FIELD)
    return digest_json(value)


@dataclass(frozen=True)
class WebAuthnCredential(WireContract):
    """Immutable public credential record; private authenticator keys are excluded."""

    protocol_version: int
    credential_id: str
    principal_id: str
    rp_id: str
    allowed_origins: tuple[str, ...]
    algorithm: int
    public_key_spki_b64url: str
    created_at: str
    expires_at: str | None
    credential_digest: str

    DIGEST_FIELD: ClassVar[str] = "credential_digest"

    def __post_init__(self) -> None:
        _check_version(self.protocol_version)
        credential_id = b64url_decode(self.credential_id, maximum=MAX_CREDENTIAL_ID_BYTES,
                                      label="credential_id")
        if len(credential_id) > MAX_CREDENTIAL_ID_BYTES:
            raise WebAuthnError("credential_id_size_invalid")
        _identifier(self.principal_id, "principal_id")
        rp_id = _normalize_rp_id(self.rp_id)
        if (type(self.allowed_origins) is not tuple or not self.allowed_origins
                or len(set(self.allowed_origins)) != len(self.allowed_origins)
                or self.allowed_origins != tuple(sorted(self.allowed_origins))):
            raise WebAuthnError("allowed_origins_must_be_unique_sorted")
        for origin in self.allowed_origins:
            _normalize_origin(origin, rp_id)
        public_key = b64url_decode(self.public_key_spki_b64url,
                                   maximum=MAX_PUBLIC_KEY_DER_BYTES, label="public_key")
        _public_key_for_algorithm(self.algorithm, public_key)
        _timestamp(self.created_at, "created_at")
        if self.expires_at is not None:
            created = _timestamp(self.created_at, "created_at")
            expires = _timestamp(self.expires_at, "expires_at")
            if expires <= created:
                raise WebAuthnError("credential_expiry_invalid")
        _digest(self.credential_digest, "credential_digest")
        if _contract_digest(self) != self.credential_digest:
            raise WebAuthnError("credential_digest_mismatch")


@dataclass(frozen=True)
class WebAuthnChallengeBinding(WireContract):
    """Immutable, digest-bound context stored without the raw challenge bytes."""

    protocol_version: int
    challenge_id: str
    challenge_digest: str
    approver_principal: str
    requester_principal: str
    operation_id: str
    request_id: str
    request_digest: str
    intent_digest: str
    action_code: str
    target_id: str
    scope_digest: str
    client_policy_digest: str
    specification_digest: str
    plan_digest: str
    checkpoint_digest: str
    payload_digest: str
    catalog_digest: str
    policy_digest: str
    rp_id: str
    allowed_origins: tuple[str, ...]
    created_at: str
    expires_at: str
    binding_digest: str
    binding_version: int = 1
    presentation_version: int | None = None
    presentation_digest: str | None = None

    DIGEST_FIELD: ClassVar[str] = "binding_digest"

    def __post_init__(self) -> None:
        _check_version(self.protocol_version)
        if type(self.binding_version) is not int or self.binding_version not in (1, WEBAUTHN_BINDING_VERSION):
            raise WebAuthnError("challenge_binding_version_unsupported")
        if self.binding_version == 1:
            if self.presentation_version is not None or self.presentation_digest is not None:
                raise WebAuthnError("legacy_binding_cannot_carry_presentation")
        else:
            if type(self.presentation_version) is not int or self.presentation_version != 1:
                raise WebAuthnError("presentation_version_unsupported")
            _digest(self.presentation_digest, "presentation_digest")
        for name in ("challenge_id", "approver_principal", "requester_principal",
                     "operation_id", "request_id", "target_id"):
            _identifier(getattr(self, name), name)
        if self.action_code not in ACTION_CODES:
            raise WebAuthnError("action_code_unsupported")
        for name in ("challenge_digest", "request_digest", "intent_digest", "scope_digest",
                     "client_policy_digest", "specification_digest", "plan_digest",
                     "checkpoint_digest", "payload_digest", "catalog_digest", "policy_digest",
                     "binding_digest"):
            _digest(getattr(self, name), name)
        rp_id = _normalize_rp_id(self.rp_id)
        if (type(self.allowed_origins) is not tuple or not self.allowed_origins
                or len(set(self.allowed_origins)) != len(self.allowed_origins)
                or self.allowed_origins != tuple(sorted(self.allowed_origins))):
            raise WebAuthnError("allowed_origins_must_be_unique_sorted")
        for origin in self.allowed_origins:
            _normalize_origin(origin, rp_id)
        created = _timestamp(self.created_at, "created_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= created or (expires - created).total_seconds() > MAX_CHALLENGE_TTL_SECONDS:
            raise WebAuthnError("challenge_expiry_invalid")
        payload = self.to_dict()
        payload.pop(self.DIGEST_FIELD)
        if self.binding_version == 1:
            payload.pop("binding_version")
            payload.pop("presentation_version")
            payload.pop("presentation_digest")
        if digest_json(payload) != self.binding_digest:
            raise WebAuthnError("challenge_binding_digest_mismatch")

    @classmethod
    def from_dict(cls, value):
        """Read legacy RB-05 bindings without upgrading or rewriting stored rows."""
        if type(value) is not dict:
            raise ProtocolError("contract must be a JSON object")
        legacy = {item.name for item in fields(cls)} - {
            "binding_version", "presentation_version", "presentation_digest"}
        actual = set(value)
        if actual == legacy:
            value = {**value, "binding_version": 1, "presentation_version": None,
                     "presentation_digest": None}
        return super().from_dict(value)


@dataclass(frozen=True)
class WebAuthnChallenge:
    binding: WebAuthnChallengeBinding
    challenge_b64url: str = field(repr=False)


@dataclass(frozen=True)
class WebAuthnAssertion:
    """In-memory assertion bytes. Instances are never serialized or persisted."""

    protocol_version: int
    challenge_id: str
    credential_id: str
    client_data_json: bytes = field(repr=False)
    authenticator_data: bytes = field(repr=False)
    signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != PROTOCOL_VERSION:
            raise WebAuthnError("protocol_version_unsupported")
        _identifier(self.challenge_id, "challenge_id")
        b64url_decode(self.credential_id, maximum=MAX_CREDENTIAL_ID_BYTES, label="credential_id")
        if (type(self.client_data_json) is not bytes or not 1 <= len(self.client_data_json) <= MAX_CLIENT_DATA_BYTES
                or type(self.authenticator_data) is not bytes or not 37 <= len(self.authenticator_data) <= 8192
                or type(self.signature) is not bytes or not 1 <= len(self.signature) <= MAX_SIGNATURE_BYTES):
            raise WebAuthnError("assertion_size_invalid")


@dataclass(frozen=True)
class WebAuthnVerificationResult:
    """Authentication proof metadata; it is structurally incapable of granting consent."""

    authenticated_principal: str
    operation_id: str
    challenge_id: str
    credential_digest: str
    binding_digest: str
    user_present: bool
    user_verified: bool

    @property
    def consent_granted(self) -> bool:
        return False

    @property
    def approval_created(self) -> bool:
        return False


@dataclass(frozen=True)
class CredentialStateRecord:
    credential: WebAuthnCredential
    status: str
    sign_count: int
    counter_mode: str
    last_event_id: str


@dataclass(frozen=True)
class ChallengeStateRecord:
    binding: WebAuthnChallengeBinding
    status: str
    last_event_id: str


def _parse_client_data(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_CLIENT_DATA_BYTES:
        raise WebAuthnVerificationError("client_data_size_invalid")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise WebAuthnVerificationError("client_data_duplicate_key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise WebAuthnVerificationError("client_data_invalid_json")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    except WebAuthnVerificationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WebAuthnVerificationError("client_data_invalid_json") from exc
    if type(value) is not dict:
        raise WebAuthnVerificationError("client_data_not_object")
    return value


def verify_assertion_signature(assertion: WebAuthnAssertion, binding: WebAuthnChallengeBinding,
                               credential: WebAuthnCredential, *, challenge_bytes: bytes,
                               now: datetime) -> tuple[int, bytes]:
    """Verify WebAuthn assertion cryptography and return its counter/signed message."""
    if assertion.protocol_version != binding.protocol_version:
        raise WebAuthnVerificationError("protocol_version_mismatch")
    if assertion.challenge_id != binding.challenge_id:
        raise WebAuthnVerificationError("challenge_id_mismatch")
    if assertion.credential_id != credential.credential_id:
        raise WebAuthnVerificationError("credential_id_mismatch")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise WebAuthnVerificationError("invalid_clock")
    current = _timestamp(now.astimezone(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z"), "now")
    expiry = _timestamp(binding.expires_at, "challenge expiry")
    if current >= expiry:
        raise WebAuthnVerificationError("challenge_expired")
    if credential.principal_id != binding.approver_principal or credential.rp_id != binding.rp_id:
        raise WebAuthnVerificationError("credential_binding_mismatch")
    if credential.expires_at and current >= _timestamp(credential.expires_at, "credential expiry"):
        raise WebAuthnVerificationError("credential_expired")
    client_data = _parse_client_data(assertion.client_data_json)
    if client_data.get("type") != "webauthn.get":
        raise WebAuthnVerificationError("client_data_type_mismatch")
    expected_challenge = b64url_encode(challenge_bytes)
    if client_data.get("challenge") != expected_challenge:
        raise WebAuthnVerificationError("challenge_mismatch")
    origin = client_data.get("origin")
    if type(origin) is not str or origin not in binding.allowed_origins or origin not in credential.allowed_origins:
        raise WebAuthnVerificationError("origin_mismatch")
    cross_origin = client_data.get("crossOrigin", False)
    if type(cross_origin) is not bool or cross_origin or "topOrigin" in client_data:
        raise WebAuthnVerificationError("cross_origin_not_supported")
    auth_data = assertion.authenticator_data
    if type(auth_data) is not bytes or len(auth_data) != 37:
        raise WebAuthnVerificationError("authenticator_data_size_or_extension_unsupported")
    expected_rp_hash = hashlib.sha256(binding.rp_id.encode("ascii")).digest()
    if not hmac.compare_digest(auth_data[:32], expected_rp_hash):
        raise WebAuthnVerificationError("rp_id_hash_mismatch")
    flags = auth_data[32]
    if flags & 0x20 or flags & 0x02 or flags & 0xC0:
        raise WebAuthnVerificationError("reserved_or_unsupported_authenticator_flags")
    if not flags & 0x01:
        raise WebAuthnVerificationError("user_presence_required")
    if not flags & 0x04:
        raise WebAuthnVerificationError("user_verification_required")
    if flags & 0x10 and not flags & 0x08:
        raise WebAuthnVerificationError("backup_state_without_backup_eligibility")
    sign_count = int.from_bytes(auth_data[33:37], "big")
    signed_message = auth_data + hashlib.sha256(assertion.client_data_json).digest()
    public_key_der = b64url_decode(credential.public_key_spki_b64url,
                                   maximum=MAX_PUBLIC_KEY_DER_BYTES, label="public_key")
    key = _public_key_for_algorithm(credential.algorithm, public_key_der)
    try:
        if credential.algorithm == -7:
            key.verify(assertion.signature, signed_message, ec.ECDSA(hashes.SHA256()))
        elif credential.algorithm == -8:
            key.verify(assertion.signature, signed_message)
        else:  # RS256
            key.verify(assertion.signature, signed_message, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise WebAuthnVerificationError("signature_invalid") from exc
    except (ValueError, TypeError) as exc:
        raise WebAuthnVerificationError("signature_format_invalid") from exc
    return sign_count, signed_message


def canonical_digest(value: Any) -> str:
    """Expose the protocol canonical digest for WebAuthn binding fixtures."""
    return digest_json(value)


def challenge_digest(challenge: bytes) -> str:
    if type(challenge) is not bytes or len(challenge) != 32:
        raise WebAuthnError("challenge_size_invalid")
    return "sha256:" + hashlib.sha256(challenge).hexdigest()


def build_credential(credential_type: type[WebAuthnCredential], **values: Any) -> WebAuthnCredential:
    """Build immutable public credential metadata; never accepts a private key."""
    digest_field = credential_type.DIGEST_FIELD
    values[digest_field] = "sha256:" + "0" * 64
    payload = dict(values)
    payload.pop(digest_field)
    values[digest_field] = digest_json(payload)
    return credential_type(**values)


def build_binding(binding_type: type[WebAuthnChallengeBinding], **values: Any) -> WebAuthnChallengeBinding:
    digest_field = binding_type.DIGEST_FIELD
    values[digest_field] = "sha256:" + "0" * 64
    payload = dict(values)
    payload.pop(digest_field)
    values[digest_field] = digest_json(payload)
    return binding_type(**values)
