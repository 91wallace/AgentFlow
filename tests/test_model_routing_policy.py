import pytest

from agentflow.agents import ExecutionError, parse_model_routing_policy_toml


VALID = '''schema_version = 1
[policy]
id = "future-routing"
enabled = true
required_capabilities = ["structured-output"]
excluded_failure_classes = ["environment", "authentication", "quota", "infrastructure"]
escalation_evidence_ids = ["criterion-failed"]
max_attempts = 3
max_tokens = 50000
max_latency_seconds = 1200
max_cost = "2.50"
currency = "USD"
authorization_required = true
'''


def test_policy_is_versioned_digestable_and_never_executable_in_phase6():
    policy = parse_model_routing_policy_toml(VALID)
    assert policy.max_attempts == 3
    assert policy.executable is False
    assert policy.policy_digest.startswith("sha256:")
    assert policy.snapshot() == policy.snapshot()
    assert not hasattr(policy, "evaluate")


@pytest.mark.parametrize("change", [
    lambda text: text.replace("schema_version = 1", "schema_version = 2"),
    lambda text: text.replace("max_attempts = 3", "max_attempts = 4"),
    lambda text: text.replace('authorization_required = true', 'authorization_required = false'),
    lambda text: text.replace('max_cost = "2.50"', "max_cost = 2.5"),
    lambda text: text.replace('id = "future-routing"', 'unexpected = "x"\nid = "future-routing"'),
    lambda text: text.replace('"infrastructure"', '"unknown-error"'),
])
def test_invalid_or_unverifiable_policy_is_rejected(change):
    with pytest.raises(ExecutionError):
        parse_model_routing_policy_toml(change(VALID))
