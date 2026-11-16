"""Agent-independent execution contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
import re
from typing import Callable, Protocol

from .selection import ModelSelectionRequest, ModelSelectionObservation


class ExecutionError(ValueError):
    """An execution request or attempt was rejected."""


@dataclass(frozen=True)
class ExecutionPolicy:
    """AgentFlow-owned process controls, excluding agent-native sandbox modes.

    A CLI adapter must inherit the executor's own sandbox configuration. This
    policy deliberately cannot request or select such a mode.
    """

    timeout_seconds: int = 1800
    idle_seconds: int | None = None
    log_limit_bytes: int = 10 * 1024 * 1024
    approval: str | None = None

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 7200:
            raise ExecutionError("timeout deve estar entre 1 e 7200 segundos")
        if self.idle_seconds is not None and (type(self.idle_seconds) is not int or
                                              not 60 <= self.idle_seconds <= self.timeout_seconds):
            raise ExecutionError("timeout de inatividade inválido")
        if type(self.log_limit_bytes) is not int or not 1 <= self.log_limit_bytes <= 50 * 1024 * 1024:
            raise ExecutionError("limite de log inválido")
        if self.approval is not None and self.approval not in ("never", "on-request"):
            raise ExecutionError("política de aprovação inválida")


@dataclass(frozen=True)
class ExecutionRequest:
    project_id: str
    task_id: str
    definition_revision: int
    definition_digest: str
    operation_id: str
    workspace: Path
    instruction: str
    policy: ExecutionPolicy
    acceptance_criteria: tuple[tuple[str, str], ...] = ()
    workspace_id: str = ""
    model_selection: ModelSelectionRequest | None = None


@dataclass(frozen=True)
class ProcessSpec:
    """Concrete process request; native sandbox observation is audit-only.

    ``effective_sandbox`` is a legacy persistence field and must remain
    ``"unknown"`` unless the native executor can attest its actual setting.
    It is never a configuration input.
    """

    executable: Path
    arguments: tuple[str, ...]
    cwd: Path
    stdin: str
    environment: tuple[tuple[str, str], ...]
    adapter_name: str
    adapter_version: str
    effective_sandbox: str
    effective_approval: str
    capabilities: tuple[str, ...] = ()
    adapter_api_version: str = "1"
    provider_id: str = "unknown"
    model_id: str = "unknown"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: str
    group_id: str
    parent_pid: int
    attempt_id: str
    token: str
    parent_started_at: str = ""


@dataclass(frozen=True)
class ProcessResult:
    status: str
    exit_code: int | None
    stdout_path: Path
    stderr_path: Path
    stdout_digest: str
    stderr_digest: str
    stdout_bytes: int
    stderr_bytes: int
    reason: str
    request_metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AgentResult:
    process_status: str
    summary: str
    declared_artifacts: tuple[str, ...] = ()
    effective_provider_id: str = "unknown"
    effective_model_id: str = "unknown"
    selection_status: str = "unknown"


class AgentAdapter(Protocol):
    def prepare(self, request: ExecutionRequest) -> ProcessSpec: ...
    def interpret(self, result: ProcessResult) -> AgentResult: ...


class NormalizedAgentState(str, Enum):
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


def normalize_execution_state(status: str) -> NormalizedAgentState:
    """Map AgentFlow process/store states without treating ambiguity as failure/success."""
    if type(status) is not str:
        raise ExecutionError("estado de execução inválido")
    mapping = {
        "created": NormalizedAgentState.CREATED,
        "prepared": NormalizedAgentState.READY,
        "pending": NormalizedAgentState.READY,
        "running": NormalizedAgentState.RUNNING,
        "process_succeeded": NormalizedAgentState.SUCCEEDED,
        "succeeded": NormalizedAgentState.SUCCEEDED,
        "process_failed": NormalizedAgentState.FAILED,
        "failed": NormalizedAgentState.FAILED,
        "cancelled": NormalizedAgentState.CANCELLED,
        "timed_out": NormalizedAgentState.UNKNOWN,
        "interrupted": NormalizedAgentState.UNKNOWN,
        "unknown": NormalizedAgentState.UNKNOWN,
    }
    return mapping.get(status, NormalizedAgentState.UNKNOWN)


@dataclass(frozen=True)
class AgentAvailability:
    adapter_id: str
    status: str
    executable: str
    version: str | None
    source: str
    reason_code: str
    checked_at: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", self.adapter_id):
            raise ExecutionError("adapter_id inválido")
        if self.status not in {"available", "unavailable", "unknown"}:
            raise ExecutionError("estado de disponibilidade inválido")
        if not self.executable or "\x00" in self.executable or not self.source:
            raise ExecutionError("origem de disponibilidade inválida")
        if self.version is not None and (type(self.version) is not str or not self.version):
            raise ExecutionError("versão de disponibilidade inválida")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.reason_code):
            raise ExecutionError("reason_code de disponibilidade inválido")
        try:
            checked = datetime.fromisoformat(self.checked_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ExecutionError("checked_at inválido") from exc
        if checked.tzinfo is None:
            raise ExecutionError("checked_at deve conter timezone")


@dataclass(frozen=True)
class AgentCapabilities:
    """Distinguishes agent-session features from AgentFlow process services."""

    task_initialization: str
    structured_result_format: tuple[str, ...]
    agent_session_tracking: bool
    agent_session_cancellation: bool
    agent_session_recovery: bool
    process_monitoring_owner: str
    process_cancellation_owner: str
    process_recovery_owner: str
    evidence_collection_owner: str

    def __post_init__(self) -> None:
        if self.task_initialization not in {"supported", "unknown", "unavailable"}:
            raise ExecutionError("capability de inicialização inválida")
        if (type(self.structured_result_format) is not tuple
                or len(set(self.structured_result_format)) != len(self.structured_result_format)
                or any(type(value) is not str or not re.fullmatch(r"[a-z0-9._-]{1,32}", value)
                       for value in self.structured_result_format)):
            raise ExecutionError("formatos de resultado inválidos")
        for name in ("agent_session_tracking", "agent_session_cancellation", "agent_session_recovery"):
            if type(getattr(self, name)) is not bool:
                raise ExecutionError(f"{name} precisa ser booleano")
        for name in ("process_monitoring_owner", "process_cancellation_owner",
                     "process_recovery_owner", "evidence_collection_owner"):
            value = getattr(self, name)
            if type(value) is not str or not re.fullmatch(r"[a-z][a-z0-9._-]{0,127}", value):
                raise ExecutionError(f"{name} inválido")


class LifecycleAgentAdapter(AgentAdapter, Protocol):
    """Versioned common adapter surface; process lifecycle remains AgentFlow-owned."""

    adapter_id: str
    api_version: str

    def discover(self) -> AgentAvailability: ...
    def capabilities(self) -> AgentCapabilities: ...
    def prepare_task(self, request: ExecutionRequest) -> ProcessSpec: ...
    def collect_result(self, result: ProcessResult) -> AgentResult: ...
    def normalize_state(self, status: str) -> NormalizedAgentState: ...


class ProcessRuntime(Protocol):
    def run(self, spec: ProcessSpec, attempt_id: str, policy: ExecutionPolicy,
            on_started: "StartedCallback", cancel: "Cancellation",
            before_spawn: "Callable[[], None] | None" = None) -> ProcessResult: ...


class StartedCallback(Protocol):
    def __call__(self, identity: ProcessIdentity) -> None: ...


class Cancellation(Protocol):
    def is_set(self) -> bool: ...
