"""Bridge the grant-scoped APL controller to the existing Codex adapter/runtime.

This is an injected execution component, not a dispatch switch. It has no
authority to create grants and the CLI does not construct it by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import base64
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Protocol
from threading import Event

from agentflow.agents import ExecutionPolicy, ExecutionRequest, ProcessIdentity
from agentflow.planning.prompts import next_task_prompt
from agentflow.apl.contracts import ExecutionOutcome, JobProposal
from .failure_recovery import QualityResult

from .authorized_execution import VerifiedExecutionGrant


class CodexExecutionError(RuntimeError):
    """The Codex bridge rejected a grant, workspace, or process result."""


@dataclass(frozen=True)
class PinnedWorkspace:
    path: Path
    project_id: str
    workspace_id: str
    identity_digest: str


class ExecutionWorkspaceResolver(Protocol):
    """Resolve the current persisted binding; implementations must fail closed."""

    def resolve(self, cycle_id: str) -> PinnedWorkspace: ...


class ExecutionResultAcceptance(Protocol):
    """Validate job-specific acceptance criteria against durable process evidence."""

    def validate(self, cycle_id: str, job: JobProposal, outcome: ExecutionOutcome) -> bool: ...


@dataclass(frozen=True)
class AcceptanceEvaluation:
    quality: QualityResult
    evidence: tuple[dict, ...]


class PlanGateAcceptanceVerifier:
    """Verify task criteria with the project's allowlisted gates on a copied snapshot.

    Gates run as a distinct process through the existing validation runtime. A
    process receipt or a provider's claim alone never produces PASS.
    """

    def __init__(self, *, plan, workspace: Path, authorized_scope: tuple[str, ...],
                 evidence_directory: Path, runtime=None, allow_unconfined_network=False,
                 max_snapshot_bytes: int = 1024**3):
        if (not workspace.is_absolute() or not authorized_scope
                or type(allow_unconfined_network) is not bool
                or type(max_snapshot_bytes) is not int or max_snapshot_bytes < 1):
            raise ValueError("acceptance_verifier_configuration_invalid")
        self.plan, self.workspace = plan, workspace.resolve(strict=True)
        self.scope, self.evidence_directory = tuple(authorized_scope), evidence_directory
        self.allow_unconfined_network = allow_unconfined_network
        self.max_snapshot_bytes = max_snapshot_bytes
        if runtime is None:
            from agentflow.validation import LocalProcessRuntime
            runtime = LocalProcessRuntime()
        self.runtime = runtime

    def validate(self, cycle_id: str, job: JobProposal, outcome: ExecutionOutcome) -> bool:
        return self.evaluate(cycle_id, job, outcome).quality is QualityResult.PASS

    def evaluate(self, cycle_id: str, job: JobProposal,
                 outcome: ExecutionOutcome) -> AcceptanceEvaluation:
        receipts = [item for item in outcome.evidence
                    if item.get("kind") in {"codex_process_receipt", "agent_process_receipt"}]
        executor_finished = bool(outcome.status == "succeeded" and len(receipts) == 1
                                 and receipts[0].get("process_status") == "process_succeeded")
        command_succeeded = bool(executor_finished and receipts[0].get("exit_code") == 0)
        base = {"kind": "acceptance_evaluation", "cycle_id": cycle_id,
                "job_id": job.job_id,
                "operation_id": receipts[0].get("operation_id") if receipts else None,
                "attempt_id": receipts[0].get("attempt_id") if receipts else None,
                "executor_finished": executor_finished,
                "executor_exit_code": receipts[0].get("exit_code") if receipts else None,
                "executor_command_succeeded": command_succeeded,
                "artifacts_status": "verified_by_acceptance_gates_pending",
                "criteria_status": "inconclusive", "work_status": "not_approved"}
        if not command_succeeded:
            quality = (QualityResult.FAIL if outcome.status == "failed"
                       or (executor_finished and receipts[0].get("exit_code") not in (0, None))
                       else QualityResult.INCONCLUSIVE)
            base.update(artifacts_status="not_verified", criteria_status=quality.value,
                        work_status="not_approved", reason="executor_not_successful")
            return AcceptanceEvaluation(quality, (base,))
        try:
            result = self._run_gates(cycle_id, job, receipts[0])
        except (OSError, ValueError, RuntimeError) as exc:
            base.update(artifacts_status="inconclusive", criteria_status="inconclusive",
                        work_status="not_approved",
                        reason=f"acceptance_preflight:{type(exc).__name__}")
            return AcceptanceEvaluation(QualityResult.INCONCLUSIVE, (base,))
        gates = result
        statuses = {item["status"] for item in gates}
        if "fail" in statuses:
            quality = QualityResult.FAIL
        elif not gates or statuses != {"pass"}:
            quality = QualityResult.INCONCLUSIVE
        else:
            quality = QualityResult.PASS
        base.update(artifacts_status=("verified_by_acceptance_gates" if quality is QualityResult.PASS
                                      else "inconclusive"),
                    criteria_status=quality.value,
                    work_status=("approved" if quality is QualityResult.PASS else "not_approved"),
                    gate_count=len(gates),
                    gate_receipts=gates)
        return AcceptanceEvaluation(quality, (base,))

    def _run_gates(self, cycle_id: str, job: JobProposal, receipt: dict) -> list[dict]:
        from agentflow.planning.models import Task
        from agentflow.validation import (CommandDefinition, RuntimeLimits,
                                          RuntimeProfile, RuntimeRequest,
                                          StructuredGateValidator)
        task = next((item for item in self.plan.tasks if item.id == job.job_id), None)
        if not isinstance(task, Task) or not task.gates or not task.acceptance_criteria:
            raise ValueError("acceptance_gates_missing")
        if tuple(item.id for item in task.acceptance_criteria) != job.acceptance_criteria:
            raise ValueError("acceptance_criteria_plan_mismatch")
        criteria = {item.id for item in task.acceptance_criteria}
        required_gates = {gate.gate_id for gate in task.gates if gate.required}
        gates_by_id = {gate.gate_id: gate for gate in task.gates}
        selected = set(required_gates)
        pending = list(selected)
        while pending:
            gate = gates_by_id[pending.pop()]
            for dependency in gate.depends_on:
                if dependency not in gates_by_id:
                    raise ValueError("acceptance_gate_dependency_missing")
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
        coverage = {criterion for gate in task.gates if gate.gate_id in selected
                    for criterion in gate.criterion_ids}
        if not criteria <= coverage:
            raise ValueError("acceptance_criteria_not_covered")

        platform = "windows" if os.name == "nt" else "linux"
        commands = {command.command_id: command
                    for command in self.plan.specification.validation_commands
                    if command.platform == platform}
        ordered = [gate for gate_id in self.plan.gate_order
                   for gate in task.gates if gate.gate_id == gate_id and gate_id in selected]
        if len(ordered) != len(selected):
            raise ValueError("acceptance_gate_order_incomplete")
        operation = str(receipt.get("operation_id") or "")
        attempt = str(receipt.get("attempt_id") or operation)
        safe_key = hashlib.sha256(f"{cycle_id}:{job.job_id}:{attempt}".encode()).hexdigest()
        gate_results = []
        with tempfile.TemporaryDirectory(prefix="agentflow-apl-verify-") as temp_name:
            snapshot_path = Path(temp_name) / "snapshot"
            snapshot_path.mkdir()
            manifest, size = self._copy_scoped_workspace(snapshot_path)
            if size > self.max_snapshot_bytes:
                raise ValueError("acceptance_snapshot_quota_exceeded")
            snapshot_digest = "sha256:" + hashlib.sha256(json.dumps(
                manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            self.evidence_directory.mkdir(parents=True, exist_ok=True)
            if self.evidence_directory.is_symlink():
                raise ValueError("acceptance_evidence_directory_symlink_forbidden")
            for gate in ordered:
                command = commands.get(gate.command_id)
                if command is None:
                    gate_results.append({"gate_id": gate.gate_id, "status": "inconclusive",
                                         "reason": "allowlisted_command_missing_for_platform"})
                    continue
                capabilities = self.runtime.inspect_capabilities(RuntimeProfile.LOCAL_EXPLICIT)
                cap_states = {item.name: item.state.value for item in capabilities.capabilities}
                if (cap_states.get("network_none") != "enforced"
                        and not self.allow_unconfined_network):
                    gate_results.append({"gate_id": gate.gate_id, "status": "inconclusive",
                                         "reason": "validation_network_isolation_unavailable"})
                    continue
                report_root = self.evidence_directory / safe_key
                report_root.mkdir(mode=0o700, exist_ok=True)
                gate_output = report_root / gate.gate_id
                gate_output.mkdir(mode=0o700, exist_ok=False)
                command_contract = CommandDefinition(
                    command.command_id, command.executable, command.executable_digest,
                    command.arguments, command.working_directory, command.expected_version)
                substitutions = {"{snapshot_dir}": str(snapshot_path),
                                 "{output_dir}": str(gate_output),
                                 "{gate_id}": gate.gate_id,
                                 "{definition_revision}": str(gate.version),
                                 "{snapshot_digest}": snapshot_digest}
                arguments = tuple(substitutions.get(value, value)
                                  for value in command.arguments)
                cwd = snapshot_path if command.working_directory == "snapshot" else gate_output
                limits = RuntimeLimits(timeout_seconds=gate.timeout_seconds,
                                        output_bytes=gate.output_limit_bytes)
                request = RuntimeRequest(
                    command_contract, arguments, cwd, (), limits, gate_output,
                    self.allow_unconfined_network, command.container_image,
                    command.container_executable, snapshot_path,
                    "apl-verify-" + safe_key[:24], gate.gate_id)
                runtime_result = self.runtime.run(
                    request, Event(), lambda _identity: None, lambda _identity: None)
                validator = StructuredGateValidator(
                    gate.gate_id, gate.version, snapshot_digest, gate.criterion_ids,
                    max_report_bytes=min(gate.output_limit_bytes, 1024 * 1024))
                passed, reason = validator.validate(runtime_result)
                if passed:
                    status = "pass"
                elif (runtime_result.spawn_failed and runtime_result.process_stopped
                      or runtime_result.exit_code not in (0, None)
                      and not runtime_result.timed_out and not runtime_result.cancelled
                      and runtime_result.process_stopped):
                    status = "fail"
                else:
                    status = "inconclusive"
                gate_results.append({
                    "gate_id": gate.gate_id, "status": status, "reason": reason,
                    "validator_id": validator.validator_id, "validator_version": validator.version,
                    "validator_digest": validator.digest, "snapshot_digest": snapshot_digest,
                    "exit_code": runtime_result.exit_code,
                    "stdout_digest": runtime_result.stdout_digest,
                    "stderr_digest": runtime_result.stderr_digest,
                    "stdout_path": str(runtime_result.stdout_path),
                    "stderr_path": str(runtime_result.stderr_path),
                    "artifacts_status": "verified_by_allowlisted_gate" if passed else "not_verified",
                })
                if status != "pass":
                    break
        return gate_results

    def _copy_scoped_workspace(self, destination: Path) -> tuple[list[dict], int]:
        manifest: list[dict] = []
        total = 0
        copied: set[str] = set()
        for raw in self.scope:
            if raw == "network:unconfined":
                continue
            candidate = Path(raw)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ValueError("acceptance_scope_path_invalid")
            unresolved = self.workspace
            for part in candidate.parts:
                unresolved = unresolved / part
                if unresolved.is_symlink():
                    raise ValueError("acceptance_scope_symlink_forbidden")
            source = (self.workspace / candidate).resolve(strict=True)
            if self.workspace != source and self.workspace not in source.parents:
                raise ValueError("acceptance_scope_path_escaped_workspace")
            if source.is_symlink():
                raise ValueError("acceptance_scope_symlink_forbidden")
            relative = source.relative_to(self.workspace)
            target = destination / relative
            if source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                paths = [(source, target, relative.as_posix())]
            elif source.is_dir():
                paths = []
                for directory, dirs, files in os.walk(source, followlinks=False):
                    current = Path(directory)
                    dirs[:] = [name for name in dirs
                               if name.casefold() not in {".git", ".agentflow", "node_modules"}]
                    for name in list(dirs):
                        child = current / name
                        if child.is_symlink():
                            raise ValueError("acceptance_snapshot_symlink_forbidden")
                    for name in files:
                        child = current / name
                        if child.is_symlink() or not child.is_file():
                            raise ValueError("acceptance_snapshot_nonregular_file")
                        rel = child.relative_to(self.workspace)
                        paths.append((child, destination / rel, rel.as_posix()))
            else:
                raise ValueError("acceptance_scope_not_file_or_directory")
            for input_path, output_path, key in paths:
                if key in copied:
                    continue
                copied.add(key)
                size = input_path.stat().st_size
                total += size
                if total > self.max_snapshot_bytes:
                    raise ValueError("acceptance_snapshot_quota_exceeded")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(input_path, output_path)
                hasher = hashlib.sha256()
                with output_path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        hasher.update(chunk)
                digest_value = hasher.hexdigest()
                manifest.append({"path": key, "size": size, "sha256": digest_value})
        support_roots = self._validation_support_roots()
        for source_root in support_roots:
            if not source_root.exists():
                continue
            if source_root.is_symlink() or not source_root.is_dir():
                raise ValueError("validation_dependency_root_invalid")
            relative_root = source_root.relative_to(self.workspace)
            total = self._copy_validation_tree(
                source_root, source_root, destination / relative_root, relative_root,
                manifest, copied, total, 0, set())
        manifest.sort(key=lambda item: item["path"])
        if not manifest:
            raise ValueError("acceptance_snapshot_empty")
        return manifest, total

    def _validation_support_roots(self) -> tuple[Path, ...]:
        roots = set()
        for command in self.plan.specification.validation_commands:
            arguments = command.arguments
            try:
                if "--checks-b64" in arguments:
                    index = arguments.index("--checks-b64")
                    checks = json.loads(base64.b64decode(
                        arguments[index + 1], altchars=b"-_", validate=True).decode("utf-8"))
                else:
                    index = arguments.index("--checks-json")
                    checks = json.loads(arguments[index + 1])
            except (ValueError, IndexError, json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(checks, list):
                continue
            for check in checks:
                if not isinstance(check, dict) or not isinstance(check.get("package_directory"), str):
                    continue
                package = check["package_directory"]
                if package and (package.startswith("/") or ".." in Path(package).parts
                                or "\\" in package):
                    raise ValueError("validation_package_path_invalid")
                directories = [self.workspace / "node_modules"]
                if package:
                    directories.insert(0, self.workspace / package / "node_modules")
                for candidate in directories:
                    if candidate.is_symlink():
                        raise ValueError("validation_dependency_root_symlink_forbidden")
                    resolved = candidate.resolve(strict=False)
                    if (resolved.name.casefold() == "node_modules"
                            and self.workspace in resolved.parents):
                        roots.add(resolved)
        return tuple(sorted(roots, key=lambda item: str(item).casefold()))

    def _copy_validation_tree(self, source: Path, tree_root: Path, destination: Path,
                              relative_root: Path, manifest: list[dict], copied: set[str],
                              total: int, depth: int, stack: set[Path]) -> int:
        if depth > 64:
            raise ValueError("validation_dependency_tree_too_deep")
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            raise ValueError("validation_dependency_link_broken") from exc
        if tree_root != resolved and tree_root not in resolved.parents:
            raise ValueError("validation_dependency_link_escaped")
        if resolved.is_dir():
            if resolved in stack:
                raise ValueError("validation_dependency_link_cycle")
            stack.add(resolved)
            destination.mkdir(parents=True, exist_ok=True)
            try:
                children = sorted(source.iterdir(), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise ValueError("validation_dependency_tree_unreadable") from exc
            for child in children:
                path_parts = child.relative_to(self.workspace).parts
                lower_parts = {part.casefold() for part in path_parts}
                sensitive = (lower_parts & {".git", ".agentflow", "credentials", "secrets"}
                             or any(part.startswith(".env") or "credential" in part
                                    or part.endswith((".pem", ".p12", ".pfx", ".key",
                                                     ".sqlite", ".sqlite3", ".db", ".token"))
                                    or part in {"id_rsa", "id_ed25519"}
                                    for part in lower_parts))
                if sensitive:
                    raise ValueError("validation_dependency_sensitive_path_rejected")
                relative = child.relative_to(self.workspace)
                key = relative.as_posix()
                if key in copied:
                    continue
                total = self._copy_validation_tree(
                    child, tree_root, destination / child.name, relative, manifest,
                    copied, total, depth + 1, stack)
            stack.remove(resolved)
            return total
        if not resolved.is_file():
            raise ValueError("validation_dependency_nonregular_file")
        if len(copied) >= 100_000:
            raise ValueError("validation_dependency_file_count_exceeded")
        size = resolved.stat().st_size
        total += size
        if total > self.max_snapshot_bytes:
            raise ValueError("acceptance_snapshot_quota_exceeded")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, destination)
        hasher = hashlib.sha256()
        with destination.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        manifest.append({"path": relative_root.as_posix(), "size": size,
                         "sha256": hasher.hexdigest()})
        copied.add(relative_root.as_posix())
        return total


class AuthorizedJobExecutor:
    """Use CodexAdapter and LocalRuntime for one exact, already-authorized job.

    The executor requires an authoritative workspace resolver and never accepts
    a workspace path supplied by the task or planner. The sandbox mode is not
    selected by AgentFlow; the Codex CLI applies its native user/project
    configuration. AgentFlow keeps independent timeout, cancellation,
    authorization, evidence, and recovery controls. This does not claim OS
    isolation.
    """

    def __init__(self, *, adapter, runtime, workspace_resolver: ExecutionWorkspaceResolver,
                 task_timeout_seconds: int = 1800, log_limit_bytes: int = 10 * 1024 * 1024,
                 clock: Callable[[], datetime] | None = None, handoff_resolver=None):
        if (not callable(getattr(adapter, "prepare_task", None))
                or not callable(getattr(adapter, "collect_result", None))
                or not callable(getattr(runtime, "run", None))
                or not callable(getattr(workspace_resolver, "resolve", None))):
            raise ValueError("codex_execution_dependencies_required")
        if type(task_timeout_seconds) is not int or not 1 <= task_timeout_seconds <= 7200:
            raise ValueError("codex_task_timeout_invalid")
        if type(log_limit_bytes) is not int or not 1 <= log_limit_bytes <= 50 * 1024 * 1024:
            raise ValueError("codex_log_limit_invalid")
        self.adapter, self.runtime = adapter, runtime
        self.handoff_resolver = handoff_resolver
        self.workspace_resolver = workspace_resolver
        self.task_timeout_seconds, self.log_limit_bytes = task_timeout_seconds, log_limit_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(self, cycle_id: str, job: JobProposal, authorized_scope: tuple[str, ...],
                operation_id: str) -> ExecutionOutcome:
        raise CodexExecutionError("verified_grant_binding_required")

    def execute_authorized(self, cycle_id: str, job: JobProposal,
                           grant: VerifiedExecutionGrant, operation_id: str, *,
                           attempt_id: str | None = None, strategy_id: str = "default",
                           remaining_seconds: float | None = None,
                           pre_spawn_check: Callable[[], None] | None = None,
                           cancellation, on_started: Callable[[ProcessIdentity], None]) -> ExecutionOutcome:
        if not isinstance(grant, VerifiedExecutionGrant) or grant.state != "approved":
            raise CodexExecutionError("verified_grant_required")
        if f"job:{job.job_id}" not in grant.allowed_operations:
            raise CodexExecutionError("job_not_authorized_by_grant")
        if not set(job.required_scope) <= set(grant.authorized_scope):
            raise CodexExecutionError("job_scope_not_authorized")
        if not re.fullmatch(r"apl-op-[a-f0-9]{32}", operation_id):
            raise CodexExecutionError("operation_id_invalid")
        if (attempt_id is not None and not re.fullmatch(r"apl-attempt-[a-f0-9]{32}", attempt_id)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", strategy_id)):
            raise CodexExecutionError("attempt_or_strategy_invalid")

        binding = self.workspace_resolver.resolve(cycle_id)
        if (not isinstance(binding, PinnedWorkspace) or not binding.project_id
                or not binding.workspace_id or not binding.identity_digest):
            raise CodexExecutionError("authoritative_workspace_binding_unavailable")
        try:
            root = binding.path.resolve(strict=True)
            info = root.stat()
        except (OSError, RuntimeError) as exc:
            raise CodexExecutionError("authorized_workspace_unavailable") from exc
        if not root.is_dir() or not info.st_dev or not info.st_ino:
            raise CodexExecutionError("authorized_workspace_identity_unavailable")
        canonical = os.path.normcase(str(root))
        identity = f"{info.st_dev}:{info.st_ino}"
        actual_digest = "sha256:" + hashlib.sha256(
            f"{canonical}\0{identity}".encode("utf-8")).hexdigest()
        if (canonical != os.path.normcase(str(Path(grant.workspace_path).resolve(strict=True)))
                or binding.workspace_id != grant.workspace_id
                or binding.identity_digest != grant.workspace_identity_digest
                or actual_digest != grant.workspace_identity_digest):
            raise CodexExecutionError("authorized_workspace_binding_changed")

        if type(grant.limits.max_seconds) not in (int, float) or grant.limits.max_seconds < 1:
            raise CodexExecutionError("grant_task_timeout_below_runtime_resolution")
        task_limit = grant.task_limits_for(job.job_id)
        timeout_ceiling = min(self.task_timeout_seconds, math.floor(grant.limits.max_seconds),
                              math.floor(task_limit.max_seconds))
        if remaining_seconds is not None:
            if type(remaining_seconds) not in (int, float) or remaining_seconds <= 0:
                raise CodexExecutionError("task_time_limit_exhausted")
            timeout_ceiling = min(timeout_ceiling, math.floor(remaining_seconds))
        timeout = timeout_ceiling
        if timeout < 1:
            raise CodexExecutionError("grant_task_timeout_below_runtime_resolution")
        criteria = tuple((f"criterion-{index + 1}", text)
                         for index, text in enumerate(job.acceptance_criteria))
        from agentflow.planning.handoff import dependency_context, extract_codex_handoff
        outcomes = self.handoff_resolver(cycle_id) if self.handoff_resolver else {}
        instruction = next_task_prompt(job.job_id, job.instruction, job.required_scope,
                                       dependency_context(job.depends_on, outcomes))
        request_data = {
            "cycle_id": cycle_id, "job_id": job.job_id,
            "instruction": instruction, "depends_on": list(job.depends_on),
            "required_scope": list(job.required_scope),
            "acceptance_criteria": list(job.acceptance_criteria),
            "grant_id": grant.grant_id, "plan_digest": grant.accepted_plan_digest,
            "revision": grant.revision, "workspace_id": grant.workspace_id,
        }
        definition_digest = "sha256:" + hashlib.sha256(json.dumps(
            request_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        request = ExecutionRequest(
            project_id=binding.project_id,
            task_id=job.job_id,
            definition_revision=grant.revision,
            definition_digest=definition_digest,
            operation_id=operation_id,
            workspace=root,
            instruction=instruction,
            policy=ExecutionPolicy(timeout_seconds=timeout, log_limit_bytes=self.log_limit_bytes,
                                   approval="never"),
            acceptance_criteria=criteria,
            workspace_id=grant.workspace_id,
        )
        spec = self.adapter.prepare_task(request)
        if (spec.adapter_name != grant.executor_id
                or spec.adapter_version != grant.executor_version):
            raise CodexExecutionError("executor_binding_does_not_match_grant")
        if (Path(spec.cwd).resolve(strict=True) != root
                or grant.executor_id == "codex" and spec.effective_approval != "never"):
            raise CodexExecutionError("codex_process_policy_mismatch")
        result = self.runtime.run(spec, attempt_id or operation_id, request.policy, on_started,
                                  cancellation, before_spawn=pre_spawn_check)
        agent_result = self.adapter.collect_result(result)
        evidence = ({
            "kind": "codex_process_receipt" if grant.executor_id == "codex" else "agent_process_receipt",
            "agent_id": grant.executor_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id or operation_id,
            "strategy_id": strategy_id,
            "process_status": result.status,
            "process_reason": result.reason,
            "exit_code": result.exit_code,
            "stdout_digest": result.stdout_digest,
            "stderr_digest": result.stderr_digest,
            "stdout_bytes": result.stdout_bytes,
            "stderr_bytes": result.stderr_bytes,
            "stdout_path": str(result.stdout_path),
            "stderr_path": str(result.stderr_path),
            "provider_id": agent_result.effective_provider_id,
            "model_id": agent_result.effective_model_id,
            "workspace_identity_digest": actual_digest,
        },)
        normalized = self.adapter.normalize_state(agent_result.process_status)
        status = "succeeded" if normalized.value == "succeeded" else (
            "failed" if normalized.value == "failed" else "unknown")
        handoff = extract_codex_handoff(result.stdout_path) if grant.executor_id == "codex" else None
        if grant.executor_id != "codex":
            from agentflow.planning.handoff import validate_handoff
            try:
                handoff = validate_handoff(json.loads(self.adapter.message_text(result.stdout_path)))
            except (ValueError, OSError, RuntimeError):
                pass
        return ExecutionOutcome(status, agent_result.summary, evidence, progress_key=operation_id, handoff=handoff)


class AgentAcceptanceEvidenceValidator:
    """Require an explicit acceptance verifier; process exit is not task success."""

    def __init__(self, acceptance_verifier: ExecutionResultAcceptance):
        if not callable(getattr(acceptance_verifier, "validate", None)):
            raise ValueError("codex_acceptance_verifier_required")
        self.acceptance_verifier = acceptance_verifier

    def validate(self, cycle_id: str, job: JobProposal, outcome: ExecutionOutcome) -> bool:
        return self.evaluate(cycle_id, job, outcome).quality is QualityResult.PASS

    def evaluate(self, cycle_id: str, job: JobProposal,
                 outcome: ExecutionOutcome) -> AcceptanceEvaluation:
        receipts = [item for item in outcome.evidence
                    if item.get("kind") in {"codex_process_receipt", "agent_process_receipt"}]
        if (outcome.status != "succeeded" or len(receipts) != 1
                or receipts[0].get("process_status") != "process_succeeded"
                or receipts[0].get("exit_code") != 0
                or not receipts[0].get("stdout_digest")
                or not receipts[0].get("stderr_digest")):
            quality = (QualityResult.FAIL if outcome.status == "failed"
                       or len(receipts) == 1 and receipts[0].get("exit_code") not in (0, None)
                       else QualityResult.INCONCLUSIVE)
            return AcceptanceEvaluation(quality, ({
                "kind": "acceptance_evaluation", "cycle_id": cycle_id,
                "job_id": job.job_id,
                "executor_finished": bool(len(receipts) == 1 and
                                           receipts[0].get("process_status") == "process_succeeded"),
                "executor_exit_code": receipts[0].get("exit_code") if len(receipts) == 1 else None,
                "executor_command_succeeded": False,
                "artifacts_status": "not_verified", "criteria_status": quality.value,
                "work_status": "not_approved", "reason": "executor_receipt_invalid",
            },))
        evaluator = getattr(self.acceptance_verifier, "evaluate", None)
        if callable(evaluator):
            result = evaluator(cycle_id, job, outcome)
            if isinstance(result, AcceptanceEvaluation):
                return result
            if (isinstance(getattr(result, "quality", None), QualityResult)
                    and type(getattr(result, "evidence", None)) is tuple):
                return AcceptanceEvaluation(result.quality, result.evidence)
            raise ValueError("acceptance_evaluation_contract_invalid")
        passed = bool(self.acceptance_verifier.validate(cycle_id, job, outcome))
        quality = QualityResult.PASS if passed else QualityResult.FAIL
        return AcceptanceEvaluation(quality, ({
            "kind": "acceptance_evaluation", "cycle_id": cycle_id,
            "job_id": job.job_id, "executor_finished": True,
            "executor_exit_code": 0, "executor_command_succeeded": True,
            "artifacts_status": "verified_by_injected_verifier" if passed else "not_verified",
            "criteria_status": quality.value,
            "work_status": "approved" if passed else "not_approved",
            "verification_source": type(self.acceptance_verifier).__name__,
        },))


# Compatibility names for existing integrations and historical tests.
CodexAuthorizedJobExecutor = AuthorizedJobExecutor
CodexAcceptanceEvidenceValidator = AgentAcceptanceEvidenceValidator
