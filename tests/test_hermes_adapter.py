import json
from pathlib import Path
import subprocess

import pytest

from agentflow.agents import (AdapterDescriptor, AgentRegistry, AntigravityAdapter,
                              ClaudeCodeAdapter, ExecutionError, ExecutionPolicy,
                              ExecutionRequest, HermesAdapter, ModelSelectionRequest,
                              OpenCodeAdapter, ProcessResult)


PROVIDER = "anthropic"
MODEL = "claude-sonnet-4"
_DEFAULT_SELECTION = object()


def request(tmp_path, *, selection=_DEFAULT_SELECTION, policy=None):
    (tmp_path / ".git").mkdir(exist_ok=True)
    selected = (ModelSelectionRequest("hermes", PROVIDER, MODEL, "user:alice")
                if selection is _DEFAULT_SELECTION else selection)
    return ExecutionRequest("sample", "first", 1, "sha256:" + "a" * 64, "hermes-op",
                            tmp_path, "Inspect and implement the requested change",
                            policy or ExecutionPolicy(), model_selection=selected)


def fake_runner(*, version="Hermes Agent v0.30.1", global_help=None, chat_help=None):
    def run(command, **kwargs):
        if command[-1] == "--version":
            output = version
        elif command == [command[0], "--help"]:
            output = global_help or "--safe-mode --ignore-rules"
        elif command[-2:] == ["chat", "--help"]:
            output = chat_help or "--oneshot --query-file --provider --model --format stream-json --toolsets"
        else:
            raise AssertionError("prepare must not invoke Hermes chat")
        return subprocess.CompletedProcess(command, 0, output, "")
    return run


def adapter(tmp_path, runner=None):
    executable = tmp_path / "hermes"
    executable.write_text("test shim; never executed", encoding="utf-8")
    return HermesAdapter(executable, runner=runner or fake_runner())


def process_result(tmp_path, events, *, status="process_succeeded", exit_code=0,
                   selected_provider=PROVIDER, selected_model=MODEL):
    stdout = tmp_path / "stdout.jsonl"
    stderr = tmp_path / "stderr.log"
    stdout.write_text("" if events is None else "\n".join(
        item if isinstance(item, str) else json.dumps(item) for item in events), encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ProcessResult(status, exit_code, stdout, stderr, "sha256:" + "a" * 64,
                         "sha256:" + "b" * 64, stdout.stat().st_size, 0, "test",
                         (("provider_id", selected_provider), ("model_id", selected_model)))


def success_events(model=MODEL, *, provider=None, terminal_code=0):
    init = {"type": "system", "subtype": "init", "model": model, "session_id": "session-safe-id"}
    if provider is not None:
        init["provider"] = provider
    return [
        init,
        {"type": "text", "text": "streamed response"},
        {"type": "tool_use", "name": "read_file", "timestamp": 1},
        {"type": "tool_result", "name": "read_file", "output": "file content", "is_error": False},
        {"type": "result", "session_id": "session-safe-id", "exit_code": terminal_code},
    ]


def test_discovery_preflight_and_request_restricts_features_without_writing_config(tmp_path):
    original = tmp_path / "config.yaml"
    original.write_text("model:\n  default: user-choice\n", encoding="utf-8")
    spec = adapter(tmp_path).prepare(request(tmp_path))
    assert spec.adapter_name == "hermes" and spec.adapter_version == "0.30.1"
    assert spec.provider_id == PROVIDER and spec.model_id == MODEL
    assert spec.arguments == (
        "chat", "--oneshot", "--query-file", "-", "--provider", PROVIDER,
        "--model", MODEL, "--format", "stream-json", "--toolsets", "file",
        "--safe-mode", "--ignore-rules")
    assert spec.stdin == "Inspect and implement the requested change"
    assert spec.environment == ()
    assert spec.effective_sandbox == spec.effective_approval == "unknown"
    assert original.read_text(encoding="utf-8") == "model:\n  default: user-choice\n"


def test_preflight_rejects_missing_executable_version_and_flags(tmp_path):
    with pytest.raises(ExecutionError, match="não encontrado"):
        HermesAdapter(tmp_path / "missing")
    with pytest.raises(ExecutionError, match="não identificável"):
        adapter(tmp_path, fake_runner(version="Hermes development build")).prepare(request(tmp_path))
    with pytest.raises(ExecutionError, match="perfil headless"):
        adapter(tmp_path, fake_runner(chat_help="--oneshot --query-file --provider --model")).prepare(request(tmp_path))
    old_runner = fake_runner(version="Hermes Agent v0.1.0",
                             chat_help="--oneshot --query-file --provider --model")
    with pytest.raises(ExecutionError, match="perfil headless"):
        adapter(tmp_path, old_runner).prepare(request(tmp_path))


@pytest.mark.parametrize("selection", [None,
    ModelSelectionRequest("hermes", "auto", MODEL, "user:alice")])
def test_prepare_requires_explicit_provider_and_model(tmp_path, selection):
    if selection is None:
        selection = None
    with pytest.raises(ExecutionError, match="seleção manual"):
        adapter(tmp_path).prepare(request(tmp_path, selection=selection))


def test_prepare_blocks_sandbox_policies_and_does_not_forward_credentials(tmp_path, monkeypatch):
    secret = "real-looking-test-secret-never-used"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    environments = []
    base = fake_runner()
    def capture(command, **kwargs):
        environments.append(kwargs["env"])
        return base(command, **kwargs)
    with pytest.raises(ExecutionError, match="não atesta enforcement"):
        adapter(tmp_path, capture).prepare(request(
            tmp_path, policy=ExecutionPolicy(sandbox="workspace-write")))
    adapter(tmp_path, capture).prepare(request(tmp_path))
    assert environments and all(secret not in env.values() for env in environments)


def test_interpret_confirms_model_and_provider_when_qualified(tmp_path):
    interpreted = adapter(tmp_path).interpret(process_result(
        tmp_path, success_events(provider=PROVIDER)))
    assert interpreted.process_status == "process_succeeded"
    assert interpreted.selection_status == "confirmed"
    assert interpreted.effective_provider_id == PROVIDER
    assert interpreted.effective_model_id == MODEL
    assert "conclusão requer validação externa" in interpreted.summary


def test_interpret_observes_model_without_inventing_unreported_provider(tmp_path):
    interpreted = adapter(tmp_path).interpret(process_result(tmp_path, success_events(provider=None)))
    assert interpreted.effective_model_id == MODEL
    assert interpreted.effective_provider_id == "unknown"
    assert interpreted.selection_status == "unknown"


def test_interpret_flags_model_identity_divergence(tmp_path):
    interpreted = adapter(tmp_path).interpret(process_result(
        tmp_path, success_events("openai/gpt-test", provider="openai")))
    assert interpreted.selection_status == "mismatch"
    assert interpreted.process_status == "process_succeeded"


def test_documented_init_model_does_not_claim_provider_identity(tmp_path):
    selected = ModelSelectionRequest("hermes", "openrouter", "anthropic/claude-sonnet-4", "user:alice")
    interpreted = adapter(tmp_path).interpret(process_result(
        tmp_path, success_events("anthropic/claude-sonnet-4"),
        selected_provider="openrouter", selected_model="anthropic/claude-sonnet-4"))
    assert interpreted.effective_model_id == "anthropic/claude-sonnet-4"
    assert interpreted.effective_provider_id == "unknown"
    assert interpreted.selection_status == "unknown"


@pytest.mark.parametrize("events", [
    None,
    [{"type": "system", "subtype": "init", "model": f"{PROVIDER}/{MODEL}"}],
    ["{malformed-json"],
    [{"type": "unknown", "payload": "ignored?"}],
    [*success_events(), {"type": "text", "text": "after terminal"}],
    [{"type": "system", "subtype": "init", "model": f"{PROVIDER}/{MODEL}"},
     {"type": "result", "exit_code": 0},
     {"type": "result", "exit_code": 0}],
])
def test_interpret_rejects_incomplete_malformed_or_unexpected_jsonl(tmp_path, events):
    with pytest.raises(ExecutionError):
        adapter(tmp_path).interpret(process_result(tmp_path, events))


def test_interpret_rejects_tool_outside_allowlisted_file_toolset(tmp_path):
    events = [{"type": "system", "subtype": "init", "model": f"{PROVIDER}/{MODEL}"},
              {"type": "tool_use", "name": "terminal"},
              {"type": "result", "exit_code": 0}]
    with pytest.raises(ExecutionError, match="fora do toolset file"):
        adapter(tmp_path).interpret(process_result(tmp_path, events))


@pytest.mark.parametrize("status,code,expected", [
    ("timed_out", None, "timed_out"),
    ("cancelled", 130, "cancelled"),
    ("interrupted", None, "interrupted"),
])
def test_interpret_preserves_runtime_timeout_and_cancellation(tmp_path, status, code, expected):
    interpreted = adapter(tmp_path).interpret(process_result(tmp_path, None, status=status, exit_code=code))
    assert interpreted.process_status == expected


def test_interpret_checks_process_and_terminal_exit_codes_and_missing_credentials(tmp_path):
    inconsistent = adapter(tmp_path).interpret(process_result(
        tmp_path, success_events(terminal_code=1), exit_code=0))
    assert inconsistent.process_status == "process_failed"
    failed = adapter(tmp_path).interpret(process_result(
        tmp_path, success_events(terminal_code=1), status="process_failed", exit_code=1))
    assert failed.process_status == "process_failed"
    interrupted = adapter(tmp_path).interpret(process_result(
        tmp_path, success_events(terminal_code=130), status="process_failed", exit_code=130))
    assert interrupted.process_status == "cancelled"
    no_auth = adapter(tmp_path).interpret(process_result(
        tmp_path, [], status="process_failed", exit_code=1))
    assert no_auth.process_status == "process_failed"


def test_errors_do_not_echo_secret_material_and_registry_keeps_all_adapters(tmp_path):
    secret = "sk-ant-api03-" + "z" * 32
    error_events = [{"type": "system", "subtype": "init", "model": f"{PROVIDER}/{MODEL}"},
                    {"type": "result", "exit_code": 1, "error": secret}]
    failed = adapter(tmp_path).interpret(process_result(
        tmp_path, error_events, status="process_failed", exit_code=1))
    assert secret not in failed.summary

    hermes = adapter(tmp_path)
    inert_runner = lambda *args, **kwargs: None
    existing = [
        (AntigravityAdapter(_fake_executable(tmp_path, "agy"), runner=inert_runner), "antigravity"),
        (ClaudeCodeAdapter(_fake_executable(tmp_path, "claude"), runner=inert_runner), "claude-code"),
        (OpenCodeAdapter(_fake_executable(tmp_path, "opencode"), runner=inert_runner), "opencode"),
        (hermes, "hermes"),
    ]
    registry = AgentRegistry()
    for registered, adapter_id in existing:
        descriptor = AdapterDescriptor(adapter_id, "1", "1", str(registered.executable),
                                       ("windows", "linux"))
        registry.register(descriptor, registered)
    assert registry.resolve("hermes") is hermes
    assert registry.ids() == ("antigravity", "claude-code", "hermes", "opencode")
    assert (AntigravityAdapter.adapter_id, ClaudeCodeAdapter.adapter_id,
            OpenCodeAdapter.adapter_id) == ("antigravity", "claude-code", "opencode")


def _fake_executable(directory: Path, name: str) -> Path:
    executable = directory / name
    executable.write_text("test shim; never executed", encoding="utf-8")
    return executable
