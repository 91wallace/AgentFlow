from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.agents.contracts import (ExecutionError, ExecutionPolicy, ExecutionRequest,
                                        ProcessResult)
from agentflow.agents.factory import SelectedAdapter, create_adapter, final_message
from agentflow.agents.gemini import GeminiAdapter
from agentflow.config import AgentChoice, Config, parse_config, resolve_agent


def adapter(tmp_path):
    executable = tmp_path / "gemini"
    executable.touch()
    def runner(command, **kwargs):
        assert command[1:] in (["--version"], ["--help"])
        return SimpleNamespace(returncode=0, stderr="", stdout=(
            "0.40.0" if command[1] == "--version" else "--output-format stream-json --model"))
    return GeminiAdapter(executable, runner=runner)


def request(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return ExecutionRequest("project", "task", 1, "sha256:" + "a" * 64, "operation",
                            tmp_path, "implemente a tarefa com os gates definidos", ExecutionPolicy())


def result(tmp_path, events, **changes):
    stdout = tmp_path / "stdout.jsonl"
    stdout.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
    return replace(ProcessResult("process_succeeded", 0, stdout, tmp_path / "stderr",
                                  "", "", 0, 0, ""), **changes)


def events():
    return [{"type": "init", "session_id": "test-session", "model": "native-model"},
            {"type": "message", "role": "assistant", "content": "done", "delta": True},
            {"type": "result", "status": "success"}]


def test_native_default_uses_existing_process_contract_and_preserves_permissions(tmp_path):
    wrapper = SelectedAdapter(adapter(tmp_path), AgentChoice("gemini"))
    spec = wrapper.prepare_task(request(tmp_path))
    assert spec.arguments == ("--output-format", "stream-json")
    assert spec.stdin.startswith("implemente")
    assert spec.cwd == tmp_path.resolve()
    assert spec.provider_id == spec.model_id == ""
    assert spec.effective_sandbox == "unknown"
    assert "native-permissions-unmodified" in spec.capabilities
    value = wrapper.collect_result(result(tmp_path, events()))
    assert value.process_status == "process_succeeded"
    assert value.effective_model_id == "unknown"
    assert value.effective_provider_id == value.selection_status == "unknown"
    assert final_message(tmp_path / "stdout.jsonl", "gemini") == "done"


@pytest.mark.parametrize("stream", [[], [{"type": "result", "status": "success"}],
    events() + [{"type": "message"}], events() + [events()[-1]],
    [events()[0], events()[0], events()[-1]], [None], [{"type": "unexpected"}],
    [events()[0], {"type": "result", "status": "partial"}]])
def test_malformed_or_incomplete_stream_is_not_success(tmp_path, stream):
    native = adapter(tmp_path)
    with pytest.raises(ExecutionError):
        native.interpret(result(tmp_path, stream))


@pytest.mark.parametrize("event", [
    {"type": "error", "severity": "error", "message": "failed"},
    {"type": "result", "status": "error"}])
def test_reported_errors_are_conservative_even_with_success_result(tmp_path, event):
    native = adapter(tmp_path)
    stream = events()
    if event["type"] == "result":
        stream[-1] = event
    else:
        stream.insert(1, event)
    assert native.interpret(result(tmp_path, stream)).process_status == "process_failed"


def test_warning_alone_does_not_override_terminal_success(tmp_path):
    native = adapter(tmp_path)
    stream = events()
    stream.insert(1, {"type": "error", "severity": "warning", "message": "retry"})
    assert native.interpret(result(tmp_path, stream)).process_status == "process_succeeded"


def test_recoverable_tool_failure_does_not_replace_independent_acceptance_gates(tmp_path):
    native = adapter(tmp_path)
    stream = events()
    stream.insert(1, {"type": "tool_result", "status": "error"})
    # An executor may run a failing test and then fix it in the same session.
    # Process success still doesn't conclude the task: acceptance is external.
    assert native.interpret(result(tmp_path, stream)).process_status == "process_succeeded"


def test_only_usage_can_identify_model_and_never_attests_provider(tmp_path):
    native = adapter(tmp_path)
    stream = events()
    stream[0]["model"] = "auto"
    stream[-1]["stats"] = {"models": {"observed-model": {"input_tokens": 12}}}
    value = native.interpret(result(tmp_path, stream))
    assert value.effective_model_id == "observed-model"
    assert value.effective_provider_id == value.selection_status == "unknown"
    stream[-1]["stats"]["models"]["other-model"] = {"input_tokens": 3}
    assert native.interpret(result(tmp_path, stream)).effective_model_id == "unknown"


@pytest.mark.parametrize("state", ["cancelled", "interrupted", "timed_out", "process_failed"])
def test_process_failure_never_parses_a_stale_success_log(tmp_path, state):
    native = adapter(tmp_path)
    value = native.interpret(result(tmp_path, events(), status=state, exit_code=1))
    assert value.process_status == state


def test_manual_provider_model_rejected_before_any_inference():
    with pytest.raises(ExecutionError, match="atestação"):
        create_adapter(AgentChoice("gemini", "google", "manual-model"))


def test_configuration_accepts_gemini_and_native_default():
    config = parse_config(b'schema_version = 1\nagent = "gemini"\n')
    assert resolve_agent(config).agent == "gemini"
    assert resolve_agent(Config(), agent="gemini").model == ""


def test_discovery_observes_version_without_authentication_or_model_calls(tmp_path):
    value = adapter(tmp_path).discover()
    assert value.version == "0.40.0"
    assert value.reason_code == "version_observed"


def test_preflight_does_not_expose_probe_error_or_secrets(tmp_path):
    native = adapter(tmp_path)
    native.runner = lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="secret", stderr="token")
    value = native.discover()
    assert value.status == "unknown"
    assert "secret" not in str(value)


def test_headless_rejects_interactive_approval(tmp_path):
    native = adapter(tmp_path)
    with pytest.raises(ExecutionError, match="interativa"):
        native.prepare(replace(request(tmp_path), policy=ExecutionPolicy(approval="on-request")))


def test_shared_planner_reads_gemini_deltas_through_existing_runtime_contract(tmp_path):
    from agentflow.apl.codex_planner import CodexCLIPlannerAdapter
    text = '{"schema_version":1,"tasks":[]}'
    stream = events()
    stream[1]["content"] = text[:10]
    stream.insert(2, {"type": "message", "role": "assistant", "delta": True, "content": text[10:]})
    class Runtime:
        def run(self, spec, attempt_id, policy, on_started, cancel):
            assert spec.adapter_name == "gemini"
            assert spec.stdin == "Return JSON"
            return result(tmp_path, stream)
    planner = CodexCLIPlannerAdapter(tmp_path / "state.sqlite3", enabled=True,
        agent_adapter=SelectedAdapter(adapter(tmp_path), AgentChoice("gemini")), runtime=Runtime())
    assert planner._invoke_structured("Return JSON", "initial-plan") == text
