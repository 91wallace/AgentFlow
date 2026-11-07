import copy

import pytest

from agentflow.approval.protocol import (
    AcceptedSnapshot,
    ApprovalDecision,
    ApprovalRequest,
    ExecutionAuthorization,
    ExecutionReceipt,
    OperationIntent,
    ProtocolError,
    SnapshotProposal,
    build_contract,
    canonical_json,
    digest_json,
    parse_canonical_json,
)


NOW = "2026-11-06T12:00:00.000000Z"
LATER = "2026-11-06T12:05:00.000000Z"
DIGEST = "sha256:" + "1" * 64


def _snapshot(snapshot_id="spec-1", kind="specification", operation_id="snapshot-op-1"):
    content = {"kind": kind, "project": "demo", "version": 1}
    return SnapshotProposal(
        protocol_version=1, snapshot_id=snapshot_id, kind=kind, project_id="demo",
        revision_id=snapshot_id, schema_version=1, content_json=canonical_json(content),
        canonical_digest=digest_json(content), provenance_json=canonical_json({"source": "test"}),
        proposed_by="operator-1", operation_id=operation_id, proposed_at=NOW, parent_digest=None,
    )


def _intent():
    values = dict(
        protocol_version=1, operation_id="op-1", action_code="provider_call", principal_id="user-1",
        client_id="client-1", project_id="demo", task_id="task-1", attempt_id="attempt-1",
        specification_snapshot_id="spec-snapshot", specification_digest=DIGEST,
        specification_revision_id="spec-revision-1", plan_snapshot_id="plan-snapshot",
        plan_revision_id="plan-revision-1", plan_digest=DIGEST,
        checkpoint_snapshot_id="checkpoint-snapshot", checkpoint_digest=DIGEST,
        target_id="provider-gateway", executor_version="1.0", provider_id="provider-a",
        model_id="model-a", payload_snapshot_id="payload-snapshot", payload_digest=DIGEST,
        parameters_json=canonical_json({"temperature": "0"}),
        budget_json=canonical_json({"cost_state": "estimated", "max_cost": "1.00"}),
        policy_digest=DIGEST, catalog_digest=DIGEST, created_at=NOW, expires_at=LATER,
    )
    return build_contract(OperationIntent, **values)


def _request(intent=None, **changes):
    intent = intent or _intent()
    values = dict(
        protocol_version=1, request_id="request-1", operation_id=intent.operation_id,
        action_code=intent.action_code, intent_digest=intent.intent_digest,
        requester_principal="user-1", nonce_digest=DIGEST, created_at=NOW,
        expires_at=LATER, status="pending",
    )
    values.update(changes)
    return build_contract(ApprovalRequest, **values)


def _decision(request, decision="approved"):
    return build_contract(
        ApprovalDecision, protocol_version=1, decision_id=f"decision-{decision}",
        request_id=request.request_id, operation_id=request.operation_id,
        request_digest=request.request_digest, intent_digest=request.intent_digest,
        decision=decision, approver_principal="human-1", assurance="webauthn-uv",
        challenge_digest=DIGEST, decided_at=NOW, expires_at=LATER,
    )


def _authorization(request, intent):
    return build_contract(
        ExecutionAuthorization, protocol_version=1, authorization_id="auth-1",
        request_id=request.request_id, operation_id=request.operation_id,
        intent_digest=intent.intent_digest, audience="trusted-gateway", target_id=intent.target_id,
        payload_digest=intent.payload_digest, nonce_digest=DIGEST, issued_at=NOW, expires_at=LATER,
        status="approved",
    )


def _receipt(intent, authorization, status="succeeded"):
    return build_contract(
        ExecutionReceipt, protocol_version=1, receipt_id=f"receipt-{status}",
        operation_id=intent.operation_id, intent_digest=intent.intent_digest,
        authorization_digest=authorization.authorization_digest, target_id=intent.target_id,
        executor_id="gateway-1", executor_version="1.0", payload_digest=intent.payload_digest,
        status=status, started_at=NOW, completed_at=LATER if status != "pending" else None,
        effect_key="effect-1" if status != "pending" else None,
        result_digest=DIGEST if status == "succeeded" else None,
    )


def test_canonicalization_golden_vectors_are_stable_and_utf8():
    value = {"b": 2, "a": 1}
    assert canonical_json(value) == '{"a":1,"b":2}'
    assert digest_json(value) == "sha256:43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    unicode_value = {"z": "café", "a": 1}
    assert canonical_json(unicode_value) == '{"a":1,"z":"café"}'
    assert digest_json(unicode_value) == "sha256:5ecd369ac1093230a0bfc1b92db4f887e42f36a3c56ac8272ff41df93565fedb"
    assert parse_canonical_json(canonical_json(unicode_value)) == unicode_value


@pytest.mark.parametrize("value", [1.0, float("nan"), {"nested": [float("inf")]}, {1: "key"}])
def test_canonicalizer_rejects_ambiguous_json_numbers_and_keys(value):
    with pytest.raises(ProtocolError):
        canonical_json(value)


@pytest.mark.parametrize("value", [
    {"access_token": "test-only-placeholder"},
    {"nested": {"webauthn_assertion": "test-only-placeholder"}},
    {"payload": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key"},
    {"payload": "Bearer abcdefghijklmnopqrstuvwxyz012345"},
])
def test_protocol_rejects_secret_fields_and_recognizable_credential_text(value):
    with pytest.raises(ProtocolError, match="credential|assertion"):
        canonical_json(value)


def test_parser_rejects_duplicate_keys_noncanonical_input_and_oversized_payload():
    for value in ('{"a":1,"a":2}', '{ "a":1}', '{"a":NaN}'):
        with pytest.raises(ProtocolError):
            parse_canonical_json(value)
    with pytest.raises(ProtocolError, match="64 KiB"):
        parse_canonical_json('{"x":"' + "a" * 65_530 + '"}')


def test_all_protocol_contracts_round_trip_and_self_verify():
    proposal = _snapshot()
    accepted = AcceptedSnapshot(
        protocol_version=1, snapshot_id="accepted-1", kind="specification", project_id="demo",
        revision_id="rev-1", schema_version=1, content_json=proposal.content_json,
        canonical_digest=proposal.canonical_digest, provenance_json=proposal.provenance_json,
        accepted_by="human-1", accepted_at=NOW, parent_digest=None,
    )
    intent = _intent()
    request = _request(intent)
    decision = _decision(request)
    authorization = _authorization(request, intent)
    receipt = _receipt(intent, authorization)
    for contract in (accepted, proposal, intent, request, decision, authorization, receipt):
        assert type(contract).from_json(contract.to_json()) == contract


def test_wire_contracts_reject_unknown_missing_and_future_version_fields():
    value = _intent().to_dict()
    value["unexpected"] = "not ignored"
    with pytest.raises(ProtocolError, match="unknown"):
        OperationIntent.from_dict(value)
    value = _intent().to_dict()
    del value["provider_id"]
    with pytest.raises(ProtocolError, match="missing"):
        OperationIntent.from_dict(value)
    value = _intent().to_dict()
    value["protocol_version"] = 2
    with pytest.raises(ProtocolError, match="unsupported"):
        OperationIntent.from_dict(value)


def test_contract_digest_changes_when_any_included_field_changes():
    original = _intent().to_dict()
    original_digest = _intent().intent_digest
    for field, current in original.items():
        if field == "intent_digest":
            continue
        changed = copy.deepcopy(original)
        if type(current) is int:
            changed[field] = current + 1
        elif current is None:
            changed[field] = "changed"
        else:
            changed[field] = current + "x"
        changed.pop("intent_digest")
        assert digest_json(changed) != original_digest, field


def test_contract_rejects_digest_tampering_and_inconsistent_snapshot():
    values = _intent().to_dict()
    values["model_id"] = "different-model"
    with pytest.raises(ProtocolError, match="digest mismatch"):
        OperationIntent.from_dict(values)
    values = _snapshot().__dict__.copy()
    values["canonical_digest"] = DIGEST
    with pytest.raises(ProtocolError, match="snapshot content digest mismatch"):
        SnapshotProposal(**values)


def test_request_digest_binds_identity_fields_but_not_mutable_status():
    request = _request()
    approved = ApprovalRequest.from_dict({**request.to_dict(), "status": "approved"})
    assert approved.request_digest == request.request_digest
    changed = request.to_dict()
    changed["action_code"] = "provider_model_change"
    with pytest.raises(ProtocolError, match="request digest mismatch"):
        ApprovalRequest.from_dict(changed)


def test_intent_and_request_expiry_and_status_are_strict():
    with pytest.raises(ProtocolError, match="expiry"):
        _request(expires_at=NOW)
    with pytest.raises(ProtocolError, match="status"):
        _request(status="approved-ish")
    with pytest.raises(ProtocolError, match="action_code"):
        _request(action_code="unknown-protected-action")
