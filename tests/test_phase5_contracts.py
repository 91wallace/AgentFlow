from dataclasses import FrozenInstanceError

import pytest

from agentflow.delivery import (ArtifactReference, DeliveryAuthorization,
                                DeliveryRequest)
from agentflow.validation import (CapabilityState, GateDefinition, GateStatus,
                                  RuntimeCapabilities, RuntimeCapability,
                                  RuntimeLimits, RuntimeProfile, RunStatus,
                                  ValidationRequest, ValidationResult)


def request(**overrides):
    fields = dict(
        operation_id="operation-1",
        validation_run_id="run-1",
        project_id="demo",
        task_id="task-1",
        definition_revision=1,
        definition_digest="sha256:definition",
        checkpoint_id="checkpoint-1",
        commit_oid="a" * 40,
        tree_oid="b" * 40,
        workspace_id="workspace-1",
        gate_snapshot='{"gates":[]}',
        gate_snapshot_digest="sha256:gates",
        policy_snapshot='{"policy":"fixed"}',
        policy_digest="sha256:policy",
        gate_ids=("unit-tests",),
        runtime_profile=RuntimeProfile.LOCAL_EXPLICIT,
    )
    return ValidationRequest(**(fields | overrides))


def test_gate_definition_and_runtime_limits_are_immutable_and_bounded():
    gate = GateDefinition("unit-tests", 1, ("criterion-1",), "pytest")
    assert gate.timeout_seconds == 600
    with pytest.raises(FrozenInstanceError):
        gate.required = False

    assert RuntimeLimits(timeout_seconds=3600).timeout_seconds == 3600
    for timeout in (0, 3601):
        with pytest.raises(ValueError):
            RuntimeLimits(timeout_seconds=timeout)
    with pytest.raises(ValueError):
        RuntimeLimits(cpu_count=9)
    with pytest.raises(ValueError):
        RuntimeLimits(memory_bytes=17 * 1024**3)
    with pytest.raises(ValueError):
        RuntimeLimits(process_count=257)


def test_validation_request_requires_identity_and_unique_gate_ids():
    with pytest.raises(ValueError, match="required"):
        request(checkpoint_id="")
    with pytest.raises(ValueError, match="duplicate selected gate"):
        request(gate_ids=("unit-tests", "unit-tests"))


def test_capability_report_rejects_duplicate_names():
    capability = RuntimeCapability("network", CapabilityState.UNAVAILABLE, "not supported")
    with pytest.raises(ValueError, match="duplicate"):
        RuntimeCapabilities(RuntimeProfile.LOCAL_EXPLICIT, "local", "1", (capability, capability))


def test_validation_and_gate_results_are_distinct_immutable_contracts():
    result = ValidationResult("run-1", "operation-1", RunStatus.PASSED, (), "sha256:req", "sha256:snapshot")
    assert result.status is RunStatus.PASSED
    assert GateStatus.NOT_RUN.value == "not_run"


def test_delivery_authorization_is_bound_to_operation_destination_and_artifact():
    artifact = ArtifactReference("artifact-1", "sha256:artifact", 100, "a" * 40, "b" * 40,
                                 "sha256:snapshot")
    authorization = DeliveryAuthorization("auth-1", "publish-1", "staging", artifact.sha256,
                                          "operator", "2026-11-05T00:00:00Z")
    request = DeliveryRequest("publish-1", "demo", artifact, "staging", authorization)
    assert request.authorization.authorization_id == "auth-1"
    with pytest.raises(ValueError, match="does not match"):
        DeliveryRequest("publish-2", "demo", artifact, "staging", authorization)
    with pytest.raises(ValueError, match="does not match"):
        DeliveryRequest("publish-1", "demo", artifact, "production", authorization)
