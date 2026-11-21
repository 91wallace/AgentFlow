from dataclasses import replace
import importlib
import json

import pytest

from agentflow.config import AgentChoice, Config, resolve_agent
from agentflow.agents.factory import SelectedAdapter
from agentflow.execution_selection import persist_selection, read_selection


@pytest.mark.parametrize("agent,module_name,request_name", [
    ("antigravity", "test_antigravity_adapter", "make_request"),
    ("opencode", "test_opencode_adapter", "request"),
    ("claude-code", "test_claude_code_adapter", "request"),
])
def test_native_default_prepare_and_results_without_profile(tmp_path, agent, module_name, request_name):
    fixture = importlib.import_module(module_name)
    request = replace(getattr(fixture, request_name)(tmp_path), model_selection=None)
    native = fixture.adapter(tmp_path)
    original_runner = native.runner
    def runner(command, **kwargs):
        assert "models" not in command  # No catalogue required for native defaults.
        return original_runner(command, **kwargs)
    native.runner = runner
    choice = resolve_agent(Config(), invoker=agent)
    wrapper = SelectedAdapter(native, choice)
    spec = wrapper.prepare_task(request)
    assert "--model" not in spec.arguments
    assert spec.provider_id == spec.model_id == ""
    assert "native-default-model" in spec.capabilities
    if agent == "antigravity":
        events = [{"event": "init", "init": {}}, {"event": "result", "result": {"status": "SUCCESS"}}]
        result = fixture.result(tmp_path, "\n".join(json.dumps(item) for item in events))
        failed = [{"event": "init", "init": {}}, {"event": "result", "result": {"status": "ERROR"}}]
        failure = lambda: fixture.result(tmp_path, "\n".join(json.dumps(item) for item in failed))
    elif agent == "claude-code":
        events = [{"type": "system", "subtype": "init"}, {"type": "result", "subtype": "success"}]
        result = fixture.process_result(tmp_path, events)
        failure = lambda: fixture.process_result(tmp_path, [{"type": "system", "subtype": "init"},
                                                   {"type": "result", "is_error": True}])
    else:
        result = fixture.result(tmp_path, [{"type": "text", "part": {"text": "done"}}])
        failure = lambda: fixture.result(tmp_path, [{"type": "error"}])
    result = replace(result, request_metadata=(("provider_id", ""), ("model_id", "")))
    parsed = wrapper.collect_result(result)
    assert parsed.process_status == "process_succeeded"
    assert parsed.selection_status == "unknown"
    assert parsed.effective_model_id == "unknown"
    assert parsed.effective_provider_id == "unknown"
    parsed_failure = wrapper.collect_result(replace(failure(), request_metadata=()))
    assert parsed_failure.process_status == "process_failed"


def test_native_default_intent_is_persisted_without_inventing_model(tmp_path):
    (tmp_path / ".agentflow").mkdir()
    choice = AgentChoice("antigravity")
    persist_selection(tmp_path, "cycle", choice)
    assert read_selection(tmp_path, "cycle") == choice
    assert choice.provider == choice.model == ""
