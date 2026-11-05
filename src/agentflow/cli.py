"""User-facing CLI; no agent execution commands exist in phases 1 or 2."""

import argparse
from dataclasses import asdict
import hashlib
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


class _NeverCancel:
    def is_set(self) -> bool:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--version", action="version", version=f"agentflow {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "plan", "state", "execute", "cancel", "recover", "cleanup", "workspaces", "git-recover", "checkpoint", "verify-no-changes", "complete"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, default=Path.cwd(), help="caminho dentro do repositório Git")
        if name == "execute":
            command.add_argument("--task", required=True)
            command.add_argument("--operation-id", required=True)
            command.add_argument("--sandbox", required=True, choices=("read-only", "workspace-write", "danger-full-access"))
            command.add_argument("--approval", required=True, choices=("never", "on-request"))
            command.add_argument("--timeout", type=int, default=1800)
            command.add_argument("--retry-reason", default="", help="decisão humana de repetição após reconciliar o workspace")
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
        if args.command == "plan":
            plan = load_plan(root / ".agentflow")
            print(f"{plan.specification.project_id}: {len(plan.tasks)} tarefas; ordem: {', '.join(plan.order)}")
            return 0
        if args.command == "state":
            plan = load_plan(root / ".agentflow")
            for task in inspect_state(root / ".agentflow" / "state.sqlite3", plan):
                print(f"{task.task_id}: {task.status} (revisão {task.definition_revision})")
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
        config = load_config(root / ".agentflow" / "config.toml")
        checks = diagnose(config)
        for check in checks:
            requirement = "obrigatório" if check.required else "opcional"
            print(f"{check.name}: {check.status} ({requirement}) — {check.detail}")
        return 1 if any(c.required and c.status != "ok" for c in checks) else 0
    except (ConfigError, InitError, PlanError, StateError, ExecutionError, GitError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
