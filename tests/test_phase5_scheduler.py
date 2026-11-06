from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import threading

import pytest

from agentflow.core.git import GitCoordinator
from agentflow.git.contracts import CheckpointRequest, WorkspaceRequest
from agentflow.git.provider import GitLocalProvider
from agentflow.planning import canonical, digest, load_plan
from agentflow.planning.loader import command_data, gate_data
from agentflow.state import ConflictError
from agentflow.state.validation import ValidationStateStore
from agentflow.validation import (CapabilityState, GateStatus, RuntimeCapabilities,
                                  RuntimeCapability, RuntimeLimits, RuntimeProfile,
                                  RuntimeResult, RunStatus, SnapshotManager,
                                  ValidationRequest, ValidationScheduler,
                                  policy_snapshot)
from test_git_workspace import git, make_repo


def make_validation_project(tmp_path, gates=None, *, tasks_schema=3):
    gates = gates or [
        {"id": "gate-a", "dependencies": []},
        {"id": "gate-b", "dependencies": ["gate-a"]},
        {"id": "gate-independent", "dependencies": [], "required": False},
    ]
    repository = make_repo(tmp_path / "repository")
    executable = str(Path(os.sys.executable).resolve())
    executable_digest = "sha256:" + "a" * 64
    spec = '''schema_version = 2
project_id = "demo"
title = "Demo"
objective = "Check gates"
[[acceptance_criteria]]
id = "spec-ready"
description = "Specification is ready"
[validation]
commands = [{ id = "check", platform = "%s", executable = %s, sha256 = "%s", arguments = ["--version"], working_directory = "snapshot", version = "test" }]
''' % ("windows" if os.name == "nt" else "linux", json.dumps(executable), executable_digest)
    task_text = f'''schema_version = {tasks_schema}
[[tasks]]
id = "task-one"
title = "Task one"
description = "Validation test task"
versioned_paths = ["answer.txt"]
[[tasks.acceptance_criteria]]
id = "works"
description = "The validated task works"
'''
    if tasks_schema >= 4:
        task_text += 'satisfies = ["spec-ready"]\n'
    for gate in gates:
        task_text += f'''[[tasks.gates]]
id = "{gate["id"]}"
version = 1
criterion_ids = ["works"]
command_id = "check"
depends_on = {json.dumps(gate["dependencies"])}
required = {str(gate.get("required", True)).lower()}
'''
    directory = repository / ".agentflow"
    directory.mkdir()
    (directory / "spec.toml").write_text(spec, encoding="utf-8")
    (directory / "tasks.toml").write_text(task_text, encoding="utf-8")
    git(repository, "add", "--", ".agentflow/spec.toml", ".agentflow/tasks.toml")
    git(repository, "commit", "--quiet", "-m", "validation definitions")
    plan = load_plan(directory)
    database = tmp_path / "state.sqlite3"
    store = ValidationStateStore(database)
    store.import_plan(plan)
    provider = GitLocalProvider()
    root, _, head = provider.inspect(repository)
    task = plan.tasks[0]
    definition_digest = store.db.execute(
        "SELECT definition_digest FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=1",
        (plan.specification.project_id, task.id)).fetchone()[0]
    workspace = GitCoordinator(store, provider).create_workspace(
        WorkspaceRequest(plan.specification.project_id, task.id, 1, definition_digest,
                         "workspace-create-1", root, head, task.versioned_paths))
    (workspace.path / "answer.txt").write_text("baseline\n", encoding="utf-8")
    checkpoint = GitCoordinator(store, provider).create_checkpoint(
        CheckpointRequest(workspace.workspace_id, "checkpoint-1", task.versioned_paths,
                          "Test", "test@example.test", "validation checkpoint"))
    return repository, plan, store, provider, workspace, checkpoint


def request_for(plan, store, workspace, checkpoint, *, operation="validation-op-1",
                run_id="run-validation-1", allow_unconfined_network=False, attempt_id=None):
    task = plan.tasks[0]
    selected = set(gate.gate_id for gate in task.gates)
    gates = [gate for gate_id in plan.gate_order if gate_id in selected
             for gate in task.gates if gate.gate_id == gate_id]
    command_ids = {gate.command_id for gate in gates}
    commands = [command_data(command) for command in sorted(
        (command for command in plan.specification.validation_commands
         if command.command_id in command_ids),
        key=lambda item: (item.command_id, item.platform))]
    config = {
        "version": 1,
        "gates": [gate_data(gate) for gate in gates],
        "commands": commands,
        "criteria": [{"id": criterion.id, "description": criterion.description}
                     for criterion in sorted(task.acceptance_criteria, key=lambda item: item.id)
                     if any(criterion.id in gate.criterion_ids for gate in gates)],
    }
    gate_snapshot = canonical(config)
    gate_digest = digest(config)
    policy, policy_digest = policy_snapshot(RuntimeProfile.LOCAL_EXPLICIT,
                                            RuntimeLimits(), required_capabilities=(),
                                            allow_unconfined_network=allow_unconfined_network)
    definition_digest = store.db.execute(
        "SELECT definition_digest FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=1",
        (plan.specification.project_id, task.id)).fetchone()[0]
    return ValidationRequest(
        operation, run_id, plan.specification.project_id, task.id, 1,
        definition_digest, checkpoint.checkpoint_id, checkpoint.commit_oid,
        checkpoint.tree_oid, workspace.workspace_id, gate_snapshot, gate_digest,
        policy, policy_digest, tuple(gate.gate_id for gate in gates),
        RuntimeProfile.LOCAL_EXPLICIT,
        allow_unconfined_network,
        attempt_id=attempt_id,
    )


class FakeRuntime:
    def __init__(self, outcomes=None, *, mutate_snapshot=False, crash=False,
                 include_network=True):
        self.outcomes = outcomes or {}
        self.mutate_snapshot = mutate_snapshot
        self.crash = crash
        self.calls = []
        names = {"argv_only", "timeout", "process_identity", "bounded_output",
                 "private_temp", "snapshot_digest", "network_none"}
        if not include_network:
            names.remove("network_none")
        self.capabilities = RuntimeCapabilities(
            RuntimeProfile.LOCAL_EXPLICIT, "simulated", "1",
            tuple(RuntimeCapability(name, CapabilityState.ENFORCED, effective_value="verified")
                  for name in sorted(names)))

    def inspect_capabilities(self, profile):
        assert profile is RuntimeProfile.LOCAL_EXPLICIT
        return self.capabilities

    def run(self, request, cancellation, on_spawn, on_probe):
        gate = request.output_directory.name
        self.calls.append(gate)
        identity = f"simulated:{gate}:{len(self.calls)}"
        on_probe("simulated-version-probe:" + identity)
        on_spawn(identity)
        if self.crash:
            raise RuntimeError("simulated interruption after spawn")
        if self.mutate_snapshot:
            source = request.cwd / "answer.txt"
            source.chmod(0o644)
            source.write_text("mutated by validator", encoding="utf-8")
        outcome = self.outcomes.get(gate, {})
        if outcome.get("cancel"):
            cancellation.set()
        return RuntimeResult(
            outcome.get("exit_code", 0), outcome.get("timeout", False),
            outcome.get("cancel", False), False, identity,
            request.output_directory / "stdout", request.output_directory / "stderr",
            "sha256:" + "1" * 64, "sha256:" + "2" * 64, 0, 0, False,
            self.capabilities, outcome.get("reason", ""), True,
        )


class FakeValidator:
    validator_id = "simulated-validator"
    version = "1"
    digest = "sha256:" + "3" * 64

    def __init__(self, valid=True):
        self.valid = valid

    def validate(self, result):
        return self.valid, "simulated independent check"


def make_scheduler(tmp_path, store, provider, runtime, validator=None):
    snapshot_parent = tmp_path / "snapshots"
    output_parent = tmp_path / "outputs"
    snapshot_parent.mkdir(exist_ok=True)
    output_parent.mkdir(exist_ok=True)
    return ValidationScheduler(store, provider, runtime, validator or FakeValidator(),
                               SnapshotManager(provider), snapshot_parent, output_parent)


def test_scheduler_runs_selected_dependency_dag_sequentially_and_idempotently(tmp_path):
    repository, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime()
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    request = request_for(plan, store, workspace, checkpoint)
    try:
        result = scheduler.run(request, workspace, checkpoint, plan)
        assert result.status is RunStatus.PASSED
        assert runtime.calls == ["gate-a", "gate-b", "gate-independent"]
        assert all(item.status is GateStatus.PASSED for item in result.results
                   if item.gate_id != "gate-independent")
        assert result.snapshot_digest
        repeated = scheduler.run(request, workspace, checkpoint, plan)
        assert repeated == result
        assert len(runtime.calls) == 3
        assert provider.inspect(repository)[2] != checkpoint.commit_oid
    finally:
        provider.close()
        store.close()


def test_failed_gate_marks_dependents_not_run_and_independent_gate_runs(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime({"gate-a": {"exit_code": 1}})
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    try:
        result = scheduler.run(request_for(plan, store, workspace, checkpoint),
                               workspace, checkpoint, plan)
        states = {item.gate_id: item.status for item in result.results}
        assert result.status is RunStatus.FAILED
        assert states == {"gate-a": GateStatus.FAILED, "gate-b": GateStatus.NOT_RUN,
                          "gate-independent": GateStatus.PASSED}
        assert runtime.calls == ["gate-a", "gate-independent"]
    finally:
        provider.close()
        store.close()


def test_timeout_is_inconclusive_and_dependents_are_not_run(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime({"gate-a": {"timeout": True}})
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    try:
        result = scheduler.run(request_for(plan, store, workspace, checkpoint),
                               workspace, checkpoint, plan)
        states = {item.gate_id: item.status for item in result.results}
        assert result.status is RunStatus.INCONCLUSIVE
        assert states["gate-a"] is GateStatus.INCONCLUSIVE
        assert states["gate-b"] is GateStatus.NOT_RUN
        assert states["gate-independent"] is GateStatus.PASSED
    finally:
        provider.close()
        store.close()


def test_cancellation_is_not_success_and_queued_gates_are_not_run(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime({"gate-a": {"cancel": True}})
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    try:
        result = scheduler.run(request_for(plan, store, workspace, checkpoint),
                               workspace, checkpoint, plan)
        states = {item.gate_id: item.status for item in result.results}
        assert result.status is RunStatus.CANCELLED
        assert states["gate-a"] is GateStatus.CANCELLED
        assert states["gate-b"] is GateStatus.NOT_RUN
        assert states["gate-independent"] is GateStatus.NOT_RUN
        assert runtime.calls == ["gate-a"]
    finally:
        provider.close()
        store.close()


def test_snapshot_mutation_by_gate_cannot_pass_and_original_checkout_stays_untouched(tmp_path):
    repository, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    original_head = provider.inspect(repository)[2]
    original_tracked = (repository / "tracked.txt").read_bytes()
    runtime = FakeRuntime(mutate_snapshot=True)
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    try:
        result = scheduler.run(request_for(plan, store, workspace, checkpoint),
                               workspace, checkpoint, plan)
        states = {item.gate_id: item.status for item in result.results}
        assert states["gate-a"] is GateStatus.FAILED
        assert states["gate-b"] is GateStatus.NOT_RUN
        assert provider.inspect(repository)[2] == original_head
        assert (repository / "tracked.txt").read_bytes() == original_tracked
        assert (workspace.path / "answer.txt").read_text(encoding="utf-8") == "baseline\n"
    finally:
        provider.close()
        store.close()


def test_successful_process_without_independent_validation_does_not_pass(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime()
    scheduler = make_scheduler(tmp_path, store, provider, runtime, FakeValidator(valid=False))
    try:
        result = scheduler.run(request_for(plan, store, workspace, checkpoint),
                               workspace, checkpoint, plan)
        assert result.status is RunStatus.FAILED
        assert result.results[0].status is GateStatus.FAILED
    finally:
        provider.close()
        store.close()


def test_crash_recovery_is_inconclusive_and_never_reexecutes_same_operation(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime(crash=True)
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    request = request_for(plan, store, workspace, checkpoint)
    try:
        with pytest.raises(RuntimeError, match="interruption"):
            scheduler.run(request, workspace, checkpoint, plan)
        assert scheduler.recover_interrupted(request.validation_run_id,
                                             process_stopped=None,
                                             reason="simulated parent crash") == "inconclusive"
        result = scheduler.run(request, workspace, checkpoint, plan)
        assert result.status is RunStatus.INCONCLUSIVE
        assert len(runtime.calls) == 1
        with pytest.raises(ConflictError, match="ativa ou não reconciliada"):
            store.begin_validation(replace(request, operation_id="validation-op-2",
                                           validation_run_id="run-validation-2"),
                                   workspace, checkpoint, plan)
        scheduler.recover_interrupted(request.validation_run_id,
                                      process_stopped=True,
                                      reason="runtime verified process stopped")
        assert store.db.execute("SELECT unresolved FROM validation_runs WHERE validation_run_id=?",
                                (request.validation_run_id,)).fetchone() == (0,)
    finally:
        provider.close()
        store.close()


def test_divergent_operation_id_and_concurrent_run_are_rejected(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    request = request_for(plan, store, workspace, checkpoint)
    runtime = FakeRuntime()
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    barrier = threading.Barrier(2)
    results = []

    def reserve(request_value):
        other = ValidationStateStore(store.path)
        try:
            barrier.wait()
            results.append(other.begin_validation(request_value, workspace, checkpoint, plan))
        except Exception as exc:
            results.append(exc)
        finally:
            other.close()

    try:
        other_request = replace(request, operation_id="validation-op-2",
                                validation_run_id="run-validation-2")
        threads = [threading.Thread(target=reserve, args=(value,))
                   for value in (request, other_request)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert sum(not isinstance(item, Exception) for item in results) == 1
        assert sum(isinstance(item, ConflictError) for item in results) == 1
        winner = next(item for item in (request, other_request)
                      if any(getattr(result, "operation_id", None) == item.operation_id
                             for result in results))
        divergent = replace(winner, requested_by="different requester")
        with pytest.raises(ConflictError, match="parâmetros divergentes"):
            store.begin_validation(divergent, workspace, checkpoint, plan)
    finally:
        provider.close()
        store.close()


def test_required_runtime_capability_unavailable_blocks_before_any_spawn(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime(include_network=False)
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    try:
        result = scheduler.run(request_for(plan, store, workspace, checkpoint),
                               workspace, checkpoint, plan)
        assert result.status is RunStatus.FAILED
        assert runtime.calls == []
        assert all(item.status is GateStatus.NOT_RUN for item in result.results)
        missing = next(item for item in result.results[0].capabilities
                       if item.name == "network_none")
        assert missing.state is CapabilityState.UNAVAILABLE
    finally:
        provider.close()
        store.close()


def test_unconfined_network_requires_explicit_operation_authorization(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    runtime = FakeRuntime(include_network=False)
    scheduler = make_scheduler(tmp_path, store, provider, runtime)
    request = request_for(plan, store, workspace, checkpoint,
                         allow_unconfined_network=True)
    try:
        result = scheduler.run(request, workspace, checkpoint, plan)
        assert result.status is RunStatus.PASSED
        assert runtime.calls
        capability = next(item for item in result.results[0].capabilities
                          if item.name == "network_none")
        assert capability.state is CapabilityState.UNAVAILABLE
    finally:
        provider.close()
        store.close()


def test_selecting_gate_without_its_dependency_is_rejected_before_reservation(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    request = request_for(plan, store, workspace, checkpoint)
    try:
        with pytest.raises(ConflictError, match="incluir dependências"):
            store.begin_validation(replace(request, gate_ids=("gate-b",)),
                                  workspace, checkpoint, plan)
        assert store.db.execute("SELECT count(*) FROM validation_runs").fetchone() == (0,)
    finally:
        provider.close()
        store.close()
