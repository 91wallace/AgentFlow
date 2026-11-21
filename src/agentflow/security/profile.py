"""Explicit security-profile configuration and inert local-consent contracts.

Selecting a profile is not enabling an authority. This module deliberately has
no grant issuer, execution hook, or positive authorization result.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tomllib

from agentflow.approval.consent_contracts import (
    ProfileConfigurationError,
    SecurityProfile,
    ConsentDecision,
    _DIGEST,
    _IDENTIFIER,
    _CURRENCY,
    _identifier,
    _digest,
    _timestamp,
    ConsentLimits,
    LocalConsentRequest,
    ExecutionPlanConsentBinding,
    LocalExecutionConsentRequest,
    LocalConsentReceipt,
    LocalConsentCheck,
    inspect_consent_receipt,
    LocalConsentInterface,
    UnavailableLocalConsent,
)

_LOGGER = logging.getLogger("agentflow.security.profile")


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
