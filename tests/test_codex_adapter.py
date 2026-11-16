from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from agentflow.agents import (AgentCapabilities, ExecutionError, ExecutionPolicy,
                              ExecutionRequest, NormalizedAgentState,
                              normalize_execution_state)
from agentflow.agents.codex import CodexAdapter


HELP = "--json --cd --strict-config --ask-for-approval never on-request"


def adapter(tmp_path, monkeypatch, version="codex-cli 0.166.0", help_text=HELP):
    def fake_run(command, **kwargs):
        output = version if "--version" in command else help_text
        if command[-1] == "--help" and command == [str(Path(sys.executable)), "--help"]:
            output = "--ask-for-approval never on-request"
        if command[-2:] == ["login", "status"]:
            output = "Logged in"
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return CodexAdapter(Path(sys.executable))


def request(tmp_path, policy=None):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return ExecutionRequest("p", "t", 1, "hash", "op", tmp_path, "Do work", policy or ExecutionPolicy())


def test_native_sandbox_setting_is_inherited_without_agentflow_override(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch)
    spec = subject.prepare(request(tmp_path, ExecutionPolicy(approval="never")))
    assert "--sandbox" not in spec.arguments
    assert spec.effective_sandbox == "unknown"


def test_codex_invocation_does_not_set_sandbox_and_keeps_noninteractive_approval(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch)
    spec = subject.prepare(request(tmp_path, ExecutionPolicy(approval="never")))
    assert "--sandbox" not in spec.arguments
    assert "--ask-for-approval" in spec.arguments and "never" in spec.arguments
    assert spec.arguments[:3] == ("--ask-for-approval", "never", "exec")
    assert "--config" not in spec.arguments
    assert spec.cwd == request(tmp_path).workspace.resolve()
    assert spec.arguments[spec.arguments.index("-C") + 1] == str(spec.cwd)
    assert ("AGENTFLOW_MANAGED_EXECUTION", "1") in spec.environment


def test_adapter_does_not_override_codex_project_or_sandbox_configuration(tmp_path, monkeypatch):
    subject = CodexAdapter(Path(sys.executable))
    def fake_run(command, **kwargs):
        if "--version" in command:
            output = "codex-cli 0.166.0"
        elif command == [str(Path(sys.executable)), "--help"]:
            output = "--ask-for-approval never on-request"
        elif command[-2:] == ["login", "status"]:
            output = "Logged in"
        else:
            output = HELP
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    spec = subject.prepare(request(tmp_path, ExecutionPolicy(approval="never")))
    assert "--config" not in spec.arguments
    assert "--sandbox" not in spec.arguments


def test_unknown_version_or_capability_fails_before_spawn(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch, version="codex-cli 0.999.0")
    with pytest.raises(ExecutionError, match="não homologada"):
        subject.prepare(request(tmp_path))
    subject = adapter(tmp_path, monkeypatch, help_text="--json")
    with pytest.raises(ExecutionError, match="capacidades"):
        subject.prepare(request(tmp_path))


def test_codex_adapter_rejects_non_git_workspace(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch)
    invalid = tmp_path / "not-a-repository"
    invalid.mkdir()
    with pytest.raises(ExecutionError, match="workspace Git inválido"):
        subject.prepare(ExecutionRequest("p", "t", 1, "hash", "op", invalid, "Do work",
                                         ExecutionPolicy(approval="never")))


def test_codex_exposes_common_lifecycle_contract_without_claiming_session_recovery(
        tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch)
    availability = subject.discover()
    assert availability.status == "available"
    assert availability.version == "0.166.0"
    assert availability.reason_code == "version_tested"
    capability = subject.capabilities()
    assert isinstance(capability, AgentCapabilities)
    assert capability.structured_result_format == ("jsonl",)
    assert capability.agent_session_tracking is False
    assert capability.agent_session_cancellation is False
    assert capability.agent_session_recovery is False
    assert capability.process_monitoring_owner == "agentflow.runtime.local"
    assert capability.process_cancellation_owner == "agentflow.runtime.local"
    assert capability.process_recovery_owner == "agentflow.state.execution"


def test_codex_experimental_version_is_discovered_but_not_marked_homologated(
        tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch, version="codex-cli 0.167.0")
    availability = subject.discover()
    assert availability.status == "unknown"
    assert availability.reason_code == "version_not_homologated"


def test_codex_0169_version_is_behaviorally_homologated(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch, version="codex-cli 0.169.0")
    availability = subject.discover()
    assert availability.status == "available"
    assert availability.version == "0.169.0"
    assert availability.reason_code == "version_tested"


def test_shared_state_normalization_preserves_ambiguous_outcomes():
    assert normalize_execution_state("prepared") is NormalizedAgentState.READY
    assert normalize_execution_state("running") is NormalizedAgentState.RUNNING
    assert normalize_execution_state("process_succeeded") is NormalizedAgentState.SUCCEEDED
    assert normalize_execution_state("cancelled") is NormalizedAgentState.CANCELLED
    assert normalize_execution_state("timed_out") is NormalizedAgentState.UNKNOWN
    assert normalize_execution_state("interrupted") is NormalizedAgentState.UNKNOWN
    assert normalize_execution_state("future-status") is NormalizedAgentState.UNKNOWN
