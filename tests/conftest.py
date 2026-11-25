"""Shared pytest fixtures for test isolation."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_global_config(tmp_path, monkeypatch):
    """Isolate global AgentFlow config per test and for child subprocesses."""
    config_path = tmp_path / "agentflow-global-config" / "config.toml"
    monkeypatch.setenv("AGENTFLOW_GLOBAL_CONFIG", str(config_path))
    monkeypatch.delenv("AGENTFLOW_CALLING_AGENT", raising=False)
    return config_path
