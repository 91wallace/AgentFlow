from dataclasses import replace
import pytest

from agentflow.core.git import GitCoordinator
from agentflow.git.contracts import (Checkpoint, CheckpointRequest, GitConflict, TreeEntry,
                                     Workspace, WorkspaceRequest)
from agentflow.git.provider import GitLocalProvider
from agentflow.state.validation import ValidationStateStore
from agentflow.validation import SnapshotError, SnapshotManager
from test_git_workspace import git, make_plan, make_repo


def checkpointed_project(tmp_path):
    repository = make_repo(tmp_path / "repo")
    plan = make_plan(repository)
    database = tmp_path / "state.sqlite3"
    store = ValidationStateStore(database)
    store.import_plan(plan)
    provider = GitLocalProvider()
    root, _, base_oid = provider.inspect(repository)
    task = plan.tasks[0]
    workspace = GitCoordinator(store, provider).create_workspace(
        WorkspaceRequest(plan.specification.project_id, task.id, 1,
                         store.db.execute("SELECT definition_digest FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=1",
                                          (plan.specification.project_id, task.id)).fetchone()[0],
                         "workspace-create-1", root, base_oid, task.versioned_paths))
    (workspace.path / "answer ü [1].txt").write_text("checkpoint answer\n", encoding="utf-8")
    checkpoint = GitCoordinator(store, provider).create_checkpoint(
        CheckpointRequest(workspace.workspace_id, "checkpoint-1", task.versioned_paths,
                          "Test", "test@example.test", "test checkpoint"))
    return repository, store, provider, workspace, checkpoint


def test_snapshot_materializes_exact_verified_checkpoint_tree_and_preserves_checkout(tmp_path):
    repository, store, provider, workspace, checkpoint = checkpointed_project(tmp_path)
    try:
        base_before = provider.inspect(repository)[2]
        original = (repository / "tracked.txt").read_text(encoding="utf-8")
        parent = tmp_path / "snapshots"
        parent.mkdir()
        manager = SnapshotManager(provider)
        snapshot = manager.materialize(workspace, checkpoint, parent)
        assert manager.verify(snapshot)
        assert snapshot.commit_oid == checkpoint.commit_oid
        assert snapshot.tree_oid == checkpoint.tree_oid
        assert {entry.path for entry in snapshot.entries} == {
            "tracked.txt", "answer ü [1].txt"}
        assert (snapshot.path / "answer ü [1].txt").read_text(encoding="utf-8") == "checkpoint answer\n"
        assert (snapshot.path / "tracked.txt").read_text(encoding="utf-8") == original
        assert provider.inspect(repository)[2] == base_before
        assert (repository / "tracked.txt").read_text(encoding="utf-8") == original
    finally:
        provider.close()
        store.close()


def test_snapshot_detects_content_and_unexpected_file_mutations(tmp_path):
    _, store, provider, workspace, checkpoint = checkpointed_project(tmp_path)
    try:
        parent = tmp_path / "snapshots"
        parent.mkdir()
        manager = SnapshotManager(provider)
        snapshot = manager.materialize(workspace, checkpoint, parent)
        changed = snapshot.path / "answer ü [1].txt"
        changed.chmod(0o644)
        changed.write_text("tampered", encoding="utf-8")
        assert not manager.verify(snapshot)
        changed.unlink()
        (snapshot.path / "unexpected.txt").write_text("extra", encoding="utf-8")
        assert not manager.verify(snapshot)
    finally:
        provider.close()
        store.close()


def test_snapshot_rejects_unverified_or_stale_checkpoint(tmp_path):
    _, store, provider, workspace, checkpoint = checkpointed_project(tmp_path)
    try:
        parent = tmp_path / "snapshots"
        parent.mkdir()
        manager = SnapshotManager(provider)
        with pytest.raises(SnapshotError, match="identidade verificada"):
            manager.materialize(workspace, replace(checkpoint, verified=False), parent)
        (workspace.path / "answer ü [1].txt").write_text("changed after checkpoint", encoding="utf-8")
        with pytest.raises(SnapshotError, match="identidade verificada"):
            manager.materialize(workspace, checkpoint, parent)
        assert list(parent.iterdir()) == []
    finally:
        provider.close()
        store.close()


class UnsupportedTreeProvider:
    def __init__(self, entry):
        self.entry = entry

    def verify_checkpoint(self, workspace, checkpoint):
        return True

    def tree_entries(self, repository, tree_oid):
        return (self.entry,)

    def write_blob(self, repository, object_id, destination, max_bytes):
        raise AssertionError("unsupported tree entry must be rejected before reading its content")


@pytest.mark.parametrize("entry", [
    TreeEntry("link", 0o120000, "a" * 40, "blob"),
    TreeEntry("submodule", 0o160000, "b" * 40, "commit"),
    TreeEntry("../escape", 0o100644, "c" * 40, "blob"),
    TreeEntry(".agentflow/state.sqlite3", 0o100644, "d" * 40, "blob"),
])
def test_snapshot_rejects_symlinks_submodules_escape_and_operational_data(tmp_path, entry):
    parent = tmp_path / "snapshots"
    parent.mkdir()
    workspace = Workspace("ws-" + "1" * 32, "p", "t", 1, tmp_path,
                          tmp_path, "branch", "a" * 40, "active",
                          repository_root=tmp_path)
    checkpoint = Checkpoint("cp-1", workspace.workspace_id, "op", "b" * 40,
                            "a" * 40, "c" * 40, "branch", (), "sha256:" + "0" * 64,
                            True, "test", "test@example.test")
    with pytest.raises(SnapshotError):
        SnapshotManager(UnsupportedTreeProvider(entry)).materialize(workspace, checkpoint, parent)
    assert list(parent.iterdir()) == []


def test_snapshot_quota_fails_and_cleans_its_own_temporary(tmp_path):
    _, store, provider, workspace, checkpoint = checkpointed_project(tmp_path)
    try:
        parent = tmp_path / "snapshots"
        parent.mkdir()
        manager = SnapshotManager(provider, max_snapshot_bytes=1)
        with pytest.raises(GitConflict, match="quota"):
            manager.materialize(workspace, checkpoint, parent)
        assert list(parent.iterdir()) == []
    finally:
        provider.close()
        store.close()
