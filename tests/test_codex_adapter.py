from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from agentflow.agents import ExecutionError, ExecutionPolicy, ExecutionRequest
from agentflow.agents.codex import CodexAdapter


HELP = "--sandbox read-only workspace-write danger-full-access --config --json --cd --strict-config --ask-for-approval never on-request"


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


def test_inherited_settings_fail_before_spawn(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch)
    with pytest.raises(ExecutionError, match="não verificável"):
        subject.prepare(request(tmp_path))


def test_explicit_full_access_and_noninteractive_approval(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch)
    spec = subject.prepare(request(tmp_path, ExecutionPolicy(sandbox="danger-full-access", approval="never")))
    assert "danger-full-access" in spec.arguments
    assert "--ask-for-approval" in spec.arguments and "never" in spec.arguments


def test_unknown_version_or_capability_fails_before_spawn(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch, version="codex-cli 0.999.0")
    with pytest.raises(ExecutionError, match="não homologada"):
        subject.prepare(request(tmp_path))
    subject = adapter(tmp_path, monkeypatch, help_text="--json")
    with pytest.raises(ExecutionError, match="capacidades"):
        subject.prepare(request(tmp_path))


def test_partial_policy_is_unverifiable(tmp_path, monkeypatch):
    subject = adapter(tmp_path, monkeypatch)
    with pytest.raises(ExecutionError, match="não verificável"):
        subject.prepare(request(tmp_path, ExecutionPolicy(sandbox="read-only")))
