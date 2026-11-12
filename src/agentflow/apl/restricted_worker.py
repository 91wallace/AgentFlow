"""Fail-closed contracts for a future OS-confined semantic inference worker.

This module deliberately does not create a process.  Job Objects and the
existing execution runtime provide process lifetime controls, not a complete
security boundary, so a real worker backend is unavailable until a platform
implementation can attest and test every required control.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
import sys

from .replanning import proposal_from_mapping


class IsolationControl(StrEnum):
    PROCESS_IDENTITY = "process_identity"
    WORKSPACE_DENIED = "workspace_denied"
    EXECUTOR_CREDENTIALS_DENIED = "executor_credentials_denied"
    NETWORK_DENIED = "network_denied"
    SUBPROCESS_DENIED = "subprocess_denied"
    EXTENSIONS_DISABLED = "extensions_disabled"
    RESOURCE_LIMITS = "resource_limits"
    DEADLINE_ENFORCED = "deadline_enforced"
    TREE_TERMINATION = "tree_termination"
    PRIVATE_STORAGE = "private_storage"
    OUTPUT_LIMIT = "output_limit"


REQUIRED_ISOLATION_CONTROLS = frozenset(IsolationControl)
MAX_WORKER_INPUT_BYTES = 1024 * 1024
MAX_WORKER_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_WORKER_TIMEOUT_SECONDS = 300
MAX_WORKER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024


class WorkerStatus(StrEnum):
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class TerminationStatus(StrEnum):
    TERMINATED = "terminated"
    NOT_RUNNING = "not_running"
    UNKNOWN = "unknown"


def _strict_json_object(raw: str, *, limit: int, label: str) -> dict:
    if not isinstance(raw, str):
        raise ValueError(f"{label} is missing or exceeds its byte limit")
    try:
        encoded_size = len(raw.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8 text") from exc
    if encoded_size > limit:
        raise ValueError(f"{label} is missing or exceeds its byte limit")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"{label} contains a non-standard JSON constant: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, UnicodeEncodeError) as exc:
        raise ValueError(f"{label} is not valid bounded JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


@dataclass(frozen=True)
class RestrictedWorkerSpec:
    """Bounded structured request; deliberately contains no executable/path."""

    request_id: str
    input_json: str
    timeout_seconds: int = 30
    memory_limit_bytes: int = 512 * 1024 * 1024
    output_limit_bytes: int = 256 * 1024
    schema_version: int = 1

    def __post_init__(self):
        if not isinstance(self.request_id, str) or not self.request_id.strip() or len(self.request_id) > 128:
            raise ValueError("worker request_id must be a non-empty bounded string")
        _strict_json_object(self.input_json, limit=MAX_WORKER_INPUT_BYTES, label="worker input")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= MAX_WORKER_TIMEOUT_SECONDS:
            raise ValueError("worker timeout is outside the supported limit")
        if type(self.memory_limit_bytes) is not int or not 16 * 1024 * 1024 <= self.memory_limit_bytes <= MAX_WORKER_MEMORY_BYTES:
            raise ValueError("worker memory limit is outside the supported limit")
        if type(self.output_limit_bytes) is not int or not 1 <= self.output_limit_bytes <= MAX_WORKER_OUTPUT_BYTES:
            raise ValueError("worker output limit is outside the supported limit")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported worker request schema")


@dataclass(frozen=True)
class IsolationPolicy:
    """Immutable minimum policy; callers cannot relax required controls."""

    controls: frozenset[IsolationControl] = REQUIRED_ISOLATION_CONTROLS
    network_allowed: bool = False
    workspace_access: bool = False
    executor_credentials_allowed: bool = False
    subprocesses_allowed: bool = False
    extensions_allowed: bool = False

    def __post_init__(self):
        try:
            controls = frozenset(IsolationControl(item) for item in self.controls)
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown isolation control") from exc
        if controls != REQUIRED_ISOLATION_CONTROLS:
            raise ValueError("mandatory isolation controls cannot be removed")
        object.__setattr__(self, "controls", controls)
        for name in ("network_allowed", "workspace_access", "executor_credentials_allowed",
                     "subprocesses_allowed", "extensions_allowed"):
            if getattr(self, name) is not False:
                raise ValueError(f"restricted worker policy forbids {name}")


@dataclass(frozen=True)
class IsolationEvidence:
    control: IsolationControl
    enforced: bool
    verified: bool
    mechanism: str
    observation: str

    def __post_init__(self):
        try:
            object.__setattr__(self, "control", IsolationControl(self.control))
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown isolation evidence control") from exc
        if type(self.enforced) is not bool or type(self.verified) is not bool:
            raise ValueError("isolation evidence flags must be booleans")
        if not isinstance(self.mechanism, str) or not isinstance(self.observation, str):
            raise ValueError("isolation evidence text fields must be strings")
        if self.verified and (not self.enforced or not self.mechanism.strip() or not self.observation.strip()):
            raise ValueError("verified isolation evidence must describe an enforced control")


@dataclass(frozen=True)
class CapabilityProbe:
    """Observed platform capability report; a report is not itself an attestation."""

    platform: str
    backend_id: str
    evidence: tuple[IsolationEvidence, ...]
    probe_id: str = ""

    def __post_init__(self):
        if not isinstance(self.platform, str) or not self.platform:
            raise ValueError("probe platform is required")
        if not isinstance(self.backend_id, str) or not self.backend_id:
            raise ValueError("probe backend_id is required")
        if not isinstance(self.probe_id, str):
            raise ValueError("probe_id must be a string")
        evidence = tuple(self.evidence)
        controls = [item.control for item in evidence if isinstance(item, IsolationEvidence)]
        if len(controls) != len(evidence) or len(controls) != len(set(controls)):
            raise ValueError("probe evidence must contain unique typed controls")
        object.__setattr__(self, "evidence", evidence)

    def missing_controls(self, policy: IsolationPolicy) -> tuple[IsolationControl, ...]:
        applied = {item.control for item in self.evidence if item.enforced and item.verified}
        return tuple(sorted(policy.controls - applied, key=lambda item: item.value))


@dataclass(frozen=True)
class WorkerLaunchResult:
    status: WorkerStatus
    request_id: str
    worker_id: str = ""
    output_json: str = ""
    probe: CapabilityProbe | None = None
    reason: str = ""

    def __post_init__(self):
        try:
            object.__setattr__(self, "status", WorkerStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown worker launch status") from exc
        if not isinstance(self.request_id, str) or not isinstance(self.worker_id, str):
            raise ValueError("worker result identifiers must be strings")
        if not self.request_id or not isinstance(self.output_json, str) or not isinstance(self.reason, str):
            raise ValueError("worker result fields are invalid")
        if self.probe is not None and not isinstance(self.probe, CapabilityProbe):
            raise ValueError("worker result probe must be a CapabilityProbe")
        if self.status is WorkerStatus.BLOCKED and (self.worker_id or self.output_json):
            raise ValueError("blocked launch cannot contain a worker or output")
        if self.status is WorkerStatus.COMPLETED:
            _strict_json_object(self.output_json, limit=MAX_WORKER_OUTPUT_BYTES, label="worker result")
        elif self.output_json:
            raise ValueError("non-completed worker cannot contain result output")


@dataclass(frozen=True)
class WorkerTerminationResult:
    status: TerminationStatus
    worker_id: str
    descendants_confirmed: bool
    reason: str = ""

    def __post_init__(self):
        try:
            object.__setattr__(self, "status", TerminationStatus(self.status))
        except (TypeError, ValueError) as exc:
            raise ValueError("unknown worker termination status") from exc
        if not isinstance(self.worker_id, str) or type(self.descendants_confirmed) is not bool:
            raise ValueError("invalid worker termination result")
        if not isinstance(self.reason, str):
            raise ValueError("termination reason must be a string")
        if self.status is TerminationStatus.TERMINATED and not self.descendants_confirmed:
            raise ValueError("termination requires confirmed descendant cleanup")


class RestrictedWorkerUnavailable(RuntimeError):
    pass


class RestrictedInferenceHarness:
    """Safe APL seam that refuses to spawn until a verified OS backend exists.

    No caller-supplied probe, executable, environment, or launch callback is
    accepted here: requested settings or fabricated evidence must not enable
    execution. Adding a platform backend requires an explicit reviewed code
    change and OS-level adversarial tests.
    """

    def __init__(self, policy: IsolationPolicy | None = None):
        self.policy = policy or IsolationPolicy()

    def probe(self) -> CapabilityProbe:
        reason = "no platform backend enforces and verifies the mandatory isolation controls"
        evidence = tuple(IsolationEvidence(control, False, False, "unavailable", reason)
                         for control in sorted(REQUIRED_ISOLATION_CONTROLS, key=lambda item: item.value))
        return CapabilityProbe(sys.platform, "unavailable", evidence, "static-fail-closed-v1")

    def launch(self, spec: RestrictedWorkerSpec) -> WorkerLaunchResult:
        if not isinstance(spec, RestrictedWorkerSpec):
            raise TypeError("RestrictedWorkerSpec is required")
        probe = self.probe()
        missing = probe.missing_controls(self.policy)
        if probe.backend_id == "unavailable" or missing:
            names = ", ".join(item.value for item in missing)
            return WorkerLaunchResult(WorkerStatus.BLOCKED, spec.request_id, probe=probe,
                                      reason=f"restricted worker launch denied; missing: {names}")
        # A future backend must be a reviewed platform implementation. This
        # generic harness intentionally has no process creation path.
        raise RestrictedWorkerUnavailable("no reviewed restricted worker launcher is installed")

    def terminate(self, worker_id: str) -> WorkerTerminationResult:
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id is required")
        return WorkerTerminationResult(TerminationStatus.NOT_RUNNING, worker_id, True,
                                       "no worker process has been launched by this harness")


def decode_worker_proposal(raw: str, *, max_bytes: int = MAX_WORKER_OUTPUT_BYTES):
    """Decode a bounded proposal response; APL validation is still required."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_WORKER_OUTPUT_BYTES:
        raise ValueError("proposal output limit is outside the supported range")
    envelope = _strict_json_object(raw, limit=max_bytes, label="worker output")
    if set(envelope) != {"schema_version", "proposal"} or type(envelope["schema_version"]) is not int:
        raise ValueError("worker output has missing or unsupported envelope fields")
    if envelope["schema_version"] != 1 or not isinstance(envelope["proposal"], dict):
        raise ValueError("worker output schema is unsupported")
    return proposal_from_mapping(envelope["proposal"])
