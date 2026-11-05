import json
from pathlib import Path
import subprocess

import pytest

from agentflow.agents import (ClaudeCodeAdapter, ExecutionError, ExecutionPolicy,
                              ExecutionRequest, ModelSelectionRequest, ProcessResult)


MODEL = "claude-sonnet-5-5"


def request(tmp_path, *, policy=None):
    (tmp_path / ".git").mkdir(exist_ok=True)
    selection = ModelSelectionRequest("claude-code", "anthropic", MODEL, "user:alice")
    return ExecutionRequest("sample", "first", 1, "sha256:" + "a" * 64, "claude-op",
                            tmp_path, "Do exactly this task", policy or ExecutionPolicy(),
                            model_selection=selection)


def fake_runner(version="2.1.300", auth_method="claude.ai"):
    def run(command, **kwargs):
        if command[-1] == "--version":
            output = f"{version} (Claude Code)"
        elif command[-3:] == ["auth", "status", "--json"]:
            output = json.dumps({"loggedIn": True, "authMethod": auth_method})
        else:
            raise AssertionError("unexpected/non-probe CLI invocation")
        return subprocess.CompletedProcess(command, 0, output, "")
    return run


def adapter(tmp_path, runner=None):
    executable = tmp_path / "claude"
    executable.write_text("test shim; never executed", encoding="utf-8")
    return ClaudeCodeAdapter(executable, runner=runner or fake_runner())


def process_result(tmp_path, events):
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ProcessResult("process_succeeded", 0, stdout, stderr, "sha256:" + "a" * 64,
                         "sha256:" + "b" * 64, stdout.stat().st_size, 0, "ok",
                         (("adapter_id", "claude-code"), ("provider_id", "anthropic"),
                          ("model_id", MODEL)))


def test_prepare_uses_manual_model_noninteractive_and_does_not_write_user_config(tmp_path):
    spec = adapter(tmp_path).prepare(request(tmp_path))
    assert "--model" in spec.arguments and spec.arguments[spec.arguments.index("--model") + 1] == MODEL
    assert "--permission-prompts" in spec.arguments
    assert "none" in spec.arguments
    assert "--no-session-persistence" in spec.arguments
    assert spec.effective_sandbox == "unknown"
    assert spec.provider_id == "anthropic"
    assert spec.stdin == "Do exactly this task"


def test_prepare_rejects_unverified_version_auth_and_generic_sandbox(tmp_path):
    with pytest.raises(ExecutionError, match="anterior"):
        adapter(tmp_path, fake_runner(version="2.1.100")).prepare(request(tmp_path))
    with pytest.raises(ExecutionError, match="não confirmado"):
        adapter(tmp_path, fake_runner(auth_method="third_party")).prepare(request(tmp_path))
    with pytest.raises(ExecutionError, match="apenas comandos shell"):
        adapter(tmp_path).prepare(request(tmp_path, policy=ExecutionPolicy(sandbox="workspace-write")))
    with pytest.raises(ExecutionError, match="incompatível"):
        adapter(tmp_path).prepare(request(tmp_path, policy=ExecutionPolicy(approval="on-request")))


def test_interpret_confirms_model_and_reports_api_retry_without_agent_retry(tmp_path):
    events = [
        {"type": "system", "subtype": "init", "model": MODEL, "tools": ["Read", "Edit"]},
        {"type": "system", "subtype": "api_retry", "attempt": 1, "error": "overloaded"},
        {"type": "result", "subtype": "success", "is_error": False, "result": "finished"},
    ]
    result = adapter(tmp_path).interpret(process_result(tmp_path, events))
    assert result.effective_provider_id == "anthropic"
    assert result.effective_model_id == MODEL
    assert result.selection_status == "confirmed"
    assert "provider_api_retries=1" in result.summary


def test_interpret_rejects_success_without_confirmation_and_never_concludes_task(tmp_path):
    final = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    unknown = adapter(tmp_path).interpret(process_result(tmp_path, [
        {"type": "system", "subtype": "init", "tools": []}, final]))
    assert unknown.selection_status == "unknown"
    mismatch = adapter(tmp_path).interpret(process_result(tmp_path, [
        {"type": "system", "subtype": "init", "model": "other-model"}, final]))
    assert mismatch.selection_status == "mismatch"
    assert mismatch.process_status == "process_succeeded"
