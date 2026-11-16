"""User-facing CLI; no agent execution commands exist in phases 1 or 2."""

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from . import __version__
from .config import ConfigError, load_config
from .security import SecurityProfile, profile_diagnostic, save_profile_configuration
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


def _apl_acceptance_policy_digest(plan) -> str:
    value = {
        "spec_digest": plan.spec_digest,
        "tasks_digest": plan.tasks_digest,
        "gate_order": list(plan.gate_order),
        "gates": [{"task_id": task.id,
                   "definitions": [gate_data(gate) for gate in task.gates]}
                  for task in plan.tasks],
        "validation_commands": [command_data(command)
                                for command in plan.specification.validation_commands],
    }
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _validate_apl_acceptance_readiness(plan, snapshot) -> None:
    platform = "windows" if os.name == "nt" else "linux"
    commands = {command.command_id for command in plan.specification.validation_commands
                if command.platform == platform}
    for job in snapshot.jobs:
        task = next((item for item in plan.tasks if item.id == job.job_id), None)
        if task is None or not task.gates or not task.acceptance_criteria:
            raise StateError(f"task {job.job_id} has no independent acceptance gates")
        criteria = {item.id for item in task.acceptance_criteria}
        covered = {criterion for gate in task.gates for criterion in gate.criterion_ids}
        if (tuple(item.id for item in task.acceptance_criteria) != job.acceptance_criteria
                or not criteria <= covered
                or any(gate.command_id not in commands for gate in task.gates if gate.required)):
            raise StateError(f"task {job.job_id} acceptance gate binding is incomplete")


def _generated_plan_digest(plan) -> str:
    return f"spec:{plan.spec_digest};tasks:{plan.tasks_digest}"


def _write_objective_cycle_manifest(root: Path, cycle_id: str, plan_directory: Path,
                                    plan, operation_id: str) -> None:
    directory = root / ".agentflow" / "objective-runs"
    if (root / ".agentflow").is_symlink() or directory.is_symlink():
        raise StateError("objective_run_manifest_symlink_forbidden")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"cycle-{cycle_id}.json"
    payload = {"schema_version": 1, "cycle_id": cycle_id,
               "operation_id": operation_id,
               "plan_directory": str(plan_directory.resolve(strict=True)),
               "plan_digest": _generated_plan_digest(plan)}
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise StateError("objective_run_manifest_symlink_forbidden")
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError("objective_run_manifest_invalid_preserved") from exc
        if current != payload:
            raise StateError("objective_run_manifest_conflict_preserved")
        return
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.link(temporary, target)
    except FileExistsError:
        current = json.loads(target.read_text(encoding="utf-8"))
        if current != payload:
            raise StateError("objective_run_manifest_conflict_preserved")
    finally:
        temporary.unlink(missing_ok=True)


def _objective_plan_directory(root: Path, cycle_id: str, fallback: Path) -> Path:
    target = root / ".agentflow" / "objective-runs" / f"cycle-{cycle_id}.json"
    if not target.is_file():
        return fallback
    if target.is_symlink() or (root / ".agentflow").is_symlink():
        raise StateError("objective_run_manifest_symlink_forbidden")
    try:
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError("objective_run_manifest_invalid_preserved") from exc
    if record.get("schema_version") != 1 or record.get("cycle_id") != cycle_id:
        raise StateError("objective_run_manifest_invalid_preserved")
    directory = Path(record.get("plan_directory", "")).resolve(strict=True)
    control = (root / ".agentflow").resolve(strict=True)
    if directory != control and control not in directory.parents:
        raise StateError("objective_run_plan_directory_outside_agentflow_state")
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--version", action="version", version=f"agentflow {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "plan", "state", "audit", "execute", "cancel", "recover", "cleanup", "workspaces", "git-recover", "checkpoint", "verify-no-changes", "complete", "validate", "validation-state", "validation-cleanup", "approval-preview"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, default=Path.cwd(), help="caminho dentro do repositório Git")
        if name == "approval-preview":
            command.add_argument("--broker-db", type=Path, required=True,
                                 help="SQLite do Broker, aberto somente para leitura")
            command.add_argument("--request-id", required=True)
        if name == "audit":
            command.add_argument("--format", choices=("human", "json"), default="human")
        if name == "execute":
            command.add_argument("--task", required=True)
            command.add_argument("--operation-id", required=True)
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
    objective_run = commands.add_parser(
        "run", help="planejar e executar um objetivo no workspace local")
    objective_run.add_argument("objective", help="objetivo solicitado pelo operador")
    objective_run.add_argument("--path", type=Path, default=Path.cwd(), help="workspace do projeto")
    objective_run.add_argument("--allow-unconfined-network", action="store_true",
                               help="registra explicitamente que a validação local pode usar rede")
    objective_run.add_argument("--max-seconds", type=float, default=3600)
    objective_run.add_argument("--max-estimated-cost", type=float, default=0.10,
                               help="estimativa operacional, não teto financeiro do provider")
    objective_run.add_argument("--max-attempts-per-job", type=int, default=3)
    objective_run.add_argument("--task-timeout", type=int, default=1800)
    objective_run.add_argument("--planner-timeout", type=int, default=240)
    objective_run.add_argument("--operation-id", default="",
                               help="chave explícita para iniciar uma nova execução; sem ela o pedido idêntico é idempotente")
    profile = commands.add_parser("profile", help="configurar ou diagnosticar o perfil de segurança")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_status = profile_commands.add_parser("status", help="mostrar perfil e capacidades indisponíveis")
    profile_status.add_argument("--path", type=Path, default=Path.cwd())
    profile_select = profile_commands.add_parser("select", help="registrar seleção explícita, sem habilitar grants")
    profile_select.add_argument("--path", type=Path, default=Path.cwd())
    profile_select.add_argument("--profile", required=True,
                                choices=tuple(item.value for item in SecurityProfile))
    profile_select.add_argument("--confirm", action="store_true", required=True,
                                help="confirma a seleção local persistente do perfil")
    codex_skill = commands.add_parser("codex-skill", help="instalar a Skill local do Codex CLI")
    codex_skill_commands = codex_skill.add_subparsers(dest="codex_skill_command", required=True)
    codex_skill_install = codex_skill_commands.add_parser("install", help="instalar a Skill AgentFlow")
    codex_skill_install.add_argument("--path", type=Path, default=Path.cwd(),
                                     help="workspace de destino da Skill")
    codex_skill_install.add_argument("--global", dest="global_install", action="store_true",
                                     help="instalar em ~/.agents/skills (opção global explícita)")
    codex_skill_install.add_argument("--update", action="store_true",
                                     help="atualizar somente uma Skill AgentFlow gerenciada e inalterada")
    webauthn_enroll = commands.add_parser(
        "webauthn-enroll", help="cadastrar interativamente credencial Windows platform (não concede autorização)")
    webauthn_enroll.add_argument("--broker-db", type=Path, required=True,
                                 help="SQLite Broker local explícito para armazenar a chave pública")
    local_auth = commands.add_parser("local-auth", help="credencial do operador LOCAL_SUPERVISED")
    local_auth_commands = local_auth.add_subparsers(dest="local_auth_command", required=True)
    local_auth_status = local_auth_commands.add_parser("status", help="diagnóstico local sem autenticar")
    local_auth_status.add_argument("--path", type=Path, default=Path.cwd())
    local_auth_enroll = local_auth_commands.add_parser("enroll", help="cadastro interativo do operador local")
    local_auth_enroll.add_argument("--path", type=Path, default=Path.cwd())
    local_auth_enroll.add_argument("--confirm-bootstrap", action="store_true", required=True,
                                   help="confirma bootstrap explícito do proprietário local")
    local_auth_revoke = local_auth_commands.add_parser("revoke", help="revogar a credencial local")
    local_auth_revoke.add_argument("--path", type=Path, default=Path.cwd())
    local_auth_revoke.add_argument("--confirm-revocation", action="store_true", required=True,
                                   help="confirma explicitamente a revogação")
    apl = commands.add_parser("apl", help="inspecionar ciclos Autonomous Planning Loop")
    apl_commands = apl.add_subparsers(dest="apl_command", required=True)
    for apl_name in ("start", "status", "history", "inspect", "pause", "resume", "cancel", "next", "submit-result", "replan", "proposals", "accept-plan", "reject-plan", "handoff", "handoff-status", "handoff-resume", "handoff-cancel", "handoff-review", "authorize-plan", "run", "run-status", "run-cancel", "run-resume"):
        apl_command = apl_commands.add_parser(apl_name)
        apl_command.add_argument("--path", type=Path, default=Path.cwd(), help="caminho dentro do repositório Git")
        if apl_name == "start":
            apl_command.add_argument("--profile", required=True, choices=("developer",))
            apl_command.add_argument("--objective", required=True)
            apl_command.add_argument("--scope", action="append", required=True,
                                     help="caminho explicitamente autorizado; pode repetir")
        elif apl_name not in {"status", "proposals", "accept-plan", "reject-plan",
                              "handoff", "handoff-status", "handoff-resume", "handoff-cancel",
                              "handoff-review", "authorize-plan", "run", "run-status",
                              "run-cancel", "run-resume"}:
            apl_command.add_argument("--cycle-id", required=True)
        if apl_name == "submit-result":
            apl_command.add_argument("--attempt-id", required=True,
                                     help="referência para tentativa já persistida e validada")
        if apl_name == "replan":
            apl_command.add_argument("--planner", required=True, choices=("deterministic", "codex"))
            apl_command.add_argument("--request-id", default="", help="chave idempotente opcional")
            apl_command.add_argument("--enable-experimental-codex", action="store_true",
                                     help="opt-in explícito ao planejador Codex não homologado")
            apl_command.add_argument("--planner-timeout", type=int, default=240)
            apl_command.add_argument("--planner-max-input-bytes", type=int, default=65536)
            apl_command.add_argument("--planner-max-output-bytes", type=int, default=65536)
        if apl_name in {"proposals", "accept-plan", "reject-plan"}:
            apl_command.add_argument("--cycle-id", required=True)
        if apl_name in {"accept-plan", "reject-plan"}:
            apl_command.add_argument("--proposal-id", required=True)
        if apl_name == "accept-plan":
            apl_command.add_argument("--confirm", action="store_true", required=True,
                                     help="confirma explicitamente a ativação da revisão proposta")
        if apl_name in {"handoff", "handoff-status", "handoff-resume", "handoff-cancel"}:
            apl_command.add_argument("--project-id", required=True)
            apl_command.add_argument("--principal-id", required=True,
                                     help="claim conferido pelo Broker contra a identidade mTLS")
            apl_command.add_argument("--broker-port", type=int, required=True)
            apl_command.add_argument("--server-name", default="localhost")
            apl_command.add_argument("--ca", type=Path, required=True)
            apl_command.add_argument("--client-cert", type=Path, required=True)
            apl_command.add_argument("--client-key", type=Path, required=True)
            apl_command.add_argument("--server-fingerprint", required=True)
            apl_command.add_argument("--timeout", type=float, default=5.0)
        if apl_name in {"handoff-status", "handoff-resume", "handoff-cancel", "handoff-review"}:
            apl_command.add_argument("--cycle-id", required=True)
            apl_command.add_argument("--proposal-id", required=True)
        if apl_name == "authorize-plan":
            apl_command.add_argument("--cycle-id", required=True)
            apl_command.add_argument("--executor-id", required=True)
            apl_command.add_argument("--executor-version", required=True)
            apl_command.add_argument("--max-attempts-per-job", type=int, default=1)
            apl_command.add_argument("--financial-budget-mode", required=True,
                                     choices=("hard_cap_required", "estimated_cost_accepted"))
        if apl_name == "run":
            apl_command.add_argument("--cycle-id", required=True)
            apl_command.add_argument("--operation-id", required=True,
                                     help="idempotency key for this explicit operator start")
            apl_command.add_argument("--max-jobs", type=int, required=True)
            apl_command.add_argument("--max-attempts-per-job", type=int, default=1)
            apl_command.add_argument("--max-seconds", type=float, required=True)
            apl_command.add_argument("--max-estimated-cost", type=float, required=True,
                                     help="finite estimate, not a provider-enforced financial cap")
            apl_command.add_argument("--currency", default="USD")
            apl_command.add_argument("--validity-seconds", type=int, default=3600)
            apl_command.add_argument("--task-timeout", type=int, default=1800)
            apl_command.add_argument("--plan-dir", type=Path, default=None,
                                     help=argparse.SUPPRESS)
        if apl_name in {"run-status", "run-cancel", "run-resume"}:
            apl_command.add_argument("--run-id", required=True)
        if apl_name == "run-resume":
            apl_command.add_argument("--plan-dir", type=Path, default=None,
                                     help=argparse.SUPPRESS)
        if apl_name == "handoff":
            apl_command.add_argument("--cycle-id", required=True)
            apl_command.add_argument("--planner", required=True, choices=("deterministic", "codex"))
            apl_command.add_argument("--request-id", default="")
            apl_command.add_argument("--enable-experimental-codex", action="store_true")
            apl_command.add_argument("--planner-timeout", type=int, default=240)
            apl_command.add_argument("--planner-max-input-bytes", type=int, default=65536)
            apl_command.add_argument("--planner-max-output-bytes", type=int, default=65536)
    try:
        args = parser.parse_args(argv)
        if args.command == "run":
            if os.environ.get("AGENTFLOW_MANAGED_EXECUTION") == "1":
                raise StateError("nested_agentflow_orchestration_blocked")
            root = repository_root(args.path.resolve())
            if not args.allow_unconfined_network:
                raise StateError("network_scope_requires_explicit_--allow-unconfined-network; local pytest validation cannot enforce network isolation")
            if (args.max_seconds <= 0 or args.max_estimated_cost <= 0
                    or args.max_attempts_per_job < 1 or not 1 <= args.task_timeout <= 7200
                    or not 1 <= args.planner_timeout <= 900):
                raise StateError("objective_run_limits_invalid")
            from .apl.initial_planning import prepare_objective_plan
            from .apl.codex_planner import CodexCLIPlannerAdapter
            from .apl import SQLiteCycleStore, SupervisedCycleService
            from .apl import SQLiteAuthorizedExecutionStore
            state_path = root / ".agentflow" / "state.sqlite3"
            planner = CodexCLIPlannerAdapter(
                state_path, enabled=True, timeout_seconds=args.planner_timeout,
                max_input_bytes=65536, max_output_bytes=65536)
            workspace_identity = str(root.resolve(strict=True))
            objective_digest = hashlib.sha256(args.objective.encode("utf-8")).hexdigest()
            request_id = hashlib.sha256(
                f"{workspace_identity}\0{objective_digest}".encode()).hexdigest()[:32]
            plan, plan_directory, _ = prepare_objective_plan(
                root, args.objective, request_id, planner)
            if not plan.tasks or any(not task.gates or not task.acceptance_criteria
                                     or not task.versioned_paths for task in plan.tasks):
                raise StateError("objective_run_requires_tasks_scopes_and_independent_acceptance_gates")
            with ExecutionStore(state_path) as core_store:
                core_store.import_plan(plan)
            cycle_store = SQLiteCycleStore(state_path)
            scope = tuple(sorted({path for task in plan.tasks for path in task.versioned_paths}))
            scope += ("network:unconfined",)
            request_hash = hashlib.sha256(
                f"{workspace_identity}\0{args.objective}\0{plan.spec_digest}\0{plan.tasks_digest}".encode()
            ).hexdigest()
            operation_id = args.operation_id or "objective-run-" + request_hash[:32]
            cycle_id = hashlib.sha256(f"cycle:{operation_id}".encode()).hexdigest()[:32]
            _write_objective_cycle_manifest(root, cycle_id, plan_directory, plan, operation_id)
            execution_store = SQLiteAuthorizedExecutionStore(str(state_path))
            previous = execution_store.local_contract_for_operation(operation_id)
            if previous is not None:
                contract, _contract_state = previous
                if contract.authorization.proposal_id != cycle_id:
                    raise StateError("objective_run_operation_binding_conflict")
                prior_run = execution_store.run_for_contract(contract.contract_id)
                if prior_run is not None:
                    print(json.dumps({"event": "existing_run", "run_id": prior_run["run_id"],
                                      "cycle_id": cycle_id, "status": prior_run["status"],
                                      "dispatch_started": False,
                                      "next": "use apl run-status, run-resume or run-cancel"}, sort_keys=True),
                          flush=True)
                    return 0 if prior_run["status"] == "completed" else 2
            try:
                cycle = cycle_store.load(cycle_id)
                if (cycle.objective != args.objective or cycle.authorized_scope != tuple(sorted(scope))
                        or cycle.plan_digest != f"spec:{plan.spec_digest};tasks:{plan.tasks_digest}"):
                    raise StateError("objective_run_cycle_binding_conflict")
            except KeyError:
                cycle = SupervisedCycleService(cycle_store, state_path).start(
                    plan, args.objective, scope, cycle_id=cycle_id)
            print(json.dumps({"event": "cycle_persisted", "cycle_id": cycle.cycle_id,
                              "task_count": len(cycle.jobs), "plan_digest": cycle.plan_digest,
                              "plan_directory": str(plan_directory), "dispatch_started": False},
                             sort_keys=True), flush=True)
            nested_args = ["apl", "run", "--path", str(root), "--cycle-id", cycle.cycle_id,
                           "--plan-dir", str(plan_directory), "--operation-id", operation_id,
                           "--max-jobs", str(len(cycle.jobs)),
                           "--max-attempts-per-job", str(args.max_attempts_per_job),
                           "--max-seconds", str(args.max_seconds),
                           "--max-estimated-cost", str(args.max_estimated_cost),
                           "--validity-seconds", "3600", "--task-timeout", str(args.task_timeout)]
            return main(nested_args)
        if args.command == "codex-skill":
            from .codex_skill import install_codex_skill
            result = install_codex_skill(
                workspace=args.path.resolve(), global_install=args.global_install,
                update=args.update, python_executable=sys.executable)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "init":
            root, created = initialize(args.path)
            print(f"{root}: configuração {'criada' if created else 'existente; sem alterações'}")
            return 0
        if args.command == "profile":
            root = repository_root(args.path.resolve())
            profile_path = root / ".agentflow" / "security-profile.toml"
            if args.profile_command == "select":
                selected = SecurityProfile(args.profile)
                save_profile_configuration(profile_path, selected)
            diagnostic = profile_diagnostic(profile_path)
            print(json.dumps(diagnostic.to_dict(), ensure_ascii=False, sort_keys=True))
            return 2 if diagnostic.configuration_status == "invalid_or_missing" else 0
        if args.command == "approval-preview":
            from .approval.presentation import PresentationError, render_readonly_preview
            try:
                sys.stdout.write(render_readonly_preview(args.broker_db, args.request_id))
            except PresentationError as exc:
                print(f"Erro: {exc}", file=sys.stderr)
                return 2
            return 0
        if args.command == "webauthn-enroll":
            from .approval.persistence import BrokerStore
            from .security.windows_webauthn import (
                WindowsHelloCredentialEnrollment, WindowsWebAuthnApi, WindowsWebAuthnError,
            )
            try:
                api = WindowsWebAuthnApi()
                if not api.platform_uv_available():
                    raise WindowsWebAuthnError("windows_platform_uv_authenticator_unavailable")
                with BrokerStore(args.broker_db) as broker_store:
                    record, evidence = WindowsHelloCredentialEnrollment(
                        broker_store, api).enroll_interactive()
                print(json.dumps({**evidence, "credential_id": record.credential.credential_id,
                                  "status": record.status, "dispatch_enabled": False},
                                 sort_keys=True, ensure_ascii=False))
                return 0
            except Exception as exc:
                print(f"Enrollment indisponível: {exc}", file=sys.stderr)
                return 2
        if args.command == "local-auth":
            from .apl import SQLiteCycleStore
            from .security import (LocalPassphraseError, SQLiteLocalOperatorCredentialStore,
                                   current_local_operator_principal,
                                   enroll_local_operator_interactive,
                                   revoke_local_operator_interactive)
            try:
                root = repository_root(args.path.resolve())
                state_path = root / ".agentflow" / "state.sqlite3"
                if not state_path.is_file():
                    raise LocalPassphraseError("agentflow_state_store_missing")
                from .security import load_profile_configuration
                selected_profile = load_profile_configuration(
                    root / ".agentflow" / "security-profile.toml")
                if selected_profile.profile is not SecurityProfile.LOCAL_SUPERVISED:
                    raise LocalPassphraseError("local_supervised_profile_required")
                principal = current_local_operator_principal()
                apl_store = SQLiteCycleStore(state_path, initialize=False)
                credentials = SQLiteLocalOperatorCredentialStore(apl_store)
                if args.local_auth_command == "status":
                    active = credentials.active(principal)
                    print(json.dumps({"profile": "LOCAL_SUPERVISED",
                                      "principal": principal,
                                      "credential_enrolled": active is not None,
                                      "mechanism": "local_passphrase_scrypt",
                                      "dispatch_enabled": False}, sort_keys=True))
                    return 0 if active else 2
                if not sys.stdin.isatty():
                    raise LocalPassphraseError("interactive_terminal_required")
                if args.local_auth_command == "enroll":
                    credential_id = enroll_local_operator_interactive(credentials, principal)
                else:
                    revoked = revoke_local_operator_interactive(credentials, principal)
                    print(json.dumps({"profile": "LOCAL_SUPERVISED", "principal": principal,
                                      "revoked": revoked, "dispatch_enabled": False}, sort_keys=True))
                    return 0 if revoked else 2
                print(json.dumps({"profile": "LOCAL_SUPERVISED", "principal": principal,
                                  "credential_id": credential_id, "enrolled": True,
                                  "dispatch_enabled": False}, sort_keys=True))
                return 0
            except Exception as exc:
                print(f"Autenticação local indisponível: {exc}", file=sys.stderr)
                return 2
        root = repository_root(args.path.resolve())
        if args.command == "apl":
            from .apl import SQLiteCycleStore, SupervisedCycleService
            from .apl.contracts import APLState
            state_path = root / ".agentflow" / "state.sqlite3"
            if args.apl_command != "start" and not state_path.is_file():
                raise StateError("banco operacional não existe; nenhum ciclo APL encontrado")
            if args.apl_command == "start":
                plan = load_plan(root / ".agentflow")
                with ExecutionStore(state_path) as core_store:
                    core_store.import_plan(plan)
                store = SQLiteCycleStore(state_path)
                service = SupervisedCycleService(store, state_path)
                cycle = service.start(plan, args.objective, tuple(args.scope))
                print(f"Ciclo supervisionado criado: {cycle.cycle_id} ({cycle.state.value}); jobs={len(cycle.jobs)}")
                return 0
            store = SQLiteCycleStore(state_path, initialize=args.apl_command in {"replan", "proposals", "accept-plan", "reject-plan"})
            service = SupervisedCycleService(store, state_path)
            if args.apl_command in {"run-status", "run-cancel"}:
                from .apl import SQLiteAuthorizedExecutionStore
                execution_store = SQLiteAuthorizedExecutionStore(str(state_path))
                run = execution_store.load(args.run_id)
                binding = json.loads(run["binding_json"])
                if binding.get("authorization_mode") != "local_execution_contract":
                    raise StateError("local_execution_contract_required")
                if args.apl_command == "run-cancel":
                    revoked = execution_store.revoke_local_contract(
                        run["grant_id"], reason="operator_cli_cancel")
                    print(json.dumps({"run_id": args.run_id, "contract_revoked": revoked,
                                      "cancel_requested": execution_store.cancel_requested(args.run_id),
                                      "dispatch_enabled": False}, sort_keys=True))
                    return 0 if revoked or execution_store.cancel_requested(args.run_id) else 2
                print(json.dumps({
                    "run_id": run["run_id"], "cycle_id": run["cycle_id"],
                    "status": run["status"], "reason": run["reason"],
                    "jobs_started": run["jobs_started"], "attempts": run["attempts"],
                    "consumed_seconds": run["consumed_seconds"],
                    "consumed_estimated_cost": run["consumed_budget"],
                    "tasks": [{"job_id": item["job_id"], "status": item["status"],
                               "attempts": item["attempts"],
                               "evidence_digest": item["evidence_digest"],
                               "process_identity_recorded": bool(item["process_identity_json"])}
                              for item in run["tasks"]],
                    "dispatch_enabled": run["status"] in {"ready", "running"},
                }, ensure_ascii=False, sort_keys=True))
                return 0
            if args.apl_command in {"run", "run-resume"}:
                from .apl import (CodexAcceptanceEvidenceValidator,
                                  CodexAuthorizedJobExecutor, ExecutionLimits,
                                  FinancialBudgetMode, FinancialBudgetPolicy,
                                  AuthorizedExecutionController,
                                  LocalAuthorizedExecutionService,
                                  LocalExecutionContractResolver, PinnedWorkspace,
                                  SQLiteAuthorizedExecutionStore, TaskExecutionLimits)
                from .apl.codex_execution import PlanGateAcceptanceVerifier

                execution_store = SQLiteAuthorizedExecutionStore(str(state_path))
                operation_id = ""
                if args.apl_command == "run":
                    plan_directory = ((args.plan_dir or root / ".agentflow").resolve(strict=True))
                    plan = load_plan(plan_directory)
                    snapshot = store.load(args.cycle_id)
                    if len(snapshot.jobs) > args.max_jobs:
                        raise StateError("plan_exceeds_requested_max_jobs")
                    _validate_apl_acceptance_readiness(plan, snapshot)
                    if "network:unconfined" not in snapshot.authorized_scope:
                        raise StateError(
                            "local_acceptance_runtime_cannot_enforce_network_none; "
                            "recreate_cycle_with_explicit_network:unconfined_scope")
                    cycle_id = args.cycle_id
                    operation_id = args.operation_id
                    max_jobs = args.max_jobs
                    max_attempts = args.max_attempts_per_job
                    max_seconds = args.max_seconds
                    estimated_cost = args.max_estimated_cost
                    currency = args.currency
                    validity_seconds = args.validity_seconds
                    task_timeout = args.task_timeout
                else:
                    existing = execution_store.load(args.run_id)
                    binding = json.loads(existing["binding_json"])
                    if binding.get("authorization_mode") != "local_execution_contract":
                        raise StateError("local_execution_contract_required")
                    cycle_id = existing["cycle_id"]
                    plan_directory = ((args.plan_dir or _objective_plan_directory(
                        root, cycle_id, root / ".agentflow")).resolve(strict=True))
                    plan = load_plan(plan_directory)
                    operation_id = "resume:" + existing["grant_id"]
                    authorization = binding
                    max_jobs = authorization["limits"]["max_jobs"]
                    max_attempts = authorization["limits"]["max_attempts_per_job"]
                    max_seconds = authorization["limits"]["max_seconds"]
                    estimated_cost = authorization["budget_policy"]["estimated_amount"]
                    currency = authorization["budget_policy"]["currency"]
                    validity_seconds = 3600
                    task_timeout = min(1800, max(1, int(max_seconds)))
                snapshot = store.load(cycle_id)
                if "network:unconfined" not in snapshot.authorized_scope:
                    raise StateError("local_acceptance_network_scope_missing")
                if args.apl_command == "run":
                    if (args.max_jobs < 1 or args.max_attempts_per_job < 1
                            or args.max_seconds <= 0 or args.max_estimated_cost <= 0
                            or args.validity_seconds < 1 or args.validity_seconds > 86400
                            or args.task_timeout < 1 or args.task_timeout > 7200
                            or len(args.currency) != 3 or not args.currency.isalpha()):
                        raise StateError("local_execution_limits_invalid")
                policy_digest = _apl_acceptance_policy_digest(plan)
                adapter = CodexAdapter()
                availability = adapter.discover()
                if availability.status != "available" or not availability.version:
                    raise StateError(
                        f"codex_cli_not_homologated:{availability.reason_code}:no_job_dispatched")
                root_real = root.resolve(strict=True)
                info = root_real.stat()
                workspace_identity = "sha256:" + hashlib.sha256(
                    f"{os.path.normcase(str(root_real))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
                workspace_id = "local-" + hashlib.sha256(
                    f"{plan.specification.project_id}:{workspace_identity}".encode()).hexdigest()[:24]
                pinned = PinnedWorkspace(root_real, plan.specification.project_id,
                                         workspace_id, workspace_identity)

                class CurrentWorkspaceResolver:
                    def resolve(self, candidate_cycle_id):
                        return pinned if candidate_cycle_id == cycle_id else None

                class CurrentAcceptancePolicyResolver:
                    def resolve(self, candidate_cycle_id):
                        if candidate_cycle_id != cycle_id:
                            return ""
                        return _apl_acceptance_policy_digest(load_plan(plan_directory))

                executor = CodexAuthorizedJobExecutor(
                    adapter=adapter, runtime=LocalRuntime(root / ".agentflow" / "runs"),
                    workspace_resolver=CurrentWorkspaceResolver(),
                    task_timeout_seconds=(task_timeout if args.apl_command == "run"
                                          else min(1800, max(1, int(max_seconds)))))
                validator = CodexAcceptanceEvidenceValidator(PlanGateAcceptanceVerifier(
                    plan=plan, workspace=root_real, authorized_scope=snapshot.authorized_scope,
                    evidence_directory=root / ".agentflow" / "acceptance-evidence",
                    allow_unconfined_network=True))
                resolver = LocalExecutionContractResolver(
                    contract_store=execution_store, cycle_store=store,
                    workspace_resolver=CurrentWorkspaceResolver(),
                    executor_id="codex", executor_version=availability.version,
                    acceptance_policy_resolver=CurrentAcceptancePolicyResolver())
                controller = AuthorizedExecutionController(
                    cycle_store=store, execution_store=execution_store,
                    grant_resolver=resolver, executor=executor,
                    evidence_validator=validator)
                local_service = LocalAuthorizedExecutionService(
                    cycle_store=store, execution_store=execution_store,
                    resolver=resolver, controller=controller,
                    workspace_resolver=CurrentWorkspaceResolver())
                if args.apl_command == "run":
                    limits = ExecutionLimits(
                        max_jobs, max_attempts, max_seconds, args.max_estimated_cost,
                        max_iterations=max_jobs * max_attempts, max_concurrency=1)
                    task_limits = TaskExecutionLimits(
                        max_attempts, min(max_seconds, task_timeout),
                        args.max_estimated_cost / max_jobs)

                    def announce_persisted_run(run_id):
                        print(json.dumps({"event": "run_persisted", "run_id": run_id,
                                          "dispatch_started": False}, sort_keys=True),
                              flush=True)

                    contract, result = local_service.start(
                        cycle_id=cycle_id, operation_id=operation_id,
                        limits=limits, task_limits=task_limits,
                        financial_policy=FinancialBudgetPolicy(
                            FinancialBudgetMode.ESTIMATED_COST_ACCEPTED,
                            args.max_estimated_cost, args.currency.upper()),
                        expires_at=(datetime.now(timezone.utc)
                                    + timedelta(seconds=validity_seconds)
                                    ).isoformat().replace("+00:00", "Z"),
                        acceptance_policy_digest=policy_digest,
                        on_run_persisted=announce_persisted_run)
                else:
                    print(json.dumps({"event": "run_resume_started", "run_id": args.run_id,
                                      "dispatch_started": False}, sort_keys=True), flush=True)
                    result = local_service.resume(args.run_id)
                    contract, _ = execution_store.load_local_contract(
                        execution_store.load(args.run_id)["grant_id"])
                print(json.dumps({
                    "contract_id": contract.contract_id,
                    "run_id": result["run_id"], "cycle_id": cycle_id,
                    "status": result["status"], "reason": result["reason"],
                    "dispatch_enabled": result["status"] in {"ready", "running"},
                    "jobs_started": result["jobs_started"],
                    "attempts": result["attempts"],
                }, ensure_ascii=False, sort_keys=True))
                return 0 if result["status"] == "completed" else 2
            if args.apl_command in {"handoff", "handoff-status", "handoff-resume", "handoff-cancel"}:
                from .apl import BrokerHandoffService
                from .approval.transport import LoopbackTLSClient
                client = LoopbackTLSClient(
                    host="127.0.0.1", port=args.broker_port, server_name=args.server_name,
                    trust_anchor_pem=str(args.ca), client_certificate_pem=str(args.client_cert),
                    client_private_key_pem=str(args.client_key),
                    expected_server_fingerprint=args.server_fingerprint,
                    timeout_seconds=args.timeout)
                handoff = BrokerHandoffService(store, state_path, client,
                                               project_id=args.project_id,
                                               principal_id=args.principal_id)
                if args.apl_command == "handoff":
                    if args.planner == "deterministic":
                        if args.enable_experimental_codex:
                            raise StateError("--enable-experimental-codex só se aplica a --planner codex")
                        from .apl import DeterministicPlannerAdapter
                        adapter = DeterministicPlannerAdapter(
                            rationale="deterministic baseline; no semantic changes inferred")
                    else:
                        if not args.enable_experimental_codex:
                            raise StateError("Codex requer --enable-experimental-codex explícito")
                        from .apl.codex_planner import CodexCLIPlannerAdapter
                        adapter = CodexCLIPlannerAdapter(
                            state_path, enabled=True, timeout_seconds=args.planner_timeout,
                            max_input_bytes=args.planner_max_input_bytes,
                            max_output_bytes=args.planner_max_output_bytes)
                    proposal, _status, _created = service.replan(
                        args.cycle_id, adapter, args.request_id or None)
                    result = handoff.submit(args.cycle_id, proposal.proposal_id)
                elif args.apl_command == "handoff-resume":
                    result = handoff.resume(args.cycle_id, args.proposal_id)
                elif args.apl_command == "handoff-cancel":
                    result = handoff.cancel(args.cycle_id, args.proposal_id)
                else:
                    result = handoff.status(args.cycle_id, args.proposal_id)
                print(json.dumps(result, sort_keys=True, ensure_ascii=False))
                if result["status"] == "awaiting_authorization":
                    print("Aguardando autoridade independente; Broker não autorizou dispatch.")
                return 0
            if args.apl_command == "handoff-review":
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise StateError("consent_review_requires_interactive_terminal")
                from .approval.persistence import BrokerStore
                from .security import (load_local_consent_configuration,
                                       review_persisted_handoff)
                from .security import load_profile_configuration
                profile = load_profile_configuration(root / ".agentflow" / "security-profile.toml")
                if profile.profile is not SecurityProfile.LOCAL_SUPERVISED:
                    raise StateError("local_supervised_profile_required_for_consent_review")
                config = load_local_consent_configuration(
                    root / ".agentflow" / "local-consent.toml",
                    project_root=root, state_database=state_path)
                with BrokerStore(config.broker_database) as broker_store:
                    outcome = review_persisted_handoff(
                        cycle_store=store, state_database=state_path,
                        broker_store=broker_store, config=config,
                        cycle_id=args.cycle_id, proposal_id=args.proposal_id)
                    print(json.dumps({
                        "decision": outcome.decision.value,
                        "decision_id": outcome.consent_decision_id,
                        "grant_id": outcome.grant_id,
                        "state": outcome.state,
                        "reason": outcome.reason,
                        "dispatch_enabled": False,
                    }, ensure_ascii=False, sort_keys=True))
                return 0 if outcome.state in {
                    "approved", "rejected", "blocked", "expired", "revoked", "consumed"
                } else 2
            if args.apl_command == "authorize-plan":
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise StateError("execution_consent_requires_interactive_terminal")
                from .approval.persistence import BrokerStore
                from .security import (load_local_consent_configuration,
                                       load_profile_configuration,
                                       review_accepted_execution_plan)
                profile = load_profile_configuration(root / ".agentflow" / "security-profile.toml")
                if profile.profile is not SecurityProfile.LOCAL_SUPERVISED:
                    raise StateError("local_supervised_profile_required_for_execution_consent")
                config = load_local_consent_configuration(
                    root / ".agentflow" / "local-consent.toml",
                    project_root=root, state_database=state_path)
                with BrokerStore(config.broker_database) as broker_store:
                    outcome = review_accepted_execution_plan(
                        cycle_store=store, state_database=state_path,
                        broker_store=broker_store, config=config,
                        cycle_id=args.cycle_id, executor_id=args.executor_id,
                        executor_version=args.executor_version,
                        max_attempts_per_job=args.max_attempts_per_job,
                        financial_budget_mode=args.financial_budget_mode)
                    grant_validation = None
                    if outcome.grant_id is not None and outcome.state == "approved":
                        from .security import (AuthoritativeConsentResolver,
                                               SQLiteLocalOperatorCredentialStore,
                                               current_local_operator_principal)
                        from .security.authorization import Ed25519LocalGrantVerifier
                        from .apl.authorized_execution import BrokerExecutionGrantResolver
                        from .apl.codex_execution import PinnedWorkspace
                        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

                        principal = current_local_operator_principal()
                        credentials = SQLiteLocalOperatorCredentialStore(store)
                        active = credentials.active(principal)
                        if active is None:
                            raise StateError("local_operator_credential_missing_after_consent")
                        key_id = "local-operator-" + active[0].removeprefix("local-credential-")
                        verifier = Ed25519LocalGrantVerifier({
                            key_id: Ed25519PublicKey.from_public_bytes(active[2])})

                        class CurrentLocalVerifierResolver:
                            def resolve(self, candidate_key_id, operator_identity):
                                return (verifier if candidate_key_id == key_id
                                        and operator_identity == principal else None)

                        workspace_authority = AuthoritativeConsentResolver(
                            apl_store=store, broker_store=broker_store,
                            project_id=config.project_id,
                            proposer_principal=config.proposer_principal,
                            authorized_project_ids=config.authorized_project_ids,
                            operator_identity=principal, limits=config.limits,
                            workspace_root=config.workspace_root,
                            consent_ttl_seconds=config.consent_ttl_seconds)
                        workspace_authority.register_workspace(args.cycle_id)
                        workspace_path, workspace_identity, workspace_id = (
                            workspace_authority._validate_workspace_binding(
                                args.cycle_id, store.load(args.cycle_id).authorized_scope))
                        pinned = PinnedWorkspace(
                            Path(workspace_path), config.project_id, workspace_id,
                            workspace_identity)

                        class CurrentWorkspaceResolver:
                            def resolve(self, cycle_id):
                                return pinned if cycle_id == args.cycle_id else None

                        resolver = BrokerExecutionGrantResolver(
                            broker_store=broker_store, cycle_store=store,
                            verifier_resolver=CurrentLocalVerifierResolver(),
                            workspace_resolver=CurrentWorkspaceResolver(),
                            executor_id=args.executor_id,
                            executor_version=args.executor_version)
                        verified = resolver.resolve_and_validate(
                            outcome.grant_id, args.cycle_id)
                        grant_validation = {
                            "signature_and_binding_valid": True,
                            "expires_at": verified.expires_at,
                            "plan_digest": verified.approved_plan_digest,
                            "plan_revision_id": verified.plan_revision_id,
                            "workspace_identity_digest": verified.workspace_identity_digest,
                            "allowed_operations": list(verified.allowed_operations),
                            "max_jobs": verified.limits.max_jobs,
                            "max_attempts_per_job": verified.limits.max_attempts_per_job,
                            "max_seconds": verified.limits.max_seconds,
                            "max_concurrency": verified.limits.max_concurrency,
                            "financial_budget_mode": verified.budget_policy.mode.value,
                        }
                print(json.dumps({
                    "decision": outcome.decision.value,
                    "decision_id": outcome.consent_decision_id,
                    "grant_id": outcome.grant_id,
                    "state": outcome.state,
                    "reason": outcome.reason,
                    "grant_validation": grant_validation,
                    "dispatch_enabled": False,
                }, ensure_ascii=False, sort_keys=True))
                return 0 if outcome.state in {"approved", "rejected", "blocked", "expired", "revoked"} else 2
            if args.apl_command == "replan":
                if args.planner == "deterministic":
                    if args.enable_experimental_codex:
                        raise StateError("--enable-experimental-codex só se aplica a --planner codex")
                    from .apl import DeterministicPlannerAdapter
                    adapter = DeterministicPlannerAdapter(
                        rationale="deterministic baseline: no semantic changes inferred; preserve current plan")
                else:
                    if not args.enable_experimental_codex:
                        raise StateError("Codex requer --enable-experimental-codex explícito")
                    from .apl.codex_planner import CodexCLIPlannerAdapter
                    adapter = CodexCLIPlannerAdapter(
                        state_path, enabled=True, timeout_seconds=args.planner_timeout,
                        max_input_bytes=args.planner_max_input_bytes,
                        max_output_bytes=args.planner_max_output_bytes)
                proposal, status, created = service.replan(args.cycle_id, adapter, args.request_id or None)
                print(f"Proposta {proposal.proposal_id}: {status}; nova={str(created).lower()}")
                print("Proposta semântica experimental; revisão e aceite continuam supervisionados."
                      if args.planner == "codex" else
                      "Adaptador determinístico de baseline; nenhuma inferência semântica foi realizada.")
                return 0
            if args.apl_command == "proposals":
                for proposal, digest_value, status in service.proposals(args.cycle_id):
                    print(f"{proposal.proposal_id}: {status}; source={proposal.source_revision_id}; sha256:{digest_value}")
                    print(f"  justificativa: {proposal.rationale}")
                    print(f"  condição de conclusão: {proposal.completion_condition}")
                    for change in proposal.changes:
                        print(f"  {change.action}: {change.task.job_id} scope={','.join(change.task.required_scope)} deps={','.join(change.task.depends_on)} criteria={','.join(change.task.acceptance_criteria)}")
                return 0
            if args.apl_command == "accept-plan":
                cycle = service.accept_plan(args.cycle_id, args.proposal_id, confirmed=args.confirm)
                print(f"Plano aceito: {cycle.plan_revision_id}; ciclo {cycle.cycle_id} {cycle.state.value}")
                return 0
            if args.apl_command == "reject-plan":
                changed = service.reject_plan(args.cycle_id, args.proposal_id)
                print(f"Proposta {args.proposal_id}: {'rejeitada' if changed else 'já rejeitada'}")
                return 0
            if args.apl_command in ("pause", "resume", "cancel"):
                action = getattr(service, args.apl_command)
                cycle = action(args.cycle_id)
                print(f"Ciclo {cycle.cycle_id}: {cycle.state.value}")
                return 0
            if args.apl_command == "next":
                job = service.next_job(args.cycle_id)
                if job is None:
                    print("Nenhum job elegível")
                else:
                    print(f"Job proposto: {job.job_id}\n{job.instruction}")
                    print(f"Escopo: {', '.join(job.required_scope)}")
                    print(f"Dependências: {', '.join(job.depends_on) or '-'}")
                    print(f"Critérios: {', '.join(job.acceptance_criteria)}")
                    print("Nenhuma execução foi iniciada.")
                return 0
            if args.apl_command == "submit-result":
                cycle = service.submit_result(args.cycle_id, args.attempt_id)
                print(f"Resultado persistido associado ao ciclo {cycle.cycle_id}: {cycle.state.value}")
                return 0
            if args.apl_command == "status":
                cycles = store.list_cycles()
                if not cycles:
                    print("Nenhum ciclo APL persistido")
                for cycle in cycles:
                    print(f"{cycle.cycle_id}: {cycle.state.value}; jobs={len(cycle.jobs)}; "
                          f"resultados={len(cycle.outcomes)}; iterações={cycle.iterations}; "
                          f"orçamento observado={cycle.consumed_budget:g}; motivo={cycle.reason or '-'}")
                return 0
            cycle = store.load(args.cycle_id)
            if args.apl_command == "history":
                for event in store.history(args.cycle_id):
                    print(f"r{event['revision']} {event['state']} sha256:{event['snapshot_digest']}")
                for event in store.planner_events(args.cycle_id):
                    suffix = f"; {event['detail']}" if event["detail"] else ""
                    print(f"planner r{event['cycle_revision']} {event['provider']} {event['status']} sha256:{event['request_digest']}{suffix}")
                return 0
            print(f"Ciclo {cycle.cycle_id}: {cycle.state.value}")
            print(f"Objetivo: {cycle.objective}")
            print(f"Escopo imutável: {', '.join(cycle.authorized_scope)}")
            print(f"Digest do plano: {cycle.plan_digest or '-'}")
            print(f"Bloqueio/motivo: {cycle.reason or '-'}")
            outcomes = dict(cycle.outcomes)
            for job in cycle.jobs:
                outcome = outcomes.get(job.job_id)
                print(f"Job {job.job_id}: {'pendente' if outcome is None else outcome.status}; "
                      f"escopo={','.join(job.required_scope)}; critérios={','.join(job.acceptance_criteria)}")
                if outcome is not None:
                    print(f"  resumo: {outcome.summary or '-'}")
                    for evidence in outcome.evidence:
                        print("  evidência: " + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            return 0
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
                                       ExecutionPolicy(args.timeout, approval=args.approval),
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
        profile_info = profile_diagnostic(root / ".agentflow" / "security-profile.toml")
        print("security_profile: " + json.dumps(profile_info.to_dict(), ensure_ascii=False, sort_keys=True))
        for check in checks:
            requirement = "obrigatório" if check.required else "opcional"
            print(f"{check.name}: {check.status} ({requirement}) — {check.detail}")
        return 1 if any(c.required and c.status != "ok" for c in checks) else 0
    except (ConfigError, InitError, PlanError, StateError, ExecutionError, GitError,
            ValueError, RuntimeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
