"""Storage-independent state boundary."""

from dataclasses import dataclass
from typing import Protocol

from agentflow.planning.models import Plan


class StateError(ValueError):
    """Rejected state operation."""


class IntegrityError(StateError):
    """Stored state is incompatible or damaged."""


class ConflictError(StateError):
    """Operation ID, revision, or writer conflict."""


class TransitionError(StateError):
    """Invalid transition or unmet guard."""


@dataclass(frozen=True)
class TaskState:
    task_id: str
    status: str
    definition_revision: int
    revision: int
    attempt_count: int
    revalidation_required: bool


@dataclass(frozen=True)
class Transition:
    task_id: str
    destination: str
    operation_id: str
    expected_revision: int
    reason: str = ""
    owner: str = ""
    exception_author: str = ""
    exception_reason: str = ""


@dataclass(frozen=True)
class EvidenceInput:
    task_id: str
    criterion_id: str
    kind: str
    payload: str
    source: str
    operation_id: str
    declaration_id: int | None = None
    method: str = ""
    author: str = ""
    passed: bool = False


class StateRepository(Protocol):
    def import_plan(self, plan: Plan) -> None: ...
    def list_tasks(self) -> tuple[TaskState, ...]: ...
    def transition(self, command: Transition) -> TaskState: ...
    def add_evidence(self, item: EvidenceInput) -> int: ...
    def revalidate(self, task_id: str, operation_id: str, author: str, reason: str) -> TaskState: ...
    def recover(self) -> tuple[str, ...]: ...
    def reopen(self, plan: Plan) -> tuple[str, ...]: ...
