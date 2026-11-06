import json
from pathlib import Path
import subprocess

import pytest

from agentflow.agents import (AntigravityAdapter, ExecutionError, ExecutionPolicy,
                              ExecutionRequest, ModelSelectionRequest, ProcessResult)


MODEL = "gemini-3.8-flash-medium"
HELP = "--input-format --output-format --model --print-timeout --sandbox --dangerously-skip-permissions"


def make_request(tmp_path, *, policy=None, model=MODEL):
    (tmp_path / ".git").mkdir(exist_ok=True)
    selection = ModelSelectionRequest("antigravity", "google-antigravity", model,
                                      "user:alice")
    return ExecutionRequest("sample", "first", 1, "sha256:" + "a" * 64, "op",
                            tmp_path, "Do the explicitly requested work",
                            policy or ExecutionPolicy(), model_selection=selection)


def fake_runner(version="agy v1.4.0", help_text=HELP, models=None):
    def run(command, **kwargs):
        assert "GEMINI_API_KEY" not in kwargs["env"]
        if command[-1] == "--version":
            output = version
        elif command[-1] == "--help":
            output = help_text
        elif command[-1] == "models":
            output = models if models is not None else f"{MODEL}  Gemini model"
        else:
            raise AssertionError("unexpected probe")
        return subprocess.CompletedProcess(command, 0, output, "")
    return run


def adapter(tmp_path, runner=None):
    executable = tmp_path / "agy"
    executable.write_text("test shim; never executed", encoding="utf-8")
    return AntigravityAdapter(executable, runner=runner or fake_runner())


def result(tmp_path, payload, *, stderr=""):
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text(payload, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return ProcessResult("process_succeeded", 0, stdout_path, stderr_path,
                         "sha256:" + "a" * 64, "sha256:" + "b" * 64,
                         stdout_path.stat().st_size, stderr_path.stat().st_size, "process_succeeded",
                         (("adapter_id", "antigravity"), ("provider_id", "google-antigravity"),
                          ("model_id", MODEL)))


def test_prepare_uses_documented_headless_protocol_and_manual_model_without_overrides(tmp_path,
                                                                                       monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-enter-probe")
    spec = adapter(tmp_path).prepare(make_request(tmp_path))
    assert spec.arguments == ("--input-format", "stream-json", "--output-format", "stream-json",
                              "--model", MODEL, "--print-timeout", "1800s")
    assert json.loads(spec.stdin)["message"]["content"] == "Do the explicitly requested work"
    assert "--sandbox" not in spec.arguments
    assert "--dangerously-skip-permissions" not in spec.arguments
    assert spec.effective_sandbox == "unknown"
    assert "sandbox-enforcement-unknown" in spec.capabilities


@pytest.mark.parametrize("runner", [
    fake_runner(version="agy v1.1.9"),
    fake_runner(help_text="--model only"),
    fake_runner(models="other-model  Other model"),
])
def test_unknown_versions_capabilities_or_model_fail_before_spawn(tmp_path, runner):
    with pytest.raises(ExecutionError):
        adapter(tmp_path, runner).prepare(make_request(tmp_path))


def test_implicit_model_or_unmappable_policy_is_rejected(tmp_path):
    subject = adapter(tmp_path)
    request = make_request(tmp_path)
    with pytest.raises(ExecutionError, match="seleção explícita"):
        subject.prepare(ExecutionRequest(request.project_id, request.task_id,
            request.definition_revision, request.definition_digest, request.operation_id,
            request.workspace, request.instruction, request.policy))
    with pytest.raises(ExecutionError, match="não mapeável"):
        subject.prepare(make_request(tmp_path, policy=ExecutionPolicy(sandbox="workspace-write")))


def test_interpret_requires_structured_success_and_matching_effective_model(tmp_path):
    subject = adapter(tmp_path)
    init = {"event": "init", "init": {"model": MODEL, "permission_mode": "request-review"}}
    success = {"event": "result", "result": {"status": "SUCCESS", "response": "done"}}
    parsed = subject.interpret(result(tmp_path, json.dumps(init) + "\n" + json.dumps(success)))
    assert parsed.selection_status == "confirmed"
    assert parsed.effective_model_id == MODEL
    assert parsed.process_status == "process_succeeded"

    mismatch = {"event": "init", "init": {"model": "different-model"}}
    assert subject.interpret(result(tmp_path, json.dumps(mismatch) + "\n" + json.dumps(success))).selection_status == "mismatch"
    with pytest.raises(ExecutionError, match="init/result"):
        subject.interpret(result(tmp_path, json.dumps(init)))


def test_headless_soft_deny_is_not_confused_with_successful_task(tmp_path):
    subject = adapter(tmp_path)
    payload = json.dumps({"event": "init", "init": {"model": MODEL}}) + "\n" + json.dumps(
        {"event": "result", "result": {"status": "SUCCESS", "response": "partial"}})
    parsed = subject.interpret(result(tmp_path, payload, stderr="Tool soft-denied: requires approval"))
    assert parsed.summary == "headless_permission_soft_denied"
    assert parsed.process_status == "process_succeeded"
