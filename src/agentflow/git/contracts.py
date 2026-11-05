"""Storage-independent contracts for local Git lifecycle operations."""

from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Protocol


class GitError(ValueError):
    """A Git operation failed or could not be verified."""


class GitConflict(GitError):
    """An operation, workspace, branch, or path conflicts with recorded state."""


@dataclass(frozen=True)
class WorkspaceRequest:
    project_id: str
    task_id: str
    definition_revision: int
    definition_digest: str
    operation_id: str
    repository: Path
    base_oid: str
    authorized_paths: tuple[str, ...]


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    project_id: str
    task_id: str
    definition_revision: int
    path: Path
    common_dir: Path
    branch: str
    base_oid: str
    status: str
    created: bool = False
    head_oid: str = ""
    repository_root: Path = Path()


@dataclass(frozen=True)
class CheckpointRequest:
    workspace_id: str
    operation_id: str
    paths: tuple[str, ...]
    actor_name: str
    actor_email: str
    message: str


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    workspace_id: str
    operation_id: str
    commit_oid: str
    parent_oid: str
    tree_oid: str
    branch: str
    paths: tuple[str, ...]
    pathset_digest: str
    verified: bool
    actor_name: str
    actor_email: str


class LockLease(Protocol):
    owner: dict[str, object] | None
    previous_owner: dict[str, object] | None

    def __enter__(self) -> "LockLease": ...
    def __exit__(self, *_: object) -> None: ...


class GitProvider(Protocol):
    def inspect(self, repository: Path) -> tuple[Path, Path, str]: ...
    def verify_workspace(self, workspace: Workspace) -> bool: ...
    def preflight(self, repository: Path, paths: tuple[str, ...] = ()) -> dict[str, object]: ...
    def lock(self, repository: Path, workspace_id: str, operation_id: str) -> ContextManager[LockLease]: ...
    def create_workspace(self, request: WorkspaceRequest, workspace_id: str) -> Workspace: ...
    def create_checkpoint(self, workspace: Workspace, request: CheckpointRequest) -> Checkpoint: ...
    def verify_checkpoint(self, workspace: Workspace, checkpoint: Checkpoint) -> bool: ...
    def verify_no_changes(self, workspace: Workspace) -> bool: ...
    def reconcile_operation(self, workspace: Workspace, kind: str, operation_id: str,
                            request_json: str) -> Workspace | Checkpoint | None: ...
