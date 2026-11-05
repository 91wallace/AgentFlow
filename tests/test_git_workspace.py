from pathlib import Path
import subprocess
import sys

import pytest

from agentflow.core.git import GitCoordinator
from agentflow.git.contracts import CheckpointRequest, GitConflict, WorkspaceRequest
from agentflow.git.provider import GitLocalProvider
from agentflow.agents import ExecutionPolicy, ExecutionRequest, ProcessResult, ProcessSpec
from agentflow.planning import digest, load_plan, task_data
from agentflow.state.contracts import EvidenceInput, Transition, TransitionError
from agentflow.state.git import GitStateStore


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, shell=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


def make_plan(root: Path):
    directory = root / ".agentflow"
    directory.mkdir()
    (directory / "spec.toml").write_text(
        'schema_version = 1\nproject_id = "demo"\ntitle = "Demo"\nobjective = "Test"\n'
        '[[acceptance_criteria]]\nid = "ready"\ndescription = "Ready"\n', encoding="utf-8")
    (directory / "tasks.toml").write_text(
        'schema_version = 2\n[[tasks]]\nid = "first"\ntitle = "First"\ndescription = "Write one file"\n'
        'versioned_paths = ["answer ü [1].txt"]\n'
        '[[tasks.acceptance_criteria]]\nid = "ok"\ndescription = "Answer exists"\n', encoding="utf-8")
    return load_plan(directory)


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "--quiet")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.test")
    git(path, "config", "core.autocrlf", "false")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "--", "tracked.txt")
    git(path, "commit", "--quiet", "-m", "base")
    return path


def request_for(root: Path, plan, provider: GitLocalProvider, operation_id: str) -> WorkspaceRequest:
    task = plan.tasks[0]
    top, common, head = provider.inspect(root)
    return WorkspaceRequest(plan.specification.project_id, task.id, 1,
                            digest({"task": task_data(task), "spec_digest": plan.spec_digest}),
                            operation_id, top, head, task.versioned_paths)


def test_workspace_creation_uses_own_branch_and_preserves_original_checkout(tmp_path):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    (root / "local note.txt").write_text("keep me\n", encoding="utf-8")
    before_head = git(root, "rev-parse", "HEAD")
    before_status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        request = request_for(root, plan, provider, "workspace-op")
        coordinator = GitCoordinator(store, provider)
        workspace = coordinator.create_workspace(request)
        assert workspace.status == "active"
        assert workspace.path.exists()
        assert workspace.branch == "agentflow/ws/" + workspace.workspace_id[3:]
        assert git(workspace.path, "rev-parse", "HEAD").strip().decode() == request.base_oid
        assert git(root, "rev-parse", "HEAD") == before_head
        assert git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") == before_status
        assert (root / "local note.txt").read_text(encoding="utf-8") == "keep me\n"
        again = coordinator.create_workspace(request)
        assert again.workspace_id == workspace.workspace_id
        assert git(root, "branch", "--list", workspace.branch).strip()
        assert store.operation("workspace-op")[1] == "verified"


def test_workspace_operation_id_rejects_divergent_parameters(tmp_path):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        request = request_for(root, plan, provider, "workspace-op")
        coordinator.create_workspace(request)
        divergent = WorkspaceRequest(request.project_id, request.task_id, request.definition_revision,
                                     request.definition_digest, request.operation_id,
                                     request.repository, request.base_oid, ("another.txt",))
        with pytest.raises(GitConflict, match="divergentes"):
            coordinator.create_workspace(divergent)


def test_checkpoint_uses_only_authorized_paths_and_verifies_identity(tmp_path):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    original_head = git(root, "rev-parse", "HEAD")
    original_status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        workspace = coordinator.create_workspace(request_for(root, plan, provider, "workspace-op"))
        name = "answer ü [1].txt"
        (workspace.path / name).write_text("verified answer\n", encoding="utf-8")
        request = CheckpointRequest(workspace.workspace_id, "checkpoint-op", (name,),
                                    "Reviewer", "reviewer@example.test", "Save verified output")
        checkpoint = coordinator.create_checkpoint(request)
        assert checkpoint.verified
        assert checkpoint.paths == (name,)
        assert checkpoint.parent_oid == workspace.base_oid
        assert checkpoint.commit_oid == git(workspace.path, "rev-parse", "HEAD").decode().strip()
        assert provider.verify_checkpoint(store.workspace(workspace.workspace_id), checkpoint)
        persisted = store.checkpoint_by_operation("checkpoint-op")
        assert persisted == checkpoint
        repeated = coordinator.create_checkpoint(request)
        assert repeated == checkpoint
        with pytest.raises(GitConflict, match="divergentes"):
            coordinator.create_checkpoint(CheckpointRequest(workspace.workspace_id, "checkpoint-op", (name,),
                "Reviewer", "reviewer@example.test", "different operation parameters"))
        assert git(root, "rev-parse", "HEAD") == original_head
        assert git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") == original_status


def test_checkpoint_rejects_paths_outside_task_scope(tmp_path):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        workspace = coordinator.create_workspace(request_for(root, plan, provider, "workspace-op"))
        (workspace.path / "other.txt").write_text("no\n", encoding="utf-8")
        with pytest.raises(GitConflict, match="escopo autorizado"):
            coordinator.create_checkpoint(CheckpointRequest(workspace.workspace_id, "bad-checkpoint",
                ("other.txt",), "Reviewer", "reviewer@example.test", "no"))
        assert (workspace.path / "other.txt").read_text(encoding="utf-8") == "no\n"
        assert store.operation("bad-checkpoint") is None


def test_checkpoint_preserves_but_refuses_unrelated_workspace_changes(tmp_path):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        workspace = coordinator.create_workspace(request_for(root, plan, provider, "workspace-op"))
        (workspace.path / "answer ü [1].txt").write_text("authorized\n", encoding="utf-8")
        unrelated = workspace.path / "notes for user.txt"
        unrelated.write_text("preserve\n", encoding="utf-8")
        request = CheckpointRequest(workspace.workspace_id, "blocked-checkpoint",
            ("answer ü [1].txt",), "Reviewer", "reviewer@example.test", "Save answer")
        with pytest.raises(GitConflict, match="fora da lista selecionada"):
            coordinator.create_checkpoint(request)
        assert unrelated.read_text(encoding="utf-8") == "preserve\n"
        assert (workspace.path / "answer ü [1].txt").read_text(encoding="utf-8") == "authorized\n"
        assert git(workspace.path, "rev-parse", "HEAD").decode().strip() == workspace.base_oid
        assert store.operation("blocked-checkpoint")[1] == "unknown"


def test_checkpoint_does_not_retry_after_interruption_and_preserves_files(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        workspace = coordinator.create_workspace(request_for(root, plan, provider, "workspace-op"))
        name = "answer ü [1].txt"
        target = workspace.path / name
        target.write_text("first\n", encoding="utf-8")
        original = provider.validate_paths
        calls = 0

        def racing_validate(path, paths, *, allow_missing=True):
            nonlocal calls
            calls += 1
            result = original(path, paths, allow_missing=allow_missing)
            if calls == 3:
                target.write_text("raced\n", encoding="utf-8")
            return result

        monkeypatch.setattr(provider, "validate_paths", racing_validate)
        request = CheckpointRequest(workspace.workspace_id, "racing-checkpoint", (name,),
                                    "Reviewer", "reviewer@example.test", "Save output")
        with pytest.raises(GitConflict, match="mudou depois do staging"):
            coordinator.create_checkpoint(request)
        assert target.read_text(encoding="utf-8") == "raced\n"
        assert store.operation("racing-checkpoint")[1] == "unknown"
        assert git(workspace.path, "rev-parse", "HEAD").decode().strip() == workspace.base_oid
        with pytest.raises(GitConflict, match="unknown"):
            coordinator.create_checkpoint(request)


def test_recovery_verifies_workspace_created_before_sqlite_confirmation(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        request = request_for(root, plan, provider, "workspace-crash")
        original = provider.create_workspace

        def create_then_crash(req, workspace_id):
            original(req, workspace_id)
            raise OSError("simulated crash after worktree effect")

        monkeypatch.setattr(provider, "create_workspace", create_then_crash)
        with pytest.raises(OSError):
            coordinator.create_workspace(request)
        assert store.operation("workspace-crash")[1] == "unknown"
        workspace_id = store.operation("workspace-crash")[2]
        path = store.workspace(workspace_id).path
        assert path.exists()
        with pytest.raises(GitConflict, match="unknown"):
            coordinator.create_workspace(request)
        monkeypatch.setattr(provider, "create_workspace", original)
        assert coordinator.recover() == (("workspace-crash", "verified"),)
        assert store.operation("workspace-crash")[1] == "verified"
        assert store.workspace(workspace_id).status == "active"
        events_before = store.db.execute("SELECT count(*) FROM git_events WHERE operation_id='workspace-crash'").fetchone()[0]
        assert coordinator.recover() == ()
        assert store.db.execute("SELECT count(*) FROM git_events WHERE operation_id='workspace-crash'").fetchone()[0] == events_before


def test_recovery_recognizes_exact_checkpoint_commit_without_duplicate(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        workspace = coordinator.create_workspace(request_for(root, plan, provider, "workspace-op"))
        name = "answer ü [1].txt"
        (workspace.path / name).write_text("committed once\n", encoding="utf-8")
        request = CheckpointRequest(workspace.workspace_id, "checkpoint-crash", (name,),
                                    "Reviewer", "reviewer@example.test", "Save once")
        original = provider.create_checkpoint

        def commit_then_crash(ws, req):
            original(ws, req)
            raise OSError("simulated crash after ref update")

        monkeypatch.setattr(provider, "create_checkpoint", commit_then_crash)
        with pytest.raises(OSError):
            coordinator.create_checkpoint(request)
        assert store.operation("checkpoint-crash")[1] == "unknown"
        before = git(workspace.path, "rev-list", "--count", f"{workspace.base_oid}..HEAD").strip()
        assert before == b"1"
        with pytest.raises(GitConflict, match="unknown"):
            coordinator.create_checkpoint(request)
        monkeypatch.setattr(provider, "create_checkpoint", original)
        pending = store.pending_git_operations()[0]
        observed = provider.reconcile_operation(store.workspace(workspace.workspace_id), pending[1],
                                               pending[0], pending[4])
        assert observed is not None
        assert coordinator.recover() == (("checkpoint-crash", "verified"),)
        assert store.operation("checkpoint-crash")[1] == "verified"
        checkpoint = store.checkpoint_by_operation("checkpoint-crash")
        assert checkpoint and provider.verify_checkpoint(store.workspace(workspace.workspace_id), checkpoint)
        assert git(workspace.path, "rev-list", "--count", f"{workspace.base_oid}..HEAD").strip() == b"1"
        assert coordinator.recover() == ()


@pytest.mark.parametrize("produce_change", [False, True])
def test_task_completion_requires_validated_attempt_and_git_verification(tmp_path, produce_change):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        ws_request = request_for(root, plan, provider, "workspace-complete")
        workspace = coordinator.create_workspace(ws_request)
        task = plan.tasks[0]
        definition_digest = ws_request.definition_digest
        spec = ProcessSpec(Path(sys.executable), (), workspace.path, "do task", (), "fake", "test",
                           "workspace-write", "never")
        request = ExecutionRequest("demo", "first", 1, definition_digest, "execution-complete",
                                   workspace.path, "do task", ExecutionPolicy(sandbox="workspace-write", approval="never"),
                                   (("ok", "Answer exists"),), workspace.workspace_id)
        attempt = store.prepare(request, spec, "test-owner")
        result = ProcessResult("process_succeeded", 0, tmp_path / "out", tmp_path / "err",
                              "0" * 64, "0" * 64, 0, 0, "ok")
        store.finish(attempt.attempt_id, result)
        declaration = store.add_evidence(EvidenceInput("first", "ok", "declared", "answer exists",
                                                        "task", "declare-complete"))
        verified = store.add_evidence(EvidenceInput("first", "ok", "verified", "answer exists",
                                                     "review", "verify-complete", declaration,
                                                     "manual review", "reviewer", True))
        store.set_validation(attempt.attempt_id, True, (verified,), "verified criterion")
        state = store.list_tasks()[0]
        with pytest.raises(TransitionError, match="checkpoint"):
            coordinator.complete_task(Transition("first", "completed", "complete-too-early", state.revision))
        if produce_change:
            (workspace.path / "answer ü [1].txt").write_text("verified answer\n", encoding="utf-8")
            coordinator.create_checkpoint(CheckpointRequest(workspace.workspace_id, "checkpoint-complete",
                ("answer ü [1].txt",), "Reviewer", "reviewer@example.test", "Verified result"))
        else:
            coordinator.verify_no_changes(workspace.workspace_id, "verify-no-versioned-changes")
        state = store.list_tasks()[0]
        assert coordinator.complete_task(Transition("first", "completed", "complete-now", state.revision)).status == "completed"


def test_clean_workspace_verification_is_idempotent_and_recoverable(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "repo")
    plan = make_plan(root)
    with GitStateStore(tmp_path / "state.sqlite3") as store, GitLocalProvider() as provider:
        store.import_plan(plan)
        coordinator = GitCoordinator(store, provider)
        workspace = coordinator.create_workspace(request_for(root, plan, provider, "workspace-clean"))
        operation_id = "verify-clean"
        original = store.complete_no_changes

        def fail_before_confirmation(op_id, ws):
            raise OSError("simulated interruption before SQLite confirmation")

        monkeypatch.setattr(store, "complete_no_changes", fail_before_confirmation)
        with pytest.raises(OSError):
            coordinator.verify_no_changes(workspace.workspace_id, operation_id)
        assert store.operation(operation_id)[1] == "unknown"
        monkeypatch.setattr(store, "complete_no_changes", original)
        assert coordinator.recover() == ((operation_id, "verified"),)
        assert coordinator.verify_no_changes(workspace.workspace_id, operation_id)
        assert coordinator.recover() == ()
