"""End-to-end operational integration test for P1 runtime, worker, and MCP."""

from __future__ import annotations

import threading
import time

from agentflow.autonomous_runtime import AutonomousRuntime
from agentflow.cli import main as cli_main
from agentflow.global_config import GlobalConfig, RoleModelConfig, save_global_config
from agentflow.mcp_server import _call as mcp_call


def test_end_to_end_autonomous_runtime_and_mcp_reconnect(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.agents.discovery.discover_agents", lambda *a, **kw: [])
    root = tmp_path / "project"
    root.mkdir()
    import subprocess
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert cli_main(["init", "--path", str(root), "--non-interactive"]) == 0

    cfg = GlobalConfig(
        max_concurrent_workers=2,
        worker_distribution_policy="round-robin",
        subagents={
            "research": RoleModelConfig(agent="gemini", provider="google", model="gemini-2.5-pro"),
            "testing": RoleModelConfig(agent="codex"),
        },
    )
    cfg.fallback_policy = "ordered"
    cfg.fallback_models = ["codex"]
    save_global_config(cfg)

    runtime = AutonomousRuntime(root, "run-e2e-1", config=cfg)

    active = 0
    max_active = 0
    lock = threading.Lock()
    attempts: list[tuple[str, str]] = []

    def _runner(model_cfg: RoleModelConfig, task: dict, idem_key: str):
        nonlocal active, max_active
        tid = task["task_id"]
        attempts.append((tid, model_cfg.agent))
        if tid == "t1" and model_cfg.agent == "gemini":
            raise TimeoutError("simulated transient timeout")
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return {"task": tid, "agent": model_cfg.agent, "idem": idem_key}

    tasks = [
        {"task_id": "t1", "role": "research", "depends_on": []},
        {"task_id": "t2", "role": "testing", "depends_on": []},
        {"task_id": "t3", "role": "development", "depends_on": ["t1", "t2"]},
    ]
    results = runtime.execute_tasks(tasks, _runner)
    assert results == {"t1": "completed", "t2": "completed", "t3": "completed"}
    assert max_active <= 2
    assert ("t1", "gemini") in attempts and ("t1", "codex") in attempts

    # Client 1 queries initial events and context deltas
    ev1 = mcp_call(root, "agentflow_events", {"run_id": "run-e2e-1", "since_seq": 0, "limit": 3})
    assert len(ev1["events"]) == 3
    last_seq = ev1["events"][-1]["seq"]

    d1 = mcp_call(root, "agentflow_context_deltas", {"run_id": "run-e2e-1", "since_version": 0})
    assert len(d1["deltas"]) == 3
    last_ver = d1["deltas"][-1]["version"]

    # Client 2 reconnects and fetches remaining events by cursor
    ev2 = mcp_call(root, "agentflow_events", {"run_id": "run-e2e-1", "since_seq": last_seq, "limit": 50})
    assert all(e["seq"] > last_seq for e in ev2["events"])
    assert any(e["event_type"] == "fallback_applied" for e in ev1["events"] + ev2["events"])

    d2 = mcp_call(root, "agentflow_context_deltas", {"run_id": "run-e2e-1", "since_version": last_ver})
    assert d2["deltas"] == []
