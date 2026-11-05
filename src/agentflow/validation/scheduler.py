"""Explicit, sequential validation orchestration; no default real runtime."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import shutil
from threading import Event
from typing import TYPE_CHECKING

from agentflow.git.contracts import Checkpoint, GitProvider, Workspace
from agentflow.planning import Plan, canonical, digest
from agentflow.planning.loader import command_data, gate_data
from agentflow.state.contracts import ConflictError, TransitionError

from .contracts import (CapabilityState, GateDefinition, GateStatus, GateValidator,
                        RunStatus, RuntimeCapabilities, RuntimeCapability,
                        RuntimeLimits, RuntimeProfile, RuntimeRequest,
                        RuntimeResult, ValidationRequest, ValidationResult,
                        ValidationRuntime)
from .snapshot import SnapshotError, SnapshotManager

if TYPE_CHECKING:
    from agentflow.state.validation import ValidationStateStore


SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOCAL_REQUIRED = {"argv_only", "timeout", "process_identity", "bounded_output",
                  "private_temp", "snapshot_digest"}
DOCKER_REQUIRED = LOCAL_REQUIRED | {"pinned_image", "network_none", "readonly_source",
                                   "cpu_limit", "memory_limit", "process_limit",
                                   "no_privileges", "no_host_socket", "workspace_limit"}


def policy_snapshot(profile: RuntimeProfile, limits: RuntimeLimits | None = None, *,
                    required_capabilities: tuple[str, ...] = (),
                    allow_unconfined_network: bool = False) -> tuple[str, str]:
    limits = limits or RuntimeLimits()
    value = {
        "version": 1,
        "runtime_profile": profile.value,
        "limits": asdict(limits),
        "required_capabilities": sorted(set(required_capabilities)),
        "network": {"mode": "unconfined" if allow_unconfined_network else "disabled",
                    "allow_unconfined": allow_unconfined_network},
    }
    return canonical(value), digest(value)


class ValidationScheduler:
    """Run only the explicitly selected gates against a verified snapshot."""

    def __init__(self, store: ValidationStateStore, provider: GitProvider,
                 runtime: ValidationRuntime, validator: GateValidator,
                 snapshot_manager: SnapshotManager, snapshot_parent: Path,
                 output_parent: Path):
        self.store = store
        self.provider = provider
        self.runtime = runtime
        self.validator = validator
        self.snapshot_manager = snapshot_manager
        self.snapshot_parent = snapshot_parent
        self.output_parent = output_parent

    def run(self, request: ValidationRequest, workspace: Workspace,
            checkpoint: Checkpoint, plan: Plan,
            cancellation: Event | None = None) -> ValidationResult:
        if not SAFE_COMPONENT.fullmatch(request.validation_run_id):
            raise ConflictError("validation_run_id não pode formar caminho de armazenamento")
        cancellation = cancellation or Event()
        reservation = self.store.begin_validation(request, workspace, checkpoint, plan)
        if not reservation.created:
            if reservation.status in {item.value for item in (
                    RunStatus.PASSED, RunStatus.FAILED, RunStatus.INCONCLUSIVE,
                    RunStatus.CANCELLED)}:
                return self.store.validation_result(reservation.validation_run_id)
            raise ConflictError("run anterior não terminal; reconciliação explícita necessária")

        self.store.transition_run(request.validation_run_id, RunStatus.PREFLIGHT,
                                  expected=RunStatus.REQUESTED)
        try:
            self._prepare_directory(self.snapshot_parent)
            self._prepare_directory(self.output_parent)
            snapshot = self.snapshot_manager.materialize(workspace, checkpoint,
                                                         self.snapshot_parent)
            stored_snapshot_bytes = self.store.db.execute(
                "SELECT COALESCE(sum(s.size_bytes),0) FROM validation_snapshots s "
                "JOIN validation_runs r USING(validation_run_id) "
                "WHERE r.project_id=? AND s.state IN ('verified','quarantined')",
                (request.project_id,)).fetchone()[0]
            retained_log_bytes = self.store.db.execute(
                "SELECT COALESCE(sum(g.stdout_bytes+g.stderr_bytes),0) "
                "FROM validation_gate_results g JOIN validation_runs r USING(validation_run_id) "
                "WHERE r.project_id=? AND g.logs_retained=1", (request.project_id,)).fetchone()[0]
            retained_artifact_bytes = self.store.db.execute(
                "SELECT COALESCE(sum(size_bytes),0) FROM validation_artifacts "
                "WHERE project_id=? AND status='verified'", (request.project_id,)).fetchone()[0]
            if (stored_snapshot_bytes + snapshot.size_bytes > 10 * 1024**3
                    or stored_snapshot_bytes + retained_log_bytes + retained_artifact_bytes
                    + snapshot.size_bytes > 20 * 1024**3):
                shutil.rmtree(snapshot.path)
                raise SnapshotError("quota total de snapshots do projeto (10 GiB) excedida")
            self.store.record_snapshot(request.validation_run_id, snapshot)
        except (OSError, SnapshotError, ValueError) as exc:
            self.store.transition_run(request.validation_run_id, RunStatus.FAILED,
                                      expected=RunStatus.PREFLIGHT,
                                      reason=f"snapshot/preflight: {type(exc).__name__}: {exc}")
            return self.store.validation_result(request.validation_run_id)

        selected = self._selected_gates(request, plan)
        if isinstance(self.validator, dict):
            validators = self.validator
        elif hasattr(self.validator, "for_gate"):
            validators = {gate.gate_id: self.validator.for_gate(gate, request, snapshot)
                          for gate in selected}
        else:
            validators = {gate.gate_id: self.validator for gate in selected}
        if set(validators) != {gate.gate_id for gate in selected}:
            raise ConflictError("validador ausente/divergente para gate selecionado")
        self.store.prepare_gate_results(request, selected, validators,
                                        snapshot_digest=snapshot.snapshot_digest)
        try:
            capabilities = self.runtime.inspect_capabilities(request.runtime_profile)
            policy = json.loads(request.policy_snapshot)
            limits = RuntimeLimits(**policy["limits"])
            capabilities = self._complete_capabilities(capabilities, request, policy)
            missing = self._missing_capabilities(capabilities, request, policy,
                                                 require_enforced=False)
        except Exception as exc:
            self._mark_queued_not_run(request, selected,
                                      f"capability preflight falhou: {type(exc).__name__}")
            self.store.transition_run(request.validation_run_id, RunStatus.FAILED,
                                      expected=RunStatus.PREFLIGHT,
                                      reason="capabilities do runtime não puderam ser verificadas")
            return self.store.validation_result(request.validation_run_id)
        if missing:
            self._mark_queued_not_run(request, selected,
                                      "capacidade obrigatória indisponível: " + ", ".join(missing),
                                      capabilities)
            self.store.transition_run(request.validation_run_id, RunStatus.FAILED,
                                      expected=RunStatus.PREFLIGHT,
                                      reason="preflight bloqueado por capacidade indisponível")
            return self.store.validation_result(request.validation_run_id)

        run_output = self.output_parent / request.validation_run_id
        if run_output.exists() or run_output.is_symlink():
            self._mark_queued_not_run(request, selected,
                                      "diretório de output já existe; preservado",
                                      capabilities)
            self.store.transition_run(request.validation_run_id, RunStatus.FAILED,
                                      expected=RunStatus.PREFLIGHT,
                                      reason="diretório de output não é exclusivo")
            return self.store.validation_result(request.validation_run_id)
        try:
            run_output.mkdir(mode=0o700)
        except OSError as exc:
            self._mark_queued_not_run(request, selected,
                                      "diretório de output não pôde ser reservado",
                                      capabilities)
            self.store.transition_run(request.validation_run_id, RunStatus.FAILED,
                                      expected=RunStatus.PREFLIGHT,
                                      reason=f"output preflight: {type(exc).__name__}")
            return self.store.validation_result(request.validation_run_id)

        self.store.transition_run(request.validation_run_id, RunStatus.RUNNING,
                                  expected=RunStatus.PREFLIGHT)
        outcomes: dict[str, GateStatus] = {}
        unresolved = False
        gate_map = {gate.gate_id: gate for gate in selected}
        command_map = {command.command_id: command
                       for command in plan.specification.validation_commands}
        platform = "windows" if os.name == "nt" else "linux"
        stored_log_bytes = self.store.db.execute(
            "SELECT COALESCE(sum(stdout_bytes+stderr_bytes),0) FROM validation_gate_results g "
            "JOIN validation_runs r USING(validation_run_id) WHERE r.project_id=? AND g.logs_retained=1",
            (request.project_id,)).fetchone()[0]
        run_output_remaining = min(64 * 1024**2,
                                   max(0, 5 * 1024**3 - stored_log_bytes))

        for gate in selected:
            if cancellation.is_set():
                self._terminalize_not_run(request, gate, "cancelamento explícito", capabilities)
                outcomes[gate.gate_id] = GateStatus.NOT_RUN
                continue
            if run_output_remaining <= 0:
                self._terminalize_not_run(request, gate,
                                          "quota de logs por run esgotada", capabilities)
                outcomes[gate.gate_id] = GateStatus.NOT_RUN
                continue
            dependencies = [dependency for dependency in gate.depends_on
                            if dependency in gate_map]
            failed_dependency = next((dependency for dependency in dependencies
                                      if outcomes.get(dependency) is not GateStatus.PASSED), None)
            if failed_dependency:
                dependency_status = outcomes[failed_dependency]
                self._terminalize_not_run(
                    request, gate,
                    f"dependência {failed_dependency} terminou {dependency_status.value}",
                    capabilities)
                outcomes[gate.gate_id] = GateStatus.NOT_RUN
                continue

            self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                       GateStatus.PREFLIGHT, expected=GateStatus.QUEUED,
                                       capabilities_json=self._caps_json(capabilities))
            try:
                self.snapshot_manager.verify_or_raise(snapshot)
                if not self.provider.verify_checkpoint(workspace, checkpoint):
                    raise SnapshotError("checkpoint deixou de ser vigente")
            except SnapshotError as exc:
                self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                           GateStatus.FAILED, expected=GateStatus.PREFLIGHT,
                                           reason=f"snapshot/checkpoint inválido: {exc}",
                                           capabilities_json=self._caps_json(capabilities))
                outcomes[gate.gate_id] = GateStatus.FAILED
                self._mark_queued_not_run(request, selected, "fonte imutável inválida",
                                          capabilities, outcomes)
                break
            except OSError as exc:
                self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                           GateStatus.INCONCLUSIVE, expected=GateStatus.PREFLIGHT,
                                           reason=f"integridade não verificável: {type(exc).__name__}",
                                           capabilities_json=self._caps_json(capabilities))
                outcomes[gate.gate_id] = GateStatus.INCONCLUSIVE
                self._mark_queued_not_run(request, selected, "snapshot indisponível",
                                          capabilities, outcomes)
                break

            command = command_map.get(gate.command_id)
            platform_commands = [item for item in plan.specification.validation_commands
                                 if item.command_id == gate.command_id and item.platform == platform]
            if command is None or not platform_commands:
                self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                           GateStatus.FAILED, expected=GateStatus.PREFLIGHT,
                                           reason=f"allowlist sem comando para {platform}",
                                           capabilities_json=self._caps_json(capabilities))
                outcomes[gate.gate_id] = GateStatus.FAILED
                continue
            selected_command = platform_commands[0]
            gate_output = self.output_parent / request.validation_run_id / gate.gate_id
            try:
                gate_output.mkdir(mode=0o700)
            except OSError as exc:
                self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                           GateStatus.FAILED, expected=GateStatus.PREFLIGHT,
                                           reason=f"workspace de output indisponível: {type(exc).__name__}",
                                           capabilities_json=self._caps_json(capabilities))
                outcomes[gate.gate_id] = GateStatus.FAILED
                continue
            command_contract = self._command_contract(selected_command)
            substitutions = {
                "{snapshot_dir}": str(snapshot.path),
                "{output_dir}": str(gate_output),
                "{gate_id}": gate.gate_id,
                "{definition_revision}": str(request.definition_revision),
                "{snapshot_digest}": snapshot.snapshot_digest,
            }
            arguments = tuple(next((value for placeholder, value in substitutions.items()
                                    if argument == placeholder), argument)
                              for argument in selected_command.arguments)
            limits_for_gate = replace(
                limits,
                timeout_seconds=min(limits.timeout_seconds, gate.timeout_seconds),
                output_bytes=min(limits.output_bytes, gate.output_limit_bytes,
                                 run_output_remaining),
            )
            runtime_request = RuntimeRequest(
                command_contract, arguments,
                snapshot.path if selected_command.working_directory == "snapshot" else gate_output,
                (), limits_for_gate, gate_output, request.allow_unconfined_network,
                selected_command.container_image,
                selected_command.container_executable,
                snapshot.path,
                request.validation_run_id,
                gate.gate_id,
            )
            self.store.record_gate_spawn_intent(request.validation_run_id, gate.gate_id)
            spawned: list[str] = []

            def on_spawn(identity: str) -> None:
                if not identity or spawned:
                    raise ConflictError("runtime forneceu identidade de processo inválida/duplicada")
                self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                           GateStatus.RUNNING, expected=GateStatus.PREFLIGHT,
                                           capabilities_json=self._caps_json(capabilities),
                                           process_identity=identity)
                spawned.append(identity)

            def on_probe(identity: str) -> None:
                self.store.record_gate_probe_identity(request.validation_run_id,
                                                      gate.gate_id, identity)

            process_result = self.runtime.run(runtime_request, cancellation, on_spawn,
                                              on_probe)
            process_policy = json.loads(request.policy_snapshot)
            process_result = replace(process_result, capabilities=self._complete_capabilities(
                process_result.capabilities, request, process_policy))
            if process_result.spawn_failed and not spawned:
                gate_status = (GateStatus.FAILED if process_result.process_stopped
                               else GateStatus.INCONCLUSIVE)
                self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                           gate_status, expected=GateStatus.PREFLIGHT,
                                           reason=process_result.reason or "spawn falhou",
                                           capabilities_json=self._caps_json(process_result.capabilities))
                outcomes[gate.gate_id] = gate_status
                if not process_result.process_stopped:
                    unresolved = True
                    self._mark_queued_not_run(request, selected,
                                              "version probe/spawn não reconciliado",
                                              capabilities, outcomes)
                    break
                continue
            if not spawned:
                self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                           GateStatus.INCONCLUSIVE, expected=GateStatus.PREFLIGHT,
                                           reason="runtime não persistiu identidade após spawn",
                                           capabilities_json=self._caps_json(process_result.capabilities))
                outcomes[gate.gate_id] = GateStatus.INCONCLUSIVE
                unresolved = True
                self._mark_queued_not_run(request, selected, "spawn sem identidade reconciliável",
                                          capabilities, outcomes)
                break

            gate_status, reason, is_unresolved = self._evaluate_result(
                process_result, spawned[0], request, workspace, checkpoint,
                snapshot, cancellation, validators[gate.gate_id])
            emitted_bytes = process_result.stdout_bytes + process_result.stderr_bytes
            run_output_remaining = max(0, run_output_remaining - emitted_bytes)
            unresolved = unresolved or is_unresolved
            self.store.transition_gate(
                request.validation_run_id, gate.gate_id, gate_status,
                expected=GateStatus.RUNNING, exit_code=process_result.exit_code,
                reason=reason, capabilities_json=self._caps_json(process_result.capabilities),
                stdout_digest=process_result.stdout_digest,
                stderr_digest=process_result.stderr_digest,
                stdout_bytes=process_result.stdout_bytes,
                stderr_bytes=process_result.stderr_bytes,
                truncated=process_result.truncated,
                process_identity=process_result.process_identity,
            )
            outcomes[gate.gate_id] = gate_status
            if unresolved:
                self._mark_queued_not_run(request, selected, "processo não pôde ser reconciliado",
                                          capabilities, outcomes)
                break

        run_status = (RunStatus.INCONCLUSIVE if unresolved else
                      self._aggregate(selected, outcomes, cancellation.is_set()))
        self.store.transition_run(request.validation_run_id, run_status,
                                  expected=RunStatus.RUNNING,
                                  reason="validação sequencial finalizada",
                                  unresolved=unresolved)
        return self.store.validation_result(request.validation_run_id)

    def recover_interrupted(self, run_id: str, *, process_stopped: bool | None,
                            reason: str) -> str:
        return self.store.recover_interrupted_validation(run_id,
                                                          process_stopped=process_stopped,
                                                          reason=reason)

    def _selected_gates(self, request: ValidationRequest, plan: Plan) -> tuple[GateDefinition, ...]:
        task = next((item for item in plan.tasks if item.id == request.task_id), None)
        if task is None:
            raise ConflictError("tarefa não existe no plano carregado")
        selected = set(request.gate_ids)
        return tuple(gate for gate_id in plan.gate_order if gate_id in selected
                     for gate in task.gates if gate.gate_id == gate_id)

    @staticmethod
    def _command_contract(command):
        from .contracts import CommandDefinition
        return CommandDefinition(command.command_id, command.executable,
                                 command.executable_digest, command.arguments,
                                 command.working_directory, command.expected_version)

    @staticmethod
    def _missing_capabilities(capabilities: RuntimeCapabilities,
                              request: ValidationRequest, policy: dict, *,
                              require_enforced: bool = True) -> tuple[str, ...]:
        if capabilities.profile is not request.runtime_profile:
            return ("runtime_profile",)
        reported = {item.name: item.state for item in capabilities.capabilities}
        required = set(policy.get("required_capabilities", []))
        required |= (DOCKER_REQUIRED if request.runtime_profile is RuntimeProfile.DOCKER_ISOLATED
                     else LOCAL_REQUIRED)
        if not request.allow_unconfined_network:
            required.add("network_none")
        accepted = ({CapabilityState.ENFORCED} if require_enforced else
                    {CapabilityState.ENFORCED, CapabilityState.SUPPORTED})
        missing = sorted(name for name in required if reported.get(name) not in accepted)
        if request.allow_unconfined_network:
            missing = [name for name in missing if name != "network_none"]
        return tuple(missing)

    @staticmethod
    def _complete_capabilities(capabilities: RuntimeCapabilities,
                               request: ValidationRequest, policy: dict) -> RuntimeCapabilities:
        expected = {"argv_only", "timeout", "process_identity", "bounded_output",
                    "private_temp", "snapshot_digest", "network_none", "cpu_limit",
                    "memory_limit", "process_limit", "readonly_source", "workspace_limit"}
        if request.runtime_profile is RuntimeProfile.DOCKER_ISOLATED:
            expected |= {"pinned_image", "no_privileges", "no_host_socket"}
        expected |= set(policy.get("required_capabilities", []))
        existing = {item.name: item for item in capabilities.capabilities}
        for name in expected - existing.keys():
            existing[name] = RuntimeCapability(name, CapabilityState.UNAVAILABLE,
                                               "runtime did not report this capability")
        return RuntimeCapabilities(capabilities.profile, capabilities.runtime_id,
                                   capabilities.runtime_version,
                                   tuple(existing[name] for name in sorted(existing)))

    def _evaluate_result(self, result: RuntimeResult, spawned_identity: str,
                         request: ValidationRequest, workspace: Workspace,
                         checkpoint: Checkpoint, snapshot,
                         cancellation, validator) -> tuple[GateStatus, str, bool]:
        if result.process_identity != spawned_identity:
            return GateStatus.INCONCLUSIVE, "identidade final diverge da identidade persistida", True
        if not result.process_stopped:
            return GateStatus.INCONCLUSIVE, "término da árvore de processos não foi confirmado", True
        if not result.logs_sanitized:
            return GateStatus.INCONCLUSIVE, "logs descartados porque a sanitização não foi verificada", False
        if result.capabilities.profile is not request.runtime_profile:
            return GateStatus.INCONCLUSIVE, "perfil runtime mudou durante a execução", False
        missing = self._missing_capabilities(result.capabilities, request,
                                             json.loads(request.policy_snapshot))
        if missing:
            return GateStatus.INCONCLUSIVE, "capacidade efetiva divergiu: " + ", ".join(missing), False
        if result.cancelled or cancellation.is_set():
            return GateStatus.CANCELLED, "gate cancelado", False
        if result.timed_out:
            return GateStatus.INCONCLUSIVE, "timeout; nenhum resultado de validação presumido", False
        if result.truncated:
            return GateStatus.INCONCLUSIVE, "limite de logs atingido; evidência incompleta", False
        if result.spawn_failed:
            return GateStatus.FAILED, result.reason or "spawn falhou", False
        if result.exit_code != 0:
            return GateStatus.FAILED, f"processo retornou código {result.exit_code}", False
        try:
            self.snapshot_manager.verify_or_raise(snapshot)
            if not self.provider.verify_checkpoint(workspace, checkpoint):
                return GateStatus.FAILED, "checkpoint de origem mudou durante o gate", False
        except SnapshotError as exc:
            return GateStatus.FAILED, f"mutação da fonte: {exc}", False
        except OSError as exc:
            return GateStatus.INCONCLUSIVE, f"integridade não verificável: {type(exc).__name__}", False
        try:
            valid, reason = validator.validate(result)
        except Exception as exc:
            return GateStatus.INCONCLUSIVE, f"validador falhou: {type(exc).__name__}", False
        if not valid:
            return GateStatus.FAILED, reason or "validação independente recusou o resultado", False
        return GateStatus.PASSED, reason or "validação independente aprovada", False

    @staticmethod
    def _aggregate(gates: tuple[GateDefinition, ...],
                   outcomes: dict[str, GateStatus], cancelled: bool = False) -> RunStatus:
        required = [gate for gate in gates if gate.required]
        statuses = [outcomes.get(gate.gate_id, GateStatus.NOT_RUN) for gate in required]
        if any(status is GateStatus.FAILED for status in statuses):
            return RunStatus.FAILED
        by_id = {gate.gate_id: gate for gate in gates}

        def ancestors(gate_id: str, seen: set[str] | None = None) -> set[str]:
            seen = set() if seen is None else seen
            if gate_id in seen:
                return set()
            seen.add(gate_id)
            direct = set(by_id[gate_id].depends_on) & by_id.keys()
            result = set(direct)
            for dependency in direct:
                result |= ancestors(dependency, seen)
            return result

        required_not_run_ancestors = {
            outcomes.get(ancestor, GateStatus.NOT_RUN)
            for gate in required if outcomes.get(gate.gate_id) is GateStatus.NOT_RUN
            for ancestor in ancestors(gate.gate_id)
        }
        if (any(status is GateStatus.CANCELLED for status in statuses)
                or cancelled and any(status is GateStatus.NOT_RUN for status in statuses)
                or GateStatus.CANCELLED in required_not_run_ancestors):
            return RunStatus.CANCELLED
        if (GateStatus.FAILED in required_not_run_ancestors
                or any(status is GateStatus.FAILED for status in statuses)):
            return RunStatus.FAILED
        if (any(status in {GateStatus.INCONCLUSIVE, GateStatus.NOT_RUN} for status in statuses)
                or GateStatus.INCONCLUSIVE in required_not_run_ancestors
                or GateStatus.NOT_RUN in required_not_run_ancestors):
            return RunStatus.INCONCLUSIVE
        return RunStatus.PASSED

    def _mark_queued_not_run(self, request, gates, reason, capabilities=None, outcomes=None):
        statuses = outcomes if outcomes is not None else {}
        for gate in gates:
            if statuses.get(gate.gate_id) is not None:
                continue
            rows = {row[0]: row[1] for row in self.store.gate_rows(request.validation_run_id)}
            current = rows.get(gate.gate_id)
            if current == GateStatus.QUEUED.value:
                self._terminalize_not_run(request, gate, reason, capabilities)
                statuses[gate.gate_id] = GateStatus.NOT_RUN

    def _terminalize_not_run(self, request, gate, reason, capabilities=None):
        self.store.transition_gate(request.validation_run_id, gate.gate_id,
                                   GateStatus.NOT_RUN, expected=GateStatus.QUEUED,
                                   reason=reason,
                                   capabilities_json=self._caps_json(capabilities) if capabilities else "[]")

    @staticmethod
    def _caps_json(capabilities: RuntimeCapabilities | None) -> str:
        items = [] if capabilities is None else [
            {"name": item.name, "state": item.state.value,
             "effective_value": item.effective_value[:128]}
            for item in sorted(capabilities.capabilities, key=lambda value: value.name)
        ]
        return canonical(items)

    @staticmethod
    def _prepare_directory(path: Path) -> None:
        candidate = path.absolute()
        current = Path(candidate.anchor)
        for component in candidate.parts[1:]:
            current = current / component
            if current.exists() and _is_link_or_reparse(current):
                raise ConflictError("caminho de storage contém symlink/reparse point")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _is_link_or_reparse(path) or not path.is_dir():
            raise ConflictError("diretório runtime inválido")


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            import stat
            attributes = path.lstat().st_file_attributes
            return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except (OSError, AttributeError):
            return False
    return False
