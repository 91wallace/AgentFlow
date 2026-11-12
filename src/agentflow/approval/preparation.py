"""Non-operational Broker/TEG deployment preparation and contract checks.

These types validate a proposed deployment manifest and the internal consistency
of existing v1 records. They do not authenticate signatures, prove administrative
separation, issue consent/grants, or enable an authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import re
import ssl
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from .identity import certificate_fingerprint
from .protocol import ApprovalDecision, ApprovalRequest, ExecutionAuthorization

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


class AuthorityPreparationError(ValueError):
    """Invalid deployment manifest, peer identity, or inconsistent grant chain."""


def _identifier(value: str, name: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise AuthorityPreparationError(f"{name}_invalid")


def _fingerprint(value: str, name: str) -> None:
    if type(value) is not str or not _FINGERPRINT.fullmatch(value):
        raise AuthorityPreparationError(f"{name}_invalid")


@dataclass(frozen=True)
class ServiceDomainConfig:
    """Non-secret inventory entry; presence is not proof of deployed identity."""

    domain_id: str
    service_id: str
    operator_principal: str
    endpoint: str
    trust_root_fingerprint: str
    service_certificate_fingerprint: str
    key_custody_reference: str
    recovery_reference: str

    def __post_init__(self):
        if type(self.endpoint) is not str:
            raise AuthorityPreparationError("service_endpoint_invalid")
        for field in ("domain_id", "service_id", "operator_principal",
                      "key_custody_reference", "recovery_reference"):
            _identifier(getattr(self, field), field)
        _fingerprint(self.trust_root_fingerprint, "trust_root_fingerprint")
        _fingerprint(self.service_certificate_fingerprint, "service_certificate_fingerprint")
        try:
            parsed = urlsplit(self.endpoint)
            # Accessing .port validates malformed/out-of-range port syntax.
            _ = parsed.port
        except ValueError as exc:
            raise AuthorityPreparationError("service_endpoint_invalid") from exc
        try:
            address = ipaddress.ip_address(parsed.hostname) if parsed.hostname else None
        except ValueError:
            address = None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.hostname.casefold() == "localhost"
                or (address is not None and address.is_loopback)):
            raise AuthorityPreparationError("service_endpoint_must_be_remote_https_without_userinfo")


@dataclass(frozen=True)
class ApproverDeviceConfig:
    principal_id: str
    device_id: str
    rp_id: str
    origin: str

    def __post_init__(self):
        if type(self.origin) is not str:
            raise AuthorityPreparationError("approver_origin_invalid")
        for field in ("principal_id", "device_id", "rp_id"):
            _identifier(getattr(self, field), field)
        try:
            parsed = urlsplit(self.origin)
            _ = parsed.port
        except ValueError as exc:
            raise AuthorityPreparationError("approver_origin_invalid") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
                or (parsed.hostname != self.rp_id
                    and not parsed.hostname.endswith("." + self.rp_id))):
            raise AuthorityPreparationError("approver_origin_rp_mismatch")


@dataclass(frozen=True)
class AuthorityProvisioningManifest:
    """Preparation-only record for the approved Broker/TEG/anchor topology."""

    schema_version: int
    broker: ServiceDomainConfig
    teg: ServiceDomainConfig
    audit_anchor: ServiceDomainConfig
    approver: ApproverDeviceConfig
    recovery_owners: tuple[str, ...]

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise AuthorityPreparationError("manifest_schema_unsupported")
        services = (self.broker, self.teg, self.audit_anchor)
        if any(not isinstance(item, ServiceDomainConfig) for item in services):
            raise AuthorityPreparationError("service_domain_config_required")
        if not isinstance(self.approver, ApproverDeviceConfig):
            raise AuthorityPreparationError("approver_device_config_required")
        for field in ("domain_id", "service_id", "operator_principal", "endpoint", "trust_root_fingerprint",
                      "service_certificate_fingerprint", "key_custody_reference",
                      "recovery_reference"):
            values = [getattr(item, field) for item in services]
            if len(set(values)) != len(values):
                raise AuthorityPreparationError(f"service_{field}_must_be_independent")
        operators = {item.operator_principal for item in services}
        if self.approver.principal_id in operators:
            raise AuthorityPreparationError("approver_must_not_operate_service_domain")
        if (type(self.recovery_owners) is not tuple or len(self.recovery_owners) < 2
                or len(set(self.recovery_owners)) != len(self.recovery_owners)):
            raise AuthorityPreparationError("recovery_requires_distinct_two_person_owners")
        for owner in self.recovery_owners:
            _identifier(owner, "recovery_owner")
        if set(self.recovery_owners) & operators:
            raise AuthorityPreparationError("recovery_owner_must_be_separate_from_service_operators")
        if self.approver.principal_id in self.recovery_owners:
            raise AuthorityPreparationError("approver_must_be_separate_from_recovery_owners")

    @property
    def preparation_only(self) -> bool:
        return True

    @classmethod
    def from_toml(cls, path: str | Path) -> "AuthorityProvisioningManifest":
        try:
            with Path(path).open("rb") as stream:
                data = tomllib.load(stream)
            if set(data) != {"schema_version", "broker", "teg", "audit_anchor",
                             "approver", "recovery_owners"}:
                raise AuthorityPreparationError("manifest_fields_mismatch")
            service_keys = {"domain_id", "service_id", "operator_principal", "endpoint",
                            "trust_root_fingerprint", "service_certificate_fingerprint",
                            "key_custody_reference", "recovery_reference"}
            services = {}
            for role in ("broker", "teg", "audit_anchor"):
                if type(data[role]) is not dict or set(data[role]) != service_keys:
                    raise AuthorityPreparationError(f"{role}_fields_mismatch")
                services[role] = ServiceDomainConfig(**data[role])
            approver_keys = {"principal_id", "device_id", "rp_id", "origin"}
            if type(data["approver"]) is not dict or set(data["approver"]) != approver_keys:
                raise AuthorityPreparationError("approver_fields_mismatch")
            if type(data["recovery_owners"]) is not list:
                raise AuthorityPreparationError("recovery_owners_must_be_array")
            return cls(data["schema_version"], services["broker"], services["teg"],
                       services["audit_anchor"], ApproverDeviceConfig(**data["approver"]),
                       tuple(data["recovery_owners"]))
        except (OSError, tomllib.TOMLDecodeError, TypeError, KeyError) as exc:
            raise AuthorityPreparationError("manifest_unreadable_or_invalid") from exc


@dataclass(frozen=True)
class PeerPinCheck:
    service_id: str
    certificate_fingerprint: str
    tls_chain_verified: bool
    hostname_verified: bool
    pin_matched: bool
    administrative_independence_verified: bool = False

    @property
    def accepted_for_authority(self) -> bool:
        # Certificate identity is only a transport prerequisite, never an authority grant.
        return False


def verify_tls_peer_pin(secure_socket: ssl.SSLSocket,
                        service: ServiceDomainConfig) -> PeerPinCheck:
    """Check a completed, verified TLS peer against a configured public cert pin.

    This does not verify deployment ownership, domain independence, consent, or
    cryptographic profiles for grants. Callers still need an approved transport.
    """
    if not isinstance(secure_socket, ssl.SSLSocket):
        raise AuthorityPreparationError("verified_tls_socket_required")
    context = secure_socket.context
    if (context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname):
        raise AuthorityPreparationError("tls_chain_and_hostname_verification_required")
    peer_der = secure_socket.getpeercert(binary_form=True)
    if not peer_der:
        raise AuthorityPreparationError("peer_certificate_missing")
    fingerprint = certificate_fingerprint(peer_der)
    if fingerprint != service.service_certificate_fingerprint:
        raise AuthorityPreparationError("service_certificate_pin_mismatch")
    return PeerPinCheck(service.service_id, fingerprint, True, True, True, False)


@dataclass(frozen=True)
class GrantBindingReport:
    request_id: str
    operation_id: str
    authorization_id: str
    structurally_consistent: bool
    cryptographic_authenticity_verified: bool = False
    current_revocation_checked: bool = False
    replay_consumed: bool = False
    authorized: bool = False
    reason: str = "external_authority_and_approved_signed_profile_required"


def inspect_grant_binding(request: ApprovalRequest, decision: ApprovalDecision,
                          authorization: ExecutionAuthorization, *,
                          expected_intent_digest: str, expected_audience: str,
                          expected_target_id: str, expected_payload_digest: str,
                          now: datetime | None = None) -> GrantBindingReport:
    """Cross-check existing v1 data records; never treats their digests as signatures."""
    if not all(isinstance(item, expected) for item, expected in (
            (request, ApprovalRequest), (decision, ApprovalDecision),
            (authorization, ExecutionAuthorization))):
        raise AuthorityPreparationError("existing_typed_contracts_required")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise AuthorityPreparationError("aware_utc_clock_required")
    from .protocol import _timestamp
    request_expiry = _timestamp(request.expires_at, "request_expiry")
    decision_expiry = _timestamp(decision.expires_at, "decision_expiry")
    auth_expiry = _timestamp(authorization.expires_at, "authorization_expiry")
    issued = _timestamp(authorization.issued_at, "authorization_issued_at")
    current = instant.astimezone(timezone.utc)
    if not (request.status == "approved" and decision.decision == "approved"
            and authorization.status == "approved"):
        raise AuthorityPreparationError("approval_chain_not_approved_or_terminal")
    if (decision.request_id != request.request_id
            or decision.operation_id != request.operation_id
            or decision.request_digest != request.request_digest
            or decision.intent_digest != request.intent_digest
            or decision.approver_principal == request.requester_principal
            or authorization.request_id != request.request_id
            or authorization.operation_id != request.operation_id
            or authorization.intent_digest != expected_intent_digest
            or request.intent_digest != expected_intent_digest
            or authorization.audience != expected_audience
            or authorization.target_id != expected_target_id
            or authorization.payload_digest != expected_payload_digest):
        raise AuthorityPreparationError("approval_chain_binding_mismatch")
    if not (issued <= current < auth_expiry and current < request_expiry
            and current < decision_expiry and auth_expiry <= request_expiry
            and auth_expiry <= decision_expiry):
        raise AuthorityPreparationError("approval_chain_expired_or_time_window_invalid")
    return GrantBindingReport(request.request_id, request.operation_id,
                              authorization.authorization_id, True)
