from dataclasses import replace
import json
from pathlib import Path

import pytest

from agentflow.git.change_tracker import GitChangeTracker
from agentflow.git.contracts import GitConflict
from agentflow.git.provider import GitLocalProvider
from agentflow.review.context import build_packet
from agentflow.review.contracts import validate_review, ReviewPolicy
from agentflow.apl.contracts import JobProposal
from test_git_provider import repository, git


@pytest.mark.parametrize("staged", [False, True])
def test_staged_and_unstaged_changes_preserve_index_and_bytes(tmp_path, staged):
    root = repository(tmp_path / "repo")
    index = root / ".git/index"
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        before = tracker.capture()
        assert before["dirty"] == []
        (root / "tracked.txt").write_text("changed\n")
        if staged:
            git(root, "add", "--", "tracked.txt")
        index_bytes = index.read_bytes()
        after = tracker.capture()
        changes = tracker.changes(before, after, ("tracked.txt",))
        assert len(changes) == 1
        assert changes[0]["kind"] == "modified"
        assert changes[0]["attribution"] == "executor_candidate"
        assert changes[0]["index_changed"] == staged
        if staged:
            assert "changed" in changes[0]["staged_diff"]
        assert index.read_bytes() == index_bytes
        assert (root / "tracked.txt").read_text() == "changed\n"


def test_untracked_added_deleted_and_exact_rename(tmp_path):
    root = repository(tmp_path / "repo")
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        before = tracker.capture()
        (root / "tracked.txt").rename(root / "renamed ü [x].txt")
        (root / "new.py").write_text("VALUE = 1\n")
        after = tracker.capture()
        changes = tracker.changes(before, after, ("tracked.txt", "renamed ü [x].txt", "new.py"))
        renamed = next(item for item in changes if item["kind"] == "renamed")
        assert renamed["old_path"] == "tracked.txt"
        assert next(item for item in changes if item["path"] == "tracked.txt")["kind"] == "deleted"
        assert next(item for item in changes if item["path"] == "new.py")["kind"] == "added"


@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked"])
def test_preexisting_changes_are_not_claimed_or_automatically_published(tmp_path, kind):
    root = repository(tmp_path / "repo")
    name = "new.txt" if kind == "untracked" else "tracked.txt"
    (root / name).write_text("user content\n")
    if kind == "staged":
        git(root, "add", "--", name)
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        before = tracker.capture()
        assert tracker.changes(before, before, (name,)) == []
        (root / name).write_text("user content\nexecutor addition\n")
        after = tracker.capture()
        assert tracker.changes(before, after, (name,))[0]["attribution"] == "ambiguous"
        with pytest.raises(GitConflict, match="not_owned"):
            tracker.publish(after, [{"path": name, "before_sha256": after["files"][name]["sha256"], "content": "bad overwrite"}], set())
        assert (root / name).read_text().startswith("user content")


def test_conflicted_index_is_rejected_without_modification(tmp_path):
    root = repository(tmp_path / "repo")
    oid = git(root, "rev-parse", ":tracked.txt").decode().strip()
    git(root, "update-index", "--index-info", input_bytes=(f"0 {'0'*40}\ttracked.txt\n100644 {oid} 1\ttracked.txt\n100644 {oid} 2\ttracked.txt\n").encode())
    original = (root / ".git/index").read_bytes()
    with GitLocalProvider() as provider, pytest.raises(GitConflict, match="conflict"):
        GitChangeTracker(provider, root).capture()
    assert (root / ".git/index").read_bytes() == original


def test_publication_detects_concurrent_changes_and_keeps_index(tmp_path):
    root = repository(tmp_path / "repo")
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        expected = tracker.capture()
        edits = [{"path": "tracked.txt", "before_sha256": expected["files"]["tracked.txt"]["sha256"], "content": "fixed\n"}]
        index = (root / ".git/index").read_bytes()
        published = tracker.publish(expected, edits, {"tracked.txt"})
        assert (root / "tracked.txt").read_text() == "fixed\n"
        assert (root / ".git/index").read_bytes() == index
        (root / "unrelated.txt").write_text("external")
        with pytest.raises(GitConflict, match="state_changed"):
            tracker.publish(published, edits, {"tracked.txt"})
        assert (root / "unrelated.txt").read_text() == "external"


def test_incremental_context_has_symbols_imports_contracts_tests_not_whole_repo(tmp_path):
    root = repository(tmp_path / "repo")
    for name, text in {"main.py": "from contracts import Rule\ndef useful():\n    return 1\n",
                       "contracts.py": "class Rule: pass\n", "test_main.py": "from main import useful\n",
                       "unrelated.py": "X = 0\n"}.items():
        (root / name).write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-qm", "context")
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        before = tracker.capture()
        (root / "main.py").write_text("from contracts import Rule\ndef useful():\n    return 2\n")
        after = tracker.capture()
        changes = tracker.changes(before, after, ("main.py",))
        packet = build_packet(before, after, changes, JobProposal("task", "work"))
        assert set(packet["context"]) == {"main.py", "contracts.py", "test_main.py"}
        assert packet["changed_symbols"]["main.py"][0]["name"] == "useful"
        assert "unrelated.py" not in packet["context"]
        expanded = build_packet(before, after, changes, JobProposal("task", "work"), requested=("unrelated.py",))
        assert "unrelated.py" in expanded["context"]
        (root / "contracts.py").write_text("class Rule: changed = True\n")
        assert tracker.capture()["fingerprint"] != after["fingerprint"]


def finding(path="main.py", evidence="return 2"):
    return {"id": "reported", "path": path, "line": 3, "end_line": 3, "severity": "high",
            "evidence": evidence, "description": "Incorrect result", "impact": "Wrong output",
            "suggestion": "Use the specified value", "confidence": 0.95, "resolution": "open"}


def test_findings_require_verifiable_anchors_and_stable_identifiers():
    packet = {"context": {"main.py": {"text": "# header\ndef useful():\n    return 2\n"}}, "changes": []}
    answer = {"schema_version": 1, "state": "CORRECTIONS_REQUIRED", "findings": [finding()], "context_requests": []}
    first = validate_review(answer, packet)
    answer["findings"][0]["id"] = "different-model-id"
    assert validate_review(answer, packet)["findings"][0]["id"] == first["findings"][0]["id"]
    answer["findings"][0]["evidence"] = "invented code"
    with pytest.raises(ValueError, match="evidence_not"):
        validate_review(answer, packet)


@pytest.mark.parametrize("change", [
    {"confidence": 0.1}, {"confidence": float("nan")}, {"line": 99}, {"resolution": "resolved"}])
def test_unverified_or_invalid_findings_cannot_be_confirmed(change):
    packet = {"context": {"main.py": {"text": "# header\ndef useful():\n    return 2\n"}}, "changes": []}
    answer = {"schema_version": 1, "state": "CORRECTIONS_REQUIRED", "findings": [{**finding(), **change}], "context_requests": []}
    with pytest.raises(ValueError):
        validate_review(answer, packet)


def test_review_policy_rejects_invalid_limits():
    with pytest.raises(ValueError):
        ReviewPolicy(max_corrections=-1)
    with pytest.raises(ValueError):
        ReviewPolicy(mode="adaptive")


def test_sensitive_staged_content_is_hash_only(tmp_path):
    root = repository(tmp_path / "repo")
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        before = tracker.capture()
        (root / ".env").write_text("SECRET=do-not-persist\n")
        git(root, "add", "--", ".env")
        after = tracker.capture()
        changes = tracker.changes(before, after, (".env",))
        assert after["files"][".env"]["text"] is None
        assert changes[0]["index_after_text"] is None
        assert "do-not-persist" not in json.dumps([after, changes])


def test_ignored_source_cannot_escape_scope_review(tmp_path):
    root = repository(tmp_path / "repo")
    (root / ".gitignore").write_text("ignored.py\n__pycache__/\n")
    (root / "ignored.py").write_text("user content\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__/cached.pyc").write_bytes(b"cache")
    with GitLocalProvider() as provider:
        tracker = GitChangeTracker(provider, root)
        tracker.check_scope(("tracked.txt", "__pycache__"))
        with pytest.raises(GitConflict, match="ignored_source"):
            tracker.check_scope(("ignored.py",))
        with pytest.raises(GitConflict, match="ignored_source"):
            tracker.check_scope((".",))
    assert (root / "ignored.py").read_text() == "user content\n"


def test_tracker_coordinates_with_existing_repository_lock(tmp_path):
    import hashlib
    root = repository(tmp_path / "repo")
    with GitLocalProvider() as provider:
        _, common, _ = provider.inspect(root)
        lock_id = "repo-" + hashlib.sha256(str(common).encode("utf-8")).hexdigest()[:32]
        with provider.lock(root, lock_id, "existing-git-operation"):
            with pytest.raises(GitConflict, match="ocupado"):
                with GitChangeTracker(provider, root).lock("review-operation"):
                    pytest.fail("parallel writer was admitted")
