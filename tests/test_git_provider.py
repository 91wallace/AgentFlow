import os
from pathlib import Path
import subprocess

import pytest

from agentflow.git.contracts import GitConflict, GitError
from agentflow.git.provider import GitLocalProvider


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, input=input_bytes, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, shell=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "--quiet")
    git(path, "config", "user.name", "AgentFlow Test")
    git(path, "config", "user.email", "agentflow-test@example.test")
    git(path, "config", "core.autocrlf", "false")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", "--", "tracked.txt")
    git(path, "commit", "--quiet", "-m", "base")
    return path


def test_preflight_is_read_only_and_preserves_preexisting_changes(tmp_path):
    root = repository(tmp_path / "repo")
    (root / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(root, "add", "--", "tracked.txt")
    (root / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (root / "untracked data.txt").write_text("user data\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and p.name != ".git"}
    with GitLocalProvider() as provider:
        info = provider.preflight(root)
    after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and p.name != ".git"}
    assert info["root"] == root.resolve()
    assert info["dirty"]
    assert after == before
    assert (root / "untracked data.txt").exists()


@pytest.mark.parametrize("path", ["../outside", "/outside", "C:/outside", "a//b", "a/./b", "x\\y"])
def test_path_validation_rejects_escape_and_ambiguous_syntax(tmp_path, path):
    root = repository(tmp_path / "repo")
    with GitLocalProvider() as provider, pytest.raises(GitConflict):
        provider.validate_paths(root, (path,))


def test_paths_with_unicode_spaces_and_metacharacters_are_literal(tmp_path):
    root = repository(tmp_path / "repo")
    name = "docs/guia ü [final] & +#.md"
    (root / "docs").mkdir()
    (root / name).write_text("hello\n", encoding="utf-8")
    with GitLocalProvider() as provider:
        validated = provider.validate_paths(root, (name,), allow_missing=False)
        assert validated == (root / name,)
        info = provider.preflight(root, (name,))
        assert info["paths"] == validated


def test_changed_path_parsing_preserves_spaces_unicode_and_special_chars(tmp_path):
    root = repository(tmp_path / "repo")
    name = "sp ace ü [x] &#.txt"
    (root / name).write_text("new\n", encoding="utf-8")
    with GitLocalProvider() as provider:
        assert provider.changed_paths(root) == (name,)


def test_symlink_escape_is_rejected_deterministically(tmp_path, monkeypatch):
    root = repository(tmp_path / "repo")
    target = root / "link.txt"
    original = Path.is_symlink

    def is_symlink(path: Path) -> bool:
        return True if path == target else original(path)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    with GitLocalProvider() as provider, pytest.raises(GitConflict, match="symlink"):
        provider.validate_paths(root, ("link.txt",))


def test_submodule_gitlink_is_rejected(tmp_path):
    root = repository(tmp_path / "repo")
    result = subprocess.run(["git", "update-index", "--add", "--cacheinfo", "160000",
                             "a" * 40, "vendor/module"], cwd=root, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, shell=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    with GitLocalProvider() as provider, pytest.raises(GitConflict, match="submódulo"):
        provider.preflight(root, ("vendor/module/file.txt",))


@pytest.mark.parametrize("configuration", ["hook", "filter", "attribute"])
def test_executable_hooks_and_filters_are_rejected(tmp_path, configuration):
    root = repository(tmp_path / "repo")
    if configuration == "hook":
        git(root, "config", "core.hooksPath", str(tmp_path / "hooks"))
    elif configuration == "filter":
        git(root, "config", "filter.evil.clean", "echo unsafe")
    else:
        (root / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    with GitLocalProvider() as provider, pytest.raises(GitConflict):
        provider.preflight(root)


def test_git_subprocess_output_limit_is_enforced_while_reading(tmp_path):
    root = repository(tmp_path / "repo")
    with GitLocalProvider() as provider:
        provider._run(root, "config", "--local", "--add", "agentflow.large", "x" * 4096)
        provider.MAX_STREAM = 512
        with pytest.raises(GitError, match="saída do Git excedeu o limite"):
            provider._run(root, "config", "--local", "--list")
