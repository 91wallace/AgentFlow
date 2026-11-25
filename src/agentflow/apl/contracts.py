"""Versioned contracts for the native Autonomous Planning Loop."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from agentflow.agents.contracts import Cancellation


class APLState(StrEnum):
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    EVALUATING = "EVALUATING"
    REPLANNING = "REPLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERING = "RECOVERING"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class APLLimits:
    max_jobs: int = 10
    max_iterations: int = 10
    max_seconds: float = 3600
    max_budget: float = 0
    no_progress_limit: int = 2

    def __post_init__(self):
        if self.max_jobs < 1 or self.max_iterations < 1 or self.max_seconds <= 0 or self.max_budget < 0 or self.no_progress_limit < 1:
            raise ValueError("APL limits must be positive; budget must be non-negative")


@dataclass(frozen=True)
class JobProposal:
    job_id: str
    instruction: str
    depends_on: tuple[str, ...] = ()
    required_scope: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str  # succeeded, failed, unknown, approval_required
    summary: str
    evidence: tuple[dict, ...] = ()
    cost: float = 0
    progress_key: str = ""
    handoff: dict | None = None


class ResultObserver(Protocol):
    def __call__(self, cycle_id: str, job: JobProposal, outcome: ExecutionOutcome, *,
                 remaining_seconds: float, cancellation: Cancellation, task_status: str) -> object: ...


@dataclass(frozen=True)
class Evaluation:
    complete: bool
    passed: bool
    progress: bool
    rationale: str = ""


@dataclass(frozen=True)
class CycleSnapshot:
    cycle_id: str
    objective: str
    authorized_scope: tuple[str, ...]
    state: APLState
    jobs: tuple[JobProposal, ...]
    outcomes: tuple[tuple[str, ExecutionOutcome], ...]
    iterations: int
    consumed_budget: float
    version: int = 1
    reason: str = ""
    revision: int = 0
    plan_digest: str = ""
    plan_revision_id: str = ""


class Planner(Protocol):
    def plan(self, objective: str, scope: tuple[str, ...], history: tuple[tuple[str, ExecutionOutcome], ...]) -> tuple[JobProposal, ...]: ...


class Executor(Protocol):
    def execute(self, cycle_id: str, job: JobProposal, authorized_scope: tuple[str, ...]) -> ExecutionOutcome: ...


class Evaluator(Protocol):
    def evaluate(self, objective: str, outcomes: tuple[tuple[str, ExecutionOutcome], ...]) -> Evaluation: ...


class Policy(Protocol):
    def authorize(self, job: JobProposal, authorized_scope: tuple[str, ...]) -> str: ...


class CycleStore(Protocol):
    def create(self, snapshot: CycleSnapshot) -> CycleSnapshot: ...
    def load(self, cycle_id: str) -> CycleSnapshot: ...
    def save(self, snapshot: CycleSnapshot, expected_iterations: int) -> CycleSnapshot: ...


@dataclass(frozen=True)
class RemainingLimits:
    jobs: int
    iterations: int
    seconds: float
    budget: float


@dataclass(frozen=True)
class EvidenceReference:
    evidence_id: int
    job_id: str
    criterion_id: str


@dataclass(frozen=True)
class ValidatedResult:
    job_id: str
    attempt_id: str
    status: str
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class ReplanContext:
    objective: str
    authorized_scope: tuple[str, ...]
    current_plan: tuple[JobProposal, ...]
    plan_revision_id: str
    completed_jobs: tuple[str, ...]
    validated_results: tuple[ValidatedResult, ...]
    verified_evidence: tuple[EvidenceReference, ...]
    blockers: tuple[str, ...]
    remaining_limits: RemainingLimits
    cycle_revision: int = 0
    previously_executed_jobs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskChange:
    action: str  # add or modify
    task: JobProposal


@dataclass(frozen=True)
class PlanProposal:
    proposal_id: str
    source_revision_id: str
    source_cycle_revision: int
    rationale: str
    changes: tuple[TaskChange, ...]
    completion_condition: str
    evidence_used: tuple[int, ...] = ()
    resolves_blockers: tuple[str, ...] = ()
    schema_version: int = 1


class PlannerAdapter(Protocol):
    def propose(self, context: ReplanContext) -> PlanProposal: ...
