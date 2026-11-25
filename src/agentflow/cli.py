"""Local initialization, planning and explicit task execution CLI."""

import argparse
from contextlib import ExitStack
from functools import lru_cache
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from . import __version__
from .config import AGENTS, AgentChoice, Config, ConfigError, load_config, resolve_agent
from .agents.factory import create_adapter
from .execution_selection import read_selection, persist_selection
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
from .planning.prompts import next_task_prompt, task_instruction
from .planning.loader import command_data, gate_data


class _NeverCancel:
    def is_set(self) -> bool:
        return False


def _apl_acceptance_policy_digest(plan, choice=None, roles=None, review_policy=None, review_engine=None) -> str:
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
    if choice is not None:
        value["executor_selection"] = {"agent": choice.agent, "provider": choice.provider, "model": choice.model}
    if roles is not None:
        value["execution_roles"] = roles.to_mapping()
    if review_policy is not None:
        value["review_policy"] = review_policy
    if review_engine is not None:
        value["review_engine"] = review_engine
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _review_engine_version():
    from .apl import code_review
    from .git import change_tracker
    from .review import contracts, context
    return digest({"schema_version": 1, "sources": [hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                   for module in (code_review, change_tracker, contracts, context)]})


def _role_choice(binding):
    selection = binding.model_selection
    return AgentChoice(binding.agent_id, selection.provider_id if selection else "",
                       selection.model_id if selection else "", "roles")


def _role_planner(state_path, request_id, roles, *, timeout=240, idle=300, max_plan_reads=8):
    from .agents.factory import create_role_adapter
    from .apl.codex_planner import CodexCLIPlannerAdapter
    from .apl.role_orchestration import JournaledOrchestrator
    from .state.roles import RoleCallJournal
    planner = CodexCLIPlannerAdapter(state_path, enabled=True, timeout_seconds=timeout,
        max_input_bytes=None, max_output_bytes=65536, idle_seconds=idle or None,
        max_plan_reads=max_plan_reads, agent_adapter=create_role_adapter(roles.orchestrator))
    return JournaledOrchestrator(RoleCallJournal(state_path), request_id, planner)


def _agent_arguments(command, *, init=False):
    command.add_argument("--agent", choices=AGENTS)
    command.add_argument("--invoker-agent", choices=AGENTS, help="identidade informada pela integração chamadora")
    if not init:
        command.add_argument("--idle-seconds", type=_idle_value, default=300,
                             help="timeout sem saída; padrão 300s; 0 desativa")
        command.add_argument("--provider")
        command.add_argument("--model")


def _idle_value(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("idle-seconds deve ser >= 0")
    return value


def _configured_runtime(path, args):
    idle = getattr(args, "idle_seconds", 300)
    return LocalRuntime(path) if idle == 300 else LocalRuntime(path, default_idle_seconds=idle or None)


def _selected_agent(args, root):
    path = root / ".agentflow" / "config.toml"
    config = load_config(path) if path.is_file() else Config()
    return resolve_agent(config, agent=args.agent, invoker=args.invoker_agent,
                         provider=args.provider, model=args.model)


def _adapter_for_choice(choice):
    return CodexAdapter() if choice.agent == "codex" and not choice.model else create_adapter(choice)


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


def _completed_milestone_run(root: Path, run_id: str, objective: str) -> bool:
    import sqlite3
    from .apl import SQLiteAuthorizedExecutionStore, SQLiteCycleStore
    state_path = root / ".agentflow" / "state.sqlite3"
    if not state_path.is_file():
        return False
    try:
        run = SQLiteAuthorizedExecutionStore(str(state_path), initialize=False).load(run_id)
        cycle = SQLiteCycleStore(state_path, initialize=False).load(run["cycle_id"])
        binding = json.loads(run["binding_json"])
        return (run["status"] == "completed" and bool(run["tasks"])
                and all(task["status"] == "completed" for task in run["tasks"])
                and cycle.state.value == "COMPLETED"
                and cycle.objective == objective
                and Path(binding["workspace_path"]).resolve(strict=True) == root.resolve(strict=True)
                and binding["plan_revision_id"] == cycle.plan_revision_id
                and canonical([asdict(job) for job in cycle.jobs]) == run["plan_json"])
    except (KeyError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        raise StateError(f"não foi possível validar saída do marco: {exc}") from exc


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
    control = root.resolve(strict=True)
    if directory != control and control not in directory.parents:
        raise StateError("objective_run_plan_directory_outside_agentflow_state")
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--version", action="version", version=f"agentflow {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    agents = commands.add_parser("agents", help="descobrir CLIs sem iniciar inferência")
    agents.add_argument("--path", type=Path, default=Path.cwd())
    agents.add_argument("--format", choices=("human", "json"), default="human")
    cfg_cmd = commands.add_parser("config", help="gerenciar configuração global de agentes e modelos")
    cfg_sub = cfg_cmd.add_subparsers(dest="config_action")
    cfg_sub.add_parser("show", help="exibir configuração global atual")
    cfg_sub.add_parser("check", help="verificar disponibilidade dos agentes configurados")
    cfg_sub.add_parser("reset", help="restaurar configuração global padrão")
    cfg_sub.add_parser("wizard", help="assistente interativo de configuração global")
    cfg_set = cfg_sub.add_parser("set", help="atualizar configuração global")
    cfg_set.add_argument("--orchestrator-agent", choices=AGENTS)
    cfg_set.add_argument("--orchestrator-provider", default=None)
    cfg_set.add_argument("--orchestrator-model", default=None)
    cfg_set.add_argument("--worker-agent", choices=AGENTS)
    cfg_set.add_argument("--worker-provider", default=None)
    cfg_set.add_argument("--worker-model", default=None)
    cfg_set.add_argument("--max-workers", type=int)
    cfg_set.add_argument("--distribution-policy", choices=("round-robin", "reuse-first", "isolated"))
    cfg_set.add_argument("--subagent", action="append", default=[], help="papel=agente (ex: research=gemini)")
    cfg_set.add_argument("--adversarial", choices=("on", "off"))
    cfg_set.add_argument("--adversarial-agent", choices=AGENTS)
    cfg_set.add_argument("--fallback-policy", choices=("none", "ordered", "same-provider"))
    cfg_set.add_argument("--fallback-models", default=None, help="lista separada por vírgula")
    for name in ("init", "doctor", "plan", "state", "audit", "execute", "cancel", "recover", "cleanup", "workspaces", "git-recover", "checkpoint", "verify-no-changes", "complete", "validate", "validation-state", "validation-cleanup", "approval-preview", "review-status"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, default=Path.cwd(), help="caminho dentro do repositório Git")
        if name == "init":
            _agent_arguments(command, init=True)
            command.add_argument("--non-interactive", action="store_true",
                                 help="usar configuração global válida ou padrão sem perguntas")
            command.add_argument("--keep-global", action="store_true",
                                 help="manter configuração global existente sem perguntar")
        if name == "execute":
            _agent_arguments(command)
        if name == "approval-preview":
            command.add_argument("--broker-db", type=Path, required=True,
                                 help="SQLite do Broker, aberto somente para leitura")
            command.add_argument("--request-id", required=True)
        if name == "audit":
            command.add_argument("--format", choices=("human", "json"), default="human")
        if name == "review-status":
            command.add_argument("--operation-id", required=True)
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
        "run", aliases=["start"], help="run legado; start seleciona papéis e delega entre agentes")
    objective_run.add_argument("objective", nargs="?", help="objetivo; omitido: lê TASKS.json")
    objective_run.add_argument("--tasks-file", type=Path,
                               help="backlog JSON; relativo à raiz do projeto")
    objective_run.add_argument("--plan-dir", type=Path, help="plano TOML pronto; dispensa planejador")
    _agent_arguments(objective_run)
    for role in ("orchestrator", "executor", "reviewer"):
        objective_run.add_argument(f"--{role}-agent", choices=(*AGENTS, "same") if role == "reviewer" else AGENTS)
        objective_run.add_argument(f"--{role}-provider")
        objective_run.add_argument(f"--{role}-model")
    objective_run.add_argument("--configure-only", action="store_true",
                               help="descobrir e persistir papéis sem inferência ou dispatch")
    objective_run.add_argument("--review-mode", choices=("automatic", "off"))
    objective_run.add_argument("--review-max-corrections", type=int, default=2)
    objective_run.add_argument("--review-max-calls", type=int, default=8)
    objective_run.add_argument("--review-max-seconds", type=int, default=600)
    objective_run.add_argument("--review-max-payload-bytes", type=int, default=524288)
    objective_run.add_argument("--review-no-progress-limit", type=int, default=1)
    objective_run.add_argument("--path", type=Path, default=Path.cwd(), help="workspace do projeto")
    objective_run.add_argument("--allow-unconfined-network", action="store_true",
                               help=argparse.SUPPRESS)
    objective_run.add_argument("--max-seconds", type=float, default=3600)
    objective_run.add_argument("--max-estimated-cost", type=float, default=0.10,
                               help="estimativa operacional, não teto financeiro do provider")
    objective_run.add_argument("--max-attempts-per-job", type=int, default=3)
    objective_run.add_argument("--task-timeout", type=int, default=1800)
    objective_run.add_argument("--planner-timeout", type=int, default=240)
    objective_run.add_argument("--planner-max-plan-reads", type=int, default=8)
    objective_run.add_argument("--operation-id", default="",
                               help="chave explícita para iniciar uma nova execução; sem ela o pedido idêntico é idempotente")
    objective_run.add_argument("--managed", action="store_true", help="executar APL por worker persistente destacado")
    profile = commands.add_parser("profile", help="compatibilidade: perfil local não é mais necessário")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_status = profile_commands.add_parser("status", help="informar que perfil e autenticação não são requeridos")
    profile_status.add_argument("--path", type=Path, default=Path.cwd())
    profile_select = profile_commands.add_parser("select", help="compatibilidade: não grava perfil nem habilita execução")
    profile_select.add_argument("--path", type=Path, default=Path.cwd())
    profile_select.add_argument("--profile", required=True,
                                choices=("LOCAL_SUPERVISED", "INDEPENDENT_AUTHORITY"))
    profile_select.add_argument("--confirm", action="store_true", required=True,
                                help="confirma a seleção local persistente do perfil")
    worker_commands = commands.add_parser("worker", help="controlar execução APL gerenciada")
    worker_sub = worker_commands.add_subparsers(dest="worker_command", required=True)
    for worker_action in ("status", "pause", "resume", "cancel"):
        child = worker_sub.add_parser(worker_action)
        child.add_argument("--path", type=Path, default=Path.cwd())
        if worker_action != "status":
            child.add_argument("--run-id", required=True)
    mcp = commands.add_parser("mcp", help="servidor MCP local stdio")
    mcp.add_argument("--path", type=Path, default=Path.cwd())
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
        "webauthn-enroll", help="removido: use run sem autenticação AgentFlow")
    webauthn_enroll.add_argument("--broker-db", type=Path, required=True,
                                 help="SQLite Broker local explícito para armazenar a chave pública")
    local_auth = commands.add_parser("local-auth", help="removido: use run sem autenticação AgentFlow")
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
    for apl_name in ("start", "status", "history", "inspect", "pause", "resume", "cancel", "next", "submit-result", "replan", "proposals", "accept-plan", "reject-plan", "handoff", "handoff-status", "handoff-resume", "handoff-cancel", "handoff-review", "authorize-plan", "run", "run-status", "run-cancel", "run-resume", "run-pause"):
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
                              "run-cancel", "run-resume", "run-pause"}:
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
        if apl_name in {"run", "run-resume"}:
            apl_command.add_argument("--managed", action="store_true",
                                     help="executar ou retomar pelo worker persistente destacado")
        if apl_name == "run":
            _agent_arguments(apl_command)
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
        if apl_name in {"run-status", "run-cancel", "run-resume", "run-pause"}:
            apl_command.add_argument("--run-id", required=True)
        if apl_name == "run-resume":
            apl_command.add_argument("--idle-seconds", type=_idle_value, default=300)
            apl_command.add_argument("--invoker-agent", choices=AGENTS,
                                     help="identidade chamadora; não altera executor persistido")
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
    resources = ExitStack()
    try:
        args = parser.parse_args(argv)
        if args.command == "review-status":
            from .state.review import inspect_review
            root = repository_root(args.path.resolve())
            result = inspect_review(root / ".agentflow" / "state.sqlite3", args.operation_id)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result else 2
        if args.command == "mcp":
            from .mcp_server import serve_stdio
            return serve_stdio(args.path.resolve())
        if args.command == "worker":
            from .worker import command as worker_control, inspect as worker_inspect
            result = (worker_inspect(args.path.resolve()) if args.worker_command == "status"
                      else worker_control(args.path.resolve(), args.worker_command, args.run_id))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result is not None else 2
        if args.command == "agents":
            from .agents.discovery import discover_agents
            from .agents.factory import create_registry
            config_path = args.path.resolve() / ".agentflow" / "config.toml"
            config = load_config(config_path) if config_path.is_file() else Config()
            reports = discover_agents(create_registry(), profiles=config.profiles)
            if args.format == "json":
                print(json.dumps({"schema_version": 1, "agents": [asdict(report) for report in reports]},
                                 ensure_ascii=False, sort_keys=True))
            else:
                for report in reports:
                    observed = report.availability
                    print(f"{observed.adapter_id}: {observed.status}; "
                          f"executável={observed.executable}; versão={observed.version or 'unknown'}; "
                          f"autenticação={report.authentication}; modelos={report.model_listing}")
                    if report.configured_model:
                        print(f"  modelo configurado: {report.configured_provider}/{report.configured_model} "
                              "(acesso não verificado)")
            return 0
        if args.command in {"run", "start"} or (args.command == "apl" and args.apl_command in {"run", "run-resume"}):
            if args.managed:
                operation_id = (args.operation_id if args.apl_command == "run" else
                                "resume:" + args.run_id if args.apl_command == "run-resume" else args.operation_id)
                if not operation_id:
                    raise StateError("managed_execution_requires_explicit_operation_id")
                from .worker import launch as launch_managed
                command_args = list(argv) if argv is not None else sys.argv[1:]
                command_args = [part for part in command_args if part != "--managed"]
                if args.apl_command == "run-resume":
                    operation_id = args.run_id
                result = launch_managed(args.path, command_args, operation_id=operation_id)
                print(json.dumps({"event": "managed_run_queued", **result}, sort_keys=True), flush=True)
                return 0
        if args.command in {"run", "start"}:
            managed_operation = os.environ.get("AGENTFLOW_MANAGED_OPERATION")
            if managed_operation:
                from .worker import inspect as inspect_worker
                current = inspect_worker(args.path, managed_operation)
                if current and current["run"] and current["run"].get("apl", {}).get("status") == "blocked" and current["run"]["apl"].get("reason") == "paused_at_task_boundary":
                    return 2
            if os.environ.get("AGENTFLOW_MANAGED_EXECUTION") == "1":
                raise StateError("nested_agentflow_orchestration_blocked")
            root = repository_root(args.path.resolve())
            if args.plan_dir is not None and (args.objective is not None or args.tasks_file is not None):
                raise StateError("--plan-dir não aceita objetivo ou --tasks-file")
            if args.plan_dir is not None and not (root / args.plan_dir).resolve().is_relative_to(root):
                raise StateError("--plan-dir deve pertencer ao projeto")
            ready_plan = load_plan((root / args.plan_dir).resolve()) if args.plan_dir is not None else None
            if ready_plan is not None:
                args.objective = ready_plan.specification.objective
            if args.objective is not None and args.tasks_file is not None:
                raise StateError("informe objetivo ou --tasks-file, não ambos")
            task_source = None
            master_plan = ""
            master_reference = None
            if args.objective is None:
                from .planning.task_source import read_task_source, task_source_objective
                source = args.tasks_file or Path("TASKS.json")
                task_source = read_task_source(source if source.is_absolute() else root / source)
                if isinstance(task_source, dict) and any(
                        isinstance(task, dict) and "type" in task for task in task_source["tasks"]):
                    from .planning.task_queue import prepare_active_milestone
                    active = prepare_active_milestone(
                        root, task_source,
                        lambda run_id, objective: _completed_milestone_run(root, run_id, objective))
                    task_source, master_plan = active.source, active.master_plan
                    master_reference = active.reference
                args.objective = task_source_objective(task_source)
            initialize(root)
            config = load_config(root / ".agentflow" / "config.toml")
            choice = _selected_agent(args, root)
            if config.runtime != "local":
                raise StateError("run requer runtime local; configuração docker preservada")
            if (args.max_seconds <= 0 or args.max_estimated_cost <= 0
                    or args.max_attempts_per_job < 1 or not 1 <= args.task_timeout <= 7200
                    or not 1 <= args.planner_timeout <= 900):
                raise StateError("objective_run_limits_invalid")
            from .apl.initial_planning import prepare_objective_plan
            from .apl.codex_planner import CodexCLIPlannerAdapter
            from .apl import SQLiteCycleStore, SupervisedCycleService
            from .apl import SQLiteAuthorizedExecutionStore
            state_path = root / ".agentflow" / "state.sqlite3"
            roles = None
            review_limits_changed = (args.review_max_corrections, args.review_max_calls, args.review_max_seconds,
                                     args.review_max_payload_bytes, args.review_no_progress_limit) != (2, 8, 600, 524288, 1)
            role_mode = args.command == "start" or args.configure_only or args.review_mode is not None or review_limits_changed or any(
                getattr(args, f"{role}_{field}") for role in ("orchestrator", "executor", "reviewer")
                for field in ("agent", "provider", "model"))
            role_request_id = None
            if role_mode:
                from .agents.factory import create_registry, create_role_adapter
                from .agents.discovery import discover_agents
                from .state.roles import RoleConfigurationStore
                from .role_cli import choose_roles, present_inventory
                input_data = {"workspace": str(root.resolve(strict=True)), "objective": args.objective,
                              "ready_plan": _generated_plan_digest(ready_plan) if ready_plan else None,
                              "master_plan": master_reference.reference() if master_reference else None,
                              "limits": {name: getattr(args, name) for name in (
                                  "max_seconds", "max_estimated_cost", "max_attempts_per_job",
                                  "task_timeout", "planner_timeout", "planner_max_plan_reads", "idle_seconds")}}
                from .review.contracts import ReviewPolicy
                review_policy = ReviewPolicy(mode=args.review_mode or "automatic",
                    max_corrections=args.review_max_corrections, max_calls=args.review_max_calls,
                    max_seconds=args.review_max_seconds, max_payload_bytes=args.review_max_payload_bytes,
                    no_progress_limit=args.review_no_progress_limit)
                legacy_input = dict(input_data)
                input_data["review_policy"] = asdict(review_policy)
                if review_policy.mode == "automatic":
                    input_data["review_engine"] = _review_engine_version()
                role_request_id = hashlib.sha256((str(root.resolve()) + "\0" +
                    (args.operation_id or digest(input_data))).encode()).hexdigest()[:32]
                reports = discover_agents(create_registry(), profiles=config.profiles)
                if sys.stdin.isatty():
                    present_inventory(reports, sys.stdout)
                else:
                    print(json.dumps({"event": "agent_discovery", "request_id": role_request_id,
                                      "agents": [asdict(report) for report in reports]}, ensure_ascii=False), flush=True)
                requested = {role: {field: getattr(args, f"{role}_{field}")
                                    for field in ("agent", "provider", "model")}
                             for role in ("orchestrator", "executor", "reviewer")}
                for field in ("agent", "provider", "model"):
                    legacy_value = getattr(args, field)
                    explicit_value = requested["executor"][field]
                    if legacy_value is not None:
                        if explicit_value is not None and explicit_value != legacy_value:
                            raise ConfigError("executor_selection_arguments_conflict")
                        requested["executor"][field] = legacy_value
                from .global_config import load_global_config
                global_cfg = load_global_config()
                if global_cfg is not None:
                    role_defaults = {
                        "orchestrator": global_cfg.orchestrator,
                        "executor": global_cfg.worker_default,
                    }
                    if global_cfg.adversarial_enabled and global_cfg.adversarial.agent:
                        role_defaults["reviewer"] = global_cfg.adversarial
                    for role_name, cfg_role in role_defaults.items():
                        if requested[role_name]["agent"] is None and cfg_role.agent:
                            requested[role_name]["agent"] = cfg_role.agent
                            if requested[role_name]["provider"] is None and cfg_role.provider:
                                requested[role_name]["provider"] = cfg_role.provider
                            if requested[role_name]["model"] is None and cfg_role.model:
                                requested[role_name]["model"] = cfg_role.model
                    if requested["reviewer"]["agent"] is None and not global_cfg.adversarial_enabled:
                        requested["reviewer"]["agent"] = "same"
                with RoleConfigurationStore(state_path) as role_store:
                    # Existing v8 requests retain their original immutable policy.
                    legacy_id = hashlib.sha256((str(root.resolve()) + "\0" +
                        (args.operation_id or digest(legacy_input))).encode()).hexdigest()[:32]
                    old = role_store.db.execute("SELECT input_json FROM role_requests WHERE request_id=?", (legacy_id,)).fetchone()
                    if args.review_mode is None and review_policy == ReviewPolicy() and old and old[0] == canonical(legacy_input):
                        input_data, role_request_id = legacy_input, legacy_id
                    role_store.request(role_request_id, input_data)
                    role_store.record_inventory(role_request_id, [asdict(report) for report in reports])
                    roles = choose_roles(role_store, role_request_id, reports, requested,
                                         input_stream=sys.stdin, output_stream=sys.stdout,
                                         validate_binding=create_role_adapter)
                print(json.dumps({"event": "role_configuration", "request_id": role_request_id,
                                  "status": "CONFIGURED" if roles else "AWAITING_CONFIGURATION",
                                  "roles": roles.to_mapping() if roles else None,
                                  "review_policy": input_data.get("review_policy")}, ensure_ascii=False), flush=True)
                if roles is None or args.configure_only:
                    return 0 if roles else 2
                choice = _role_choice(roles.executor)
            planner_options = ({"agent_adapter": _adapter_for_choice(choice)}
                               if roles is None and ready_plan is None and (choice.agent != "codex" or choice.model) else {})
            if roles is not None:
                planner = _role_planner(state_path, role_request_id, roles,
                    timeout=args.planner_timeout, idle=args.idle_seconds,
                    max_plan_reads=args.planner_max_plan_reads)
            elif ready_plan is not None:
                planner = None
            else:
                planner = CodexCLIPlannerAdapter(
                    state_path, enabled=True, timeout_seconds=args.planner_timeout,
                    max_input_bytes=None, max_output_bytes=65536, idle_seconds=args.idle_seconds or None,
                    max_plan_reads=args.planner_max_plan_reads, **planner_options)
            if task_source is not None:
                from .planning.task_source import TaskSourcePlanner
                planner = TaskSourcePlanner(task_source, planner)
                planner.master_plan = master_plan
                planner.master_reference = master_reference
            workspace_identity = str(root.resolve(strict=True))
            objective_digest = hashlib.sha256(args.objective.encode("utf-8")).hexdigest()
            request_id = hashlib.sha256(
                f"{workspace_identity}\0{objective_digest}".encode()).hexdigest()[:32]
            if roles is not None:
                request_id = role_request_id
            if ready_plan is not None:
                plan, plan_directory = ready_plan, (root / args.plan_dir).resolve()
            else:
                plan, plan_directory, _ = prepare_objective_plan(root, args.objective, request_id, planner)
            if roles is not None:
                # Also covers a matching pre-existing plan reused by initial_planning.
                # A TaskSourcePlanner wraps only initial decomposition, not decisions.
                decision_planner = planner.planner if task_source is not None else planner
                decision_planner.inspect_plan(plan)
            if master_reference is not None:
                master_reference.verify()
            if not plan.tasks or any(not task.gates or not task.acceptance_criteria
                                     or not task.versioned_paths for task in plan.tasks):
                raise StateError("objective_run_requires_tasks_scopes_and_independent_acceptance_gates")
            with ExecutionStore(state_path) as core_store:
                core_store.import_plan(plan)
            cycle_store = SQLiteCycleStore(state_path)
            scope = tuple(sorted({path for task in plan.tasks for path in task.versioned_paths}))
            scope += ("network:unconfined",)
            request_hash = hashlib.sha256(
                f"{workspace_identity}\0{args.objective}\0{plan.spec_digest}\0{plan.tasks_digest}\0{choice.agent}:{choice.provider}:{choice.model}".encode()
            ).hexdigest()
            operation_id = args.operation_id or "objective-run-" + request_hash[:32]
            if roles is not None:
                operation_id = "role-run-" + role_request_id
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
            persist_selection(root, cycle.cycle_id, choice)
            if roles is not None:
                with RoleConfigurationStore(state_path) as role_store:
                    role_store.bind_cycle(role_request_id, cycle.cycle_id)
            print(json.dumps({"event": "cycle_persisted", "cycle_id": cycle.cycle_id,
                              "task_count": len(cycle.jobs), "plan_digest": cycle.plan_digest,
                              "plan_directory": str(plan_directory), "dispatch_started": False},
                             sort_keys=True), flush=True)
            nested_args = ["apl", "run", "--path", str(root), "--cycle-id", cycle.cycle_id,
                           "--agent", choice.agent,
                           "--plan-dir", str(plan_directory), "--operation-id", operation_id,
                           "--max-jobs", str(len(cycle.jobs)),
                           "--max-attempts-per-job", str(args.max_attempts_per_job),
                           "--max-seconds", str(args.max_seconds),
                           "--max-estimated-cost", str(args.max_estimated_cost),
                           "--validity-seconds", "3600", "--task-timeout", str(args.task_timeout)]
            if args.idle_seconds != 300:
                nested_args += ["--idle-seconds", str(args.idle_seconds)]
            if choice.model:
                nested_args += ["--provider", choice.provider, "--model", choice.model]
            return main(nested_args)
        if args.command == "codex-skill":
            from .codex_skill import install_codex_skill
            result = install_codex_skill(
                workspace=args.path.resolve(), global_install=args.global_install,
                update=args.update, python_executable=sys.executable)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "init":
            from .agents.discovery import discover_agents
            from .agents.factory import create_registry
            from .global_config import (RoleModelConfig, default_global_config,
                                        global_config_path, load_global_config,
                                        save_global_config)
            interactive = sys.stdin.isatty() and not args.non_interactive
            global_cfg = load_global_config()
            if global_cfg is None:
                reports = discover_agents(create_registry())
                available = [r.descriptor.adapter_id for r in reports if r.availability.status == "available"]
                preferred = args.agent or args.invoker_agent or (available[0] if available else "codex")
                if interactive:
                    print("Nenhuma configuração global encontrada. Iniciando assistente de configuração.")
                    print(f"Agentes detectados no ambiente: {', '.join(available) if available else 'nenhum (usando padrão codex)'}")
                    chosen_orch = input(f"Agente do orquestrador [{preferred}]: ").strip() or preferred
                    chosen_worker = input(f"Agente padrão dos workers [{chosen_orch}]: ").strip() or chosen_orch
                    global_cfg = default_global_config(chosen_orch)
                    global_cfg.worker_default = RoleModelConfig(agent=chosen_worker)
                else:
                    global_cfg = default_global_config(preferred)
                save_global_config(global_cfg)
            elif interactive and not args.keep_global:
                print("Foi encontrada uma configuração global do AgentFlow.\n")
                print("Deseja manter a configuração atual de agentes e modelos?")
                print("1. Manter configuração global (padrão)")
                print("2. Alterar configuração")
                choice = input("Escolha [1]: ").strip() or "1"
                if choice == "2":
                    chosen_orch = input(f"Agente do orquestrador [{global_cfg.orchestrator.agent}]: ").strip() or global_cfg.orchestrator.agent
                    chosen_worker = input(f"Agente padrão dos workers [{global_cfg.worker_default.agent}]: ").strip() or global_cfg.worker_default.agent
                    global_cfg.orchestrator = RoleModelConfig(agent=chosen_orch)
                    global_cfg.worker_default = RoleModelConfig(agent=chosen_worker)
                    save_global_config(global_cfg)
            effective_init_agent = args.agent or args.invoker_agent or global_cfg.orchestrator.agent
            root, created = initialize(args.path, agent=effective_init_agent)
            print(f"{root}: configuração {'criada' if created else 'existente; sem alterações'}")
            config = load_config(root / ".agentflow" / "config.toml")
            print(f"Agente: {config.effective_agent}; runtime: {config.runtime}. Global: {global_config_path()}")
            print("Próximo: agentflow run" if (root / "TASKS.json").is_file()
                  else 'Próximo: agentflow run "<objetivo>" ou crie TASKS.json e use agentflow run')
            return 0
        if args.command == "config":
            from .agents.discovery import discover_agents
            from .agents.factory import create_registry
            from .global_config import (RoleModelConfig, default_global_config,
                                        global_config_path, load_global_config,
                                        save_global_config)
            action = args.config_action or "show"
            if action == "reset":
                cfg = default_global_config()
                path = save_global_config(cfg)
                print(json.dumps({"reset": True, "path": str(path), "config": cfg.to_dict()}, ensure_ascii=False, sort_keys=True))
                return 0
            cfg = load_global_config() or default_global_config()
            if action == "show":
                print(json.dumps({"path": str(global_config_path()), "config": cfg.to_dict()}, ensure_ascii=False, sort_keys=True))
                return 0
            if action == "check":
                reports = {r.descriptor.adapter_id: r for r in discover_agents(create_registry())}
                checks = {}
                for role, agent_name in (("orchestrator", cfg.orchestrator.agent), ("worker_default", cfg.worker_default.agent)):
                    rep = reports.get(agent_name)
                    checks[role] = {"agent": agent_name, "status": rep.availability.status if rep else "unknown", "authentication": rep.authentication if rep else "unknown"}
                if cfg.adversarial_enabled and cfg.adversarial.agent:
                    rep = reports.get(cfg.adversarial.agent)
                    checks["adversarial"] = {"agent": cfg.adversarial.agent, "status": rep.availability.status if rep else "unknown", "authentication": rep.authentication if rep else "unknown"}
                print(json.dumps({"checks": checks}, ensure_ascii=False, sort_keys=True))
                return 0
            if action == "wizard":
                chosen_orch = input(f"Agente do orquestrador [{cfg.orchestrator.agent}]: ").strip() or cfg.orchestrator.agent
                chosen_worker = input(f"Agente padrão dos workers [{cfg.worker_default.agent}]: ").strip() or cfg.worker_default.agent
                cfg.orchestrator = RoleModelConfig(agent=chosen_orch)
                cfg.worker_default = RoleModelConfig(agent=chosen_worker)
                path = save_global_config(cfg)
                print(json.dumps({"saved": True, "path": str(path), "config": cfg.to_dict()}, ensure_ascii=False, sort_keys=True))
                return 0
            if action == "set":
                if args.orchestrator_agent:
                    cfg.orchestrator = RoleModelConfig(
                        agent=args.orchestrator_agent,
                        provider=args.orchestrator_provider if args.orchestrator_provider is not None else cfg.orchestrator.provider,
                        model=args.orchestrator_model if args.orchestrator_model is not None else cfg.orchestrator.model,
                    )
                if args.worker_agent:
                    cfg.worker_default = RoleModelConfig(
                        agent=args.worker_agent,
                        provider=args.worker_provider if args.worker_provider is not None else cfg.worker_default.provider,
                        model=args.worker_model if args.worker_model is not None else cfg.worker_default.model,
                    )
                if args.max_workers is not None:
                    cfg.max_concurrent_workers = args.max_workers
                if args.distribution_policy:
                    cfg.worker_distribution_policy = args.distribution_policy
                for item in args.subagent:
                    if "=" not in item:
                        raise ConfigError("use o formato papel=agente em --subagent")
                    role_name, sub_agent = item.split("=", 1)
                    cfg.subagents[role_name.strip()] = RoleModelConfig(agent=sub_agent.strip())
                if args.adversarial is not None:
                    cfg.adversarial_enabled = args.adversarial == "on"
                if args.adversarial_agent:
                    cfg.adversarial = RoleModelConfig(agent=args.adversarial_agent)
                if args.fallback_policy:
                    cfg.fallback_policy = args.fallback_policy
                if args.fallback_models is not None:
                    cfg.fallback_models = [m.strip() for m in args.fallback_models.split(",") if m.strip()]
                path = save_global_config(cfg)
                print(json.dumps({"saved": True, "path": str(path), "config": cfg.to_dict()}, ensure_ascii=False, sort_keys=True))
                return 0
        if args.command == "profile":
            print(json.dumps({"profile": None, "configuration_status": "not_required",
                              "authentication_required": False, "authority_available": False,
                              "grant_issuance_enabled": False, "dispatch_enabled": False,
                              "next_command": "agentflow run",
                              "reason": "local_execution_requires_no_security_profile"}, sort_keys=True))
            return 0
        if (args.command in {"local-auth", "webauthn-enroll"}
                or args.command == "apl" and args.apl_command in {"authorize-plan", "handoff-review"}):
            raise StateError("autenticação AgentFlow removida do fluxo local; use agentflow run")
        if args.command == "approval-preview":
            from .approval.presentation import PresentationError, render_readonly_preview
            try:
                sys.stdout.write(render_readonly_preview(args.broker_db, args.request_id))
            except PresentationError as exc:
                print(f"Erro: {exc}", file=sys.stderr)
                return 2
            return 0
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
            if args.apl_command == "run-pause":
                from .worker import command as worker_control
                result = worker_control(root, "pause", args.run_id)
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
            if args.apl_command in {"run-status", "run-cancel"}:
                from .apl import SQLiteAuthorizedExecutionStore
                from .state.roles import roles_for_cycle, RoleCallJournal
                execution_store = SQLiteAuthorizedExecutionStore(str(state_path))
                run = execution_store.load(args.run_id)
                binding = json.loads(run["binding_json"])
                selected = read_selection(root, run["cycle_id"])
                role_info = roles_for_cycle(state_path, run["cycle_id"])
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
                    "executor": binding["executor_id"], "executor_version": binding["executor_version"],
                    "provider": selected.provider if selected else None,
                    "model": selected.model if selected else None,
                    "selection_mode": "manual" if selected and selected.model else "cli-default",
                    **({"roles": role_info[1].to_mapping(),
                        "orchestrator_seconds": RoleCallJournal(state_path).completed_decision_seconds(role_info[0])}
                       if role_info else {}),
                    "status": run["status"], "reason": run["reason"],
                    "jobs_started": run["jobs_started"], "attempts": run["attempts"],
                    "consumed_seconds": run["consumed_seconds"],
                    "consumed_estimated_cost": run["consumed_budget"],
                    "tasks": [{"job_id": item["job_id"], "status": item["status"],
                               "operation_id": item["operation_id"],
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
                    if existing.get("status") == "completed":
                        print(json.dumps({"event": "existing_run", "run_id": args.run_id,
                                          "cycle_id": existing["cycle_id"], "status": "completed",
                                          "dispatch_started": False}, sort_keys=True))
                        return 0
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
                stored_choice = read_selection(root, cycle_id)
                from .state.roles import roles_for_cycle, RoleConfigurationStore, request_input
                role_info = roles_for_cycle(state_path, cycle_id)
                observer = None
                selected_roles = None
                review_config = None
                review_engine = None
                if role_info:
                    from .agents.factory import create_role_adapter, create_registry
                    from .agents.discovery import discover_agents
                    from .apl.role_selection import validate_roles
                    role_request_id, selected_roles = role_info
                    validate_roles(selected_roles, discover_agents(create_registry()))
                    choice = _role_choice(selected_roles.executor)
                    if (getattr(args, "agent", None) is not None and args.agent != choice.agent
                            or getattr(args, "provider", None) is not None and args.provider != choice.provider
                            or getattr(args, "model", None) is not None and args.model != choice.model):
                        raise StateError("role_executor_override_forbidden")
                    choice = persist_selection(root, cycle_id, choice)
                    with RoleConfigurationStore(state_path) as role_store:
                        selected_input = role_store.input_data(role_request_id)
                        options = selected_input["limits"]
                        review_config = selected_input.get("review_policy")
                        review_engine = selected_input.get("review_engine")
                        if review_config and review_config["mode"] == "automatic" and review_engine != _review_engine_version():
                            raise StateError("review_engine_changed: inicie nova execução; histórico preservado")
                    orchestrator = _role_planner(state_path, role_request_id, selected_roles,
                        timeout=options["planner_timeout"], idle=options["idle_seconds"],
                        max_plan_reads=options["planner_max_plan_reads"])
                    observer = orchestrator.observe_result
                elif args.apl_command == "run":
                    choice = persist_selection(root, cycle_id, _selected_agent(args, root))
                else:
                    choice = stored_choice or AgentChoice(binding["executor_id"])
                policy_digest = _apl_acceptance_policy_digest(plan, choice if read_selection(root, cycle_id) else None,
                                                             selected_roles, review_config, review_engine)
                adapter = create_role_adapter(selected_roles.executor) if selected_roles else _adapter_for_choice(choice)
                availability = adapter.discover()
                if availability.status != "available" or not availability.version:
                    raise StateError(
                        f"executor_unavailable:{choice.agent}:{availability.reason_code}:no_job_dispatched")
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
                        latest_roles = roles_for_cycle(state_path, cycle_id)
                        latest_input = request_input(state_path, latest_roles[0]) if latest_roles else {}
                        if latest_input.get("review_engine") and latest_input["review_engine"] != _review_engine_version():
                            return "review_engine_changed"
                        return _apl_acceptance_policy_digest(load_plan(plan_directory), read_selection(root, cycle_id),
                            latest_roles[1] if latest_roles else None,
                            latest_input.get("review_policy"), latest_input.get("review_engine"))

                executor = CodexAuthorizedJobExecutor(
                    adapter=adapter, runtime=_configured_runtime(root / ".agentflow" / "runs", args),
                    handoff_resolver=lambda candidate: dict(store.load(candidate).outcomes),
                    workspace_resolver=CurrentWorkspaceResolver(),
                    task_timeout_seconds=(task_timeout if args.apl_command == "run"
                                          else min(1800, max(1, int(max_seconds)))))
                validator = CodexAcceptanceEvidenceValidator(PlanGateAcceptanceVerifier(
                    plan=plan, workspace=root_real, authorized_scope=snapshot.authorized_scope,
                    evidence_directory=root / ".agentflow" / "acceptance-evidence",
                    allow_unconfined_network=True))
                if review_config and review_config["mode"] == "automatic":
                    from .apl.code_review import ReviewedJobExecutor, ReviewAcceptanceValidator
                    from .apl.codex_planner import CodexCLIPlannerAdapter
                    from .git.change_tracker import GitChangeTracker
                    from .review.contracts import ReviewPolicy
                    from .state.review import ReviewJournal, ReviewStore
                    from .state.roles import RoleCallJournal
                    with ReviewStore(state_path):
                        pass
                    tracker = GitChangeTracker(resources.enter_context(GitLocalProvider()), root_real)
                    review_journal = ReviewJournal(state_path)
                    policy = ReviewPolicy(**review_config)
                    def review_client(binding):
                        return CodexCLIPlannerAdapter(state_path, enabled=True,
                            timeout_seconds=min(options["planner_timeout"], policy.max_seconds),
                            max_input_bytes=None, max_output_bytes=65536,
                            idle_seconds=options["idle_seconds"] or None,
                            agent_adapter=create_role_adapter(binding))
                    executor = ReviewedJobExecutor(executor, validator, tracker, review_journal,
                        RoleCallJournal(state_path), role_request_id, selected_roles,
                        review_client(selected_roles.reviewer), review_client(selected_roles.executor), policy,
                        implementation=review_engine)
                    validator = ReviewAcceptanceValidator(validator, tracker, review_journal, implementation=review_engine)
                resolver = LocalExecutionContractResolver(
                    contract_store=execution_store, cycle_store=store,
                    workspace_resolver=CurrentWorkspaceResolver(),
                    executor_id=choice.agent, executor_version=availability.version,
                    acceptance_policy_resolver=CurrentAcceptancePolicyResolver())
                controller = AuthorizedExecutionController(
                    cycle_store=store, execution_store=execution_store,
                    grant_resolver=resolver, executor=executor,
                    evidence_validator=validator, result_observer=observer)
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
                    "executor": choice.agent, "provider": choice.provider or None, "model": choice.model or None,
                    "selection_mode": "manual" if choice.model else "cli-default",
                    "run_id": result["run_id"], "cycle_id": cycle_id,
                    "status": result["status"], "reason": result["reason"],
                    "dispatch_enabled": result["status"] in {"ready", "running"},
                    "jobs_started": result["jobs_started"],
                    "attempts": result["attempts"],
                    **({"orchestrator_seconds": orchestrator.store.completed_decision_seconds(role_request_id)}
                       if selected_roles else {}),
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
                    from .planning.handoff import dependency_context
                    context = dependency_context(job.depends_on, dict(service.store.load(args.cycle_id).outcomes))
                    print(next_task_prompt(job.job_id, job.instruction, job.required_scope, context))
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
            choice = _selected_agent(args, root)
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
                                       next_task_prompt(task.id, task_instruction(task), task.versioned_paths),
                                       ExecutionPolicy(args.timeout, approval=args.approval),
                                       tuple((criterion.id, criterion.description) for criterion in task.acceptance_criteria),
                                       workspace_id)
            adapter = _adapter_for_choice(choice)
            if hasattr(adapter, "prepare_request"):
                request = adapter.prepare_request(request)
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
                runtime = _configured_runtime(root / ".agentflow" / "runs", args)
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
        print("Autenticação AgentFlow: não requerida")
        for check in checks:
            requirement = "obrigatório" if check.required else "opcional"
            print(f"{check.name}: {check.status} ({requirement}) — {check.detail}")
        return 1 if any(c.required and c.status != "ok" for c in checks) else 0
    except (ConfigError, InitError, PlanError, StateError, ExecutionError, GitError,
            ValueError, RuntimeError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    finally:
        resources.close()
