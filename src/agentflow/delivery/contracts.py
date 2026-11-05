"""Build provenance and explicitly authorized delivery contracts."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from agentflow.validation.contracts import BuildStatus


class DeliveryStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ArtifactStatus(StrEnum):
    VERIFIED = "verified"
    EXPIRED = "expired"
    CORRUPT = "corrupt"


@dataclass(frozen=True)
class ArtifactReference:
    artifact_id: str
    sha256: str
    size_bytes: int
    source_commit_oid: str
    source_tree_oid: str
    snapshot_digest: str


@dataclass(frozen=True)
class BuildRequest:
    operation_id: str
    project_id: str
    task_id: str
    definition_revision: int
    checkpoint_id: str
    source_commit_oid: str
    source_tree_oid: str
    snapshot_digest: str
    builder_id: str
    builder_version: str
    builder_digest: str
    configuration_digest: str
    dependency_digest: str
    output_directory_id: str
    source_run_id: str = ""
    snapshot_path: Path | None = None
    output_path: Path | None = None


@dataclass(frozen=True)
class BuildResult:
    operation_id: str
    status: BuildStatus
    artifact: ArtifactReference | None
    builder_id: str
    builder_version: str
    builder_digest: str
    configuration_digest: str
    dependency_digest: str
    reason: str = ""


@dataclass(frozen=True)
class DeliveryAuthorization:
    authorization_id: str
    operation_id: str
    destination_id: str
    artifact_digest: str
    authorized_by: str
    authorized_at: str


@dataclass(frozen=True)
class DeliveryRequest:
    operation_id: str
    project_id: str
    artifact: ArtifactReference
    destination_id: str
    authorization: DeliveryAuthorization
    credential_reference_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.authorization.operation_id != self.operation_id
                or self.authorization.destination_id != self.destination_id
                or self.authorization.artifact_digest != self.artifact.sha256):
            raise ValueError("delivery authorization does not match operation, destination and artifact")


@dataclass(frozen=True)
class DeliveryResult:
    operation_id: str
    status: DeliveryStatus
    destination_id: str
    artifact_digest: str
    provider_id: str
    provider_version: str
    receipt_digest: str = ""
    reason: str = ""


class ArtifactBuilder(Protocol):
    @property
    def builder_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def digest(self) -> str: ...

    def build(self, request: BuildRequest) -> BuildResult: ...


class DeliveryProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def deliver(self, request: DeliveryRequest) -> DeliveryResult: ...
