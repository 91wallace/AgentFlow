from pathlib import Path

from agentflow.git.contracts import (Checkpoint, CheckpointRequest, GitProvider,
                                     Workspace, WorkspaceRequest)


class FakeGitProvider:
    def inspect(self, repository: Path) -> tuple[Path, str]:
        return repository.resolve(), "a" * 40

    def create_workspace(self, request: WorkspaceRequest, workspace_id: str) -> Workspace:
        return Workspace(workspace_id, request.project_id, request.task_id,
                         request.definition_revision, request.repository / workspace_id,
                         request.repository, "agentflow/ws/" + workspace_id[3:],
                         request.base_oid, "active", True)

    def create_checkpoint(self, workspace: Workspace, request: CheckpointRequest) -> Checkpoint:
        return Checkpoint("cp-1", workspace.workspace_id, request.operation_id,
                          "b" * 40, workspace.base_oid, "c" * 40, workspace.branch,
                          request.paths, "sha256:paths", True, request.actor_name, request.actor_email)

    def verify_checkpoint(self, workspace: Workspace, checkpoint: Checkpoint) -> bool:
        return checkpoint.workspace_id == workspace.workspace_id and checkpoint.verified

    def inspect_operation(self, workspace: Workspace, operation_id: str) -> dict[str, str] | None:
        return {"status": "verified"} if operation_id else None


def test_git_provider_contract_is_replaceable_without_subprocesses(tmp_path):
    provider: GitProvider = FakeGitProvider()
    request = WorkspaceRequest("demo", "task", 1, "sha256:def", "op-ws",
                               tmp_path, "a" * 40, ("docs/ü [x].md",))
    workspace = provider.create_workspace(request, "ws-" + "1" * 32)
    checkpoint_request = CheckpointRequest(workspace.workspace_id, "op-cp",
                                           request.authorized_paths, "Reviewer", "r@example.test", "checkpoint")
    checkpoint = provider.create_checkpoint(workspace, checkpoint_request)
    assert provider.verify_checkpoint(workspace, checkpoint)
    assert provider.inspect_operation(workspace, "op-cp")["status"] == "verified"
