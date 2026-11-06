import json
from pathlib import Path
import subprocess

import pytest

from agentflow.agents import (ExecutionError, ExecutionPolicy, ExecutionRequest,
                              ModelSelectionRequest, OpenCodeAdapter, ProcessResult)


def request(tmp_path, *, model="claude-sonnet-5-5", policy=None):
    (tmp_path / ".git").mkdir(exist_ok=True)
    selection = ModelSelectionRequest("opencode", "anthropic", model, "user:alice")
    return ExecutionRequest("sample", "first", 1, "sha256:" + "a" * 64, "opencode-op",
                            tmp_path, "Read-only review", policy or ExecutionPolicy(),
                            model_selection=selection)


def runner(model="anthropic/claude-sonnet-5-5"):
    def run(command, **kwargs):
        if command[-1] == "--version":
            output = "1.2.3"
        elif command[-1] == "--help" and command[-2:-1] == ["run"]:
            output = "run --format <format> --model <provider/model>"
        elif command[-1] == "--help":
            output = "OpenCode --pure"
        elif command[-2:] == ["models", "anthropic"]:
            output = model + "\n"
        else:
            raise AssertionError("adapter must not start a real agent during prepare")
        return subprocess.CompletedProcess(command, 0, output, "")
    return run


def adapter(tmp_path, fake_runner=None):
    executable = tmp_path / "opencode"
    executable.write_text("test shim; never executed", encoding="utf-8")
    return OpenCodeAdapter(executable, runner=fake_runner or runner())


def result(tmp_path, events):
    stdout = tmp_path / "stdout.jsonl"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ProcessResult("process_succeeded", 0, stdout, stderr, "sha256:" + "a" * 64,
                         "sha256:" + "b" * 64, stdout.stat().st_size, 0, "ok",
                         (("provider_id", "anthropic"), ("model_id", "claude-sonnet-5-5")))


def test_prepare_uses_manual_provider_model_without_config_mutation(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text('{"model":"keep-existing"}', encoding="utf-8")
    spec = adapter(tmp_path).prepare(request(tmp_path))
    assert spec.arguments[:6] == ("--pure", "run", "--format", "json", "--model", "anthropic/claude-sonnet-5-5")
    assert spec.arguments[-1] == "Read-only review"
    assert spec.provider_id == "anthropic" and spec.model_id == "claude-sonnet-5-5"
    assert spec.effective_sandbox == spec.effective_approval == "unknown"
    assert "--auto" not in spec.arguments
    assert "--pure" in spec.arguments
    assert config.read_text(encoding="utf-8") == '{"model":"keep-existing"}'


def test_prepare_blocks_unknown_model_and_requested_sandbox(tmp_path):
    with pytest.raises(ExecutionError, match="não confirmado"):
        adapter(tmp_path, runner(model="anthropic/another-model")).prepare(request(tmp_path))
    with pytest.raises(ExecutionError, match="não atesta"):
        adapter(tmp_path).prepare(request(tmp_path, policy=ExecutionPolicy(sandbox="workspace-write")))


def test_prepare_requires_observed_headless_capabilities(tmp_path):
    def unsupported(command, **kwargs):
        if command[-1] == "--version":
            output = "1.2.3"
        elif command[-1] == "--help" and command[-2:-1] == ["run"]:
            output = "run supports --model but not json output"
        elif command[-1] == "--help":
            output = "OpenCode --pure"
        else:
            raise AssertionError("unsupported capabilities must fail before model lookup")
        return subprocess.CompletedProcess(command, 0, output, "")
    with pytest.raises(ExecutionError, match="capabilities headless"):
        adapter(tmp_path, unsupported).prepare(request(tmp_path))


def test_interpret_only_confirms_structured_effective_model(tmp_path):
    confirmed = adapter(tmp_path).interpret(result(tmp_path, [
        {"type": "message.updated", "info": {"providerID": "anthropic", "modelID": "claude-sonnet-5-5"}},
        {"type": "text", "text": "done"},
    ]))
    assert confirmed.selection_status == "confirmed"
    assert confirmed.effective_model_id == "claude-sonnet-5-5"
    unknown = adapter(tmp_path).interpret(result(tmp_path, [{"type": "text", "text": "success"}]))
    assert unknown.selection_status == "unknown"
    assert unknown.process_status == "process_succeeded"


def test_interpret_rejects_divergent_identity_and_structured_error(tmp_path):
    mismatch = adapter(tmp_path).interpret(result(tmp_path, [
        {"type": "message.updated", "info": {"providerID": "openai", "modelID": "gpt-x"}}
    ]))
    assert mismatch.selection_status == "mismatch"
    failed = adapter(tmp_path).interpret(result(tmp_path, [
        {"type": "message.updated", "info": {"providerID": "anthropic", "modelID": "claude-sonnet-5-5"}},
        {"type": "session.error", "error": "redacted"}
    ]))
    assert failed.process_status == "process_failed"
