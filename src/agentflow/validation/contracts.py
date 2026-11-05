"""Immutable validation contracts and replaceable runtime boundaries."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from agentflow.planning.models import GateDefinition as GateDefinition

class GateStatus(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    CANCELLED = "cancelled"
    NOT_RUN = "not_run"


class RunStatus(StrEnum):
    REQUESTED = "requested"
    PREFLIGHT = "preflight"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    CANCELLED = "cancelled"


class CapabilityState(StrEnum):
    ENFORCED = "enforced"
    SUPPORTED = "supported"
    UNAVAILABLE = "unavailable"


class RuntimeProfile(StrEnum):
    LOCAL_EXPLICIT = "local-explicit"
    DOCKER_ISOLATED = "docker-isolated"


class BuildStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    mode: int
    git_object_id: str
    size_bytes: int
    content_digest: str


@dataclass(frozen=True)
class ValidationSnapshot:
    snapshot_id: str
    checkpoint_id: str
    commit_oid: str
    tree_oid: str
    entries: tuple[SnapshotEntry, ...]
    manifest_json: str
    snapshot_digest: str
    path: Path
    size_bytes: int


@dataclass(frozen=True)
class RuntimeLimits:
    timeout_seconds: int = 600
    cpu_count: int = 2
    memory_bytes: int = 4 * 1024**3
    process_count: int = 64
    output_bytes: int = 8 * 1024**2
    workspace_bytes: int = 4 * 1024**3

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("timeout must be between 1 and 3600 seconds")
        if not 1 <= self.cpu_count <= 8:
            raise ValueError("CPU limit must be between 1 and 8")
        if not 1 <= self.memory_bytes <= 16 * 1024**3:
            raise ValueError("memory limit must be between 1 byte and 16 GiB")
        if not 1 <= self.process_count <= 256:
            raise ValueError("process limit must be between 1 and 256")
        if not 1 <= self.output_bytes <= 8 * 1024**2:
            raise ValueError("output limit must be between 1 byte and 8 MiB")
        if not 1 <= self.workspace_bytes <= 4 * 1024**3:
            raise ValueError("workspace limit must be between 1 byte and 4 GiB")


@dataclass(frozen=True)
class RuntimeCapability:
    name: str
    state: CapabilityState
    detail: str = ""
    effective_value: str = ""


@dataclass(frozen=True)
class RuntimeCapabilities:
    profile: RuntimeProfile
    runtime_id: str
    runtime_version: str
    capabilities: tuple[RuntimeCapability, ...]

    def __post_init__(self) -> None:
        names = [capability.name for capability in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("duplicate runtime capability")


@dataclass(frozen=True)
class CommandDefinition:
    command_id: str
    executable: str
    executable_digest: str
    arguments: tuple[str, ...]
    working_directory: str
    expected_version: str


@dataclass(frozen=True)
class ValidationRequest:
    operation_id: str
    validation_run_id: str
    project_id: str
    task_id: str
    definition_revision: int
    definition_digest: str
    checkpoint_id: str
    commit_oid: str
    tree_oid: str
    workspace_id: str
    gate_snapshot: str
    gate_snapshot_digest: str
    policy_snapshot: str
    policy_digest: str
    gate_ids: tuple[str, ...]
    runtime_profile: RuntimeProfile
    allow_unconfined_network: bool = False
    attempt_id: str = ""
    requested_by: str = "local-user"

    def __post_init__(self) -> None:
        required = (self.operation_id, self.validation_run_id, self.project_id,
                    self.task_id, self.definition_digest, self.checkpoint_id,
                    self.commit_oid, self.tree_oid, self.workspace_id,
                    self.gate_snapshot, self.gate_snapshot_digest,
                    self.policy_snapshot, self.policy_digest)
        if any(not item for item in required):
            raise ValueError("validation request is missing required identity or digest")
        if self.definition_revision < 1 or not self.gate_ids:
            raise ValueError("positive definition revision and at least one gate are required")
        if len(set(self.gate_ids)) != len(self.gate_ids):
            raise ValueError("duplicate selected gate")


@dataclass(frozen=True)
class GateResult:
    validation_run_id: str
    gate_id: str
    status: GateStatus
    task_revision: int
    checkpoint_id: str
    commit_oid: str
    tree_oid: str
    gate_definition_digest: str
    validator_id: str
    validator_version: str
    validator_digest: str
    snapshot_digest: str
    started_at: str = ""
    finished_at: str = ""
    exit_code: int | None = None
    reason: str = ""
    stdout_digest: str = ""
    stderr_digest: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    truncated: bool = False
    capabilities: tuple[RuntimeCapability, ...] = ()
    artifact_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationResult:
    validation_run_id: str
    operation_id: str
    status: RunStatus
    results: tuple[GateResult, ...]
    request_digest: str
    snapshot_digest: str
    reason: str = ""


@dataclass(frozen=True)
class RuntimeRequest:
    command: CommandDefinition
    arguments: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...]
    limits: RuntimeLimits
    output_directory: Path
    network_authorized: bool = False
    container_image: str = ""
    container_executable: str = ""
    snapshot_path: Path | None = None
    validation_run_id: str = ""
    gate_id: str = ""


@dataclass(frozen=True)
class RuntimeResult:
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    spawn_failed: bool
    process_identity: str
    stdout_path: Path
    stderr_path: Path
    stdout_digest: str
    stderr_digest: str
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool
    capabilities: RuntimeCapabilities
    reason: str = ""
    process_stopped: bool = False
    logs_sanitized: bool = True


class ValidationRuntime(Protocol):
    def inspect_capabilities(self, profile: RuntimeProfile) -> RuntimeCapabilities: ...

    def run(self, request: RuntimeRequest, cancellation: "Cancellation",
            on_spawn: Callable[[str], None],
            on_probe: Callable[[str], None]) -> RuntimeResult: ...


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


class GateValidator(Protocol):
    @property
    def validator_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def digest(self) -> str: ...

    def validate(self, result: RuntimeResult) -> tuple[bool, str]: ...


class ValidationRunner(Protocol):
    def run(self, request: ValidationRequest) -> ValidationResult: ...
