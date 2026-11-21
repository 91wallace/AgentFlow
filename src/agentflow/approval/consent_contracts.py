"""Pure consent/profile value contracts; no configuration or authentication IO."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import re
from typing import Protocol


class ProfileConfigurationError(ValueError):
    """Profile selection or consent binding is missing or invalid."""


class SecurityProfile(str, Enum):
    LOCAL_SUPERVISED = "LOCAL_SUPERVISED"
    INDEPENDENT_AUTHORITY = "INDEPENDENT_AUTHORITY"


class ConsentDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    UNAVAILABLE = "unavailable"


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _identifier(value: str, field: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ProfileConfigurationError(f"{field}_invalid")


def _digest(value: str, field: str) -> None:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ProfileConfigurationError(f"{field}_invalid")


def _timestamp(value: str, field: str) -> datetime:
    if type(value) is not str:
        raise ProfileConfigurationError(f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProfileConfigurationError(f"{field}_invalid") from exc
    if parsed.tzinfo is None:
        raise ProfileConfigurationError(f"{field}_must_be_timezone_aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ConsentLimits:
    max_jobs: int
    max_iterations: int
    timeout_seconds: int
    estimated_budget: Decimal
    budget_currency: str

    def __post_init__(self) -> None:
        for name in ("max_jobs", "max_iterations", "timeout_seconds"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ProfileConfigurationError(f"{name}_must_be_positive_integer")
        if (not isinstance(self.estimated_budget, Decimal)
                or not self.estimated_budget.is_finite() or self.estimated_budget < 0):
            raise ProfileConfigurationError("estimated_budget_invalid")
        if type(self.budget_currency) is not str or not _CURRENCY.fullmatch(self.budget_currency):
            raise ProfileConfigurationError("budget_currency_invalid")


@dataclass(frozen=True)
class LocalConsentRequest:
    """Immutable payload intended for consent UI; it cannot authorize work."""

    profile: SecurityProfile
    proposal_id: str
    proposal_digest: str
    revision: int
    workspace_id: str
    allowed_operations: tuple[str, ...]
    limits: ConsentLimits
    expires_at: str
    operator_identity: str

    def __post_init__(self) -> None:
        if self.profile is not SecurityProfile.LOCAL_SUPERVISED:
            raise ProfileConfigurationError("local_consent_requires_local_supervised_profile")
        _identifier(self.proposal_id, "proposal_id")
        _digest(self.proposal_digest, "proposal_digest")
        if type(self.revision) is not int or self.revision < 1:
            raise ProfileConfigurationError("revision_invalid")
        _identifier(self.workspace_id, "workspace_id")
        _identifier(self.operator_identity, "operator_identity")
        if (type(self.allowed_operations) is not tuple or not self.allowed_operations
                or len(set(self.allowed_operations)) != len(self.allowed_operations)):
            raise ProfileConfigurationError("allowed_operations_invalid")
        for operation in self.allowed_operations:
            _identifier(operation, "allowed_operation")
        if not isinstance(self.limits, ConsentLimits):
            raise ProfileConfigurationError("consent_limits_required")
        _timestamp(self.expires_at, "expires_at")

    @property
    def request_digest(self) -> str:
        payload = {
            "profile": self.profile.value,
            "proposal_id": self.proposal_id,
            "proposal_digest": self.proposal_digest,
            "revision": self.revision,
            "workspace_id": self.workspace_id,
            "allowed_operations": list(self.allowed_operations),
            "limits": {
                "max_jobs": self.limits.max_jobs,
                "max_iterations": self.limits.max_iterations,
                "timeout_seconds": self.limits.timeout_seconds,
                "estimated_budget": str(self.limits.estimated_budget),
                "budget_currency": self.limits.budget_currency,
            },
            "expires_at": self.expires_at,
            "operator_identity": self.operator_identity,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionPlanConsentBinding:
    """Exact plan/executor binding displayed and authenticated for one-run consent."""

    cycle_id: str
    plan_revision_id: str
    plan_content_digest: str
    objective_digest: str
    workspace_path: str
    workspace_identity_digest: str
    authorized_scope: tuple[str, ...]
    task_graph: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]
    executor_id: str
    executor_version: str
    max_attempts_per_job: int
    max_duration_seconds: float
    max_concurrency: int
    financial_budget_mode: str
    strategies_by_job: tuple[tuple[str, tuple[str, ...]], ...]
    task_limits_by_job: tuple[tuple[str, int, float, float], ...]

    def __post_init__(self) -> None:
        for name in ("cycle_id", "plan_revision_id", "executor_id", "executor_version"):
            _identifier(getattr(self, name), name)
        _digest(self.plan_content_digest, "plan_content_digest")
        _digest(self.objective_digest, "objective_digest")
        if (type(self.workspace_path) is not str or not self.workspace_path
                or "\x00" in self.workspace_path):
            raise ProfileConfigurationError("execution_workspace_path_invalid")
        _digest(self.workspace_identity_digest, "workspace_identity_digest")
        if (type(self.authorized_scope) is not tuple or not self.authorized_scope
                or len(set(self.authorized_scope)) != len(self.authorized_scope)):
            raise ProfileConfigurationError("execution_authorized_scope_invalid")
        for item in self.authorized_scope:
            _identifier(item, "execution_scope")
        if (type(self.task_graph) is not tuple or not self.task_graph
                or any(type(item) is not tuple or len(item) != 3
                       or not all(type(value) is tuple for value in item[1:])
                       for item in self.task_graph)):
            raise ProfileConfigurationError("execution_task_graph_invalid")
        if any(type(item[0]) is not str for item in self.task_graph):
            raise ProfileConfigurationError("execution_task_graph_invalid")
        task_ids = {item[0] for item in self.task_graph}
        if len(task_ids) != len(self.task_graph):
            raise ProfileConfigurationError("execution_task_graph_invalid")
        for job_id, dependencies, task_scope in self.task_graph:
            _identifier(job_id, "execution_job_id")
            if (any(type(dep) is not str for dep in dependencies)
                    or len(set(dependencies)) != len(dependencies)
                    or any(type(dep) is not str or dep not in task_ids for dep in dependencies)
                    or any(type(scope) is not str for scope in task_scope)
                    or len(set(task_scope)) != len(task_scope)
                    or any(scope not in self.authorized_scope for scope in task_scope)):
                raise ProfileConfigurationError("execution_task_binding_invalid")
        if (type(self.max_attempts_per_job) is not int or self.max_attempts_per_job < 1
                or type(self.max_duration_seconds) not in (int, float)
                or not math.isfinite(self.max_duration_seconds) or self.max_duration_seconds <= 0
                or type(self.max_concurrency) is not int or self.max_concurrency != 1):
            raise ProfileConfigurationError("execution_operational_limits_invalid")
        if self.financial_budget_mode not in {"hard_cap_required", "estimated_cost_accepted"}:
            raise ProfileConfigurationError("execution_financial_policy_invalid")
        if (type(self.strategies_by_job) is not tuple
                or any(type(item) is not tuple or len(item) != 2
                       or type(item[1]) is not tuple or not item[1]
                       or any(type(strategy) is not str
                              or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", strategy)
                              for strategy in item[1])
                       for item in self.strategies_by_job)
                or any(type(item[0]) is not str for item in self.strategies_by_job)
                or {item[0] for item in self.strategies_by_job} != task_ids):
            raise ProfileConfigurationError("execution_strategies_invalid")
        if (type(self.task_limits_by_job) is not tuple
                or any(type(item) is not tuple or len(item) != 4
                       or type(item[1]) is not int or item[1] < 1
                       or type(item[2]) not in (int, float) or not math.isfinite(item[2])
                       or item[2] <= 0 or type(item[3]) not in (int, float)
                       or not math.isfinite(item[3]) or item[3] < 0
                       for item in self.task_limits_by_job)
                or any(type(item[0]) is not str for item in self.task_limits_by_job)
                or {item[0] for item in self.task_limits_by_job} != task_ids):
            raise ProfileConfigurationError("execution_task_limits_invalid")

    def to_payload(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "plan_revision_id": self.plan_revision_id,
            "plan_content_digest": self.plan_content_digest,
            "objective_digest": self.objective_digest,
            "workspace_path": self.workspace_path,
            "workspace_identity_digest": self.workspace_identity_digest,
            "authorized_scope": list(self.authorized_scope),
            "task_graph": [[job, list(deps), list(scope)]
                           for job, deps, scope in self.task_graph],
            "executor_id": self.executor_id, "executor_version": self.executor_version,
            "max_attempts_per_job": self.max_attempts_per_job,
            "max_duration_seconds": self.max_duration_seconds,
            "max_concurrency": self.max_concurrency,
            "financial_budget_mode": self.financial_budget_mode,
            "strategies_by_job": [[job, list(strategies)]
                                   for job, strategies in self.strategies_by_job],
            "task_limits_by_job": [list(item) for item in self.task_limits_by_job],
        }

    @classmethod
    def from_payload(cls, value: dict) -> "ExecutionPlanConsentBinding":
        expected = {"cycle_id", "plan_revision_id", "plan_content_digest", "objective_digest", "workspace_path",
                    "workspace_identity_digest", "authorized_scope", "task_graph",
                    "executor_id", "executor_version", "max_attempts_per_job",
                    "max_duration_seconds", "max_concurrency", "financial_budget_mode",
                    "strategies_by_job", "task_limits_by_job"}
        if type(value) is not dict or set(value) != expected:
            raise ProfileConfigurationError("execution_plan_binding_fields_invalid")
        try:
            return cls(
                value["cycle_id"], value["plan_revision_id"], value["plan_content_digest"],
                value["objective_digest"],
                value["workspace_path"],
                value["workspace_identity_digest"], tuple(value["authorized_scope"]),
                tuple((item[0], tuple(item[1]), tuple(item[2])) for item in value["task_graph"]),
                value["executor_id"], value["executor_version"],
                value["max_attempts_per_job"], value["max_duration_seconds"],
                value["max_concurrency"], value["financial_budget_mode"],
                tuple((item[0], tuple(item[1])) for item in value["strategies_by_job"]),
                tuple(tuple(item) for item in value["task_limits_by_job"]),
            )
        except (KeyError, TypeError, IndexError) as exc:
            raise ProfileConfigurationError("execution_plan_binding_invalid") from exc


@dataclass(frozen=True)
class LocalExecutionConsentRequest(LocalConsentRequest):
    """Distinct consent request that covers execution of the complete plan."""

    execution_binding: ExecutionPlanConsentBinding

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.execution_binding, ExecutionPlanConsentBinding):
            raise ProfileConfigurationError("execution_plan_binding_required")
        operations = tuple(sorted("job:" + item[0]
                                  for item in self.execution_binding.task_graph))
        if (tuple(sorted(self.allowed_operations)) != operations
                or len(operations) > self.limits.max_jobs
                or self.execution_binding.max_duration_seconds > self.limits.timeout_seconds
                or self.execution_binding.cycle_id == ""):
            raise ProfileConfigurationError("execution_consent_plan_binding_mismatch")
        if (self.execution_binding.financial_budget_mode == "estimated_cost_accepted"
                and self.limits.estimated_budget <= 0):
            raise ProfileConfigurationError("explicit_execution_budget_required")

    @property
    def request_digest(self) -> str:
        payload = {"consent_request_digest": super().request_digest,
                   "execution_binding": self.execution_binding.to_payload()}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LocalConsentReceipt:
    request_digest: str
    decision: ConsentDecision
    operator_identity: str
    explicit_action: bool
    recorded_at: str
    expires_at: str

    def __post_init__(self) -> None:
        _digest(self.request_digest, "request_digest")
        if not isinstance(self.decision, ConsentDecision):
            raise ProfileConfigurationError("consent_decision_invalid")
        _identifier(self.operator_identity, "operator_identity")
        if type(self.explicit_action) is not bool:
            raise ProfileConfigurationError("explicit_action_required")
        recorded = _timestamp(self.recorded_at, "recorded_at")
        expires = _timestamp(self.expires_at, "expires_at")
        if expires <= recorded:
            raise ProfileConfigurationError("consent_expiry_invalid")


@dataclass(frozen=True)
class LocalConsentCheck:
    decision: ConsentDecision | None
    request_digest_matches: bool
    operator_identity_matches: bool
    explicit_action_present: bool
    within_validity: bool
    authenticated: bool = False
    authorizes_execution: bool = False
    reason: str = "local_consent_interface_not_implemented_or_authenticated"


def inspect_consent_receipt(request: LocalConsentRequest,
                            receipt: LocalConsentReceipt,
                            *, now: datetime | None = None) -> LocalConsentCheck:
    """Inspect record shape/bindings only; caller identity is not authenticated."""
    if not isinstance(request, LocalConsentRequest) or not isinstance(receipt, LocalConsentReceipt):
        raise ProfileConfigurationError("typed_local_consent_contracts_required")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ProfileConfigurationError("aware_clock_required")
    current = instant.astimezone(timezone.utc)
    recorded = _timestamp(receipt.recorded_at, "recorded_at")
    receipt_expiry = _timestamp(receipt.expires_at, "receipt_expiry")
    request_expiry = _timestamp(request.expires_at, "request_expiry")
    valid = (recorded <= current < receipt_expiry and current < request_expiry
             and receipt_expiry <= request_expiry)
    return LocalConsentCheck(
        decision=receipt.decision,
        request_digest_matches=receipt.request_digest == request.request_digest,
        operator_identity_matches=receipt.operator_identity == request.operator_identity,
        explicit_action_present=receipt.explicit_action,
        within_validity=valid,
    )


class LocalConsentInterface(Protocol):
    """Interface for a future operator UI; implementations are not authority by type."""

    def request_consent(self, request: LocalConsentRequest) -> LocalConsentReceipt | None: ...


class UnavailableLocalConsent:
    """Default until a trusted, explicit local consent UI is implemented."""

    def request_consent(self, request: LocalConsentRequest) -> LocalConsentReceipt | None:
        return None
