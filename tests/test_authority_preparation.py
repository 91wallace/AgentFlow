from datetime import datetime, timedelta, timezone

import pytest

from agentflow.approval import (
    ApproverDeviceConfig,
    AuthorityPreparationError,
    AuthorityProvisioningManifest,
    ServiceDomainConfig,
    inspect_grant_binding,
)
from agentflow.approval.contracts import UnavailableApprovalAuthority
from agentflow.approval.protocol import (
    ApprovalDecision,
    ApprovalRequest,
    ExecutionAuthorization,
    build_contract,
)


def _fp(character: str) -> str:
    return "sha256:" + character * 64


def _service(role: str, index: int) -> ServiceDomainConfig:
    host = f"{role}.example.invalid"
    return ServiceDomainConfig(
        domain_id=f"domain-{role}",
        service_id=f"service-{role}",
        operator_principal=f"operator-{role}",
        endpoint=f"https://{host}/api",
        trust_root_fingerprint=_fp(str(index)),
        service_certificate_fingerprint=_fp(str(index + 3)),
        key_custody_reference=f"custody-{role}",
        recovery_reference=f"recovery-{role}",
    )


def _manifest(**changes) -> AuthorityProvisioningManifest:
    values = {
        "schema_version": 1,
        "broker": _service("broker", 1),
        "teg": _service("teg", 2),
        "audit_anchor": _service("anchor", 3),
        "approver": ApproverDeviceConfig(
            "human-approver", "approval-device-01", "approve.example.invalid",
            "https://approve.example.invalid",
        ),
        "recovery_owners": ("recovery-owner-1", "recovery-owner-2"),
    }
    values.update(changes)
    return AuthorityProvisioningManifest(**values)


def test_manifest_is_declarative_and_never_authorizes():
    manifest = _manifest()
    assert manifest.preparation_only is True
    assert manifest.broker.operator_principal != manifest.teg.operator_principal


@pytest.mark.parametrize("changes", [
    {"teg": _service("broker", 2)},
    {"approver": ApproverDeviceConfig(
        "operator-broker", "approval-device-01", "approve.example.invalid",
        "https://approve.example.invalid",
    )},
    {"recovery_owners": ("operator-broker", "recovery-owner-2")},
])
def test_manifest_rejects_declared_identity_overlap(changes):
    with pytest.raises(AuthorityPreparationError):
        _manifest(**changes)


@pytest.mark.parametrize("endpoint", [
    "http://broker.example.invalid",
    "https://localhost:8443",
    "https://127.0.0.1:8443",
    "https://user:password@broker.example.invalid",
])
def test_service_endpoint_must_be_remote_https(endpoint):
    with pytest.raises(AuthorityPreparationError):
        ServiceDomainConfig(
            "broker-domain", "broker-service", "broker-operator", endpoint,
            _fp("1"), _fp("2"), "custody", "recovery",
        )


def _grant_chain(now: datetime):
    instant = now.astimezone(timezone.utc)
    created = instant.isoformat().replace("+00:00", "Z")
    expiry = (instant + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    digest = _fp("a")
    request = build_contract(
        ApprovalRequest,
        protocol_version=1,
        request_id="request-1",
        operation_id="operation-1",
        action_code="provider_call",
        intent_digest=digest,
        requester_principal="apl-client",
        nonce_digest=_fp("b"),
        created_at=created,
        expires_at=expiry,
        status="approved",
    )
    decision = build_contract(
        ApprovalDecision,
        protocol_version=1,
        decision_id="decision-1",
        request_id=request.request_id,
        operation_id=request.operation_id,
        request_digest=request.request_digest,
        intent_digest=request.intent_digest,
        decision="approved",
        approver_principal="independent-human",
        assurance="webauthn-uv",
        challenge_digest=_fp("c"),
        decided_at=created,
        expires_at=expiry,
    )
    authorization = build_contract(
        ExecutionAuthorization,
        protocol_version=1,
        authorization_id="authorization-1",
        request_id=request.request_id,
        operation_id=request.operation_id,
        intent_digest=request.intent_digest,
        audience="teg-service",
        target_id="local-target",
        payload_digest=_fp("d"),
        nonce_digest=_fp("e"),
        issued_at=created,
        expires_at=expiry,
        status="approved",
    )
    return request, decision, authorization


def test_grant_binding_report_is_structural_only_and_fail_closed():
    now = datetime.now(timezone.utc)
    request, decision, authorization = _grant_chain(now)
    report = inspect_grant_binding(
        request, decision, authorization,
        expected_intent_digest=request.intent_digest,
        expected_audience=authorization.audience,
        expected_target_id=authorization.target_id,
        expected_payload_digest=authorization.payload_digest,
        now=now,
    )
    assert report.structurally_consistent
    assert report.cryptographic_authenticity_verified is False
    assert report.current_revocation_checked is False
    assert report.replay_consumed is False
    assert report.authorized is False


def test_grant_binding_rejects_mismatched_plan_intent():
    now = datetime.now(timezone.utc)
    request, decision, authorization = _grant_chain(now)
    with pytest.raises(AuthorityPreparationError, match="binding_mismatch"):
        inspect_grant_binding(
            request, decision, authorization,
            expected_intent_digest=_fp("f"),
            expected_audience=authorization.audience,
            expected_target_id=authorization.target_id,
            expected_payload_digest=authorization.payload_digest,
            now=now,
        )


def test_grant_binding_rejects_expired_chain():
    now = datetime.now(timezone.utc)
    request, decision, authorization = _grant_chain(now - timedelta(minutes=2))
    with pytest.raises(AuthorityPreparationError, match="expired"):
        inspect_grant_binding(
            request, decision, authorization,
            expected_intent_digest=request.intent_digest,
            expected_audience=authorization.audience,
            expected_target_id=authorization.target_id,
            expected_payload_digest=authorization.payload_digest,
            now=now,
        )


def test_unavailable_authority_stays_unavailable():
    assert UnavailableApprovalAuthority().status().available is False
