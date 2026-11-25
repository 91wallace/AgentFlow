"""Real-process tests for the detached worker and stdio MCP server."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from agentflow import worker
from agentflow.state.sqlite import SQLiteStore


def _init_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / ".agentflow" / "state.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteStore(db_path, execution=True, git=True, validation=True,
                     agent_models=True, sdd=True, roles=True, review=True, workers=True):
        pass
    return db_path


def _mcp_rpc(proc: subprocess.Popen, req_id: int, method: str, params: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
    proc.stdin.flush()
    line = proc.stdout.readline().decode("utf-8")
    if not line:
        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        raise AssertionError(f"MCP server closed stdout unexpectedly (rc={proc.poll()}): {err}")
    return json.loads(line)


def test_worker_schema_uses_dedicated_active_run_id_column(tmp_path):
    db_path = _init_workspace(tmp_path / "repo")
    with sqlite3.connect(db_path) as db:
        cols = [row[1] for row in db.execute("PRAGMA table_info(managed_workers)").fetchall()]
    assert "active_run_id" in cols
    assert "state_db" in cols


def test_worker_inspect_returns_none_when_uninitialized(tmp_path):
    assert worker.inspect(tmp_path / "missing") is None


def test_worker_launch_idempotency_and_survives_client_exit(tmp_path):
    root = tmp_path / "repo"
    db_path = _init_workspace(root)
    op_id = "op-e2e-1"
    argv = ["start", "Echo hello", "--path", str(root), "--operation-id", op_id, "--managed"]

    first = worker.launch(root, argv, operation_id=op_id)
    assert first["detached"] is True
    assert first["run_id"] == op_id
    worker_id = first["worker_id"]
    assert worker_id.startswith("af-worker-")

    # Idempotent re-launch with same operation_id reuses the run without duplicating
    second = worker.launch(root, argv, operation_id=op_id)
    assert second["reused"] is True
    assert second["run_id"] == op_id

    status = worker.inspect(root, op_id)
    assert status is not None
    assert status["active"] is True
    assert status["worker_id"] == worker_id
    assert status["state_db"] == str(db_path)
    assert status["mapped_run_id"] == op_id

    # Clean up detached worker process
    pid = status["pid"]
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        os.kill(pid, 9)


def test_mcp_stdio_handoff_between_two_clients(tmp_path):
    root = tmp_path / "repo"
    _init_workspace(root)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    # Client 1 connects over stdio
    c1 = subprocess.Popen(
        [sys.executable, "-c", "import sys; from agentflow.cli import main; raise SystemExit(main(sys.argv[1:]))", "mcp", "--path", str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        init_res = _mcp_rpc(c1, 1, "initialize", {})
        assert init_res["result"]["serverInfo"]["name"] == "agentflow"

        tools_res = _mcp_rpc(c1, 2, "tools/list", {})
        tool_names = {t["name"] for t in tools_res["result"]["tools"]}
        assert {"agentflow_start", "agentflow_status", "agentflow_pause",
                "agentflow_resume", "agentflow_cancel", "agentflow_agents",
                "agentflow_configure", "agentflow_tasks", "agentflow_results",
                "agentflow_review", "agentflow_metrics", "agentflow_context"} <= tool_names

        # Launch a run via Client 1
        start_res = _mcp_rpc(c1, 3, "tools/call", {
            "name": "agentflow_start",
            "arguments": {"objective": "Test objective", "operation_id": "mcp-op-1"},
        })
        start_data = start_res["result"]["structuredContent"]
        assert start_data["detached"] is True
        worker_pid = worker.inspect(root, "mcp-op-1")["pid"]
    finally:
        if c1.stdin:
            c1.stdin.close()
        c1.wait(timeout=5)

    # Client 1 is now closed; worker must still be alive
    time.sleep(0.2)
    mid_status = worker.inspect(root, "mcp-op-1")
    assert mid_status is not None
    assert mid_status["active"] is True
    assert mid_status["pid"] == worker_pid

    # Client 2 connects over stdio and queries the same persisted execution
    c2 = subprocess.Popen(
        [sys.executable, "-c", "import sys; from agentflow.cli import main; raise SystemExit(main(sys.argv[1:]))", "mcp", "--path", str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        _mcp_rpc(c2, 1, "initialize", {})
        status_res = _mcp_rpc(c2, 2, "tools/call", {
            "name": "agentflow_status",
            "arguments": {"operation_id": "mcp-op-1"},
        })
        status_data = status_res["result"]["structuredContent"]
        assert status_data["active"] is True
        assert status_data["pid"] == worker_pid
        assert status_data["run"]["run_id"] == "mcp-op-1"
    finally:
        if c2.stdin:
            c2.stdin.close()
        c2.wait(timeout=5)
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(worker_pid), "/F"], capture_output=True)
        else:
            os.kill(worker_pid, 9)
