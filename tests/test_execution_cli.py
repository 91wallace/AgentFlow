import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time

from agentflow.agents import AgentResult, ProcessIdentity, ProcessResult, ProcessSpec
from agentflow.agents import ExecutionPolicy, ExecutionRequest
from agentflow.agents import codex
from agentflow.cli import main
from agentflow.planning import load_plan
from agentflow.state.execution import ExecutionStore
from test_planning import SPEC, TASKS


class FakeAdapter:
    runs = 0
    def __init__(self):
        pass
    def prepare(self, request):
        return ProcessSpec(Path(sys.executable), (), request.workspace, request.instruction, (),
                          "fake", "test", request.policy.sandbox, request.policy.approval)
    def interpret(self, result):
        return AgentResult("process_succeeded", "completed")


class FakeRuntime:
    runs = 0
    def __init__(self, log_directory):
        self.log_directory = log_directory
    def run(self, spec, attempt_id, policy, on_started, cancel):
        type(self).runs += 1
        directory = self.log_directory / attempt_id
        directory.mkdir(parents=True)
        output = directory / "stdout.log"
        output.write_text(json.dumps({"type": "turn.completed"}) + "\n", encoding="utf-8")
        on_started(ProcessIdentity(123, "marker", "123", 1, attempt_id, "token"))
        return ProcessResult("process_succeeded", 0, output, directory / "stderr.log", "0" * 64, "0" * 64, 40, 0, "ok")


class WaitingRuntime(FakeRuntime):
    started = Event()
    def run(self, spec, attempt_id, policy, on_started, cancel):
        directory = self.log_directory / attempt_id
        directory.mkdir(parents=True)
        on_started(ProcessIdentity(123, "marker", "123", 1, attempt_id, "token"))
        type(self).started.set()
        flag = directory / "cancel.flag"
        deadline = time.monotonic() + 5
        while not flag.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        output = directory / "stdout.log"
        output.write_text("", encoding="utf-8")
        status = "cancelled" if flag.exists() else "process_succeeded"
        return ProcessResult(status, None, output, directory / "stderr.log", "0" * 64, "0" * 64, 0, 0, status)


def project(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    control = root / ".agentflow"
    control.mkdir()
    (control / "spec.toml").write_text(SPEC, encoding="utf-8")
    (control / "tasks.toml").write_text(TASKS, encoding="utf-8")
    with ExecutionStore(control / "state.sqlite3") as store:
        store.import_plan(load_plan(control))
    return root


def test_execute_cli_is_explicit_and_idempotent(tmp_path, monkeypatch, capsys):
    root = project(tmp_path)
    FakeRuntime.runs = 0
    monkeypatch.setattr(codex, "CodexAdapter", FakeAdapter)
    monkeypatch.setattr("agentflow.cli.LocalRuntime", FakeRuntime)
    command = ["execute", "--path", str(root), "--task", "first", "--operation-id", "run-1",
               "--sandbox", "workspace-write", "--approval", "never"]
    assert main(command) == 0
    assert main(command) == 0
    assert FakeRuntime.runs == 1
    with ExecutionStore(root / ".agentflow" / "state.sqlite3") as store:
        assert store.attempt(store.db.execute("SELECT attempt_id FROM execution_attempts").fetchone()[0]).status == "process_succeeded"
    assert "nenhum processo reiniciado" in capsys.readouterr().out


def test_cancel_cli_signals_active_attempt(tmp_path, monkeypatch, capsys):
    root = project(tmp_path)
    WaitingRuntime.started.clear()
    monkeypatch.setattr(codex, "CodexAdapter", FakeAdapter)
    monkeypatch.setattr("agentflow.cli.LocalRuntime", WaitingRuntime)
    base = ["--path", str(root)]
    runner = Thread(target=main, args=( ["execute", *base, "--task", "first", "--operation-id", "run-cancel",
                                        "--sandbox", "workspace-write", "--approval", "never"], ))
    runner.start()
    assert WaitingRuntime.started.wait(5)
    with ExecutionStore(root / ".agentflow" / "state.sqlite3") as store:
        attempt_id = store.db.execute("SELECT attempt_id FROM execution_attempts").fetchone()[0]
    assert main(["cancel", *base, "--attempt", attempt_id]) == 0
    runner.join(5)
    assert not runner.is_alive()
    with ExecutionStore(root / ".agentflow" / "state.sqlite3") as store:
        assert store.attempt(attempt_id).status == "cancelled"


def test_recover_cli_reports_ambiguous_prepared_attempt(tmp_path, capsys):
    root = project(tmp_path)
    control = root / ".agentflow"
    with ExecutionStore(control / "state.sqlite3") as store:
        definition = store.db.execute("SELECT definition_digest FROM task_definitions WHERE task_id='first'").fetchone()[0]
        request = ExecutionRequest("sample", "first", 1, definition, "prepared-run", root, "work",
                                   ExecutionPolicy(sandbox="workspace-write", approval="never"))
        attempt = store.prepare(request, FakeAdapter().prepare(request), "owner")
    assert main(["recover", "--path", str(root)]) == 0
    assert attempt.attempt_id in capsys.readouterr().out
    with ExecutionStore(control / "state.sqlite3") as store:
        assert store.attempt(attempt.attempt_id).status == "interrupted"
