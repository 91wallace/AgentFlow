"""Fail-closed, grant-scoped continuous execution for an immutable APL plan.

This module deliberately has no default grant validator or executor. Applications
must inject both, and the production CLI remains unavailable until the local
Broker/TEG dispatch gates have been homologated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable, Protocol
from uuid import uuid4

from .contracts import APLState, CycleSnapshot, ExecutionOutcome, JobProposal
from .failure_recovery import (EffectState, FailureDiagnosis, FailureKind,
                               IntelligentFailureRecoveryPolicy, PriorFailure,
                               QualityResult, Recoverability, RecoveryContext,
                               RecoveryLimits)


class AuthorizedExecutionError(RuntimeError):
    """A plan could not be dispatched within its exact persisted grant."""


class NoEffectExecutionError(RuntimeError):
    """Trusted executor confirms it did not start an external effect."""

    def __init__(self, message: str, *, diagnosis: FailureDiagnosis | None = None):
        super().__init__(message)
        self.diagnosis = diagnosis or FailureDiagnosis(
            FailureKind.TRANSIENT, Recoverability.POTENTIALLY_RECOVERABLE,
            EffectState.NO_EFFECT_PROVEN, "executor_confirmed_no_effect",
        )


class FinancialBudgetMode(str, Enum):
    """Financial policy; an estimate is never a provider-enforced hard cap."""

    HARD_CAP_REQUIRED = "hard_cap_required"
    ESTIMATED_COST_ACCEPTED = "estimated_cost_accepted"


@dataclass(frozen=True)
class FinancialBudgetPolicy:
    mode: FinancialBudgetMode
    estimated_amount: float | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FinancialBudgetMode):
            raise ValueError("financial_budget_mode_invalid")
        if self.mode is FinancialBudgetMode.HARD_CAP_REQUIRED:
            if self.estimated_amount is not None or self.currency is not None:
                raise ValueError("hard_cap_policy_cannot_claim_estimate_as_enforcement")
            return
        if (type(self.estimated_amount) not in (int, float)
                or not math.isfinite(self.estimated_amount) or self.estimated_amount <= 0
                or type(self.currency) is not str
                or not re.fullmatch(r"[A-Z]{3}", self.currency)):
            raise ValueError("explicit_finite_cost_estimate_required")


def _financial_estimate_limit(grant: "VerifiedExecutionGrant") -> float:
    policy = grant.budget_policy
    if not isinstance(policy, FinancialBudgetPolicy):
        raise AuthorizedExecutionError("explicit_financial_budget_policy_required")
    if policy.mode is FinancialBudgetMode.HARD_CAP_REQUIRED:
        raise AuthorizedExecutionError("financial_hard_cap_unavailable")
    values = [float(policy.estimated_amount)]
    if grant.limits.max_estimated_budget > 0:
        values.append(float(grant.limits.max_estimated_budget))
    return min(values)


@dataclass(frozen=True)
class ExecutionLimits:
    max_jobs: int
    max_attempts_per_job: int
    max_seconds: float
    max_estimated_budget: float = 0.0
    max_iterations: int = 100
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if (type(self.max_jobs) is not int or self.max_jobs < 1
                or type(self.max_attempts_per_job) is not int or self.max_attempts_per_job < 1
                or type(self.max_seconds) not in (float, int) or not math.isfinite(self.max_seconds)
                or self.max_seconds <= 0
                or type(self.max_estimated_budget) not in (float, int)
                or not math.isfinite(self.max_estimated_budget) or self.max_estimated_budget < 0
                or type(self.max_iterations) is not int or self.max_iterations < 1
                or type(self.max_concurrency) is not int or self.max_concurrency != 1):
            raise ValueError("execution_limits_invalid")


@dataclass(frozen=True)
class TaskExecutionLimits:
    max_attempts: int
    max_seconds: float
    max_estimated_budget: float = 0.0

    def __post_init__(self) -> None:
        if (type(self.max_attempts) is not int or self.max_attempts < 1
                or type(self.max_seconds) not in (int, float) or not math.isfinite(self.max_seconds)
                or self.max_seconds <= 0
                or type(self.max_estimated_budget) not in (int, float)
                or not math.isfinite(self.max_estimated_budget) or self.max_estimated_budget < 0):
            raise ValueError("task_execution_limits_invalid")


@dataclass(frozen=True)
class VerifiedExecutionGrant:
    """Authority-resolved grant and approved plan binding.

    The resolver must verify grant signature/status/expiry/revocation and resolve
    the proposal from authoritative stores on every call. It must not accept a
    caller-created grant or claims supplied by the executor.
    """

    grant_id: str
    request_digest: str
    proposal_id: str
    approved_plan_digest: str
    revision: int
    plan_revision_id: str
    accepted_plan_digest: str
    workspace_id: str
    workspace_path: str
    workspace_identity_digest: str
    allowed_operations: tuple[str, ...]
    authorized_scope: tuple[str, ...]
    expires_at: str
    limits: ExecutionLimits
    state: str = "approved"
    strategies_by_job: tuple[tuple[str, tuple[str, ...]], ...] = ()
    operator_identity: str = ""
    executor_id: str = ""
    executor_version: str = ""
    task_limits_by_job: tuple[tuple[str, TaskExecutionLimits], ...] = ()
    budget_policy: FinancialBudgetPolicy | None = None
    authorization_mode: str = "authenticated_grant"
    task_graph: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = ()
    acceptance_policy_digest: str = ""

    def __post_init__(self) -> None:
        for value in (self.grant_id, self.proposal_id, self.plan_revision_id, self.workspace_id,
                      self.workspace_path):
            if type(value) is not str or not value.strip():
                raise ValueError("grant_binding_identifier_invalid")
        for value in (self.request_digest, self.approved_plan_digest, self.accepted_plan_digest,
                      self.workspace_identity_digest):
            if type(value) is not str or not value.startswith("sha256:") or len(value) != 71:
                raise ValueError("grant_binding_digest_invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("grant_binding_revision_invalid")
        if (not self.allowed_operations or len(set(self.allowed_operations)) != len(self.allowed_operations)
                or not self.authorized_scope or len(set(self.authorized_scope)) != len(self.authorized_scope)):
            raise ValueError("grant_binding_scope_invalid")
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("grant_binding_expiry_invalid") from exc
        if expiry.tzinfo is None:
            raise ValueError("grant_binding_expiry_must_be_aware")
        if not isinstance(self.limits, ExecutionLimits):
            raise ValueError("grant_binding_limits_invalid")
        if self.authorization_mode not in {"authenticated_grant", "local_execution_contract"}:
            raise ValueError("execution_authorization_mode_invalid")
        if self.authorization_mode == "local_execution_contract" and (
                type(self.task_graph) is not tuple or not self.task_graph):
            raise ValueError("local_contract_task_graph_required")
        if self.authorization_mode == "local_execution_contract":
            if not self.acceptance_policy_digest:
                object.__setattr__(self, "acceptance_policy_digest", self.accepted_plan_digest)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.acceptance_policy_digest):
                raise ValueError("local_contract_acceptance_policy_digest_invalid")
        if (type(self.strategies_by_job) is not tuple
                or any(type(item) is not tuple or len(item) != 2 for item in self.strategies_by_job)
                or len({item[0] for item in self.strategies_by_job}) != len(self.strategies_by_job)
                or any(type(job_id) is not str or not job_id
                       or type(strategies) is not tuple or not strategies
                       or len(set(strategies)) != len(strategies)
                       or any(type(value) is not str
                              or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value)
                              for value in strategies)
                       for job_id, strategies in self.strategies_by_job)):
            raise ValueError("grant_recovery_strategies_invalid")
        if self.operator_identity and (type(self.operator_identity) is not str
                                       or not self.operator_identity.strip()):
            raise ValueError("grant_operator_identity_invalid")
        if ((self.executor_id and (type(self.executor_id) is not str or not self.executor_id.strip()))
                or (self.executor_version and (type(self.executor_version) is not str
                                               or not self.executor_version.strip()))):
            raise ValueError("grant_executor_identity_invalid")
        if (type(self.task_limits_by_job) is not tuple
                or any(type(item) is not tuple or len(item) != 2 for item in self.task_limits_by_job)
                or len({item[0] for item in self.task_limits_by_job}) != len(self.task_limits_by_job)
                or any(type(job_id) is not str or not job_id
                       or not isinstance(task_limits, TaskExecutionLimits)
                       for job_id, task_limits in self.task_limits_by_job)):
            raise ValueError("grant_task_limits_invalid")

    def strategies_for(self, job_id: str) -> tuple[str, ...]:
        return dict(self.strategies_by_job).get(job_id, ("default",))

    def task_limits_for(self, job_id: str) -> TaskExecutionLimits:
        return dict(self.task_limits_by_job)[job_id]


class ExecutionGrantResolver(Protocol):
    def resolve_and_validate(self, grant_id: str, cycle_id: str) -> VerifiedExecutionGrant: ...


@dataclass(frozen=True)
class LocalExecutionContract:
    """Persisted local authorization; not a signed or independent-authority grant."""

    contract_id: str
    operation_id: str
    initiated_by: str
    objective_digest: str
    authorization: VerifiedExecutionGrant

    def __post_init__(self) -> None:
        if (not self.contract_id.startswith("apl-contract-")
                or not self.operation_id.strip()
                or self.initiated_by != "explicit_operator_cli"
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.objective_digest)
                or self.authorization.grant_id != self.contract_id
                or self.authorization.authorization_mode != "local_execution_contract"):
            raise ValueError("local_execution_contract_invalid")


def create_local_execution_contract(*, snapshot: CycleSnapshot, workspace,
                                    executor_id: str, executor_version: str,
                                    limits: ExecutionLimits, task_limits: TaskExecutionLimits,
                                    financial_policy: FinancialBudgetPolicy,
                                    expires_at: str, operation_id: str,
                                    initiated_by: str = "explicit_operator_cli",
                                    acceptance_policy_digest: str = "") -> LocalExecutionContract:
    """Build a strict local contract from the accepted snapshot and current workspace."""
    if (not isinstance(snapshot, CycleSnapshot) or snapshot.state is not APLState.READY
            or not snapshot.jobs or type(snapshot.objective) is not str
            or not snapshot.objective.strip()):
        raise AuthorizedExecutionError("local_contract_requires_ready_plan_and_objective")
    if not isinstance(limits, ExecutionLimits) or not isinstance(task_limits, TaskExecutionLimits):
        raise AuthorizedExecutionError("local_contract_limits_invalid")
    if not isinstance(financial_policy, FinancialBudgetPolicy):
        raise AuthorizedExecutionError("local_contract_financial_policy_required")
    if financial_policy.mode is FinancialBudgetMode.HARD_CAP_REQUIRED:
        raise AuthorizedExecutionError("financial_hard_cap_unavailable")
    if (not snapshot.plan_digest or not snapshot.plan_revision_id
            or len(snapshot.jobs) > limits.max_jobs
            or limits.max_concurrency != 1
            or task_limits.max_attempts > limits.max_attempts_per_job
            or task_limits.max_seconds > limits.max_seconds
            or task_limits.max_estimated_budget > limits.max_estimated_budget):
        raise AuthorizedExecutionError("local_contract_plan_or_quota_invalid")
    try:
        path = workspace.path.resolve(strict=True)
        info = path.stat()
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AuthorizedExecutionError("local_contract_workspace_or_expiry_invalid") from exc
    if (not path.is_dir() or not info.st_dev or not info.st_ino or expiry.tzinfo is None
            or expiry <= datetime.now(timezone.utc)):
        raise AuthorizedExecutionError("local_contract_workspace_or_expiry_invalid")
    canonical = os.path.normcase(str(path))
    identity = f"{info.st_dev}:{info.st_ino}"
    identity_digest = "sha256:" + hashlib.sha256(
        f"{canonical}\0{identity}".encode("utf-8")).hexdigest()
    if (workspace.identity_digest != identity_digest or not workspace.workspace_id
            or any(not set(job.required_scope) <= set(snapshot.authorized_scope)
                   for job in snapshot.jobs)):
        raise AuthorizedExecutionError("local_contract_workspace_or_scope_mismatch")
    if (initiated_by != "explicit_operator_cli"
            or type(operation_id) is not str or not operation_id.strip()
            or type(executor_id) is not str or not executor_id.strip()
            or type(executor_version) is not str or not executor_version.strip()):
        raise AuthorizedExecutionError("local_contract_initiator_not_allowed")
    task_ids = {job.job_id for job in snapshot.jobs}
    if len(task_ids) != len(snapshot.jobs):
        raise AuthorizedExecutionError("local_contract_duplicate_task_id")
    jobs_by_id = {job.job_id: job for job in snapshot.jobs}
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(job_id: str) -> None:
        if job_id in visiting:
            raise AuthorizedExecutionError("local_contract_dependency_cycle")
        if job_id in visited:
            return
        visiting.add(job_id)
        for dependency in jobs_by_id[job_id].depends_on:
            if dependency not in jobs_by_id:
                raise AuthorizedExecutionError("local_contract_dependency_missing")
            visit(dependency)
        visiting.remove(job_id)
        visited.add(job_id)
    for job_id in jobs_by_id:
        visit(job_id)
    job_limits = tuple((job_id, task_limits.max_attempts, float(task_limits.max_seconds),
                        float(task_limits.max_estimated_budget)) for job_id in sorted(task_ids))
    strategy_map = tuple((job_id, ("default",)) for job_id in sorted(task_ids))
    contract_basis = {
        "cycle_id": snapshot.cycle_id, "plan_revision_id": snapshot.plan_revision_id,
        "plan_digest": _local_plan_digest(snapshot), "objective": snapshot.objective,
        "workspace": canonical, "workspace_identity": identity_digest,
        "workspace_id": workspace.workspace_id, "executor": executor_id,
        "executor_version": executor_version, "jobs": [asdict(job) for job in snapshot.jobs],
        "scope": list(snapshot.authorized_scope), "limits": asdict(limits),
        "task_limits": asdict(task_limits), "financial_policy": asdict(financial_policy),
        "expires_at": expires_at, "operation_id": operation_id,
        "initiated_by": initiated_by,
        "acceptance_policy_digest": acceptance_policy_digest or _local_plan_digest(snapshot),
    }
    basis_digest = _sha(_canonical(contract_basis))
    contract_id = "apl-contract-" + hashlib.sha256(
        f"{operation_id}:{basis_digest}".encode("utf-8")).hexdigest()[:32]
    objective_digest = _sha(snapshot.objective)
    authorization = VerifiedExecutionGrant(
        grant_id=contract_id, request_digest=basis_digest, proposal_id=snapshot.cycle_id,
        approved_plan_digest=_local_plan_digest(snapshot), revision=max(1, snapshot.revision),
        plan_revision_id=snapshot.plan_revision_id, accepted_plan_digest=_local_plan_digest(snapshot),
        workspace_id=workspace.workspace_id, workspace_path=str(path),
        workspace_identity_digest=identity_digest,
        allowed_operations=tuple(sorted(f"job:{job_id}" for job_id in task_ids)),
        authorized_scope=tuple(snapshot.authorized_scope), expires_at=expires_at,
        limits=limits, state="approved", strategies_by_job=strategy_map,
        operator_identity="", executor_id=executor_id, executor_version=executor_version,
        task_limits_by_job=tuple((job_id, task_limits) for job_id in sorted(task_ids)),
        budget_policy=financial_policy, authorization_mode="local_execution_contract",
        task_graph=tuple((job.job_id, tuple(job.depends_on), tuple(job.required_scope))
                         for job in snapshot.jobs),
        acceptance_policy_digest=acceptance_policy_digest or _local_plan_digest(snapshot))
    return LocalExecutionContract(contract_id, operation_id, initiated_by,
                                  objective_digest, authorization)


class BrokerExecutionGrantResolver:
    """Resolve a signed full-plan grant from the existing local consent ledger.

    This adapter does not issue grants. It requires the persisted authenticated
    decision, Broker status, a v2 plan binding, the current accepted APL revision,
    current workspace identity, and the exact configured executor identity.
    """

    def __init__(self, *, broker_store, cycle_store, verifier_resolver,
                 workspace_resolver, executor_id: str, executor_version: str,
                 clock=None):
        for item in (broker_store, cycle_store, verifier_resolver, workspace_resolver):
            if item is None:
                raise ValueError("operational_grant_resolver_dependencies_required")
        if (not callable(getattr(verifier_resolver, "resolve", None))
                or not callable(getattr(workspace_resolver, "resolve", None))
                or type(executor_id) is not str or not executor_id.strip()
                or type(executor_version) is not str or not executor_version.strip()):
            raise ValueError("operational_grant_resolver_contract_invalid")
        self.broker = broker_store
        self.cycles = cycle_store
        self.verifiers = verifier_resolver
        self.workspaces = workspace_resolver
        self.executor_id, self.executor_version = executor_id, executor_version
        self.clock = clock or _utc_now

    def resolve_and_validate(self, grant_id: str, cycle_id: str) -> VerifiedExecutionGrant:
        from pathlib import Path
        from agentflow.security.authorization import LocalGrant
        from agentflow.security.profile import ExecutionPlanConsentBinding, SecurityProfile

        try:
            grant, state = self.broker.get_local_grant(grant_id)
        except Exception as exc:
            raise AuthorizedExecutionError("operational_grant_not_persisted") from exc
        if (not isinstance(grant, LocalGrant) or grant.schema_version != 2
                or not isinstance(grant.execution_binding, dict)):
            raise AuthorizedExecutionError("full_plan_execution_grant_required")
        if state != "approved":
            raise AuthorizedExecutionError(f"grant_{state}")
        instant = self.clock()
        if isinstance(instant, str):
            instant = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            raise AuthorizedExecutionError("grant_clock_invalid")
        expiry = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
        if expiry <= instant.astimezone(timezone.utc):
            raise AuthorizedExecutionError("grant_expired")
        consent = self.broker.get_local_consent_by_request(grant.request_digest)
        if consent is None:
            raise AuthorizedExecutionError("authenticated_consent_record_missing")
        record, linked_grant_id = consent
        if (record.decision.value != "approve" or not record.explicit_action
                or not record.operator_identity or not record.operator_proof_digest
                or linked_grant_id != grant.grant_id
                or record.decision_id != grant.consent_decision_id
                or record.request_digest != grant.request_digest
                or record.occurred_at != grant.issued_at
                or record.operator_identity != grant.operator_identity):
            raise AuthorizedExecutionError("authenticated_consent_grant_binding_invalid")
        verifier = self.verifiers.resolve(grant.key_id, grant.operator_identity)
        if verifier is None or not grant.verify_signature(verifier):
            raise AuthorizedExecutionError("operational_grant_signature_invalid")
        try:
            binding = ExecutionPlanConsentBinding.from_payload(grant.execution_binding)
            snapshot = self.cycles.load(cycle_id)
        except Exception as exc:
            raise AuthorizedExecutionError("operational_plan_binding_unavailable") from exc
        if (binding.cycle_id != cycle_id
                or grant.proposal_id == ""
                or snapshot.plan_digest.removeprefix("sha256:")
                   != grant.plan_digest.removeprefix("sha256:")
                or snapshot.plan_revision_id != binding.plan_revision_id
                or tuple(sorted(snapshot.authorized_scope)) != binding.authorized_scope
                or tuple((job.job_id, job.depends_on, job.required_scope)
                         for job in snapshot.jobs) != binding.task_graph):
            raise AuthorizedExecutionError("operational_plan_binding_changed")
        with self.cycles._connect(readonly=True) as db:
            revision = db.execute("SELECT sequence,plan_json,plan_digest FROM apl_plan_revisions "
                                  "WHERE cycle_id=? AND revision_id=?",
                                  (cycle_id, binding.plan_revision_id)).fetchone()
        expected_proposal_id = "execution-plan-" + hashlib.sha256(
            f"{cycle_id}:{revision[0] if revision else 0}:{grant.plan_digest}".encode("utf-8")
        ).hexdigest()[:32]
        if (binding.cycle_id != cycle_id
                or grant.proposal_id != expected_proposal_id
                or snapshot.state is not APLState.READY
                or snapshot.plan_digest.removeprefix("sha256:")
                   != grant.plan_digest.removeprefix("sha256:")
                or snapshot.plan_revision_id != binding.plan_revision_id
                or tuple(sorted(snapshot.authorized_scope)) != binding.authorized_scope
                or tuple((job.job_id, job.depends_on, job.required_scope)
                         for job in snapshot.jobs) != binding.task_graph):
            raise AuthorizedExecutionError("operational_plan_binding_changed")
        expected_jobs_json = _canonical([asdict(job) for job in snapshot.jobs])
        if revision is not None:
            stored_jobs_json = _canonical(json.loads(revision[1]))
            if revision[0] == 1:
                expected_revision_digest = _sha(_canonical({
                    "source": snapshot.plan_digest, "jobs": json.loads(revision[1])})).removeprefix("sha256:")
            else:
                expected_revision_digest = _sha(stored_jobs_json).removeprefix("sha256:")
        else:
            expected_revision_digest = ""
            stored_jobs_json = b""
        if (revision is None or revision[0] != grant.revision
                or stored_jobs_json != expected_jobs_json
                or revision[2] != expected_revision_digest
                or binding.plan_content_digest.removeprefix("sha256:")
                   != revision[2].removeprefix("sha256:")
                or (revision[0] > 1 and revision[2].removeprefix("sha256:")
                    != snapshot.plan_digest.removeprefix("sha256:"))):
            raise AuthorizedExecutionError("operational_plan_revision_changed")
        objective = snapshot.objective
        if (type(objective) is not str or not objective.strip()
                or binding.objective_digest != "sha256:" + hashlib.sha256(
                    objective.encode("utf-8")).hexdigest()):
            raise AuthorizedExecutionError("operational_plan_objective_changed")
        if (len(snapshot.jobs) > grant.limits.max_jobs
                or any(item[1] > binding.max_attempts_per_job
                       or item[2] > binding.max_duration_seconds
                       for item in binding.task_limits_by_job)
                or grant.limits.timeout_seconds > binding.max_duration_seconds
                or binding.max_concurrency != 1):
            raise AuthorizedExecutionError("operational_plan_quotas_invalid")
        workspace = self.workspaces.resolve(cycle_id)
        if workspace is None:
            raise AuthorizedExecutionError("operational_workspace_unavailable")
        try:
            path = Path(workspace.path).resolve(strict=True)
            info = path.stat()
        except (OSError, RuntimeError, TypeError) as exc:
            raise AuthorizedExecutionError("operational_workspace_unavailable") from exc
        if not path.is_dir() or not info.st_dev or not info.st_ino:
            raise AuthorizedExecutionError("operational_workspace_identity_unavailable")
        canonical = os.path.normcase(str(path))
        identity = f"{info.st_dev}:{info.st_ino}"
        actual_identity_digest = "sha256:" + hashlib.sha256(
            f"{canonical}\0{identity}".encode("utf-8")).hexdigest()
        if (canonical != os.path.normcase(binding.workspace_path)
                or actual_identity_digest != binding.workspace_identity_digest
                or workspace.identity_digest != binding.workspace_identity_digest
                or workspace.workspace_id != grant.workspace_id):
            raise AuthorizedExecutionError("operational_workspace_binding_changed")
        if (binding.executor_id != self.executor_id
                or binding.executor_version != self.executor_version):
            raise AuthorizedExecutionError("operational_executor_binding_changed")
        jobs = {job.job_id for job in snapshot.jobs}
        operations = tuple(sorted("job:" + job_id for job_id in jobs))
        if (tuple(sorted(grant.allowed_operations)) != operations
                or len(jobs) != len(snapshot.jobs)
                or len(jobs) > grant.limits.max_jobs):
            raise AuthorizedExecutionError("operational_grant_task_coverage_invalid")
        limits_by_job = {item[0]: item for item in binding.task_limits_by_job}
        strategies_by_job = {item[0]: item[1] for item in binding.strategies_by_job}
        if set(limits_by_job) != jobs or set(strategies_by_job) != jobs:
            raise AuthorizedExecutionError("operational_grant_task_quotas_invalid")
        task_limits = tuple((job, TaskExecutionLimits(values[1], values[2], values[3]))
                            for job, values in sorted(limits_by_job.items()))
        policy = (FinancialBudgetPolicy(FinancialBudgetMode.HARD_CAP_REQUIRED)
                  if binding.financial_budget_mode == "hard_cap_required" else
                  FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED,
                                        float(grant.limits.estimated_budget),
                                        grant.limits.budget_currency))
        return VerifiedExecutionGrant(
            grant_id=grant.grant_id, request_digest=grant.request_digest,
            proposal_id=grant.proposal_id, approved_plan_digest=grant.plan_digest,
            revision=grant.revision, plan_revision_id=binding.plan_revision_id,
            accepted_plan_digest=grant.plan_digest, workspace_id=grant.workspace_id,
            workspace_path=str(path), workspace_identity_digest=actual_identity_digest,
            allowed_operations=operations, authorized_scope=binding.authorized_scope,
            expires_at=grant.expires_at,
            limits=ExecutionLimits(grant.limits.max_jobs,
                                   binding.max_attempts_per_job,
                                   min(grant.limits.timeout_seconds,
                                       binding.max_duration_seconds),
                                   float(grant.limits.estimated_budget),
                                   grant.limits.max_iterations,
                                   binding.max_concurrency),
            state=state,
            strategies_by_job=tuple(sorted(strategies_by_job.items())),
            operator_identity=grant.operator_identity,
            executor_id=binding.executor_id, executor_version=binding.executor_version,
            task_limits_by_job=task_limits, budget_policy=policy,
            task_graph=binding.task_graph,
        )


class AuthorizedJobExecutor(Protocol):
    def execute(self, cycle_id: str, job: JobProposal, authorized_scope: tuple[str, ...],
                operation_id: str) -> ExecutionOutcome: ...


class AuthorizedExecutionCancellation(Protocol):
    def is_set(self) -> bool: ...


class ResultEvidenceValidator(Protocol):
    def validate(self, cycle_id: str, job: JobProposal,
                 outcome: ExecutionOutcome) -> bool | QualityResult: ...


class SQLiteAuthorizedExecutionStore:
    """Append-only execution journal sharing the APL SQLite database."""

    def __init__(self, database_path: str, *, initialize: bool = True):
        self.database_path = str(database_path)
        if not initialize:
            if not Path(self.database_path).is_file():
                raise AuthorizedExecutionError("execution_store_missing")
            return
        with sqlite3.connect(self.database_path, timeout=10) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS apl_authorized_runs(
                    run_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, grant_id TEXT NOT NULL UNIQUE,
                    binding_json TEXT NOT NULL, binding_digest TEXT NOT NULL,
                    plan_json TEXT NOT NULL, plan_digest TEXT NOT NULL,
                    recovery_limits_json TEXT NOT NULL DEFAULT '{"max_attempts_total":3,"max_equivalent_repeats":0,"max_duration_seconds":3600,"max_estimated_budget":0}',
                    recovery_limits_digest TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK(status IN ('ready','running','blocked','completed','cancelled')),
                    started_at TEXT NOT NULL, finished_at TEXT, reason TEXT NOT NULL DEFAULT '',
                    jobs_started INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    consumed_seconds REAL NOT NULL DEFAULT 0, consumed_budget REAL NOT NULL DEFAULT 0,
                    reserved_seconds REAL NOT NULL DEFAULT 0,
                    owner_pid INTEGER, owner_marker TEXT
                );
                CREATE TABLE IF NOT EXISTS apl_authorized_run_tasks(
                    run_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ready','reserved','running','retryable','completed','failed','unknown')),
                    operation_id TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0,
                    outcome_json TEXT, evidence_digest TEXT, updated_at TEXT NOT NULL,
                    process_identity_json TEXT, strategy_id TEXT NOT NULL DEFAULT 'default',
                    consumed_seconds REAL NOT NULL DEFAULT 0, consumed_budget REAL NOT NULL DEFAULT 0,
                    reserved_seconds REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(run_id,job_id),
                    FOREIGN KEY(run_id) REFERENCES apl_authorized_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS apl_authorized_run_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    job_id TEXT, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, previous_digest TEXT NOT NULL DEFAULT '',
                    event_digest TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES apl_authorized_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS apl_local_execution_contracts(
                    contract_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                    cycle_id TEXT NOT NULL, contract_json TEXT NOT NULL,
                    contract_digest TEXT NOT NULL, state TEXT NOT NULL
                    CHECK(state IN ('active','revoked','expired','completed','cancelled')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS apl_local_execution_contract_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, previous_digest TEXT NOT NULL DEFAULT '',
                    event_digest TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(contract_id) REFERENCES apl_local_execution_contracts(contract_id)
                );
                CREATE TRIGGER IF NOT EXISTS apl_local_contract_events_no_update
                BEFORE UPDATE ON apl_local_execution_contract_events BEGIN
                    SELECT RAISE(ABORT,'local contract events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS apl_local_contract_events_no_delete
                BEFORE DELETE ON apl_local_execution_contract_events BEGIN
                    SELECT RAISE(ABORT,'local contract events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS apl_local_contract_payload_immutable
                BEFORE UPDATE OF operation_id,cycle_id,contract_json,contract_digest,created_at
                ON apl_local_execution_contracts BEGIN
                    SELECT RAISE(ABORT,'local execution contract is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_run_events_no_update
                BEFORE UPDATE ON apl_authorized_run_events BEGIN
                    SELECT RAISE(ABORT,'authorized execution events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_run_events_no_delete
                BEFORE DELETE ON apl_authorized_run_events BEGIN
                    SELECT RAISE(ABORT,'authorized execution events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_run_binding_immutable
                BEFORE UPDATE OF cycle_id,grant_id,binding_json,binding_digest,plan_json,plan_digest,started_at
                ON apl_authorized_runs BEGIN
                    SELECT RAISE(ABORT,'authorized execution binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_task_binding_immutable
                BEFORE UPDATE OF operation_id ON apl_authorized_run_tasks BEGIN
                    SELECT RAISE(ABORT,'authorized task operation is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_task_outcome_immutable
                BEFORE UPDATE OF outcome_json,evidence_digest ON apl_authorized_run_tasks
                WHEN OLD.status='completed' BEGIN
                    SELECT RAISE(ABORT,'verified task outcome is immutable'); END;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(apl_authorized_runs)")}
            if "recovery_limits_json" not in columns:
                default_limits = _canonical(asdict(RecoveryLimits()))
                db.execute("ALTER TABLE apl_authorized_runs ADD COLUMN recovery_limits_json TEXT NOT NULL DEFAULT '{}' ")
                db.execute("UPDATE apl_authorized_runs SET recovery_limits_json=?", (default_limits,))
            if "recovery_limits_digest" not in columns:
                db.execute("ALTER TABLE apl_authorized_runs ADD COLUMN recovery_limits_digest TEXT NOT NULL DEFAULT ''")
                for run_id, limits_json in db.execute("SELECT run_id,recovery_limits_json FROM apl_authorized_runs").fetchall():
                    db.execute("UPDATE apl_authorized_runs SET recovery_limits_digest=? WHERE run_id=?",
                               (_sha(limits_json), run_id))
            if "owner_pid" not in columns:
                db.execute("ALTER TABLE apl_authorized_runs ADD COLUMN owner_pid INTEGER")
            if "owner_marker" not in columns:
                db.execute("ALTER TABLE apl_authorized_runs ADD COLUMN owner_marker TEXT")
            if "reserved_seconds" not in columns:
                db.execute("ALTER TABLE apl_authorized_runs ADD COLUMN reserved_seconds REAL NOT NULL DEFAULT 0")
            task_columns = {row[1] for row in db.execute("PRAGMA table_info(apl_authorized_run_tasks)")}
            if "process_identity_json" not in task_columns:
                db.execute("ALTER TABLE apl_authorized_run_tasks ADD COLUMN process_identity_json TEXT")
            if "strategy_id" not in task_columns:
                db.execute("ALTER TABLE apl_authorized_run_tasks ADD COLUMN strategy_id TEXT NOT NULL DEFAULT 'default'")
            if "consumed_seconds" not in task_columns:
                db.execute("ALTER TABLE apl_authorized_run_tasks ADD COLUMN consumed_seconds REAL NOT NULL DEFAULT 0")
            if "consumed_budget" not in task_columns:
                db.execute("ALTER TABLE apl_authorized_run_tasks ADD COLUMN consumed_budget REAL NOT NULL DEFAULT 0")
            if "reserved_seconds" not in task_columns:
                db.execute("ALTER TABLE apl_authorized_run_tasks ADD COLUMN reserved_seconds REAL NOT NULL DEFAULT 0")
            event_columns = {row[1] for row in db.execute("PRAGMA table_info(apl_authorized_run_events)")}
            if "previous_digest" not in event_columns:
                db.execute("ALTER TABLE apl_authorized_run_events ADD COLUMN previous_digest TEXT NOT NULL DEFAULT ''")
            if "event_digest" not in event_columns:
                db.execute("ALTER TABLE apl_authorized_run_events ADD COLUMN event_digest TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TRIGGER IF NOT EXISTS apl_authorized_run_recovery_limits_immutable
                BEFORE UPDATE OF recovery_limits_json,recovery_limits_digest ON apl_authorized_runs BEGIN
                    SELECT RAISE(ABORT,'IFR limits are immutable for an execution run'); END""")

    def persist_local_contract(self, contract: LocalExecutionContract) -> str:
        if not isinstance(contract, LocalExecutionContract):
            raise ValueError("typed_local_execution_contract_required")
        payload = _local_contract_payload(contract)
        encoded = _canonical(payload)
        digest_value = _sha(encoded)
        now = _utc_now()
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute(
                    "SELECT contract_id,contract_digest FROM apl_local_execution_contracts "
                    "WHERE operation_id=?", (contract.operation_id,)).fetchone()
                if prior:
                    if prior != (contract.contract_id, digest_value):
                        raise AuthorizedExecutionError("local_contract_operation_conflict")
                    db.commit()
                    return contract.contract_id
                db.execute(
                    "INSERT INTO apl_local_execution_contracts(contract_id,operation_id,cycle_id,"
                    "contract_json,contract_digest,state,created_at) VALUES(?,?,?,?,?,'active',?)",
                    (contract.contract_id, contract.operation_id,
                     contract.authorization.proposal_id, encoded, digest_value, now))
                self._local_contract_event(db, contract.contract_id, "contract.created",
                                           {"contract_digest": digest_value,
                                            "cycle_id": contract.authorization.proposal_id,
                                            "initiated_by": contract.initiated_by}, now)
                db.commit()
                return contract.contract_id
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _local_contract_event(db, contract_id: str, event_type: str,
                              payload: dict, occurred_at: str) -> None:
        prior = db.execute(
            "SELECT event_digest FROM apl_local_execution_contract_events "
            "WHERE contract_id=? ORDER BY event_id DESC LIMIT 1", (contract_id,)).fetchone()
        previous = prior[0] if prior else ""
        payload_json = _canonical(payload)
        event_digest = _sha(_canonical({"contract_id": contract_id,
                                        "event_type": event_type,
                                        "payload_json": payload_json,
                                        "occurred_at": occurred_at,
                                        "previous_digest": previous}))
        db.execute("INSERT INTO apl_local_execution_contract_events(contract_id,event_type,"
                   "payload_json,occurred_at,previous_digest,event_digest) VALUES(?,?,?,?,?,?)",
                   (contract_id, event_type, payload_json, occurred_at, previous, event_digest))

    def load_local_contract(self, contract_id: str) -> tuple[LocalExecutionContract, str]:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            row = db.execute(
                "SELECT contract_json,contract_digest,state FROM apl_local_execution_contracts "
                "WHERE contract_id=?", (contract_id,)).fetchone()
            events = db.execute(
                "SELECT event_type,payload_json,occurred_at,previous_digest,event_digest "
                "FROM apl_local_execution_contract_events WHERE contract_id=? ORDER BY event_id",
                (contract_id,)).fetchall()
        if row is None:
            raise KeyError(contract_id)
        if _sha(row[0]) != row[1]:
            raise AuthorizedExecutionError("local_execution_contract_integrity_failure")
        previous = ""
        for event_type, payload_json, occurred_at, stored_previous, stored_digest in events:
            expected = _sha(_canonical({"contract_id": contract_id,
                                        "event_type": event_type,
                                        "payload_json": payload_json,
                                        "occurred_at": occurred_at,
                                        "previous_digest": previous}))
            if stored_previous != previous or stored_digest != expected:
                raise AuthorizedExecutionError("local_contract_journal_integrity_failure")
            previous = expected
        return _local_contract_from_payload(json.loads(row[0])), row[2]

    def local_contract_for_operation(self, operation_id: str) -> tuple[LocalExecutionContract, str] | None:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            row = db.execute("SELECT contract_id FROM apl_local_execution_contracts "
                             "WHERE operation_id=?", (operation_id,)).fetchone()
        return self.load_local_contract(row[0]) if row else None

    def run_for_contract(self, contract_id: str) -> dict | None:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            row = db.execute("SELECT run_id FROM apl_authorized_runs WHERE grant_id=?",
                             (contract_id,)).fetchone()
        return self.load(row[0]) if row else None

    def revoke_local_contract(self, contract_id: str, *, reason: str = "operator_cancelled") -> bool:
        now = _utc_now()
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT state FROM apl_local_execution_contracts "
                                 "WHERE contract_id=?", (contract_id,)).fetchone()
                if row is None:
                    raise KeyError(contract_id)
                if row[0] != "active":
                    db.commit()
                    return False
                db.execute("UPDATE apl_local_execution_contracts SET state='revoked' "
                           "WHERE contract_id=? AND state='active'", (contract_id,))
                self._local_contract_event(db, contract_id, "contract.revoked",
                                           {"reason": reason[:256]}, now)
                runs = db.execute("SELECT run_id FROM apl_authorized_runs WHERE grant_id=? "
                                  "AND status IN ('ready','running')", (contract_id,)).fetchall()
                db.commit()
            except Exception:
                db.rollback()
                raise
        for (run_id,) in runs:
            self.request_cancel(run_id, now=now, reason="local_contract_revoked",
                                event_type="run.revocation_cancel_requested")
        return True


    def create(self, *, cycle_id: str, grant: VerifiedExecutionGrant,
               snapshot: CycleSnapshot, recovery_limits: RecoveryLimits | None = None) -> str:
        now = _utc_now()
        jobs = [asdict(job) for job in snapshot.jobs]
        plan_json = _canonical(jobs)
        plan_digest = _sha(plan_json)
        binding_json = _canonical(asdict(grant))
        binding_digest = _sha(binding_json)
        if recovery_limits is not None and not isinstance(recovery_limits, RecoveryLimits):
            raise ValueError("ifr_limits_invalid")
        recovery_limits_json = _canonical(asdict(recovery_limits or RecoveryLimits()))
        recovery_limits_digest = _sha(recovery_limits_json)
        run_id = "apl-run-" + uuid4().hex
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT run_id,binding_digest,plan_digest,recovery_limits_digest FROM apl_authorized_runs WHERE grant_id=?", (grant.grant_id,)).fetchone()
                if prior:
                    if prior[1:] != (binding_digest, plan_digest, recovery_limits_digest):
                        raise AuthorizedExecutionError("grant_already_bound_to_different_execution")
                    db.commit()
                    return prior[0]
                db.execute("INSERT INTO apl_authorized_runs(run_id,cycle_id,grant_id,binding_json,binding_digest,plan_json,plan_digest,recovery_limits_json,recovery_limits_digest,status,started_at) VALUES(?,?,?,?,?,?,?,?,?,'ready',?)",
                           (run_id, cycle_id, grant.grant_id, binding_json, binding_digest, plan_json,
                            plan_digest, recovery_limits_json, recovery_limits_digest, now))
                for job in snapshot.jobs:
                    operation_id = _operation_id(run_id, job.job_id, plan_digest)
                    prior = dict(snapshot.outcomes).get(job.job_id)
                    already_verified = prior is not None and prior.status == "succeeded"
                    db.execute("INSERT INTO apl_authorized_run_tasks(run_id,job_id,status,operation_id,outcome_json,evidence_digest,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (run_id, job.job_id, "completed" if already_verified else "ready",
                                operation_id, _canonical(asdict(prior)) if already_verified else None,
                                _sha(_canonical(prior.evidence)) if already_verified else None, now))
                self._event(db, run_id, None, "run.created", {
                    "cycle_id": cycle_id, "grant_id": grant.grant_id,
                    "binding_digest": binding_digest, "plan_digest": plan_digest,
                    "recovery_limits_digest": recovery_limits_digest,
                    "jobs": [job.job_id for job in snapshot.jobs],
                }, now)
                db.commit()
                return run_id
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _event(db, run_id: str, job_id: str | None, event_type: str, payload: dict, occurred_at: str) -> None:
        prior = db.execute("SELECT event_digest FROM apl_authorized_run_events WHERE run_id=? ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
        previous_digest = prior[0] if prior else ""
        payload_json = _canonical(payload)
        digest_payload = _canonical({"run_id": run_id, "job_id": job_id,
                                     "event_type": event_type, "payload_json": payload_json,
                                     "occurred_at": occurred_at, "previous_digest": previous_digest})
        event_digest = _sha(digest_payload)
        db.execute("INSERT INTO apl_authorized_run_events(run_id,job_id,event_type,payload_json,occurred_at,previous_digest,event_digest) VALUES(?,?,?,?,?,?,?)",
                   (run_id, job_id, event_type, payload_json, occurred_at, previous_digest, event_digest))

    def load(self, run_id: str) -> dict:
        with sqlite3.connect(Path(self.database_path).resolve().as_uri() + "?mode=ro",
                             uri=True, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            result = dict(row)
            tasks = db.execute("SELECT * FROM apl_authorized_run_tasks WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            result["tasks"] = [dict(item) for item in tasks]
            result["events"] = [dict(item) for item in db.execute("SELECT * FROM apl_authorized_run_events WHERE run_id=? ORDER BY event_id", (run_id,))]
        if (_sha(result["binding_json"]) != result["binding_digest"]
                or _sha(result["plan_json"]) != result["plan_digest"]
                or _sha(result["recovery_limits_json"]) != result["recovery_limits_digest"]):
            raise AuthorizedExecutionError("persisted_execution_binding_integrity_failure")
        try:
            RecoveryLimits(**json.loads(result["recovery_limits_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthorizedExecutionError("persisted_ifr_limits_invalid") from exc
        previous = ""
        for event in result["events"]:
            expected = _sha(_canonical({"run_id": event["run_id"], "job_id": event["job_id"],
                                        "event_type": event["event_type"],
                                        "payload_json": event["payload_json"],
                                        "occurred_at": event["occurred_at"],
                                        "previous_digest": previous}))
            if event["previous_digest"] != previous or event["event_digest"] != expected:
                raise AuthorizedExecutionError("execution_journal_integrity_failure")
            previous = expected
        for task in result["tasks"]:
            if task["status"] == "completed":
                try:
                    evidence = json.loads(task["outcome_json"])["evidence"]
                except (TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise AuthorizedExecutionError("verified_task_checkpoint_invalid") from exc
                if _sha(_canonical(evidence)) != task["evidence_digest"]:
                    raise AuthorizedExecutionError("verified_task_evidence_integrity_failure")
        return result

    def claim_run(self, run_id: str, *, now: str) -> None:
        marker = _current_process_marker()
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status,owner_pid,owner_marker FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                if row["status"] not in {"ready", "running"}:
                    raise AuthorizedExecutionError(f"execution_{row['status']}")
                if row["owner_pid"] is not None and _process_matches(row["owner_pid"], row["owner_marker"]):
                    raise AuthorizedExecutionError("execution_already_owned_by_live_process")
                if row["owner_pid"] is not None:
                    self._mark_dead_owner_unknown(db, run_id, now)
                    pending = db.execute("SELECT 1 FROM apl_authorized_run_tasks WHERE run_id=? AND status='unknown' LIMIT 1", (run_id,)).fetchone()
                    if pending:
                        db.commit()
                        raise AuthorizedExecutionError("recovery_found_ambiguous_task")
                db.execute("UPDATE apl_authorized_runs SET owner_pid=?,owner_marker=? WHERE run_id=?", (os.getpid(), marker, run_id))
                self._event(db, run_id, None, "run.claimed", {"pid": os.getpid(), "process_marker": marker}, now)
                db.commit()
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise

    def release_run(self, run_id: str) -> None:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            db.execute("UPDATE apl_authorized_runs SET owner_pid=NULL,owner_marker=NULL WHERE run_id=? AND owner_pid=? AND owner_marker=?",
                       (run_id, os.getpid(), _current_process_marker()))

    def request_cancel(self, run_id: str, *, now: str, reason: str = "operator_cancelled",
                       event_type: str = "run.cancel_requested") -> None:
        if event_type not in {"run.cancel_requested", "run.revocation_cancel_requested"}:
            raise ValueError("cancel_event_type_invalid")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                if row[0] in {"completed", "cancelled"}:
                    db.commit()
                    return
                exists = db.execute("SELECT 1 FROM apl_authorized_run_events WHERE run_id=? AND event_type=?",
                                    (run_id, event_type)).fetchone()
                if not exists:
                    self._event(db, run_id, None, event_type,
                                {"requested_at": now, "reason": reason[:256]}, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def cancel_requested(self, run_id: str) -> bool:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            return db.execute("SELECT 1 FROM apl_authorized_run_events WHERE run_id=? AND event_type IN ('run.cancel_requested','run.revocation_cancel_requested') LIMIT 1", (run_id,)).fetchone() is not None

    def verify_dispatch_intent(self, run_id: str, job_id: str, operation_id: str,
                               attempt_id: str, grant: VerifiedExecutionGrant) -> None:
        """Recheck persisted intent immediately before process creation; not atomic with Popen."""
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                run = db.execute("SELECT status,binding_json,binding_digest FROM apl_authorized_runs WHERE run_id=?",
                                 (run_id,)).fetchone()
                task = db.execute("SELECT status,operation_id,attempts,reserved_seconds FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?",
                                  (run_id, job_id)).fetchone()
                if (run is None or task is None or run[0] != "running"
                        or run[1] != _canonical(asdict(grant))
                        or run[2] != _sha(run[1])
                        or task[0] != "running" or task[1] != operation_id
                        or task[3] < 1 or self.cancel_requested(run_id)):
                    raise AuthorizedExecutionError("dispatch_intent_or_authorization_changed")
                expected_attempt = _attempt_id(run_id, job_id, task[2])
                if expected_attempt != attempt_id:
                    raise AuthorizedExecutionError("dispatch_attempt_binding_changed")
                intents = db.execute("SELECT payload_json FROM apl_authorized_run_events WHERE run_id=? AND job_id=? AND event_type='task.dispatch_intent_persisted'",
                                     (run_id, job_id)).fetchall()
                if not any((payload := json.loads(row[0])).get("attempt_id") == attempt_id
                           and payload.get("operation_id") == operation_id
                           and payload.get("binding_digest") == run[2]
                           for row in intents):
                    raise AuthorizedExecutionError("dispatch_intent_missing")
                db.commit()
            except Exception:
                db.rollback()
                raise

    def status(self, run_id: str) -> dict:
        return self.load(run_id)

    @classmethod
    def _mark_dead_owner_unknown(cls, db, run_id: str, now: str) -> None:
        rows = db.execute("SELECT job_id,status FROM apl_authorized_run_tasks WHERE run_id=? AND status IN ('reserved','running')", (run_id,)).fetchall()
        for job_id, previous in rows:
            db.execute("UPDATE apl_authorized_run_tasks SET status='unknown',updated_at=? WHERE run_id=? AND job_id=?", (now, run_id, job_id))
            cls._event(db, run_id, job_id, "task.recovered_unknown", {"previous_status": previous, "automatic_retry": False}, now)
        if rows:
            db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='recovery_found_ambiguous_task',owner_pid=NULL,owner_marker=NULL WHERE run_id=?", (run_id,))
        else:
            db.execute("UPDATE apl_authorized_runs SET owner_pid=NULL,owner_marker=NULL WHERE run_id=?", (run_id,))

    def reserve_next(self, run_id: str, job: JobProposal, *,
                     grant: VerifiedExecutionGrant, now: str) -> dict | None:
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                run = db.execute("SELECT status,plan_json,binding_digest,binding_json,jobs_started,attempts,consumed_seconds,consumed_budget,started_at,reserved_seconds FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if run is None:
                    raise KeyError(run_id)
                if run[0] not in {"ready", "running"}:
                    raise AuthorizedExecutionError(f"execution_{run[0]}")
                if _sha(_canonical(asdict(grant))) != run[2] or run[3] != _canonical(asdict(grant)):
                    raise AuthorizedExecutionError("atomic_reservation_grant_binding_mismatch")
                plan = json.loads(run[1])
                by_id = {item["job_id"]: item for item in plan}
                if set(dict(grant.task_limits_by_job)) != set(by_id):
                    raise AuthorizedExecutionError("grant_task_limits_do_not_cover_plan")
                task_limit = grant.task_limits_for(job.job_id)
                max_attempts = min(grant.limits.max_attempts_per_job, task_limit.max_attempts)
                max_task_seconds = min(grant.limits.max_seconds, task_limit.max_seconds)
                estimated_budget_limit = _financial_estimate_limit(grant)
                max_task_budget = _minimum_positive(estimated_budget_limit,
                                                    task_limit.max_estimated_budget)
                task = db.execute("SELECT status,operation_id,attempts,strategy_id,consumed_seconds,consumed_budget,reserved_seconds FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?", (run_id, job.job_id)).fetchone()
                if task is None or task[0] not in {"ready", "retryable"}:
                    raise AuthorizedExecutionError("task_not_reservable")
                try:
                    began = datetime.fromisoformat(run[8].replace("Z", "+00:00"))
                    instant = datetime.fromisoformat(now.replace("Z", "+00:00"))
                    if began.tzinfo is None or instant.tzinfo is None:
                        raise ValueError("naive")
                    wall_elapsed = (instant - began).total_seconds()
                except (AttributeError, TypeError, ValueError) as exc:
                    raise AuthorizedExecutionError("execution_clock_invalid") from exc
                if wall_elapsed < 0:
                    raise AuthorizedExecutionError("execution_clock_moved_backwards")
                total_elapsed = max(float(run[6]), wall_elapsed)
                global_seconds_left = grant.limits.max_seconds - total_elapsed - float(run[9])
                task_seconds_left = max_task_seconds - float(task[4]) - float(task[6])
                reservation_seconds = math.floor(min(global_seconds_left, task_seconds_left))
                total_attempt_limit = min(grant.limits.max_jobs * grant.limits.max_attempts_per_job,
                                          grant.limits.max_iterations,
                                          sum(min(grant.limits.max_attempts_per_job,
                                                  grant.task_limits_for(job_id).max_attempts)
                                              for job_id in by_id))
                block_reason = None
                if task[0] == "ready" and run[4] >= grant.limits.max_jobs:
                    block_reason = "execution_job_limit"
                elif run[5] >= total_attempt_limit:
                    block_reason = "execution_attempt_limit"
                elif total_elapsed >= grant.limits.max_seconds:
                    block_reason = "execution_time_limit"
                elif run[7] >= estimated_budget_limit:
                    block_reason = "estimated_budget_limit"
                elif task[4] >= max_task_seconds:
                    block_reason = "task_time_limit"
                elif reservation_seconds < 1:
                    block_reason = "task_timeout_below_runtime_resolution"
                elif max_task_budget and task[5] >= max_task_budget:
                    block_reason = "task_budget_limit"
                if block_reason:
                    db.execute("UPDATE apl_authorized_run_tasks SET status='failed',updated_at=? WHERE run_id=? AND job_id=?",
                               (now, run_id, job.job_id))
                    db.execute("UPDATE apl_authorized_runs SET status='blocked',reason=? WHERE run_id=?",
                               (block_reason, run_id))
                    self._event(db, run_id, job.job_id, "task.blocked", {"reason": block_reason}, now)
                    db.commit()
                    raise AuthorizedExecutionError(block_reason)
                if task[2] >= max_attempts:
                    db.execute("UPDATE apl_authorized_run_tasks SET status='failed',updated_at=? WHERE run_id=? AND job_id=?", (now, run_id, job.job_id))
                    db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='task_attempt_limit' WHERE run_id=?", (run_id,))
                    self._event(db, run_id, job.job_id, "task.blocked", {"reason": "task_attempt_limit"}, now)
                    db.commit()
                    raise AuthorizedExecutionError("task_attempt_limit")
                deps = tuple(by_id[job.job_id].get("depends_on", ()))
                completed = {row[0] for row in db.execute("SELECT job_id FROM apl_authorized_run_tasks WHERE run_id=? AND status='completed'", (run_id,))}
                if not set(deps) <= completed:
                    raise AuthorizedExecutionError("task_dependencies_incomplete")
                db.execute("UPDATE apl_authorized_run_tasks SET status='reserved',attempts=attempts+1,reserved_seconds=?,updated_at=? WHERE run_id=? AND job_id=?", (reservation_seconds, now, run_id, job.job_id))
                db.execute("UPDATE apl_authorized_runs SET status='running',jobs_started=jobs_started+CASE WHEN ?=0 THEN 1 ELSE 0 END,attempts=attempts+1,reserved_seconds=reserved_seconds+? WHERE run_id=?", (task[2], reservation_seconds, run_id))
                attempt_id = _attempt_id(run_id, job.job_id, task[2] + 1)
                self._event(db, run_id, job.job_id, "task.dispatch_intent_persisted", {
                    "operation_id": task[1], "attempt_id": attempt_id,
                    "binding_digest": run[2], "plan_digest": _sha(run[1]),
                    "reserved_seconds": reservation_seconds,
                }, now)
                self._event(db, run_id, job.job_id, "task.reserved", {"operation_id": task[1], "attempt": task[2] + 1}, now)
                self._event(db, run_id, job.job_id, "ifr.attempt_started", {
                    "attempt_id": attempt_id, "attempt_number": task[2] + 1,
                    "operation_id": task[1], "strategy_id": task[3],
                }, now)
                db.commit()
                return {"operation_id": task[1], "attempt": task[2] + 1,
                        "attempt_id": attempt_id, "strategy_id": task[3],
                        "reserved_seconds": reservation_seconds}
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise

    def mark_running(self, run_id: str, job_id: str, *, now: str) -> None:
        self._task_transition(run_id, job_id, "reserved", "running", "task.dispatched", {}, now)

    def record_process_identity(self, run_id: str, job_id: str, operation_id: str,
                                identity, *, attempt_id: str | None = None, now: str) -> None:
        """Persist a child identity before it can perform untracked work."""
        from agentflow.agents import ProcessIdentity
        if not isinstance(identity, ProcessIdentity) or identity.attempt_id != (attempt_id or operation_id):
            raise AuthorizedExecutionError("process_identity_binding_invalid")
        encoded = _canonical(asdict(identity))
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status,operation_id,process_identity_json FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?",
                                 (run_id, job_id)).fetchone()
                if row is None or row[0] != "running" or row[1] != operation_id:
                    raise AuthorizedExecutionError("process_identity_without_matching_dispatch")
                if row[2] is not None and row[2] != encoded:
                    raise AuthorizedExecutionError("process_identity_replay_conflict")
                db.execute("UPDATE apl_authorized_run_tasks SET process_identity_json=?,updated_at=? WHERE run_id=? AND job_id=?",
                           (encoded, now, run_id, job_id))
                self._event(db, run_id, job_id, "task.process_started", {
                    "pid": identity.pid, "started_at": identity.started_at,
                    "group_id": identity.group_id, "attempt_id": identity.attempt_id,
                    "operation_id": operation_id,
                }, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def record_outcome(self, run_id: str, job_id: str, outcome: ExecutionOutcome, *,
                       validated: bool, elapsed_seconds: float, now: str,
                       quality_result: QualityResult | None = None,
                       budget_currency: str) -> None:
        if type(budget_currency) is not str or not re.fullmatch(r"[A-Z]{3}", budget_currency):
            raise AuthorizedExecutionError("estimated_cost_currency_invalid")
        outcome_data = asdict(outcome)
        encoded = _canonical(outcome_data)
        status = "completed" if outcome.status == "succeeded" and validated else (
            "unknown" if outcome.status == "unknown" else "failed")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                task = db.execute("SELECT status,reserved_seconds FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
                if task is None or task[0] != "running":
                    raise AuthorizedExecutionError("task_outcome_without_running_reservation")
                db.execute("UPDATE apl_authorized_run_tasks SET status=?,outcome_json=?,evidence_digest=?,updated_at=? WHERE run_id=? AND job_id=?",
                           (status, encoded, _sha(_canonical(outcome.evidence)), now, run_id, job_id))
                elapsed = max(0.0, elapsed_seconds)
                consumed_reserved = min(float(task[1]), elapsed)
                release_reserved = max(0.0, float(task[1]) - elapsed) if status != "unknown" else 0.0
                release_total = consumed_reserved + release_reserved
                db.execute("UPDATE apl_authorized_run_tasks SET consumed_seconds=consumed_seconds+?,consumed_budget=consumed_budget+?,reserved_seconds=MAX(0,reserved_seconds-?) WHERE run_id=? AND job_id=?",
                           (elapsed, max(0.0, outcome.cost), release_total, run_id, job_id))
                db.execute("UPDATE apl_authorized_runs SET consumed_seconds=consumed_seconds+?,consumed_budget=consumed_budget+?,reserved_seconds=MAX(0,reserved_seconds-?) WHERE run_id=?",
                           (elapsed, max(0.0, outcome.cost), release_total, run_id))
                self._event(db, run_id, job_id, "quota.settled", {
                    "attempt_duration_consumed_seconds": elapsed,
                    "reserved_seconds_released": release_reserved,
                    "reserved_seconds_held_unknown": max(0.0, float(task[1]) - consumed_reserved)
                    if status == "unknown" else 0.0,
                    "cost_estimated": max(0.0, outcome.cost),
                    "cost_estimated_currency": budget_currency,
                    "cost_observed": None,
                    "cost_observation_state": "unavailable_or_unverified",
                    "financial_hard_cap": False,
                }, now)
                reason = "" if status == "completed" else ("unknown_execution_result" if status == "unknown" else "execution_or_evidence_validation_failed")
                if status != "completed":
                    db.execute("UPDATE apl_authorized_runs SET status='blocked',reason=? WHERE run_id=?", (reason, run_id))
                self._event(db, run_id, job_id, f"task.{status}", {
                    "operation_id": db.execute("SELECT operation_id FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()[0],
                    "outcome_digest": _sha(encoded), "evidence_digest": _sha(_canonical(outcome.evidence)),
                }, now)
                if quality_result is not None:
                    self._event(db, run_id, job_id, "ifr.quality_result", {
                        "quality_result": quality_result.value,
                        "evidence_digest": _sha(_canonical(outcome.evidence)),
                    }, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def mark_unknown(self, run_id: str, job_id: str, reason: str, *, now: str) -> None:
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute("UPDATE apl_authorized_run_tasks SET status='unknown',updated_at=? WHERE run_id=? AND job_id=? AND status IN ('reserved','running')", (now, run_id, job_id)).rowcount
                if changed:
                    db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='unknown_execution_result' WHERE run_id=?", (run_id,))
                    self._event(db, run_id, job_id, "task.unknown", {"reason": reason[:256]}, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def record_ifr_event(self, run_id: str, job_id: str | None, event_type: str,
                         payload: dict, *, now: str) -> None:
        if not event_type.startswith("ifr."):
            raise ValueError("ifr_event_type_required")
        encoded = _canonical(payload)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise AuthorizedExecutionError("ifr_event_payload_too_large")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT 1 FROM apl_authorized_run_events WHERE run_id=? AND job_id IS ? AND event_type=? AND payload_json=? LIMIT 1",
                                   (run_id, job_id, event_type, encoded)).fetchone()
                if prior:
                    db.commit()
                    return
                self._event(db, run_id, job_id, event_type, payload, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def record_retryable_ifr(self, run_id: str, job_id: str, strategy_id: str,
                             diagnosis: dict, decision: dict, *, elapsed_seconds: float,
                             now: str) -> None:
        if diagnosis.get("effect_state") not in {
                EffectState.NO_EFFECT_PROVEN.value, EffectState.RECONCILED_SAFE.value}:
            raise AuthorizedExecutionError("quota_release_requires_no_effect_evidence")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute("UPDATE apl_authorized_run_tasks SET status='retryable',strategy_id=?,updated_at=? WHERE run_id=? AND job_id=? AND status='running'",
                                     (strategy_id, now, run_id, job_id)).rowcount
                if changed != 1:
                    raise AuthorizedExecutionError("ifr_retry_task_transition_conflict")
                task = db.execute("SELECT reserved_seconds FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?",
                                  (run_id, job_id)).fetchone()
                held = float(task[0])
                elapsed = max(0.0, elapsed_seconds)
                released = max(0.0, held - elapsed)
                self._event(db, run_id, job_id, "ifr.diagnosis", diagnosis, now)
                self._event(db, run_id, job_id, "ifr.retry_decision", decision, now)
                db.execute("UPDATE apl_authorized_runs SET consumed_seconds=consumed_seconds+?,reserved_seconds=MAX(0,reserved_seconds-?) WHERE run_id=?",
                           (elapsed, held, run_id))
                db.execute("UPDATE apl_authorized_run_tasks SET consumed_seconds=consumed_seconds+?,reserved_seconds=0 WHERE run_id=? AND job_id=?",
                           (elapsed, run_id, job_id))
                self._event(db, run_id, job_id, "quota.settled_no_effect", {
                    "attempt_duration_consumed_seconds": elapsed,
                    "reserved_seconds_released": released,
                    "effect_state": diagnosis["effect_state"],
                    "evidence_refs": diagnosis.get("evidence_refs", []),
                }, now)
                db.execute("UPDATE apl_authorized_runs SET status='running',reason='' WHERE run_id=?", (run_id,))
                db.commit()
            except Exception:
                db.rollback()
                raise

    def escalate_ifr(self, run_id: str, job_id: str, *, task_state: str,
                     summary: dict, now: str, elapsed_seconds: float = 0.0) -> None:
        if task_state not in {"failed", "unknown"}:
            raise ValueError("ifr_escalation_task_state_invalid")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                event_summary = dict(summary)
                prior = db.execute("SELECT 1 FROM apl_authorized_run_events WHERE run_id=? AND job_id=? AND event_type='ifr.escalated' AND payload_json=? LIMIT 1",
                                   (run_id, job_id, _canonical(event_summary))).fetchone()
                if prior:
                    db.commit()
                    return
                task_quota = db.execute("SELECT reserved_seconds FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?",
                                        (run_id, job_id)).fetchone()
                held = float(task_quota[0]) if task_quota else 0.0
                elapsed = max(0.0, elapsed_seconds)
                consumed_reserved = min(held, elapsed)
                diagnosis = summary.get("diagnosis") or {}
                no_effect = diagnosis.get("effect_state") in {
                    EffectState.NO_EFFECT_PROVEN.value, EffectState.RECONCILED_SAFE.value}
                released = max(0.0, held - elapsed) if no_effect else 0.0
                db.execute("UPDATE apl_authorized_runs SET consumed_seconds=consumed_seconds+?,reserved_seconds=MAX(0,reserved_seconds-?) WHERE run_id=?",
                           (elapsed, consumed_reserved + released, run_id))
                db.execute("UPDATE apl_authorized_run_tasks SET consumed_seconds=consumed_seconds+?,reserved_seconds=MAX(0,reserved_seconds-?) WHERE run_id=? AND job_id=?",
                           (elapsed, consumed_reserved + released, run_id, job_id))
                self._event(db, run_id, job_id, "quota.escalated", {
                    "attempt_duration_consumed_seconds": elapsed,
                    "reserved_seconds_released": released,
                    "reserved_seconds_held_unknown": max(0.0, held - consumed_reserved - released),
                    "effect_state": diagnosis.get("effect_state", EffectState.UNKNOWN.value),
                }, now)
                db.execute("UPDATE apl_authorized_run_tasks SET status=?,updated_at=? WHERE run_id=? AND job_id=? AND status IN ('running','reserved','failed','unknown')",
                           (task_state, now, run_id, job_id))
                db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='ifr_intervention_required' WHERE run_id=?",
                           (run_id,))
                diagnosis = summary.get("diagnosis")
                decision = summary.get("decision")
                if diagnosis is not None:
                    self._event(db, run_id, job_id, "ifr.diagnosis", diagnosis, now)
                if decision is not None:
                    self._event(db, run_id, job_id, "ifr.retry_decision", decision, now)
                self._event(db, run_id, job_id, "ifr.escalated", event_summary, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def ifr_prior_failures(self, run_id: str, job_id: str) -> tuple[PriorFailure, ...]:
        state = self.load(run_id)
        diagnoses = [event for event in state["events"]
                     if event["job_id"] == job_id and event["event_type"] == "ifr.diagnosis"]
        failures = []
        for event in diagnoses:
            payload = json.loads(event["payload_json"])
            failures.append(PriorFailure(payload["failure_signature"], payload["strategy_id"],
                                         payload.get("progress_key", "")))
        return tuple(failures)

    def _task_transition(self, run_id, job_id, expected, target, event_type, payload, now):
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("UPDATE apl_authorized_run_tasks SET status=?,updated_at=? WHERE run_id=? AND job_id=? AND status=?", (target, now, run_id, job_id, expected)).rowcount != 1:
                    raise AuthorizedExecutionError("task_transition_conflict")
                self._event(db, run_id, job_id, event_type, payload, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def finish(self, run_id: str, *, status: str, reason: str = "", now: str) -> None:
        if status not in {"completed", "blocked", "cancelled"}:
            raise ValueError("run_terminal_state_invalid")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT status,grant_id,binding_json FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if prior is None:
                    raise KeyError(run_id)
                if prior[0] == status:
                    db.commit()
                    return
                if prior[0] in {"completed", "cancelled"}:
                    raise AuthorizedExecutionError("run_terminal_transition_conflict")
                db.execute("UPDATE apl_authorized_runs SET status=?,finished_at=?,reason=? WHERE run_id=?", (status, now, reason[:512], run_id))
                self._event(db, run_id, None, f"run.{status}", {"reason": reason[:512]}, now)
                try:
                    local_contract = (json.loads(prior[2]).get("authorization_mode")
                                      == "local_execution_contract")
                except (TypeError, json.JSONDecodeError):
                    local_contract = False
                if local_contract and status in {"completed", "cancelled"}:
                    target = "completed" if status == "completed" else "cancelled"
                    changed = db.execute(
                        "UPDATE apl_local_execution_contracts SET state=? "
                        "WHERE contract_id=? AND state='active'",
                        (target, prior[1])).rowcount
                    if changed:
                        self._local_contract_event(
                            db, prior[1], f"contract.{target}",
                            {"run_id": run_id, "reason": reason[:256]}, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def recover_unfinished(self, *, now: str) -> tuple[str, ...]:
        """Mark tasks unknown only when their owning process is confirmed absent."""
        cleanup: dict[tuple[str, str], bool] = {}
        try:
            from agentflow.runtime.local import LocalRuntime
            from agentflow.agents import ProcessIdentity
            with sqlite3.connect(self.database_path, timeout=10) as db:
                owners = db.execute("SELECT run_id,owner_pid,owner_marker FROM apl_authorized_runs WHERE status IN ('ready','running') AND owner_pid IS NOT NULL").fetchall()
                for run_id, pid, marker in owners:
                    if _process_matches(pid, marker):
                        continue
                    rows = db.execute("SELECT job_id,process_identity_json FROM apl_authorized_run_tasks WHERE run_id=? AND status IN ('reserved','running') AND process_identity_json IS NOT NULL", (run_id,)).fetchall()
                    for job_id, encoded in rows:
                        try:
                            terminated = LocalRuntime.cancel_orphan(ProcessIdentity(**json.loads(encoded)))
                        except Exception:
                            terminated = False
                        cleanup[(run_id, job_id)] = bool(terminated)
        except Exception:
            # The state below remains unknown and blocked even if OS cleanup is unavailable.
            pass
        recovered: list[str] = []
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                owners = db.execute("SELECT run_id,owner_pid,owner_marker FROM apl_authorized_runs WHERE status IN ('ready','running') AND owner_pid IS NOT NULL").fetchall()
                for run_id, pid, marker in owners:
                    if not _process_matches(pid, marker):
                        rows = db.execute("SELECT job_id FROM apl_authorized_run_tasks WHERE run_id=? AND status IN ('reserved','running')", (run_id,)).fetchall()
                        self._mark_dead_owner_unknown(db, run_id, now)
                        for (cleanup_run, job_id), terminated in cleanup.items():
                            if cleanup_run == run_id:
                                self._event(db, run_id, job_id, "task.orphan_process_cleanup",
                                            {"termination_requested": terminated,
                                             "automatic_retry": False}, now)
                        recovered.extend(f"{run_id}:{row[0]}" for row in rows)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return tuple(recovered)


class LocalExecutionContractResolver:
    """Resolve local contracts separately from Broker/independent grants."""

    def __init__(self, *, contract_store: SQLiteAuthorizedExecutionStore,
                 cycle_store, workspace_resolver, executor_id: str,
                 executor_version: str, acceptance_policy_resolver=None, clock=None):
        if (not callable(getattr(contract_store, "load_local_contract", None))
                or not callable(getattr(workspace_resolver, "resolve", None))
                or not executor_id or not executor_version):
            raise ValueError("local_execution_contract_resolver_dependencies_required")
        self.contracts, self.cycles = contract_store, cycle_store
        self.workspaces = workspace_resolver
        self.executor_id, self.executor_version = executor_id, executor_version
        self.acceptance_policy_resolver = acceptance_policy_resolver
        self.clock = clock or _utc_now

    def resolve_and_validate(self, contract_id: str, cycle_id: str) -> VerifiedExecutionGrant:
        try:
            contract, state = self.contracts.load_local_contract(contract_id)
        except KeyError as exc:
            raise AuthorizedExecutionError("local_execution_contract_not_found") from exc
        if state != "active":
            raise AuthorizedExecutionError(f"local_execution_contract_{state}")
        grant = contract.authorization
        if (grant.authorization_mode != "local_execution_contract"
                or grant.grant_id != contract.contract_id
                or grant.proposal_id != cycle_id):
            raise AuthorizedExecutionError("local_execution_contract_binding_invalid")
        instant = self.clock()
        if isinstance(instant, str):
            instant = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
        if (not isinstance(instant, datetime) or instant.tzinfo is None or expiry.tzinfo is None
                or expiry <= instant.astimezone(timezone.utc)):
            raise AuthorizedExecutionError("local_execution_contract_expired")
        try:
            snapshot = self.cycles.load(cycle_id)
            workspace = self.workspaces.resolve(cycle_id)
            path = workspace.path.resolve(strict=True)
            info = path.stat()
        except Exception as exc:
            raise AuthorizedExecutionError("local_execution_contract_context_unavailable") from exc
        identity = "sha256:" + hashlib.sha256(
            f"{os.path.normcase(str(path))}\0{info.st_dev}:{info.st_ino}".encode("utf-8")).hexdigest()
        if (snapshot.state not in {APLState.READY, APLState.PAUSED}
                or _local_plan_digest(snapshot) != grant.accepted_plan_digest
                or snapshot.plan_revision_id != grant.plan_revision_id
                or snapshot.authorized_scope != grant.authorized_scope
                or contract.objective_digest != _sha(snapshot.objective)
                or tuple((job.job_id, job.depends_on, job.required_scope)
                         for job in snapshot.jobs) != grant.task_graph
                or str(path) != grant.workspace_path or identity != grant.workspace_identity_digest
                or workspace.workspace_id != grant.workspace_id
                or self.executor_id != grant.executor_id
                or self.executor_version != grant.executor_version
                or (self.acceptance_policy_resolver is not None
                    and self.acceptance_policy_resolver.resolve(cycle_id)
                    != grant.acceptance_policy_digest)):
            raise AuthorizedExecutionError("local_execution_contract_binding_changed")
        return grant


class LocalAuthorizedExecutionService:
    """Passwordless explicit-operator entry point backed by the existing APL controller."""

    def __init__(self, *, cycle_store, execution_store: SQLiteAuthorizedExecutionStore,
                 resolver: LocalExecutionContractResolver,
                 controller: "AuthorizedExecutionController", workspace_resolver):
        if (not callable(getattr(cycle_store, "load", None))
                or not callable(getattr(execution_store, "persist_local_contract", None))
                or not callable(getattr(resolver, "resolve_and_validate", None))
                or not callable(getattr(controller, "start", None))
                or not callable(getattr(workspace_resolver, "resolve", None))):
            raise ValueError("local_execution_service_dependencies_required")
        self.cycles, self.store = cycle_store, execution_store
        self.resolver, self.controller = resolver, controller
        self.workspaces = workspace_resolver

    def start(self, *, cycle_id: str, operation_id: str, limits: ExecutionLimits,
              task_limits: TaskExecutionLimits, financial_policy: FinancialBudgetPolicy,
              expires_at: str, acceptance_policy_digest: str = "",
              on_run_persisted: Callable[[str], None] | None = None
              ) -> tuple[LocalExecutionContract, dict]:
        if on_run_persisted is not None and not callable(on_run_persisted):
            raise ValueError("local_execution_run_callback_invalid")
        snapshot = self.cycles.load(cycle_id)
        workspace = self.workspaces.resolve(cycle_id)
        prior = self.store.local_contract_for_operation(operation_id)
        if prior is not None:
            contract, state = prior
            grant = contract.authorization
            path = workspace.path.resolve(strict=True)
            info = path.stat()
            identity = "sha256:" + hashlib.sha256(
                f"{os.path.normcase(str(path))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
            same_request = (
                state in {"active", "completed", "cancelled"}
                and contract.initiated_by == "explicit_operator_cli"
                and grant.proposal_id == cycle_id
                and grant.plan_revision_id == snapshot.plan_revision_id
                and grant.accepted_plan_digest == _local_plan_digest(snapshot)
                and grant.task_graph == tuple((job.job_id, job.depends_on, job.required_scope)
                                              for job in snapshot.jobs)
                and grant.workspace_id == workspace.workspace_id
                and grant.workspace_path == str(path)
                and grant.workspace_identity_digest == identity
                and grant.executor_id == self.resolver.executor_id
                and grant.executor_version == self.resolver.executor_version
                and grant.limits == limits
                and all(value == task_limits for _, value in grant.task_limits_by_job)
                and grant.budget_policy == financial_policy
                and grant.acceptance_policy_digest
                == (acceptance_policy_digest or _local_plan_digest(snapshot))
                )
            if not same_request:
                raise AuthorizedExecutionError("local_contract_operation_replay_conflict")
            prior_run = self.store.run_for_contract(contract.contract_id)
            if prior_run is None:
                # Contract persistence precedes creation of the run record. If a
                # process stopped in that narrow interval, replay is safe: no
                # task reservation or external process can exist without the
                # run record. Revalidate the immutable contract before creating
                # the run so this recovery cannot renew or broaden authorization.
                if state != "active":
                    raise AuthorizedExecutionError("local_contract_execution_missing")
                self.resolver.resolve_and_validate(contract.contract_id, cycle_id)
                return contract, self.controller.start(
                    cycle_id, contract.contract_id, on_run_persisted=on_run_persisted)
            if on_run_persisted is not None:
                on_run_persisted(prior_run["run_id"])
            return contract, prior_run
        contract = create_local_execution_contract(
            snapshot=snapshot, workspace=workspace,
            executor_id=self.resolver.executor_id,
            executor_version=self.resolver.executor_version,
            limits=limits, task_limits=task_limits,
            financial_policy=financial_policy, expires_at=expires_at,
            operation_id=operation_id,
            acceptance_policy_digest=acceptance_policy_digest)
        self.store.persist_local_contract(contract)
        self.resolver.resolve_and_validate(contract.contract_id, cycle_id)
        return contract, self.controller.start(
            cycle_id, contract.contract_id, on_run_persisted=on_run_persisted)

    def status(self, run_id: str) -> dict:
        return self.controller.status(run_id)

    def resume(self, run_id: str) -> dict:
        state = self.store.load(run_id)
        binding = json.loads(state["binding_json"])
        if binding.get("authorization_mode") != "local_execution_contract":
            raise AuthorizedExecutionError("local_execution_contract_required")
        # Revalidate before any controller continuation. Unknown task effects
        # are rejected by the existing recovery protocol and remain blocked.
        self.resolver.resolve_and_validate(state["grant_id"], state["cycle_id"])
        return self.controller.resume(run_id)

    def cancel(self, run_id: str) -> None:
        state = self.store.load(run_id)
        binding = json.loads(state["binding_json"])
        if binding.get("authorization_mode") != "local_execution_contract":
            raise AuthorizedExecutionError("local_execution_contract_required")
        self.controller.cancel(run_id)


class AuthorizedExecutionController:
    """Sequentially execute only a complete immutable plan covered by one grant."""

    def __init__(self, *, cycle_store, execution_store: SQLiteAuthorizedExecutionStore,
                 grant_resolver: ExecutionGrantResolver, executor: AuthorizedJobExecutor,
                 evidence_validator: ResultEvidenceValidator, clock=None,
                 recovery_policy: IntelligentFailureRecoveryPolicy | None = None):
        if any(item is None for item in (cycle_store, execution_store, grant_resolver, executor, evidence_validator)):
            raise ValueError("authorized_execution_dependencies_required")
        if (not callable(getattr(grant_resolver, "resolve_and_validate", None))
                or not callable(getattr(executor, "execute", None))
                or not callable(getattr(evidence_validator, "validate", None))):
            raise ValueError("authorized_execution_contract_unavailable")
        self.cycles = cycle_store
        self.runs = execution_store
        self.grants = grant_resolver
        self.executor = executor
        self.evidence = evidence_validator
        self.clock = clock or _utc_now
        if recovery_policy is not None and not isinstance(
                recovery_policy, IntelligentFailureRecoveryPolicy):
            raise ValueError("ifr_recovery_policy_invalid")
        self.recovery = recovery_policy or IntelligentFailureRecoveryPolicy()

    def start(self, cycle_id: str, grant_id: str, *,
              on_run_persisted: Callable[[str], None] | None = None) -> dict:
        if on_run_persisted is not None and not callable(on_run_persisted):
            raise ValueError("execution_run_callback_invalid")
        snapshot = self.cycles.load(cycle_id)
        grant = self.grants.resolve_and_validate(grant_id, cycle_id)
        self._validate_binding(snapshot, grant)
        run_id = self.runs.create(cycle_id=cycle_id, grant=grant, snapshot=snapshot,
                                  recovery_limits=self.recovery.limits)
        if on_run_persisted is not None:
            on_run_persisted(run_id)
        return self.run(run_id)

    def resume(self, run_id: str) -> dict:
        run = self.runs.load(run_id)
        if run["status"] not in {"ready", "running"}:
            if run["status"] == "completed":
                return run
            raise AuthorizedExecutionError(f"execution_{run['status']}")
        return self.run(run_id)

    def cancel(self, run_id: str) -> None:
        state = self.runs.load(run_id)
        try:
            binding = json.loads(state["binding_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise AuthorizedExecutionError("persisted_execution_binding_invalid") from exc
        if binding.get("authorization_mode") == "local_execution_contract":
            self.runs.revoke_local_contract(state["grant_id"], reason="operator_cancelled")
        else:
            self.runs.request_cancel(run_id, now=self.clock())

    def status(self, run_id: str) -> dict:
        return self.runs.status(run_id)

    def run(self, run_id: str) -> dict:
        self.runs.claim_run(run_id, now=self.clock())
        try:
            return self._run_claimed(run_id)
        finally:
            self.runs.release_run(run_id)

    def _run_claimed(self, run_id: str) -> dict:
        state = self.runs.load(run_id)
        if state["status"] not in {"ready", "running"}:
            if state["status"] == "completed":
                return state
            raise AuthorizedExecutionError(f"execution_{state['status']}")
        cycle_id = state["cycle_id"]
        persisted_policy = IntelligentFailureRecoveryPolicy(
            RecoveryLimits(**json.loads(state["recovery_limits_json"])))
        if all(task["status"] == "completed" for task in state["tasks"]):
            snapshot = self.cycles.load(cycle_id)
            self._reconcile_apl_checkpoint(snapshot, state)
            self.runs.finish(run_id, status="completed", now=self.clock())
            return self.runs.load(run_id)
        try:
            binding = self.grants.resolve_and_validate(state["grant_id"], cycle_id)
            snapshot = self.cycles.load(cycle_id)
            self._validate_binding(snapshot, binding, persisted=state)
        except Exception as exc:
            self.runs.finish(run_id, status="blocked", reason="grant_validation_failed", now=self.clock())
            raise AuthorizedExecutionError("grant_validation_failed; dispatch_suspended") from exc
        started = time.monotonic()
        initial_consumed_seconds = state["consumed_seconds"]
        spent = state["consumed_budget"]
        outcomes = {job_id: outcome for job_id, outcome in snapshot.outcomes}
        if snapshot.state is not APLState.READY:
            raise AuthorizedExecutionError(f"cycle_state_{snapshot.state.value.lower()}")
        while True:
            state = self.runs.load(run_id)
            if state["status"] == "blocked":
                raise AuthorizedExecutionError(state["reason"] or "execution_blocked")
            if self.runs.cancel_requested(run_id):
                self.runs.finish(run_id, status="cancelled", reason="operator_cancelled", now=self.clock())
                return self.runs.load(run_id)
            completed = {task["job_id"] for task in state["tasks"] if task["status"] == "completed"}
            if len(completed) == len(snapshot.jobs):
                self._reconcile_apl_checkpoint(snapshot, state)
                self.runs.finish(run_id, status="completed", now=self.clock())
                return self.runs.load(run_id)
            if state["attempts"] >= binding.limits.max_jobs * binding.limits.max_attempts_per_job:
                self.runs.finish(run_id, status="blocked", reason="execution_job_or_attempt_limit", now=self.clock())
                raise AuthorizedExecutionError("execution_job_or_attempt_limit")
            if initial_consumed_seconds + time.monotonic() - started >= binding.limits.max_seconds:
                self.runs.finish(run_id, status="blocked", reason="execution_time_limit", now=self.clock())
                raise AuthorizedExecutionError("execution_time_limit")
            if state["attempts"] >= binding.limits.max_iterations:
                self.runs.finish(run_id, status="blocked", reason="execution_iteration_limit", now=self.clock())
                raise AuthorizedExecutionError("execution_iteration_limit")
            financial_estimate_limit = _financial_estimate_limit(binding)
            if spent > financial_estimate_limit:
                self.runs.finish(run_id, status="blocked", reason="estimated_budget_limit", now=self.clock())
                raise AuthorizedExecutionError("estimated_budget_limit")
            ready = next((job for job in snapshot.jobs if job.job_id not in completed
                          and set(job.depends_on) <= completed), None)
            if ready is None:
                self.runs.finish(run_id, status="blocked", reason="no_eligible_job_or_dependency_cycle", now=self.clock())
                raise AuthorizedExecutionError("no_eligible_job_or_dependency_cycle")
            current_task = next(task for task in state["tasks"] if task["job_id"] == ready.job_id)
            if current_task["status"] == "ready" and state["jobs_started"] >= binding.limits.max_jobs:
                self.runs.finish(run_id, status="blocked", reason="execution_job_or_attempt_limit", now=self.clock())
                raise AuthorizedExecutionError("execution_job_or_attempt_limit")
            # Revalidate revocation, expiry, proposal digest, workspace and operation immediately before reservation/dispatch.
            try:
                binding = self.grants.resolve_and_validate(state["grant_id"], cycle_id)
                self._validate_binding(snapshot, binding, persisted=state)
            except Exception as exc:
                self.runs.finish(run_id, status="blocked", reason="grant_validation_failed", now=self.clock())
                raise AuthorizedExecutionError("grant_validation_failed; dispatch_suspended") from exc
            reservation = self.runs.reserve_next(run_id, ready,
                                                  grant=binding,
                                                  now=self.clock())
            self.runs.mark_running(run_id, ready.job_id, now=self.clock())
            attempt_started = time.monotonic()
            try:
                execute_authorized = getattr(self.executor, "execute_authorized", None)
                if callable(execute_authorized):
                    if reservation["strategy_id"] != "default":
                        recovery_execute = getattr(self.executor, "execute_authorized_recovery", None)
                        if not callable(recovery_execute):
                            raise NoEffectExecutionError(
                                "executor_does_not_support_authorized_recovery_strategy",
                                diagnosis=FailureDiagnosis(
                                    FailureKind.SECURITY_OR_AUTHORIZATION_BLOCK,
                                    Recoverability.NOT_RECOVERABLE, EffectState.NO_EFFECT_PROVEN,
                                    "recovery_strategy_executor_unavailable"))
                        outcome = recovery_execute(
                            cycle_id, ready, binding, reservation["operation_id"],
                            reservation["attempt_id"], reservation["strategy_id"],
                            reservation["reserved_seconds"],
                            cancellation=_RunCancellation(
                                self.runs, run_id,
                                authorization_check=lambda: self._authorization_still_current(
                                    run_id, cycle_id, binding)),
                            on_started=lambda identity: self.runs.record_process_identity(
                                run_id, ready.job_id, reservation["operation_id"], identity,
                                attempt_id=reservation["attempt_id"], now=self.clock()))
                    else:
                        outcome = execute_authorized(
                            cycle_id, ready, binding, reservation["operation_id"],
                            attempt_id=reservation["attempt_id"],
                            strategy_id=reservation["strategy_id"],
                            remaining_seconds=reservation["reserved_seconds"],
                            pre_spawn_check=lambda: self._verify_before_spawn(
                                run_id, cycle_id, ready, binding, reservation),
                            cancellation=_RunCancellation(
                                self.runs, run_id,
                                authorization_check=lambda: self._authorization_still_current(
                                    run_id, cycle_id, binding)),
                            on_started=lambda identity: self.runs.record_process_identity(
                                run_id, ready.job_id, reservation["operation_id"], identity,
                                attempt_id=reservation["attempt_id"], now=self.clock()),
                        )
                else:
                    if reservation["strategy_id"] != "default":
                        recovery_executor = getattr(self.executor, "execute_recovery", None)
                        if not callable(recovery_executor):
                            raise NoEffectExecutionError(
                                "executor_does_not_support_authorized_recovery_strategy",
                                diagnosis=FailureDiagnosis(
                                    FailureKind.SECURITY_OR_AUTHORIZATION_BLOCK,
                                    Recoverability.NOT_RECOVERABLE, EffectState.NO_EFFECT_PROVEN,
                                    "recovery_strategy_executor_unavailable"))
                        outcome = recovery_executor(cycle_id, ready, binding.authorized_scope,
                                                   reservation["operation_id"],
                                                   reservation["attempt_id"],
                                                   reservation["strategy_id"],
                                                   max(0.0, min(binding.limits.max_seconds,
                                                                binding.task_limits_for(ready.job_id).max_seconds)
                                                       - current_task["consumed_seconds"]))
                    else:
                        outcome = self.executor.execute(cycle_id, ready, binding.authorized_scope,
                                                        reservation["operation_id"])
            except NoEffectExecutionError as exc:
                elapsed = time.monotonic() - attempt_started
                diagnosis = exc.diagnosis
                state = self.runs.load(run_id)
                effective_limits = RecoveryLimits(
                    max_attempts_total=min(persisted_policy.limits.max_attempts_total,
                                           binding.limits.max_attempts_per_job),
                    max_equivalent_repeats=persisted_policy.limits.max_equivalent_repeats,
                    max_duration_seconds=min(persisted_policy.limits.max_duration_seconds,
                                              binding.limits.max_seconds),
                    max_estimated_budget=_minimum_positive(
                        persisted_policy.limits.max_estimated_budget,
                        financial_estimate_limit))
                policy = IntelligentFailureRecoveryPolicy(effective_limits)
                recovery_context = RecoveryContext(
                    diagnosis=diagnosis,
                    current_strategy_id=reservation["strategy_id"],
                    recommended_strategy_id=diagnosis.recommended_strategy_id,
                    authorized_strategy_ids=binding.strategies_for(ready.job_id),
                    attempts_total=reservation["attempt"],
                    elapsed_seconds=state["consumed_seconds"] + elapsed,
                    consumed_budget=state["consumed_budget"],
                    grant_valid=True,
                    prior_failures=self.runs.ifr_prior_failures(run_id, ready.job_id),
                )
                decision = policy.decide(recovery_context)
                diagnosis_record = _diagnosis_record(diagnosis, reservation)
                decision_record = _decision_record(decision, reservation)
                alternate_supported = (
                    decision.strategy_id in {"default", reservation["strategy_id"]}
                    or callable(getattr(self.executor, "execute_recovery", None))
                    or callable(getattr(self.executor, "execute_authorized_recovery", None)))
                if decision.retry and alternate_supported:
                    self.runs.record_retryable_ifr(
                        run_id, ready.job_id, decision.strategy_id,
                        diagnosis_record, decision_record, elapsed_seconds=elapsed,
                        now=self.clock())
                    continue
                if decision.retry and not alternate_supported:
                    decision_record.update({"retry": False, "reason_code": "strategy_executor_unavailable",
                                            "safe": False})
                summary = self._intervention_summary(
                    state, ready, diagnosis, decision_record,
                    reason=str(exc)[:256], strategy_id=reservation["strategy_id"])
                summary["diagnosis"] = diagnosis_record
                summary["decision"] = decision_record
                self.runs.escalate_ifr(run_id, ready.job_id, task_state="failed",
                                       summary=summary, now=self.clock(), elapsed_seconds=elapsed)
                raise AuthorizedExecutionError("ifr_intervention_required") from exc
            except BaseException as exc:
                diagnosis = FailureDiagnosis(
                    FailureKind.OPERATIONAL_UNKNOWN, Recoverability.NOT_RECOVERABLE,
                    EffectState.UNKNOWN, "executor_result_unknown",
                    context=(("exception", type(exc).__name__),))
                state = self.runs.load(run_id)
                decision = policy_decision = self.recovery.decide(RecoveryContext(
                    diagnosis, reservation["strategy_id"], None,
                    binding.strategies_for(ready.job_id), reservation["attempt"],
                    state["consumed_seconds"], state["consumed_budget"], True,
                    self.runs.ifr_prior_failures(run_id, ready.job_id)))
                decision_record = _decision_record(policy_decision, reservation)
                summary = self._intervention_summary(state, ready, diagnosis,
                                                     decision_record,
                                                     reason=type(exc).__name__,
                                                     strategy_id=reservation["strategy_id"])
                summary["diagnosis"] = _diagnosis_record(diagnosis, reservation)
                summary["decision"] = decision_record
                self.runs.escalate_ifr(run_id, ready.job_id, task_state="unknown",
                                       summary=summary, now=self.clock(),
                                       elapsed_seconds=time.monotonic() - attempt_started)
                if isinstance(exc, Exception):
                    raise AuthorizedExecutionError(
                        "unknown_execution_result; automatic_retry_forbidden") from exc
                # Preserve operator interrupts, but durably mark the effect unknown
                # before releasing the run owner so a later resume cannot redispatch.
                raise
            quality = None
            if outcome.status == "succeeded":
                try:
                    evaluate = getattr(self.evidence, "evaluate", None)
                    if callable(evaluate):
                        evaluation = evaluate(cycle_id, ready, outcome)
                        raw_quality = getattr(evaluation, "quality", None)
                        acceptance_evidence = getattr(evaluation, "evidence", None)
                        if (not isinstance(raw_quality, QualityResult)
                                or type(acceptance_evidence) is not tuple
                                or any(type(item) is not dict for item in acceptance_evidence)):
                            raise ValueError("acceptance_evaluation_contract_invalid")
                        outcome = replace(outcome, evidence=outcome.evidence + acceptance_evidence)
                    else:
                        raw_quality = self.evidence.validate(cycle_id, ready, outcome)
                    quality = (raw_quality if isinstance(raw_quality, QualityResult)
                               else QualityResult.PASS if raw_quality is True
                               else QualityResult.FAIL if raw_quality is False
                               else QualityResult.INCONCLUSIVE)
                except Exception:
                    quality = QualityResult.INCONCLUSIVE
            valid = outcome.status == "succeeded" and quality is QualityResult.PASS
            elapsed = time.monotonic() - attempt_started
            self.runs.record_outcome(run_id, ready.job_id, outcome, validated=valid,
                                     elapsed_seconds=elapsed, now=self.clock(),
                                     quality_result=quality,
                                     budget_currency=binding.budget_policy.currency)
            if not valid:
                if outcome.status == "unknown":
                    kind, effect = FailureKind.OPERATIONAL_UNKNOWN, EffectState.UNKNOWN
                    code, recoverability = "executor_reported_unknown", Recoverability.NOT_RECOVERABLE
                elif quality is QualityResult.INCONCLUSIVE:
                    kind, effect = FailureKind.EVALUATION_INCONCLUSIVE, EffectState.EFFECTS_POSSIBLE
                    code, recoverability = "evidence_evaluation_inconclusive", Recoverability.NOT_RECOVERABLE
                elif quality is QualityResult.FAIL:
                    kind, effect = FailureKind.QUALITY_FAILURE, EffectState.EFFECTS_POSSIBLE
                    code, recoverability = "acceptance_criteria_failed", Recoverability.NOT_RECOVERABLE
                else:
                    kind, effect = FailureKind.OPERATIONAL_UNKNOWN, EffectState.EFFECTS_POSSIBLE
                    code, recoverability = "executor_reported_failure", Recoverability.NOT_RECOVERABLE
                evidence_refs = tuple(_sha(_canonical(item)) for item in outcome.evidence)
                diagnosis = FailureDiagnosis(kind, recoverability, effect, code,
                                             evidence_refs=evidence_refs,
                                             quality_result=quality)
                current_state = self.runs.load(run_id)
                decision = persisted_policy.decide(RecoveryContext(
                    diagnosis, reservation["strategy_id"], None,
                    binding.strategies_for(ready.job_id), reservation["attempt"],
                    current_state["consumed_seconds"], current_state["consumed_budget"],
                    True, self.runs.ifr_prior_failures(run_id, ready.job_id)))
                decision_record = _decision_record(decision, reservation)
                summary = self._intervention_summary(current_state, ready, diagnosis,
                                                     decision_record,
                                                     reason=decision.reason_code,
                                                     strategy_id=reservation["strategy_id"])
                summary["diagnosis"] = _diagnosis_record(diagnosis, reservation)
                summary["decision"] = decision_record
                self.runs.escalate_ifr(run_id, ready.job_id,
                                       task_state="unknown" if outcome.status == "unknown" else "failed",
                                       summary=summary, now=self.clock())
                raise AuthorizedExecutionError("ifr_intervention_required")
            spent += outcome.cost
            if spent > _financial_estimate_limit(binding):
                self.runs.finish(run_id, status="blocked", reason="estimated_budget_limit", now=self.clock())
                raise AuthorizedExecutionError("estimated_budget_limit")
            task_state = next(item for item in self.runs.load(run_id)["tasks"]
                              if item["job_id"] == ready.job_id)
            task_budget = _minimum_positive(_financial_estimate_limit(binding),
                                            binding.task_limits_for(ready.job_id).max_estimated_budget)
            if task_budget and task_state["consumed_budget"] > task_budget:
                self.runs.finish(run_id, status="blocked", reason="task_estimated_budget_limit",
                                 now=self.clock())
                raise AuthorizedExecutionError("task_estimated_budget_limit")
            try:
                self._reconcile_apl_checkpoint(self.cycles.load(cycle_id), self.runs.load(run_id))
            except Exception as exc:
                # The executor outcome is durable in the run journal; failure to update APL is recoverable from this verified checkpoint.
                raise AuthorizedExecutionError("verified_outcome_persisted_but_cycle_checkpoint_failed") from exc

    def _authorization_still_current(self, run_id: str, cycle_id: str,
                                    original: VerifiedExecutionGrant) -> bool:
        """Revalidate during a running child; revocation requests cooperative cancellation."""
        try:
            current = self.grants.resolve_and_validate(original.grant_id, cycle_id)
            snapshot = self.cycles.load(cycle_id)
            persisted = self.runs.load(run_id)
            self._validate_binding(snapshot, current, persisted=persisted)
            if _canonical(asdict(current)) == _canonical(asdict(original)):
                return True
        except Exception:
            pass
        self.runs.request_cancel(
            run_id, now=self.clock(), reason="grant_revoked_expired_or_unavailable",
            event_type="run.revocation_cancel_requested")
        return False

    def _verify_before_spawn(self, run_id: str, cycle_id: str, job: JobProposal,
                             grant: VerifiedExecutionGrant, reservation: dict) -> None:
        """Last application-controlled validation before LocalRuntime calls Popen."""
        try:
            if not self._authorization_still_current(run_id, cycle_id, grant):
                raise AuthorizedExecutionError("grant_revoked_or_changed_before_spawn")
            self.runs.verify_dispatch_intent(
                run_id, job.job_id, reservation["operation_id"],
                reservation["attempt_id"], grant)
        except Exception as exc:
            raise NoEffectExecutionError(
                "dispatch_gate_rejected_before_spawn",
                diagnosis=FailureDiagnosis(
                    FailureKind.SECURITY_OR_AUTHORIZATION_BLOCK,
                    Recoverability.NOT_RECOVERABLE,
                    EffectState.NO_EFFECT_PROVEN,
                    "pre_spawn_authorization_or_intent_invalid")) from exc

    def _reconcile_apl_checkpoint(self, original: CycleSnapshot, run_state: dict) -> None:
        """Restore verified outcomes after a crash between the run journal and APL snapshot."""
        latest = self.cycles.load(original.cycle_id)
        if (latest.plan_revision_id != original.plan_revision_id
                or latest.plan_digest != original.plan_digest):
            raise AuthorizedExecutionError("approved_plan_changed_during_recovery")
        persisted = {job_id: outcome for job_id, outcome in latest.outcomes}
        for task in run_state["tasks"]:
            if task["status"] != "completed" or not task["outcome_json"]:
                continue
            data = json.loads(task["outcome_json"])
            outcome = ExecutionOutcome(data["status"], data["summary"], tuple(data["evidence"]),
                                       data["cost"], data["progress_key"], data.get("handoff"))
            prior = persisted.get(task["job_id"])
            if prior is not None and prior != outcome:
                raise AuthorizedExecutionError("apl_outcome_conflicts_with_verified_run_checkpoint")
            persisted[task["job_id"]] = outcome
        ordered = tuple((job.job_id, persisted[job.job_id]) for job in latest.jobs if job.job_id in persisted)
        if ordered == latest.outcomes:
            return
        already = {key for key, _ in latest.outcomes}
        newly_completed_cost = sum(outcome.cost for job_id, outcome in ordered if job_id not in already)
        updated = CycleSnapshot(latest.cycle_id, latest.objective, latest.authorized_scope,
                                APLState.COMPLETED if len(ordered) == len(latest.jobs) else APLState.READY,
                                latest.jobs, ordered,
                                latest.iterations + sum(1 for key, _ in ordered if key not in already),
                                latest.consumed_budget + newly_completed_cost, latest.version, "", latest.revision,
                                latest.plan_digest, latest.plan_revision_id)
        self.cycles.save(updated, latest.iterations, expected_revision=latest.revision)

    def _intervention_summary(self, state: dict, job: JobProposal,
                              diagnosis: FailureDiagnosis, decision: dict, *,
                              reason: str, strategy_id: str) -> dict:
        task_state = {item["job_id"]: item["status"] for item in state["tasks"]}
        events = state["events"]
        return {
            "schema_version": 1,
            "requested": {"cycle_id": state["cycle_id"], "objective": self.cycles.load(
                state["cycle_id"]).objective, "job_id": job.job_id,
                "instruction_digest": _sha(job.instruction),
                "authorized_scope": list(self.cycles.load(state["cycle_id"]).authorized_scope)},
            "already_executed": sorted(key for key, value in task_state.items()
                                        if value == "completed"),
            "failed_or_inconclusive": [{"job_id": job.job_id,
                                        "failure_kind": diagnosis.failure_kind.value,
                                        "quality_result": diagnosis.quality_result.value
                                        if diagnosis.quality_result else None}],
            "corrections_attempted": [{"strategy_id": strategy_id,
                                        "attempts": sum(1 for event in events
                                                        if event["job_id"] == job.job_id
                                                        and event["event_type"] == "ifr.attempt_started")}],
            "evidence_refs": list(diagnosis.evidence_refs),
            "why_stopped": reason[:256],
            "decision": {key: decision.get(key) for key in
                         ("retry", "reason_code", "authorized", "safe", "strategy_id")},
            "safe_alternatives": (["operator_inspects_evidence_and_reconciles_effects",
                                   "operator_issues_new_scope_bound_authorization"]
                                  if diagnosis.effect_state in {EffectState.UNKNOWN,
                                                               EffectState.EFFECTS_POSSIBLE}
                                  else ["operator_selects_an_already_authorized_strategy",
                                        "operator_cancels_run"]),
            "authorization_scope_unchanged": True,
        }

    def _validate_binding(self, snapshot: CycleSnapshot, grant: VerifiedExecutionGrant,
                          persisted: dict | None = None) -> None:
        _financial_estimate_limit(grant)
        if grant.budget_policy.mode is FinancialBudgetMode.ESTIMATED_COST_ACCEPTED:
            if (grant.limits.max_estimated_budget > 0
                    and grant.budget_policy.estimated_amount > grant.limits.max_estimated_budget):
                raise AuthorizedExecutionError("financial_estimate_exceeds_execution_limit")
        if grant.state != "approved":
            raise AuthorizedExecutionError("grant_not_approved")
        if (grant.authorization_mode == "authenticated_grant"
                and (type(grant.operator_identity) is not str or not grant.operator_identity.strip())):
            raise AuthorizedExecutionError("grant_operator_identity_required")
        if (type(grant.executor_id) is not str or not grant.executor_id.strip()
                or type(grant.executor_version) is not str or not grant.executor_version.strip()):
            raise AuthorizedExecutionError("grant_executor_binding_required")
        try:
            now = self.clock()
            if isinstance(now, str):
                now = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if now.tzinfo is None:
                raise ValueError("naive_clock")
            expiry = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise AuthorizedExecutionError("grant_expiry_or_clock_invalid") from exc
        if expiry.tzinfo is None:
            raise AuthorizedExecutionError("grant_expiry_must_be_aware")
        if expiry.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            raise AuthorizedExecutionError("grant_expired")
        if snapshot.state in {APLState.CANCELLED, APLState.FAILED, APLState.BLOCKED, APLState.WAITING_APPROVAL}:
            raise AuthorizedExecutionError("cycle_not_executable")
        snapshot_plan_digest = (_local_plan_digest(snapshot)
                                if grant.authorization_mode == "local_execution_contract"
                                else snapshot.plan_digest)
        if (snapshot.plan_revision_id != grant.plan_revision_id
                or snapshot_plan_digest != grant.accepted_plan_digest
                or snapshot.authorized_scope != grant.authorized_scope):
            raise AuthorizedExecutionError("approved_plan_binding_changed")
        if (grant.authorization_mode == "local_execution_contract"
                and tuple((job.job_id, job.depends_on, job.required_scope)
                          for job in snapshot.jobs) != grant.task_graph):
            raise AuthorizedExecutionError("local_execution_contract_task_graph_changed")
        job_ids = {job.job_id for job in snapshot.jobs}
        if set(dict(grant.task_limits_by_job)) != job_ids:
            raise AuthorizedExecutionError("grant_task_limits_do_not_cover_plan")
        operations = {f"job:{job_id}" for job_id in job_ids}
        if not job_ids or len(job_ids) != len(snapshot.jobs) or operations != set(grant.allowed_operations):
            raise AuthorizedExecutionError("grant_does_not_cover_entire_plan")
        if any(not set(job.required_scope) <= set(grant.authorized_scope) for job in snapshot.jobs):
            raise AuthorizedExecutionError("plan_job_outside_authorized_scope")
        if set(dict(grant.strategies_by_job)) - job_ids:
            raise AuthorizedExecutionError("grant_recovery_strategy_for_unknown_job")
        for job_id in job_ids:
            task_limits = grant.task_limits_for(job_id)
            if (task_limits.max_attempts > grant.limits.max_attempts_per_job
                    or task_limits.max_seconds > grant.limits.max_seconds
                    or (grant.limits.max_estimated_budget > 0
                        and task_limits.max_estimated_budget > grant.limits.max_estimated_budget)):
                raise AuthorizedExecutionError("task_limits_exceed_global_grant")
        if len(snapshot.jobs) > grant.limits.max_jobs:
            raise AuthorizedExecutionError("plan_exceeds_authorized_job_limit")
        jobs = {job.job_id: job for job in snapshot.jobs}
        if len(jobs) != len(snapshot.jobs):
            raise AuthorizedExecutionError("duplicate_job_id")
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(job_id: str) -> None:
            if job_id in visiting:
                raise AuthorizedExecutionError("dependency_cycle")
            if job_id in visited:
                return
            visiting.add(job_id)
            for dependency in jobs[job_id].depends_on:
                if dependency not in jobs:
                    raise AuthorizedExecutionError("dependency_missing")
                visit(dependency)
            visiting.remove(job_id)
            visited.add(job_id)
        for job_id in jobs:
            visit(job_id)
        if persisted is not None:
            encoded = _canonical(asdict(grant))
            if (_sha(encoded) != persisted["binding_digest"]
                    or _sha(_canonical([asdict(job) for job in snapshot.jobs])) != persisted["plan_digest"]
                    or snapshot_plan_digest != json.loads(persisted["binding_json"])["accepted_plan_digest"]):
                raise AuthorizedExecutionError("persisted_run_binding_changed")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _local_plan_digest(snapshot: CycleSnapshot) -> str:
    """Cryptographic digest of the complete local APL plan binding."""
    return _sha(_canonical({
        "cycle_id": snapshot.cycle_id,
        "objective": snapshot.objective,
        "revision": snapshot.plan_revision_id,
        "jobs": [asdict(job) for job in snapshot.jobs],
        "authorized_scope": list(snapshot.authorized_scope),
    }))


def _local_contract_payload(contract: LocalExecutionContract) -> dict:
    return {
        "contract_id": contract.contract_id,
        "operation_id": contract.operation_id,
        "initiated_by": contract.initiated_by,
        "objective_digest": contract.objective_digest,
        "authorization": asdict(contract.authorization),
    }


def _local_contract_from_payload(value: dict) -> LocalExecutionContract:
    expected = {"contract_id", "operation_id", "initiated_by", "objective_digest", "authorization"}
    if type(value) is not dict or set(value) != expected or type(value["authorization"]) is not dict:
        raise AuthorizedExecutionError("local_execution_contract_schema_invalid")
    raw = value["authorization"]
    try:
        authorization = VerifiedExecutionGrant(
            grant_id=raw["grant_id"], request_digest=raw["request_digest"],
            proposal_id=raw["proposal_id"], approved_plan_digest=raw["approved_plan_digest"],
            revision=raw["revision"], plan_revision_id=raw["plan_revision_id"],
            accepted_plan_digest=raw["accepted_plan_digest"], workspace_id=raw["workspace_id"],
            workspace_path=raw["workspace_path"],
            workspace_identity_digest=raw["workspace_identity_digest"],
            allowed_operations=tuple(raw["allowed_operations"]),
            authorized_scope=tuple(raw["authorized_scope"]), expires_at=raw["expires_at"],
            limits=ExecutionLimits(**raw["limits"]), state=raw["state"],
            strategies_by_job=tuple((item[0], tuple(item[1]))
                                    for item in raw["strategies_by_job"]),
            operator_identity=raw["operator_identity"], executor_id=raw["executor_id"],
            executor_version=raw["executor_version"],
            task_limits_by_job=tuple((item[0], TaskExecutionLimits(**item[1]))
                                     for item in raw["task_limits_by_job"]),
            budget_policy=FinancialBudgetPolicy(
                FinancialBudgetMode(raw["budget_policy"]["mode"]),
                raw["budget_policy"]["estimated_amount"], raw["budget_policy"]["currency"]),
            authorization_mode=raw["authorization_mode"],
            task_graph=tuple((item[0], tuple(item[1]), tuple(item[2]))
                             for item in raw["task_graph"]),
            acceptance_policy_digest=raw.get("acceptance_policy_digest", ""),
        )
        return LocalExecutionContract(value["contract_id"], value["operation_id"],
                                      value["initiated_by"], value["objective_digest"], authorization)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise AuthorizedExecutionError("local_execution_contract_schema_invalid") from exc


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _minimum_positive(*values: float) -> float:
    positive = [value for value in values if value > 0]
    return min(positive) if positive else 0.0


def _operation_id(run_id: str, job_id: str, plan_digest: str) -> str:
    raw = _sha(f"{run_id}:{job_id}:{plan_digest}")[-32:]
    return "apl-op-" + raw


def _attempt_id(run_id: str, job_id: str, attempt: int) -> str:
    raw = _sha(f"{run_id}:{job_id}:{attempt}")[-32:]
    return "apl-attempt-" + raw


def _diagnosis_record(diagnosis: FailureDiagnosis, reservation: dict) -> dict:
    return {
        "schema_version": diagnosis.schema_version,
        "failure_kind": diagnosis.failure_kind.value,
        "recoverability": diagnosis.recoverability.value,
        "effect_state": diagnosis.effect_state.value,
        "failure_code": diagnosis.failure_code,
        "context": [list(item) for item in diagnosis.context],
        "evidence_refs": list(diagnosis.evidence_refs),
        "hypothesis": diagnosis.hypothesis[:2048],
        "progress_key": diagnosis.progress_key[:256],
        "recommended_strategy_id": diagnosis.recommended_strategy_id,
        "quality_result": diagnosis.quality_result.value if diagnosis.quality_result else None,
        "failure_signature": diagnosis.failure_signature,
        "strategy_id": reservation["strategy_id"],
        "attempt_id": reservation["attempt_id"],
        "attempt_number": reservation["attempt"],
    }


def _decision_record(decision, reservation: dict) -> dict:
    return {
        "retry": decision.retry, "necessary": decision.necessary,
        "useful": decision.useful, "authorized": decision.authorized,
        "safe": decision.safe, "strategy_id": decision.strategy_id,
        "reason_code": decision.reason_code,
        "failure_signature": decision.failure_signature,
        "attempt_id": reservation["attempt_id"],
        "attempt_number": reservation["attempt"],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class _RunCancellation:
    """Durable cancellation plus periodic grant revalidation for an active child."""

    def __init__(self, store: SQLiteAuthorizedExecutionStore, run_id: str,
                 authorization_check=None):
        self._store, self._run_id = store, run_id
        self._authorization_check = authorization_check
        self._last_authorization_check = 0.0

    def is_set(self) -> bool:
        if self._store.cancel_requested(self._run_id):
            return True
        if (self._authorization_check is not None
                and time.monotonic() - self._last_authorization_check >= 0.25):
            self._last_authorization_check = time.monotonic()
            return not self._authorization_check()
        return False


def _current_process_marker() -> str:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
        api.GetProcessTimes.restype = wintypes.BOOL
        values = [FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(api.GetCurrentProcess(), *(ctypes.byref(value) for value in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return str((values[0].high << 32) | values[0].low)
    stat = open(f"/proc/{os.getpid()}/stat", encoding="ascii").read().rsplit(")", 1)[1].split()[19]
    boot_id = open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
    return f"{boot_id}:{stat}"


def _process_matches(pid: int, marker: str | None) -> bool:
    if not marker:
        return False
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
            api.GetProcessTimes.restype = wintypes.BOOL
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                values = [FILETIME() for _ in range(4)]
                if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                    return False
                return str((values[0].high << 32) | values[0].low) == marker
            finally:
                api.CloseHandle(handle)
        stat = open(f"/proc/{pid}/stat", encoding="ascii").read().rsplit(")", 1)[1].split()[19]
        boot_id = open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
        return f"{boot_id}:{stat}" == marker
    except (OSError, IndexError):
        return False
