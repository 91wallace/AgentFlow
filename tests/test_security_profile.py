from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import pytest

from agentflow.security import (
    ConsentDecision,
    ConsentLimits,
    LocalConsentReceipt,
    LocalConsentRequest,
    ProfileConfigurationError,
    SecurityProfile,
    UnavailableLocalConsent,
    inspect_consent_receipt,
    load_profile_configuration,
    profile_diagnostic,
    save_profile_configuration,
)


def _request(now=None):
    now = now or datetime.now(timezone.utc)
    expiry = (now + timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
    return LocalConsentRequest(
        profile=SecurityProfile.LOCAL_SUPERVISED,
        proposal_id="proposal-01",
        proposal_digest="sha256:" + "a" * 64,
        revision=3,
        workspace_id="workspace-synthetic",
        allowed_operations=("write:src/math.py", "run:pytest"),
        limits=ConsentLimits(3, 2, 120, Decimal("0.00"), "USD"),
        expires_at=expiry,
        operator_identity="local-operator",
    )


@pytest.mark.parametrize("profile", list(SecurityProfile))
def test_profile_selection_is_explicit_persistent_and_never_enables_dispatch(tmp_path, profile):
    target = tmp_path / ".agentflow" / "security-profile.toml"
    target.parent.mkdir()
    config = save_profile_configuration(target, profile)
    recovered = load_profile_configuration(target)
    diagnostic = profile_diagnostic(target)
    assert recovered == config
    assert recovered.profile is profile
    assert diagnostic.profile == profile.value
    assert diagnostic.authority_available is False
    assert diagnostic.grant_issuance_enabled is False
    assert diagnostic.dispatch_enabled is False


def test_profile_diagnostic_log_identifies_profile_without_secrets(tmp_path, caplog):
    target = tmp_path / ".agentflow" / "security-profile.toml"
    target.parent.mkdir()
    save_profile_configuration(target, SecurityProfile.LOCAL_SUPERVISED)
    with caplog.at_level("INFO", logger="agentflow.security.profile"):
        profile_diagnostic(target)
    record = next(item for item in caplog.records if item.name == "agentflow.security.profile")
    assert record.security_profile == "LOCAL_SUPERVISED"
    assert record.dispatch_enabled is False


def test_missing_profile_is_not_defaulted(tmp_path):
    path = tmp_path / "missing-profile.toml"
    with pytest.raises(ProfileConfigurationError, match="not_configured"):
        load_profile_configuration(path)
    report = profile_diagnostic(path).to_dict()
    assert report["profile"] is None
    assert report["configuration_status"] == "invalid_or_missing"
    assert report["dispatch_enabled"] is False


@pytest.mark.parametrize("contents", [
    b"schema_version = 1\n",
    b'schema_version = 1\nprofile = "LOCAL"\n',
    b'schema_version = 1\nprofile = "LOCAL_SUPERVISED"\nextra = true\n',
    b'schema_version = 2\nprofile = "LOCAL_SUPERVISED"\n',
])
def test_missing_ambiguous_or_invalid_profile_is_rejected(tmp_path, contents):
    path = tmp_path / "profile.toml"
    path.write_bytes(contents)
    with pytest.raises(ProfileConfigurationError):
        load_profile_configuration(path)


def test_profile_selection_never_overwrites_existing_choice(tmp_path):
    target = tmp_path / ".agentflow" / "security-profile.toml"
    target.parent.mkdir()
    save_profile_configuration(target, SecurityProfile.LOCAL_SUPERVISED)
    with pytest.raises(ProfileConfigurationError, match="already_configured"):
        save_profile_configuration(target, SecurityProfile.INDEPENDENT_AUTHORITY)
    assert load_profile_configuration(target).profile is SecurityProfile.LOCAL_SUPERVISED


def test_consent_receipt_is_bound_but_never_authenticated_or_authorizing():
    now = datetime.now(timezone.utc)
    request = _request(now)
    receipt = LocalConsentReceipt(
        request_digest=request.request_digest,
        decision=ConsentDecision.APPROVE,
        operator_identity="local-operator",
        explicit_action=True,
        recorded_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
    )
    check = inspect_consent_receipt(request, receipt, now=now)
    assert check.request_digest_matches
    assert check.operator_identity_matches
    assert check.explicit_action_present
    assert check.within_validity
    assert check.decision is ConsentDecision.APPROVE
    assert check.authenticated is False
    assert check.authorizes_execution is False


def test_default_consent_interface_is_unavailable():
    assert UnavailableLocalConsent().request_consent(_request()) is None


def test_consent_requires_explicit_action_and_exact_proposal_binding():
    now = datetime.now(timezone.utc)
    request = _request(now)
    receipt = LocalConsentReceipt(
        request_digest="sha256:" + "b" * 64,
        decision=ConsentDecision.APPROVE,
        operator_identity="different-operator",
        explicit_action=False,
        recorded_at=now.isoformat().replace("+00:00", "Z"),
        expires_at=(now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
    )
    check = inspect_consent_receipt(request, receipt, now=now)
    assert not check.request_digest_matches
    assert not check.operator_identity_matches
    assert not check.explicit_action_present
    assert check.authorizes_execution is False


def test_consent_contract_rejects_invalid_limits_and_naive_time():
    with pytest.raises(ProfileConfigurationError, match="max_jobs"):
        ConsentLimits(0, 1, 10, Decimal("0"), "USD")
    with pytest.raises(ProfileConfigurationError, match="aware"):
        _request(datetime(2026, 1, 1))


def test_profile_cli_select_and_status_report_inert_capabilities(tmp_path, monkeypatch, capsys):
    import agentflow.cli as cli

    root = tmp_path / "project"
    (root / ".agentflow").mkdir(parents=True)
    monkeypatch.setattr(cli, "repository_root", lambda _path: root)
    assert cli.main(["profile", "select", "--path", str(root),
                     "--profile", "LOCAL_SUPERVISED", "--confirm"]) == 0
    selected = json.loads(capsys.readouterr().out)
    assert selected["profile"] == "LOCAL_SUPERVISED"
    assert selected["configuration_status"] == "configured_not_homologated"
    assert selected["grant_issuance_enabled"] is False
    assert selected["dispatch_enabled"] is False
    assert cli.main(["profile", "status", "--path", str(root)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["profile"] == "LOCAL_SUPERVISED"


def test_profile_cli_missing_config_fails_closed(tmp_path, monkeypatch, capsys):
    import agentflow.cli as cli

    root = tmp_path / "project"
    (root / ".agentflow").mkdir(parents=True)
    monkeypatch.setattr(cli, "repository_root", lambda _path: root)
    assert cli.main(["profile", "status", "--path", str(root)]) == 2
    status = json.loads(capsys.readouterr().out)
    assert status["profile"] is None
    assert status["dispatch_enabled"] is False
