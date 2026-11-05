from pathlib import Path

import pytest

from agentflow.agents import ExecutionError, ExecutionPolicy, ExecutionRequest, ProcessSpec


def test_policy_bounds_and_explicit_access():
    assert ExecutionPolicy().timeout_seconds == 1800
    assert ExecutionPolicy(timeout_seconds=7200, sandbox="danger-full-access", approval="never").sandbox == "danger-full-access"
    for value in (0, 7201, True):
        with pytest.raises(ExecutionError):
            ExecutionPolicy(timeout_seconds=value)


def test_contracts_do_not_require_codex():
    request = ExecutionRequest("p", "t", 1, "digest", "op", Path("."), "work", ExecutionPolicy())
    spec = ProcessSpec(Path("fake"), (), Path("."), request.instruction, (), "fake", "1", "inherited", "inherited")
    assert spec.adapter_name == "fake"
