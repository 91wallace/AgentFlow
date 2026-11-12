"""Explicit security-profile configuration and inert local-consent contracts.

Selecting a profile is not enabling an authority. This module deliberately has
no grant issuer, execution hook, or positive authorization result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Protocol
import tomllib


class ProfileConfigurationError(ValueError):
    """Profile selection or consent binding is missing or invalid."""


class SecurityProfile(str, Enum):
    LOCAL_SUPERVISED = "LOCAL_SUPERVISED"
    INDEPENDENT_AUTHORITY = "INDEPENDENT_AUTHORITY"


class ConsentDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    UNAVAILABLE = "unavailable"


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_LOGGER = logging.getLogger("agentflow.security.profile")


def _identifier(value: str, field: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ProfileConfigurationError(f"{field}_invalid")


def _digest(value: str, field: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ProfileConfigurationError(f"{field}_invalid")


def _timestamp(value: str, field: str) -> datetime:
    if type(value) is not str:
        raise ProfileConfigurationError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProfileConfigurationError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise ProfileConfigurationError(f"{field}_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProfileConfiguration:
    schema_version: int
    profile: SecurityProfile

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ProfileConfigurationError("profile_schema_unsupported")
        if not isinstance(self.profile, SecurityProfile):
            raise ProfileConfigurationError("explicit_security_profile_required")

    def to_toml(self) -> str:
        return f'schema_version = 1\nprofile = "{self.profile.value}"\n'


@dataclass(frozen=True)
class ProfileDiagnostic:
    profile: str | None
    configuration_status: str
    authority_available: bool = False
    grant_issuance_enabled: bool = False
    dispatch_enabled: bool = False
    reason: str = "profile_selection_does_not_enable_authority_or_dispatch"

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "configuration_status": self.configuration_status,
            "authority_available": self.authority_available,
            "grant_issuance_enabled": self.grant_issuance_enabled,
            "dispatch_enabled": self.dispatch_enabled,
            "reason": self.reason,
        }


def parse_profile_configuration(data: bytes) -> ProfileConfiguration:
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ProfileConfigurationError("profile_toml_invalid") from exc
    if type(raw) is not dict or set(raw) != {"schema_version", "profile"}:
        raise ProfileConfigurationError("profile_fields_missing_or_ambiguous")
    if type(raw["profile"]) is not str:
        raise ProfileConfigurationError("explicit_security_profile_required")
    try:
        profile = SecurityProfile(raw["profile"])
    except ValueError as exc:
        raise ProfileConfigurationError("security_profile_unknown") from exc
    return ProfileConfiguration(raw["schema_version"], profile)


def load_profile_configuration(path: Path) -> ProfileConfiguration:
    try:
        return parse_profile_configuration(path.read_bytes())
    except OSError as exc:
        raise ProfileConfigurationError("security_profile_not_configured") from exc


def save_profile_configuration(path: Path, profile: SecurityProfile) -> ProfileConfiguration:
    """Create explicit selection once; refuse overwriting an existing selection."""
    config = ProfileConfiguration(1, profile)
    if not path.parent.is_dir():
        raise ProfileConfigurationError("agentflow_configuration_directory_missing")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ProfileConfigurationError("security_profile_already_configured") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(config.to_toml())
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        # Leave an invalid file visible for diagnosis; never silently remove user state.
        raise
    return config


def profile_diagnostic(path: Path) -> ProfileDiagnostic:
    try:
        config = load_profile_configuration(path)
    except ProfileConfigurationError as exc:
        _LOGGER.warning("security profile unavailable",
                        extra={"security_profile": None,
                               "security_profile_status": "invalid_or_missing",
                               "authority_available": False,
                               "dispatch_enabled": False})
        return ProfileDiagnostic(None, "invalid_or_missing", reason=str(exc))
    status = ("configured_not_homologated" if config.profile is SecurityProfile.LOCAL_SUPERVISED
              else "configured_but_authority_unavailable")
    diagnostic = ProfileDiagnostic(config.profile.value, status)
    _LOGGER.info("security profile diagnostic",
                 extra={"security_profile": config.profile.value,
                        "security_profile_status": status,
                        "authority_available": False,
                        "dispatch_enabled": False})
    return diagnostic


@dataclass(frozen=True)
class ConsentLimits:
    max_jobs: int
    max_iterations: int
    timeout_seconds: int
    estimated_budget: Decimal
    budget_currency: str

    def __post_init__(self) -> None:
        for name in ("max_jobs", "max_iterations", "timeout_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ProfileConfigurationError(f"{name}_must_be_positive_integer")
        if (not isinstance(self.estimated_budget, Decimal)
                or not self.estimated_budget.is_finite() or self.estimated_budget < 0):
            raise ProfileConfigurationError("estimated_budget_invalid")
        if type(self.budget_currency) is not str or not _CURRENCY.fullmatch(self.budget_currency):
            raise ProfileConfigurationError("budget_currency_invalid")


@dataclass(frozen=True)
class LocalConsentRequest:
    """Immutable payload intended for consent UI; it cannot authorize work."""

    profile: SecurityProfile
    proposal_id: str
    proposal_digest: str
    revision: int
    workspace_id: str
    allowed_operations: tuple[str, ...]
    limits: ConsentLimits
    expires_at: str
    operator_identity: str

    def __post_init__(self) -> None:
        if self.profile is not SecurityProfile.LOCAL_SUPERVISED:
            raise ProfileConfigurationError("local_consent_requires_local_supervised_profile")
        _identifier(self.proposal_id, "proposal_id")
        _digest(self.proposal_digest, "proposal_digest")
        if type(self.revision) is not int or self.revision < 1:
            raise ProfileConfigurationError("revision_invalid")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.operator_identity, "operator_identity")
        if (type(self.allowed_operations) is not tuple or not self.allowed_operations
                or len(set(self.allowed_operations)) != len(self.allowed_operations)):
            raise ProfileConfigurationError("allowed_operations_invalid")
        for operation in self.allowed_operations:
            _identifier(operation, "allowed_operation")
        if not isinstance(self.limits, ConsentLimits):
            raise ProfileConfigurationError("consent_limits_required")
        _timestamp(self.expires_at, "expires_at")

    @property
    def request_digest(self) -> str:
        payload = {
            "profile": self.profile.value,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "revision": self.revision,
            "workspace_id": self.workspace_id,
            "allowed_operations": list(self.allowed_operations),
            "limits": {
                "max_jobs": self.limits.max_jobs,
                "max_iterations": self.limits.max_iterations,
                "timeout_seconds": self.limits.timeout_seconds,
                "estimated_budget": str(self.limits.estimated_budget),
                "budget_currency": self.limits.budget_currency,
            },
            "expires_at": self.expires_at,
            "operator_identity": self.operator_identity,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LocalConsentReceipt:
    request_digest: str
    decision: ConsentDecision
    operator_identity: str
    explicit_action: bool
    recorded_at: str
    expires_at: str

    def __post_init__(self) -> None:
        _digest(self.request_digest, "request_digest")
        if not isinstance(self.decision, ConsentDecision):
            raise ProfileConfigurationError("consent_decision_invalid")
        _identifier(self.operator_identity, "operator_identity")
        if type(self.explicit_action) is not bool:
            raise ProfileConfigurationError("explicit_action_required")
        recorded = _timestamp(self.recorded_at, "recorded_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= recorded:
            raise ProfileConfigurationError("consent_expiry_invalid")


@dataclass(frozen=True)
class LocalConsentCheck:
    decision: ConsentDecision | None
    request_digest_matches: bool
    operator_identity_matches: bool
    explicit_action_present: bool
    within_validity: bool
    authenticated: bool = False
    authorizes_execution: bool = False
    reason: str = "local_consent_interface_not_implemented_or_authenticated"


def inspect_consent_receipt(request: LocalConsentRequest,
                            receipt: LocalConsentReceipt,
                            *, now: datetime | None = None) -> LocalConsentCheck:
    """Inspect record shape/bindings only; caller identity is not authenticated."""
    if not isinstance(request, LocalConsentRequest) or not isinstance(receipt, LocalConsentReceipt):
        raise ProfileConfigurationError("typed_local_consent_contracts_required")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ProfileConfigurationError("aware_clock_required")
    current = instant.astimezone(timezone.utc)
    recorded = _timestamp(receipt.recorded_at, "recorded_at")
    receipt_expiry = _timestamp(receipt.expires_at, "receipt_expiry")
    request_expiry = _timestamp(request.expires_at, "request_expiry")
    valid = (recorded <= current < receipt_expiry and current < request_expiry
             and receipt_expiry <= request_expiry)
    return LocalConsentCheck(
        decision=receipt.decision,
        request_digest_matches=receipt.request_digest == request.request_digest,
        operator_identity_matches=receipt.operator_identity == request.operator_identity,
        explicit_action_present=receipt.explicit_action,
        within_validity=valid,
    )


class LocalConsentInterface(Protocol):
    """Interface for a future operator UI; implementations are not authority by type."""

    def request_consent(self, request: LocalConsentRequest) -> LocalConsentReceipt | None: ...


class UnavailableLocalConsent:
    """Default until a trusted, explicit local consent UI is implemented."""

    def request_consent(self, request: LocalConsentRequest) -> LocalConsentReceipt | None:
        return None
