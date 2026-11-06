"""User-facing CLI; no agent execution commands exist in phases 1 or 2."""

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys

from . import __version__
from .config import ConfigError, load_config
from .environment.doctor import diagnose
from .init import InitError, initialize, repository_root
from .planning import PlanError, load_plan
from .state import StateError
from .state.inspection import inspect_state
from .state.execution import ExecutionStore
from .agents import ExecutionError, ExecutionPolicy, ExecutionRequest, ProcessResult
from .agents.codex import CodexAdapter
from .runtime.local import LocalRuntime, SpawnFailure
from .runtime.local import LocalRuntime as _ProcessController
from .planning import digest
from .planning.loader import task_data
from .git.contracts import CheckpointRequest, GitError, WorkspaceRequest
from .git.provider import GitLocalProvider
from .core.git import GitCoordinator
from .state.git import GitStateStore
from .state.contracts import Transition
from .state.validation import ValidationStateStore
from .state.audit import inspect_phase7_database
from .validation import (DockerRuntime, LocalProcessRuntime, RuntimeLimits,
                         RuntimeProfile, SnapshotManager, ValidationRequest,
                         ValidationScheduler, policy_snapshot,
                         StructuredGateValidatorFactory,
                         cleanup_validation_storage)
from .planning import canonical
from .planning.loader import command_data, gate_data


class _NeverCancel:
    def is_set(self) -> bool:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--version", action="version", version=f"agentflow {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "plan", "state", "audit", "execute", "cancel", "recover", "cleanup", "workspaces", "git-recover", "checkpoint", "verify-no-changes", "complete", "validate", "validation-state", "validation-cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, default=Path.cwd(), help="caminho dentro do repositório Git")
        if name == "audit":
            command.add_argument("--format", choices=("human", "json"), default="human")
        if name == "execute":
            command.add_argument("--task", required=True)
            command.add_argument("--operation-id", required=True)
            command.add_argument("--sandbox", required=True, choices=("read-only", "workspace-write", "danger-full-access"))
            command.add_argument("--approval", required=True, choices=("never", "on-request"))
            command.add_argument("--timeout", type=int, default=1800)
            command.add_argument("--retry-reason", default="", help="decisão humana de repetição após reconciliar o workspace")
        if name == "validate":
            command.add_argument("--task", required=True)
            command.add_argument("--gate", action="append", required=True,
                                 help="ID do gate; informe também suas dependências")
            command.add_argument("--operation-id", required=True)
            command.add_argument("--runtime", required=True,
                                 choices=("local-explicit", "docker-isolated"))
            command.add_argument("--timeout", type=int, default=600)
            command.add_argument("--allow-unconfined-network", action="store_true")
        if name == "cancel":
            command.add_argument("--attempt", required=True)
        if name == "cleanup":
            command.add_argument("--logs", action="store_true", required=True)
            command.add_argument("--older-than-days", type=int, default=30)
        if name == "checkpoint":
            command.add_argument("--task", required=True)
            command.add_argument("--operation-id", required=True)
            command.add_argument("--file", action="append", required=True)
            command.add_argument("--author", required=True)
            command.add_argument("--email", required=True)
            command.add_argument("--message", required=True)
        if name in ("complete", "verify-no-changes"):
            command.add_argument("--task", required=True)
            command.add_argument("--operation-id", required=True)
        if name == "complete":
            command.add_argument("--expected-revision", type=int, required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "init":
            root, created = initialize(args.path)
            print(f"{root}: configuração {'criada' if created else 'existente; sem alterações'}")
            return 0
        root = repository_root(args.path.resolve())
        if args.command == "audit":
            report = inspect_phase7_database(root / ".agentflow" / "state.sqlite3")
            if args.format == "json":
                print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            else:
                labels = {
                    "specification_revisions": "Specification revisions",
                    "plan_revisions": "Plan revisions",
                    "requirement_coverage": "Requirement coverage",
                    "change_impacts": "Change impacts",
                    "catalog": "Model catalog",
                    "routing_decisions": "Routing decisions (preview only)",
                    "budget_accounts": "Budget accounts",
                    "budget_reservations": "Budget reservations",
                    "budget_ledger": "Budget usage ledger",
                    "escalation_proposals": "Escalation proposals (not dispatched)",
                    "task_states": "Task states",
                    "evidence_links": "Evidence and checkpoint links",
                    "audit_events": "Audit events",
                    "unknowns_and_pending": "Unknowns and pending work",
                    "unavailable_sections": "Unavailable schema sections",
                }
                print(f"SQLite schema v{report['schema_version']}; integrity={report['integrity']['integrity_check']}; "
                      f"foreign-key violations={report['integrity']['foreign_key_violations']}")
                for key, label in labels.items():
                    print(f"\n{label}:")
                    rows = report[key]
                    if not rows:
                        print("  (none)")
                    for item in rows:
                        print("  " + json.dumps(item, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "plan":
            plan = load_plan(root / ".agentflow")
            print(f"{plan.specification.project_id}: {len(plan.tasks)} tarefas; ordem: {', '.join(plan.order)}")
            return 0
        if args.command == "state":
            plan = load_plan(root / ".agentflow")
            for task in inspect_state(root / ".agentflow" / "state.sqlite3", plan):
                print(f"{task.task_id}: {task.status} (revisão {task.definition_revision})")
            return 0
        if args.command == "validation-state":
            state_path = root / ".agentflow" / "state.sqlite3"
            if not state_path.is_file():
                raise StateError("banco operacional de validação não existe")
            with ValidationStateStore(state_path) as store:
                rows = store.db.execute(
                    "SELECT validation_run_id,status,task_id,definition_revision,checkpoint_operation_id "
                    "FROM validation_runs ORDER BY created_at,validation_run_id").fetchall()
                for run_id, status, task_id, revision, checkpoint_operation in rows:
                    checkpoint = store.db.execute(
                        "SELECT checkpoint_id,commit_oid,tree_oid FROM git_checkpoints WHERE operation_id=?",
                        (checkpoint_operation,)).fetchone()
                    print(f"{run_id}: {status} task={task_id} revision={revision} "
                          f"checkpoint={checkpoint[0] if checkpoint else 'unknown'}")
                    for gate_id, gate_status, validator, version, digest_value in store.db.execute(
                            "SELECT gate_id,status,validator_id,validator_version,validator_digest "
                            "FROM validation_gate_results WHERE validation_run_id=? ORDER BY gate_id",
                            (run_id,)):
                        print(f"  {gate_id}: {gate_status} validator={validator}@{version} {digest_value}")
            return 0
        if args.command == "validation-cleanup":
            state_path = root / ".agentflow" / "state.sqlite3"
            if not state_path.is_file():
                raise StateError("banco operacional de validação não existe")
            with ValidationStateStore(state_path) as store:
                removed = cleanup_validation_storage(
                    store, root / ".agentflow" / "snapshots",
                    root / ".agentflow" / "validation-output",
                    root / ".agentflow" / "artifacts")
            print(f"Payloads locais removidos: {len(removed)}")
            for item in removed:
                print(f"  {item}")
            return 0
        if args.command == "workspaces":
            with GitStateStore(root / ".agentflow" / "state.sqlite3") as store:
                rows = store.db.execute("SELECT workspace_id,task_id,definition_revision,branch,status,worktree_path,current_head_oid FROM workspaces ORDER BY created_at,workspace_id").fetchall()
                for row in rows:
                    print(f"{row[0]} {row[1]} r{row[2]} {row[3]} {row[4]} {row[5]} HEAD={row[6]}")
            return 0
        if args.command == "git-recover":
            with GitStateStore(root / ".agentflow" / "state.sqlite3") as store, GitLocalProvider() as provider:
                for operation_id, status in GitCoordinator(store, provider).recover():
                    print(f"{operation_id}: {status}")
            return 0
        if args.command in ("checkpoint", "verify-no-changes", "complete"):
            with GitStateStore(root / ".agentflow" / "state.sqlite3") as store, GitLocalProvider() as provider:
                coordinator = GitCoordinator(store, provider)
                states = {item.task_id: item for item in store.list_tasks()}
                state = states.get(args.task)
                if state is None:
                    raise StateError(f"tarefa desconhecida: {args.task}")
                if args.command in ("checkpoint", "verify-no-changes"):
                    workspace = store.workspace_for_task(store.db.execute("SELECT project_id FROM projects").fetchone()[0], args.task, state.definition_revision)
                    if workspace is None:
                        raise StateError("workspace ativo não encontrado para a revisão vigente")
                    if args.command == "checkpoint":
                        checkpoint = coordinator.create_checkpoint(CheckpointRequest(workspace.workspace_id, args.operation_id,
                            tuple(args.file), args.author, args.email, args.message))
                        print(f"Checkpoint verificado: {checkpoint.commit_oid}")
                    else:
                        coordinator.verify_no_changes(workspace.workspace_id, args.operation_id)
                        print(f"Workspace sem alterações verificadas: {workspace.workspace_id}")
                else:
                    result = coordinator.complete_task(Transition(args.task, "completed", args.operation_id,
                                                                  args.expected_revision))
                    print(f"Tarefa concluída: {result.task_id} (revisão {result.revision})")
            return 0
        if args.command == "cancel":
            log_root = root / ".agentflow" / "runs"
            with ExecutionStore(root / ".agentflow" / "state.sqlite3") as store:
                attempt = store.attempt(args.attempt)
                if attempt.status != "running":
                    print(f"Tentativa {args.attempt}: estado {attempt.status}; sem cancelamento pendente")
                    return 0
                directory = (log_root / args.attempt).resolve()
                if directory.parent != log_root.resolve() or not directory.is_dir():
                    raise StateError("diretório da tentativa inválido")
                store.request_cancel(args.attempt)
                flag = directory / "cancel.flag"
                if not flag.exists():
                    temporary = directory / "cancel.flag.tmp"
                    temporary.write_text("cancel requested\n", encoding="utf-8")
                    os.replace(temporary, flag)
                identity = store.process_identity(args.attempt)
                cancelled = False
                if identity is not None:
                    cancelled = _ProcessController.cancel_orphan(identity)
                print(f"Cancelamento {'sinalizado' if cancelled else 'solicitado'}: {args.attempt}")
                return 0
        if args.command == "recover":
            with ExecutionStore(root / ".agentflow" / "state.sqlite3") as store:
                recovered = store.reconcile_interrupted()
                live = store.db.execute("SELECT attempt_id FROM execution_attempts WHERE status IN ('prepared','running') ORDER BY prepared_at").fetchall()
                print(f"Interrompidas para revisão: {', '.join(recovered) or 'nenhuma'}")
                print(f"Ativas com identidade possivelmente viva: {', '.join(row[0] for row in live if row[0] not in recovered) or 'nenhuma'}")
                return 0
        if args.command == "cleanup":
            with ExecutionStore(root / ".agentflow" / "state.sqlite3") as store:
                removed = store.cleanup_logs(root / ".agentflow" / "runs", args.older_than_days)
                print(f"Logs removidos: {len(removed)}")
                return 0
        if args.command == "execute":
            plan = load_plan(root / ".agentflow")
            task = next((item for item in plan.tasks if item.id == args.task), None)
            if task is None:
                raise StateError(f"tarefa desconhecida: {args.task}")
            if args.timeout < 1 or args.timeout > 7200:
                raise ExecutionError("timeout deve estar entre 1 e 7200 segundos")
            definitions = {item.id: digest({"task": task_data(item), "spec_digest": plan.spec_digest}) for item in plan.tasks}
            workspace = root.resolve()
            workspace_id = ""
            if task.versioned_paths:
                with GitLocalProvider() as provider, GitStateStore(root / ".agentflow" / "state.sqlite3") as git_store:
                    git_store.import_plan(plan)
                    top, common, head = provider.inspect(root)
                    current = next(item for item in git_store.list_tasks() if item.task_id == task.id)
                    workspace_request = WorkspaceRequest(plan.specification.project_id, task.id, current.definition_revision,
                        definitions[task.id], "workspace-" + hashlib.sha256(args.operation_id.encode("utf-8")).hexdigest()[:32],
                        top, head, task.versioned_paths)
                    managed = GitCoordinator(git_store, provider).create_workspace(workspace_request)
                    workspace = managed.path
                    workspace_id = managed.workspace_id
            request = ExecutionRequest(plan.specification.project_id, task.id, 1, definitions[task.id],
                                       args.operation_id, workspace,
                                       f"{task.title}\n\n{task.description}\n\nAcceptance criteria:\n" +
                                       "\n".join(f"- {criterion.id}: {criterion.description}" for criterion in task.acceptance_criteria),
                                       ExecutionPolicy(args.timeout, sandbox=args.sandbox, approval=args.approval),
                                       tuple((criterion.id, criterion.description) for criterion in task.acceptance_criteria),
                                       workspace_id)
            adapter = CodexAdapter()
            spec = adapter.prepare(request)  # All capability checks happen before reservation.
            state_path = root / ".agentflow" / "state.sqlite3"
            with ExecutionStore(state_path) as store:
                store.import_plan(plan)
                current = next(item for item in store.list_tasks() if item.task_id == task.id)
                request = ExecutionRequest(request.project_id, request.task_id, current.definition_revision,
                                           definitions[task.id], request.operation_id, request.workspace,
                                           request.instruction, request.policy, request.acceptance_criteria,
                                           request.workspace_id)
                attempt = store.prepare(request, spec, f"pid:{os.getpid()}", retry_reason=args.retry_reason)
                if not attempt.created:
                    print(f"Tentativa existente {attempt.attempt_id}: {attempt.status}; nenhum processo reiniciado.")
                    return 0 if attempt.status == "process_succeeded" else 1
                runtime = LocalRuntime(root / ".agentflow" / "runs")
                try:
                    result = runtime.run(spec, attempt.attempt_id, request.policy,
                                         lambda identity: store.started(attempt.attempt_id, identity), _NeverCancel())
                except SpawnFailure as exc:
                    run_directory = root / ".agentflow" / "runs" / attempt.attempt_id
                    result = ProcessResult("process_failed", None, run_directory / "stdout.log", run_directory / "stderr.log", hashlib.sha256(b"").hexdigest(),
                                           hashlib.sha256(b"").hexdigest(), 0, 0, f"spawn_failed:{type(exc).__name__}")
                except Exception as exc:
                    run_directory = root / ".agentflow" / "runs" / attempt.attempt_id
                    result = ProcessResult("interrupted", None, run_directory / "stdout.log", run_directory / "stderr.log", hashlib.sha256(b"").hexdigest(),
                                           hashlib.sha256(b"").hexdigest(), 0, 0, f"runtime_interrupted:{type(exc).__name__}")
                store.finish(attempt.attempt_id, result)
                if result.status == "process_succeeded":
                    try:
                        adapter.interpret(result)
                    except ExecutionError as exc:
                        store.set_validation(attempt.attempt_id, False, (), str(exc))
                print(f"Tentativa {attempt.attempt_id}: {result.status}; tarefa exige validação/evidências.")
                return 0 if result.status == "process_succeeded" else 1
        if args.command == "validate":
            if not 1 <= args.timeout <= 3600:
                raise StateError("timeout de gate deve estar entre 1 e 3600 segundos")
            plan = load_plan(root / ".agentflow")
            task = next((item for item in plan.tasks if item.id == args.task), None)
            if task is None or not task.gates:
                raise StateError("tarefa desconhecida ou sem gates definidos")
            selected = set(args.gate)
            known = {gate.gate_id: gate for gate in task.gates}
            if not selected <= known.keys() or not selected:
                raise StateError("seleção contém gate desconhecido")
            missing = {dependency for gate_id in selected
                       for dependency in known[gate_id].depends_on} - selected
            if missing:
                raise StateError("informe explicitamente as dependências: " + ", ".join(sorted(missing)))
            selected_gates = [known[gate_id] for gate_id in plan.gate_order
                              if gate_id in selected]
            command_ids = {gate.command_id for gate in selected_gates}
            commands = [command for command in plan.specification.validation_commands
                        if command.command_id in command_ids]
            criteria = [{"id": item.id, "description": item.description}
                        for item in sorted(task.acceptance_criteria, key=lambda value: value.id)
                        if any(item.id in gate.criterion_ids for gate in selected_gates)]
            gate_config = {"version": 1,
                           "gates": [gate_data(gate) for gate in selected_gates],
                           "commands": [command_data(item) for item in sorted(
                               commands, key=lambda value: (value.command_id, value.platform))],
                           "criteria": criteria}
            gate_snapshot = canonical(gate_config)
            profile = RuntimeProfile(args.runtime)
            limits = RuntimeLimits(timeout_seconds=args.timeout)
            policy_json, policy_digest = policy_snapshot(
                profile, limits, allow_unconfined_network=args.allow_unconfined_network)
            run_id = "run-" + hashlib.sha256(args.operation_id.encode("utf-8")).hexdigest()[:32]
            state_path = root / ".agentflow" / "state.sqlite3"
            with GitLocalProvider() as provider, ValidationStateStore(state_path) as store:
                store.import_plan(plan)
                state = next((item for item in store.list_tasks() if item.task_id == args.task), None)
                workspace = (store.workspace_for_task(plan.specification.project_id, args.task,
                                                      state.definition_revision) if state else None)
                if workspace is None:
                    raise StateError("workspace isolado da tarefa não encontrado")
                checkpoint = store.latest_checkpoint(workspace.workspace_id)
                if checkpoint is None or not checkpoint.verified:
                    raise StateError("checkpoint verificado obrigatório")
                definition_digest = store.db.execute(
                    "SELECT definition_digest FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=?",
                    (plan.specification.project_id, args.task,
                     state.definition_revision)).fetchone()[0]
                request = ValidationRequest(
                    args.operation_id, run_id, plan.specification.project_id, args.task,
                    state.definition_revision, definition_digest, checkpoint.checkpoint_id,
                    checkpoint.commit_oid, checkpoint.tree_oid, workspace.workspace_id,
                    gate_snapshot, digest(gate_config), policy_json, policy_digest,
                    tuple(gate.gate_id for gate in selected_gates), profile,
                    args.allow_unconfined_network, requested_by=os.environ.get(
                        "USERNAME", os.environ.get("USER", "local-user")))
                runtime = (DockerRuntime() if profile is RuntimeProfile.DOCKER_ISOLATED
                           else LocalProcessRuntime())
                scheduler = ValidationScheduler(
                    store, provider, runtime, StructuredGateValidatorFactory(task),
                    SnapshotManager(provider), root / ".agentflow" / "snapshots",
                    root / ".agentflow" / "validation-output")
                result = scheduler.run(request, workspace, checkpoint, plan)
                print(f"Validação {result.validation_run_id}: {result.status.value}")
                for gate in result.results:
                    print(f"{gate.gate_id}: {gate.status.value} ({gate.validator_id}@{gate.validator_version})")
                return 0 if result.status.value == "passed" else 1
        config = load_config(root / ".agentflow" / "config.toml")
        checks = diagnose(config)
        for check in checks:
            requirement = "obrigatório" if check.required else "opcional"
            print(f"{check.name}: {check.status} ({requirement}) — {check.detail}")
        return 1 if any(c.required and c.status != "ok" for c in checks) else 0
    except (ConfigError, InitError, PlanError, StateError, ExecutionError, GitError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
