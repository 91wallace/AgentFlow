import json
from dataclasses import fields

import pytest

from agentflow.apl import (
    CapabilityProbe,
    IsolationControl,
    IsolationEvidence,
    IsolationPolicy,
    RestrictedInferenceHarness,
    RestrictedWorkerSpec,
    TerminationStatus,
    WorkerStatus,
    WorkerTerminationResult,
    decode_worker_proposal,
)


def worker_spec(payload='{"objective":"test"}', **overrides):
    values = {"request_id": "request-1", "input_json": payload}
    values.update(overrides)
    return RestrictedWorkerSpec(**values)


def proposal_mapping():
    return {
        "proposal_id": "proposal-1",
        "source_revision_id": "revision-1",
        "source_cycle_revision": 2,
        "rationale": "deterministic fixture",
        "changes": [],
        "completion_condition": "review complete",
        "evidence_used": [],
        "resolves_blockers": [],
        "schema_version": 1,
    }


def test_worker_spec_accepts_bounded_structured_input():
    spec = worker_spec()
    assert spec.request_id == "request-1"
    assert json.loads(spec.input_json) == {"objective": "test"}
    assert not {"executable", "cwd", "environment", "arguments"} & {field.name for field in fields(spec)}


@pytest.mark.parametrize("payload", ["[]", "null", "{bad", '{"x":1,"x":2}', '{"x":NaN}'])
def test_worker_spec_rejects_malformed_or_ambiguous_json(payload):
    with pytest.raises(ValueError):
        worker_spec(payload)


@pytest.mark.parametrize("overrides", [
    {"timeout_seconds": 0},
    {"timeout_seconds": 301},
    {"memory_limit_bytes": 1},
    {"output_limit_bytes": 2 * 1024 * 1024 + 1},
    {"schema_version": 2},
])
def test_worker_spec_rejects_invalid_limits_and_versions(overrides):
    with pytest.raises(ValueError):
        worker_spec(**overrides)


def test_policy_cannot_disable_mandatory_control_or_enable_capability():
    with pytest.raises(ValueError, match="cannot be removed"):
        IsolationPolicy(controls=frozenset({IsolationControl.TREE_TERMINATION}))
    with pytest.raises(ValueError, match="forbids network_allowed"):
        IsolationPolicy(network_allowed=True)


def test_probe_requires_enforced_and_verified_evidence_for_each_control():
    evidence = tuple(IsolationEvidence(control, True, True, "test-fixture", "observed")
                     for control in IsolationControl)
    complete = CapabilityProbe("test", "fixture-only", evidence)
    assert complete.missing_controls(IsolationPolicy()) == ()

    partial = CapabilityProbe("test", "fixture-only", evidence[:-1])
    assert partial.missing_controls(IsolationPolicy())

    unverified = tuple(IsolationEvidence(item.control, True, False, "requested", "not observed")
                       for item in evidence)
    assert len(CapabilityProbe("test", "fixture-only", unverified).missing_controls(IsolationPolicy())) == len(IsolationControl)


def test_harness_reports_unavailable_and_never_launches_worker():
    harness = RestrictedInferenceHarness()
    result = harness.launch(worker_spec())
    assert result.status is WorkerStatus.BLOCKED
    assert not result.worker_id
    assert not result.output_json
    assert result.probe.backend_id == "unavailable"
    assert len(result.probe.missing_controls(harness.policy)) == len(IsolationControl)


@pytest.mark.parametrize("scenario", [
    "read_protected_file", "write_protected_file", "open_network", "spawn_subprocess",
    "allocate_excess_memory", "run_past_deadline", "spawn_descendant", "malformed_output",
])
def test_adversarial_worker_scenarios_are_not_dispatched_without_os_backend(scenario):
    result = RestrictedInferenceHarness().launch(worker_spec(json.dumps({"scenario": scenario})))
    # This proves fail-closed dispatch only. It does not claim the OS denied
    # the simulated operation; there is intentionally no worker process.
    assert result.status is WorkerStatus.BLOCKED
    assert result.worker_id == ""


def test_cancel_reports_no_worker_and_does_not_claim_os_termination():
    result = RestrictedInferenceHarness().terminate("worker-not-started")
    assert result.status is TerminationStatus.NOT_RUNNING
    assert result.descendants_confirmed


def test_termination_contract_rejects_unconfirmed_descendants():
    with pytest.raises(ValueError, match="confirmed descendant cleanup"):
        WorkerTerminationResult(TerminationStatus.TERMINATED, "worker-1", False)


def test_worker_proposal_response_is_bounded_strict_and_typed():
    envelope = json.dumps({"schema_version": 1, "proposal": proposal_mapping()})
    proposal = decode_worker_proposal(envelope)
    assert proposal.proposal_id == "proposal-1"
    with pytest.raises(ValueError, match="duplicate JSON key"):
        decode_worker_proposal('{"schema_version":1,"schema_version":1,"proposal":{}}')
    with pytest.raises(ValueError, match="unsupported envelope"):
        decode_worker_proposal('{"schema_version":1,"proposal":{},"command":"ignored"}')
    with pytest.raises(ValueError, match="byte limit"):
        decode_worker_proposal(envelope, max_bytes=1)
