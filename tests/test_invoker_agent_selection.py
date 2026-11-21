from dataclasses import replace
import json
from pathlib import Path

import pytest

from agentflow.config import AgentChoice, Config, ConfigError, parse_config, resolve_agent
from agentflow.execution_selection import persist_selection, read_selection
from agentflow.agents.factory import SelectedAdapter, final_message
from agentflow.agents import AgentResult, ProcessSpec


def configured():
    return parse_config(b'''schema_version = 2
[agents]
default = "codex"
selection = "invoker-or-default"
[agents.profiles.opencode]
provider = "anthropic"
model = "model-one"
[agents.profiles.claude-code]
provider = "anthropic"
model = "model-two"
''')


def test_precedence_and_profiles_do_not_cross_agent_boundaries():
    config = configured()
    assert resolve_agent(config).agent == "codex"
    assert resolve_agent(config, invoker="opencode").model == "model-one"
    selected = resolve_agent(config, agent="claude-code", invoker="opencode")
    assert (selected.agent, selected.model, selected.source) == ("claude-code", "model-two", "explicit")
    native = resolve_agent(config, invoker="antigravity")
    assert native.agent == "antigravity" and native.provider == native.model == ""
    assert resolve_agent(replace(config, selection="project-default"), invoker="opencode").agent == "codex"
    assert resolve_agent(parse_config(b'agent = "none"')).agent == "codex"


@pytest.mark.parametrize("raw", [
    b'schema_version=2\nagent="codex"', b'schema_version=2\n[agents]\ndefault="other"',
    b'schema_version=2\n[agents]\nselection="guess-parent-process"',
    b'schema_version=2\n[agents.profiles.opencode]\nmodel="only-model"',
    b'schema_version=1\n[agents]\ndefault="codex"',
])
def test_invalid_selection_configuration(raw):
    with pytest.raises(ValueError):
        parse_config(raw)


def test_init_records_invoker_as_default_without_overwriting(tmp_path, capsys):
    from agentflow.cli import main
    from test_foundation import git_repo
    from agentflow.config import load_config
    root = git_repo(tmp_path / "repo")
    assert main(["init", "--path", str(root), "--invoker-agent", "opencode"]) == 0
    path = root / ".agentflow" / "config.toml"
    assert load_config(path).effective_agent == "opencode"
    original = path.read_bytes()
    assert main(["init", "--path", str(root)]) == 0
    assert path.read_bytes() == original


def test_resume_selection_survives_config_changes_and_binds_policy(tmp_path):
    from agentflow.cli import _apl_acceptance_policy_digest
    from test_phase5_planning import SPEC_V2, TASKS_V3
    from test_planning import files
    from agentflow.planning import load_plan
    plan = load_plan(files(tmp_path / ".agentflow", TASKS_V3, SPEC_V2))
    original = AgentChoice("opencode", "anthropic", "model-one", "invoker")
    persist_selection(tmp_path, "cycle", original)
    assert read_selection(tmp_path, "cycle") == original
    changed = AgentChoice("claude-code", "anthropic", "model-two", "invoker")
    with pytest.raises(ConfigError, match="conflict"):
        persist_selection(tmp_path, "cycle", changed)
    assert _apl_acceptance_policy_digest(plan, original) != _apl_acceptance_policy_digest(plan, changed)


def test_run_with_ready_plan_selects_invoker_without_constructing_planner(tmp_path, monkeypatch):
    from agentflow import cli
    from test_foundation import git_repo
    from test_phase5_planning import SPEC_V2, TASKS_V3
    from test_planning import files
    root = git_repo(tmp_path / "repo")
    files(root / ".agentflow", TASKS_V3.replace('versioned_paths = []', 'versioned_paths = ["src"]'), SPEC_V2)
    (root / "src").mkdir()
    def forbidden(*args, **kwargs):
        pytest.fail("ready plan must not construct a planner or discover Codex")
    monkeypatch.setattr("agentflow.apl.codex_planner.CodexCLIPlannerAdapter", forbidden)
    monkeypatch.setattr(cli, "create_adapter", forbidden)
    commands = []
    original_main = cli.main
    monkeypatch.setattr(cli, "main", lambda args: commands.append(args) or 0)
    assert original_main(["run", "--path", str(root), "--plan-dir", ".agentflow",
                          "--invoker-agent", "opencode", "--provider", "anthropic", "--model", "model"]) == 0
    nested = commands[0]
    assert nested[nested.index("--agent") + 1] == "opencode"
    assert nested[nested.index("--provider") + 1] == "anthropic"
    assert nested[nested.index("--model") + 1] == "model"
    assert (root / ".agentflow" / "config.toml").read_text().find('agent = "codex"') >= 0


def test_resume_uses_saved_selection_even_when_caller_and_default_change(tmp_path, monkeypatch):
    from dataclasses import asdict
    from agentflow import cli
    from agentflow.agents import ExecutionError
    from agentflow.apl import SQLiteCycleStore, SupervisedCycleService
    from agentflow.state.execution import ExecutionStore
    from agentflow.planning import load_plan
    from test_foundation import git_repo
    from test_phase5_planning import SPEC_V2, TASKS_V3
    from test_planning import files
    from test_apl_authorized_execution import _grant

    root = git_repo(tmp_path / "repo")
    directory = files(root / ".agentflow", TASKS_V3.replace('versioned_paths = []', 'versioned_paths = ["src"]'), SPEC_V2)
    plan = load_plan(directory)
    path = directory / "state.sqlite3"
    with ExecutionStore(path) as state:
        state.import_plan(plan)
    cycles = SQLiteCycleStore(path)
    cycle = SupervisedCycleService(cycles, path).start(plan, "objective", ("src", "network:unconfined"))
    saved = AgentChoice("opencode", "anthropic", "original-model", "invoker")
    persist_selection(root, cycle.cycle_id, saved)
    (directory / "config.toml").write_text('schema_version=2\n[agents]\ndefault="claude-code"\n')
    binding = {"authorization_mode": "local_execution_contract", "executor_id": "opencode",
               "limits": {"max_jobs": 2, "max_attempts_per_job": 1, "max_seconds": 30},
               "budget_policy": {"estimated_amount": 2, "currency": "USD"}}

    class Runs:
        def __init__(self, *args, **kwargs):
            pass
        def load(self, run_id):
            return {"cycle_id": cycle.cycle_id, "binding_json": json.dumps(binding), "grant_id": "contract"}

    observed = []
    def stop_before_executor(choice):
        observed.append(choice)
        raise ExecutionError("test: stop before dispatch")
    monkeypatch.setattr("agentflow.apl.SQLiteAuthorizedExecutionStore", Runs)
    monkeypatch.setattr(cli, "create_adapter", stop_before_executor)
    assert cli.main(["apl", "run-resume", "--path", str(root), "--run-id", "run",
                     "--invoker-agent", "claude-code"]) == 2
    assert observed == [saved]


@pytest.mark.parametrize("agent", ["antigravity", "opencode", "claude-code"])
@pytest.mark.parametrize("native_default", [False, True])
def test_shared_planner_uses_selected_native_protocol(tmp_path, agent, native_default):
    from agentflow.apl.codex_planner import CodexCLIPlannerAdapter
    from agentflow.agents import ProcessResult
    text = '{"schema_version":1,"tasks":[]}'

    class Native:
        api_version = "1"
        executable = Path("native")
        def prepare(self, request):
            assert request.policy.approval is None
            assert request.model_selection is None if native_default else request.model_selection.agent_id == agent
            return ProcessSpec(self.executable, (), request.workspace, request.instruction, (), agent,
                               "2.2.300", "unknown", "unknown", provider_id="anthropic", model_id="model")
        def interpret(self, result):
            return AgentResult("process_succeeded", "done", effective_provider_id="anthropic",
                               effective_model_id="model", selection_status="confirmed")

    class Runtime:
        def run(self, *args):
            event = ({"event": "result", "result": {"response": text}} if agent == "antigravity" else
                     {"type": "result", "result": text} if agent == "claude-code" else
                     {"type": "text", "part": {"text": text}})
            path = tmp_path / "out.jsonl"
            path.write_text(json.dumps(event))
            error = tmp_path / "err"
            error.write_text("")
            return ProcessResult("process_succeeded", 0, path, error, "a", "b", path.stat().st_size, 0, "done")

    planner = CodexCLIPlannerAdapter(tmp_path / "state.sqlite3", enabled=True,
        agent_adapter=SelectedAdapter(Native(), AgentChoice(agent) if native_default else AgentChoice(agent, "anthropic", "model")), runtime=Runtime())
    assert planner._invoke_structured("Return plan JSON only", "initial-plan") == text


@pytest.mark.parametrize("agent", ["antigravity", "opencode", "claude-code"])
@pytest.mark.parametrize("native_default", [False, True])
def test_two_dependent_jobs_use_native_policy_and_persist_handoff(tmp_path, agent, native_default):
    from test_apl_authorized_execution import _grant, _snapshot, GrantResolver
    from test_apl_codex_execution import WorkspaceResolver, Runtime, Accept
    from test_handoff import handoff
    from agentflow.apl import SQLiteCycleStore
    from agentflow.apl.authorized_execution import SQLiteAuthorizedExecutionStore, AuthorizedExecutionController
    from agentflow.apl.codex_execution import CodexAuthorizedJobExecutor, CodexAcceptanceEvidenceValidator
    from agentflow.apl.contracts import JobProposal
    from agentflow.state.execution import ExecutionStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    choice = AgentChoice(agent, source="invoker") if native_default else AgentChoice(agent, "anthropic", "model", "invoker")
    requests = []

    class Native:
        adapter_id, api_version = agent, "1"
        executable = Path("native-cli")

        def _probe(self, *args):
            return "CLI 2.2.300"

        def prepare(self, request):
            requests.append(request)
            assert request.policy.approval is None
            assert request.model_selection is None if native_default else request.model_selection.agent_id == agent
            return ProcessSpec(self.executable, (), request.workspace, request.instruction, (),
                               agent, "2.2.300", "unknown", "unknown", provider_id="anthropic", model_id="model")

        def interpret(self, result):
            return AgentResult("process_succeeded", "done", effective_provider_id="anthropic",
                               effective_model_id="model", selection_status="confirmed")

    class NativeRuntime(Runtime):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            text = json.dumps(handoff())
            event = ({"event": "result", "result": {"response": text}} if agent == "antigravity" else
                     {"type": "result", "result": text} if agent == "claude-code" else
                     {"type": "text", "part": {"text": text}})
            result.stdout_path.write_text(json.dumps(event), encoding="utf-8")
            return result

    jobs = (JobProposal("first", "First task", (), ("workspace",), ("file exists",)),
            JobProposal("second", "Second task", ("first",), ("workspace",), ("file exists",)))
    snapshot = _snapshot(jobs)
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    cycles = SQLiteCycleStore(path)
    cycles.create(snapshot)
    runs = SQLiteAuthorizedExecutionStore(str(path))
    workspace_resolver = WorkspaceResolver(workspace, "workspace-id")
    pinned = workspace_resolver.resolve(snapshot.cycle_id)
    grant = replace(_grant(snapshot), executor_id=agent, executor_version="2.2.300",
                    workspace_path=str(workspace), workspace_identity_digest=pinned.identity_digest)
    adapter = SelectedAdapter(Native(), choice)
    assert adapter.discover().adapter_id == agent
    executor = CodexAuthorizedJobExecutor(adapter=adapter, runtime=NativeRuntime(tmp_path / "logs"),
        workspace_resolver=workspace_resolver, handoff_resolver=lambda cid: dict(cycles.load(cid).outcomes))
    controller = AuthorizedExecutionController(cycle_store=cycles, execution_store=runs,
        grant_resolver=GrantResolver(grant), executor=executor,
        evidence_validator=CodexAcceptanceEvidenceValidator(Accept()))
    result = controller.start(snapshot.cycle_id, grant.grant_id)
    assert result["status"] == "completed"
    assert '"task_id":"first"' in requests[1].instruction
    assert '"exports":["parse_file","SymbolNode"]' in requests[1].instruction
    assert dict(cycles.load(snapshot.cycle_id).outcomes)["first"].handoff == handoff()
