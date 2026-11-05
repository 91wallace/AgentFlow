"""Local Git contracts and adapters; no remote operations are exposed."""

from .contracts import (Checkpoint, CheckpointRequest, GitConflict, GitError,
                        GitProvider, TreeEntry, Workspace, WorkspaceRequest)
from .digests import operation_digest, pathset_digest
from .provider import GitLocalProvider

__all__ = ["Checkpoint", "CheckpointRequest", "GitConflict", "GitError", "GitProvider",
           "GitLocalProvider", "TreeEntry", "Workspace", "WorkspaceRequest", "operation_digest", "pathset_digest"]
