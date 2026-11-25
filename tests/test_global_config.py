"""Tests for global agent/model configuration, init reuse, and agentflow config."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from agentflow.cli import main
from agentflow.config import load_config
from agentflow.global_config import (
    GlobalConfig,
    GlobalConfigError,
    RoleModelConfig,
    default_global_config,
    global_config_path,
    load_global_config,
    save_global_config,
)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_global_config_path_windows_and_linux(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENTFLOW_GLOBAL_CONFIG", raising=False)
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
        assert global_config_path() == tmp_path / "AppData" / "AgentFlow" / "config.toml"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".xdg"))
        assert global_config_path() == tmp_path / ".xdg" / "agentflow" / "config.toml"


def test_first_init_creates_global_config_and_reuses_in_second_repo(monkeypatch, tmp_path):
    cfg_file = tmp_path / "global" / "config.toml"
    monkeypatch.setenv("AGENTFLOW_GLOBAL_CONFIG", str(cfg_file))

    repo1 = _git_repo(tmp_path / "repo1")
    assert main(["init", "--path", str(repo1), "--non-interactive", "--agent", "gemini"]) == 0
    assert cfg_file.is_file()
    loaded = load_global_config()
    assert loaded is not None
    assert loaded.orchestrator.agent == "gemini"
    assert load_config(repo1 / ".agentflow" / "config.toml").effective_agent == "gemini"

    # Second repo reuses global config automatically
    repo2 = _git_repo(tmp_path / "repo2")
    assert main(["init", "--path", str(repo2), "--non-interactive"]) == 0
    assert load_config(repo2 / ".agentflow" / "config.toml").effective_agent == "gemini"


def test_config_set_check_show_reset_and_propagation(monkeypatch, tmp_path, capsys):
    cfg_file = tmp_path / "global" / "config.toml"
    monkeypatch.setenv("AGENTFLOW_GLOBAL_CONFIG", str(cfg_file))

    repo1 = _git_repo(tmp_path / "repo1")
    assert main(["init", "--path", str(repo1), "--non-interactive", "--agent", "codex"]) == 0
    capsys.readouterr()

    assert main([
        "config", "set",
        "--orchestrator-agent", "antigravity",
        "--worker-agent", "gemini",
        "--max-workers", "4",
        "--distribution-policy", "reuse-first",
        "--subagent", "research=gemini",
        "--adversarial", "on",
        "--adversarial-agent", "claude-code",
        "--fallback-policy", "ordered",
        "--fallback-models", "codex,gemini",
    ]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["saved"] is True
    assert out["config"]["orchestrator"]["agent"] == "antigravity"
    assert out["config"]["workers"]["default"]["agent"] == "gemini"
    assert out["config"]["workers"]["max_concurrent"] == 4
    assert out["config"]["subagents"]["research"]["agent"] == "gemini"
    assert out["config"]["adversarial"]["enabled"] is True
    assert out["config"]["adversarial"]["agent"] == "claude-code"

    # Existing repo1 is preserved
    assert load_config(repo1 / ".agentflow" / "config.toml").effective_agent == "codex"

    # New repo2 gets the updated orchestrator
    repo2 = _git_repo(tmp_path / "repo2")
    assert main(["init", "--path", str(repo2), "--non-interactive"]) == 0
    assert load_config(repo2 / ".agentflow" / "config.toml").effective_agent == "antigravity"

    # Check & Reset
    capsys.readouterr()
    assert main(["config", "check"]) == 0
    check_out = json.loads(capsys.readouterr().out)
    assert "orchestrator" in check_out["checks"]

    assert main(["config", "reset"]) == 0
    reset_out = json.loads(capsys.readouterr().out)
    assert reset_out["reset"] is True
    assert reset_out["config"]["orchestrator"]["agent"] == "codex"


def test_corrupted_or_secret_bearing_global_config_is_rejected(tmp_path):
    bad_toml = tmp_path / "bad.toml"
    bad_toml.write_text("not = [valid", encoding="utf-8")
    with pytest.raises(GlobalConfigError, match="corrompida"):
        load_global_config(bad_toml)

    secret_toml = tmp_path / "secret.toml"
    secret_toml.write_text('schema_version = 1\napi_key = "sk-12345"\n', encoding="utf-8")
    with pytest.raises(GlobalConfigError, match="credenciais/segredos"):
        load_global_config(secret_toml)


def test_migration_from_v0_global_config(tmp_path):
    v0_toml = tmp_path / "v0.toml"
    v0_toml.write_text('agent = "gemini"\n', encoding="utf-8")
    cfg = load_global_config(v0_toml)
    assert cfg is not None
    assert cfg.schema_version == 1
    assert cfg.orchestrator.agent == "gemini"
    assert cfg.worker_default.agent == "gemini"


def test_start_applies_global_config_and_preserves_active_run(tmp_path, monkeypatch, capsys):
    import subprocess
    from agentflow import cli as cli_module
    from dataclasses import dataclass
    from agentflow.agents.contracts import AgentAvailability

    @dataclass
    class _Desc:
        adapter_id: str
        display_name: str
        api_version: str

    @dataclass
    class _Report:
        descriptor: _Desc
        availability: AgentAvailability
        capabilities: object
        authentication: str
        model_listing: str
        models: tuple
        configured_provider: str
        configured_model: str
    from agentflow.global_config import GlobalConfig, RoleModelConfig, save_global_config

    cfg_path = tmp_path / "global" / "config.toml"
    monkeypatch.setenv("AGENTFLOW_GLOBAL_CONFIG", str(cfg_path))
    save_global_config(GlobalConfig(
        orchestrator=RoleModelConfig(agent="antigravity"),
        worker_default=RoleModelConfig(agent="gemini", provider="native", model="gemini-2.5-pro"),
        adversarial_enabled=True,
        adversarial=RoleModelConfig(agent="claude-code"),
    ))

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert main(["init", "--path", str(repo), "--non-interactive"]) == 0
    capsys.readouterr()

    fake_reports = [
        _Report(
            descriptor=_Desc(adapter_id=name, display_name=name, api_version="1"),
            availability=AgentAvailability(adapter_id=name, status="available", version="1.0.0", executable=f"/bin/{name}", reason_code="ok", checked_at="2026-11-24T00:00:00Z", source="test"),
            capabilities=None,
            authentication="authenticated",
            model_listing="static",
            models=(),
            configured_provider="",
            configured_model="",
        )
        for name in ("antigravity", "gemini", "claude-code")
    ]
    monkeypatch.setattr("agentflow.agents.discovery.discover_agents", lambda *a, **kw: fake_reports)
    monkeypatch.setattr("agentflow.agents.factory.create_role_adapter", lambda *a, **kw: object())

    rc = main(["start", "Implementar algo", "--path", str(repo), "--operation-id", "op-global-1", "--configure-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"status": "CONFIGURED"' in out
    assert '"antigravity"' in out
    assert '"gemini-2.5-pro"' in out
    assert '"claude-code"' in out

    # Changing global config later does not mutate the already-configured operation.
    save_global_config(GlobalConfig(orchestrator=RoleModelConfig(agent="codex"), worker_default=RoleModelConfig(agent="codex")))
    rc2 = main(["start", "Implementar algo", "--path", str(repo), "--operation-id", "op-global-1", "--configure-only"])
    assert rc2 == 2
    assert "role_configuration_immutable" in capsys.readouterr().err

