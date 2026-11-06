"""Agent-independent execution contracts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .selection import ModelSelectionRequest, ModelSelectionObservation


class ExecutionError(ValueError):
    """An execution request or attempt was rejected."""


@dataclass(frozen=True)
class ExecutionPolicy:
    timeout_seconds: int = 1800
    idle_seconds: int | None = None
    log_limit_bytes: int = 10 * 1024 * 1024
    sandbox: str | None = None
    approval: str | None = None

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 7200:
            raise ExecutionError("timeout deve estar entre 1 e 7200 segundos")
        if self.idle_seconds is not None and (type(self.idle_seconds) is not int or
                                              not 60 <= self.idle_seconds <= self.timeout_seconds):
            raise ExecutionError("timeout de inatividade inválido")
        if type(self.log_limit_bytes) is not int or not 1 <= self.log_limit_bytes <= 50 * 1024 * 1024:
            raise ExecutionError("limite de log inválido")
        if self.sandbox is not None and self.sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise ExecutionError("sandbox inválido")
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


class ProcessRuntime(Protocol):
    def run(self, spec: ProcessSpec, attempt_id: str, policy: ExecutionPolicy,
            on_started: "StartedCallback", cancel: "Cancellation") -> ProcessResult: ...


class StartedCallback(Protocol):
    def __call__(self, identity: ProcessIdentity) -> None: ...


class Cancellation(Protocol):
    def is_set(self) -> bool: ...
