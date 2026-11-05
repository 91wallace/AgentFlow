import importlib
from pathlib import Path
import subprocess

import pytest

from agentflow.cli import main
from agentflow.config import Config, ConfigError, DEFAULT_TOML, parse_config
from agentflow.environment.doctor import diagnose
from agentflow.init import InitError, initialize


def git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_version_cli(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "agentflow 0.1.0" in capsys.readouterr().out
    for module in ("core", "planning", "state", "agents", "runtime", "validation", "git", "environment", "delivery"):
        importlib.import_module(f"agentflow.{module}")


@pytest.mark.parametrize("raw", [
    b"schema_version = true", b"schema_version = 2", b"runtime = 3",
    b'runtime = "remote"', b'agent = "other"', b"unknown = 1", b"[bad]",
    b"not toml", b"\xff",
])
def test_invalid_config(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_defaults_and_valid_config():
    assert parse_config(b"") == Config()
    assert parse_config(DEFAULT_TOML.encode()) == Config()
    assert parse_config(b'schema_version = 1\nruntime = "docker"\nagent = "codex"') == Config(1, "docker", "codex")


def test_init_idempotent_and_preserves_existing(tmp_path):
    root = git_repo(tmp_path / "repo")
    nested = root / "nested"
    nested.mkdir()
    assert initialize(nested) == (root.resolve(), True)
    target = root / ".agentflow" / "config.toml"
    target.write_text('schema_version = 1\nruntime = "docker"\nagent = "none"\n')
    before = target.read_bytes()
    assert initialize(root) == (root.resolve(), False)
    assert target.read_bytes() == before


def test_init_conflict_and_non_git(tmp_path):
    root = git_repo(tmp_path / "repo")
    directory = root / ".agentflow"
    directory.mkdir()
    target = directory / "config.toml"
    target.write_text("invalid")
    with pytest.raises(InitError):
        initialize(root)
    assert target.read_text() == "invalid"
    with pytest.raises(InitError):
        initialize(tmp_path / "missing")


def test_init_directory_conflict(tmp_path):
    root = git_repo(tmp_path / "repo")
    (root / ".agentflow").write_text("keep me")
    with pytest.raises(InitError):
        initialize(root)
    assert (root / ".agentflow").read_text() == "keep me"


@pytest.mark.parametrize("failure_at", ["mkdir", "open"])
def test_init_write_failure_is_controlled_and_preserves_files(tmp_path, monkeypatch, capsys, failure_at):
    root = git_repo(tmp_path / "repo")
    directory = root / ".agentflow"
    if failure_at == "open":
        directory.mkdir()
    existing = root / "existing.txt"
    existing.write_bytes(b"keep these bytes\r\n")
    original_mkdir = Path.mkdir
    original_open = Path.open

    def denied_mkdir(path, *args, **kwargs):
        if path == directory:
            raise PermissionError("simulated mkdir denial")
        return original_mkdir(path, *args, **kwargs)

    def denied_open(path, *args, **kwargs):
        if path == directory / "config.toml":
            raise PermissionError("simulated open denial")
        return original_open(path, *args, **kwargs)

    if failure_at == "mkdir":
        monkeypatch.setattr(Path, "mkdir", denied_mkdir)
    else:
        monkeypatch.setattr(Path, "open", denied_open)

    assert main(["init", "--path", str(root)]) == 2
    output = capsys.readouterr()
    assert "não foi possível criar" in output.err
    assert "configuração criada" not in output.out
    assert existing.read_bytes() == b"keep these bytes\r\n"
    assert not (directory / "config.toml").exists()


def test_doctor_requirements_and_optional_absence():
    def missing(executable, arguments):
        return ("ok", "git version") if executable == "git" else ("missing", "não encontrado")

    local = diagnose(Config(), missing, (3, 13))
    assert [(c.name, c.required, c.status) for c in local] == [
        ("Python", True, "ok"), ("Git", True, "ok"),
        ("Codex CLI", False, "missing"), ("Docker", False, "missing")]
    selected = diagnose(Config(1, "docker", "codex"), missing, (3, 10))
    assert all(c.required for c in selected)
    assert selected[0].status == "error"


def test_doctor_exit_codes(tmp_path, monkeypatch, capsys):
    root = git_repo(tmp_path / "repo")
    assert main(["doctor", "--path", str(root)]) == 2
    initialize(root)
    monkeypatch.setattr("agentflow.cli.diagnose", lambda config: diagnose(config, lambda exe, args: ("ok", "v") if exe == "git" else ("missing", "absent"), (3, 13)))
    assert main(["doctor", "--path", str(root)]) == 0
    assert "Docker: missing (opcional)" in capsys.readouterr().out
    (root / ".agentflow" / "config.toml").write_text('schema_version = 1\nagent = "codex"')
    assert main(["doctor", "--path", str(root)]) == 1
    assert "Codex CLI: missing (obrigatório)" in capsys.readouterr().out
    assert main(["init", "--path", str(tmp_path / "missing")]) == 2


def test_doctor_required_git_missing(tmp_path, monkeypatch):
    root = git_repo(tmp_path / "repo")
    initialize(root)
    monkeypatch.setattr("agentflow.cli.diagnose", lambda config: diagnose(config, lambda exe, args: ("missing", "absent"), (3, 13)))
    assert main(["doctor", "--path", str(root)]) == 1
