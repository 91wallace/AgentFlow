"""Small MCP stdio control plane; execution stays in the detached worker."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import uuid


PROTOCOL_VERSION = "2024-11-05"


def _tools():
    return [
        {"name": "agentflow_start", "description": "Queue an APL run in the persistent worker",
         "inputSchema": {"type": "object", "properties": {"objective": {"type": "string"}, "operation_id": {"type": "string"}, "orchestrator_agent": {"type": "string"}, "executor_agent": {"type": "string"}, "reviewer_agent": {"type": "string"}}, "required": ["objective", "operation_id"]}},
        {"name": "agentflow_status", "description": "Read worker and APL state",
         "inputSchema": {"type": "object", "properties": {"operation_id": {"type": "string"}}}},
        {"name": "agentflow_agents", "description": "Discover installed local agent CLIs", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "agentflow_configure", "description": "Persist role choices for a new objective", "inputSchema": {"type": "object", "properties": {"objective": {"type": "string"}, "operation_id": {"type": "string"}, "orchestrator_agent": {"type": "string"}, "executor_agent": {"type": "string"}, "reviewer_agent": {"type": "string"}}, "required": ["objective", "operation_id", "orchestrator_agent", "executor_agent"]}},
        {"name": "agentflow_tasks", "description": "Read persisted APL tasks", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        {"name": "agentflow_pause", "description": "Pause at an APL task boundary", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        {"name": "agentflow_resume", "description": "Resume a safe persisted APL run", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        {"name": "agentflow_cancel", "description": "Cancel an APL execution", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        {"name": "agentflow_results", "description": "Read durable execution results", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        {"name": "agentflow_review", "description": "Read persisted review records", "inputSchema": {"type": "object", "properties": {"operation_id": {"type": "string"}}, "required": ["operation_id"]}},
        {"name": "agentflow_review_status", "description": "Read review status", "inputSchema": {"type": "object", "properties": {"operation_id": {"type": "string"}}, "required": ["operation_id"]}},
        {"name": "agentflow_metrics", "description": "Read APL execution counters", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        {"name": "agentflow_context", "description": "Read run, cycle, and review context", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
        {"name": "agentflow_events", "description": "Read incremental live execution events", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "since_seq": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["run_id"]}},
        {"name": "agentflow_context_deltas", "description": "Read incremental shared context deltas", "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "project_id": {"type": "string"}, "since_version": {"type": "integer"}}, "required": ["run_id"]}},
    ]


def _call(root: Path, name: str, arguments: dict):
    from . import worker
    from .agents.discovery import discover_agents
    from .agents.factory import create_registry
    from .apl import SQLiteAuthorizedExecutionStore
    from .state.review import inspect_review
    from .state.roles import roles_for_cycle
    root = root.resolve(strict=True)
    if name == "agentflow_agents":
        return {"agents": [__import__("dataclasses").asdict(item) for item in discover_agents(create_registry())]}
    if name == "agentflow_start" or name == "agentflow_configure":
        operation = arguments.get("operation_id")
        objective = arguments.get("objective")
        if not operation or not objective:
            raise ValueError("objective and operation_id are required")
        command = ["start", objective, "--path", str(root), "--operation-id", operation]
        for role in ("orchestrator", "executor", "reviewer"):
            selected = arguments.get(role + "_agent")
            if selected:
                command.extend(("--" + role + "-agent", selected))
        # MCP has no prompts; unselected roles remain durable AWAITING_CONFIGURATION.
        if name == "agentflow_configure":
            command.append("--configure-only")
            import subprocess
            import sys
            completed = subprocess.run(
                [sys.executable, "-c", "import sys; from agentflow.cli import main; raise SystemExit(main(sys.argv[1:]))", *command],
                capture_output=True, text=True,
            )
            return {"exit_code": completed.returncode, "result": completed.stdout[:65536]}
        command.append("--managed")
        return worker.launch(root, command, operation_id=operation)
    run_id = arguments.get("run_id") or arguments.get("operation_id")
    if name == "agentflow_status":
        return worker.inspect(root, run_id)
    if name == "agentflow_pause":
        return worker.command(root, "pause", run_id)
    if name == "agentflow_resume":
        return worker.command(root, "resume", run_id)
    if name == "agentflow_cancel":
        return worker.command(root, "cancel", run_id)
    if name.startswith("agentflow_review") or name == "agentflow_review":
        return inspect_review(root / ".agentflow" / "state.sqlite3", arguments.get("operation_id", ""))
    path = root / ".agentflow" / "state.sqlite3"
    if name == "agentflow_events":
        from .communication_optimizer import CommunicationOptimizer
        from .observability import LiveEventBus
        since_seq = int(arguments.get("since_seq", 0))
        limit = int(arguments.get("limit", 100))
        bus = LiveEventBus(path)
        raw = bus.fetch_events(run_id, since_seq=since_seq, limit=limit)
        optimized = CommunicationOptimizer().optimize_messages(raw)
        return {"run_id": run_id, "events": optimized}
    if name == "agentflow_context_deltas":
        from .shared_context import SharedContextRegistry
        project_id = str(arguments.get("project_id") or root)
        since_version = int(arguments.get("since_version", 0))
        registry = SharedContextRegistry(path, project_id, run_id)
        return {"run_id": run_id, "deltas": registry.get_deltas(since_version=since_version)}
    store = SQLiteAuthorizedExecutionStore(str(path), initialize=False)
    state = store.status(run_id) if name in {"agentflow_tasks", "agentflow_results", "agentflow_metrics", "agentflow_context"} else store.load(run_id)
    if name == "agentflow_tasks":
        return {"run_id": run_id, "tasks": state["tasks"]}
    if name == "agentflow_metrics":
        return {key: state.get(key) for key in ("run_id", "status", "jobs_started", "attempts", "consumed_seconds", "consumed_budget")}
    if name == "agentflow_results":
        return {"run_id": run_id, "status": state["status"], "tasks": state["tasks"]}
    if name == "agentflow_context":
        roles = roles_for_cycle(path, state["cycle_id"])
        return {"run_id": run_id, "cycle_id": state["cycle_id"], "roles": roles[1].to_mapping() if roles else None,
                "plan": json.loads(state["plan_json"]), "events": state["events"][-50:]}
    if name == "agentflow_events":
        from .communication_optimizer import CommunicationOptimizer
        from .observability import LiveEventBus
        run_id = arguments["run_id"]
        since_seq = int(arguments.get("since_seq", 0))
        limit = int(arguments.get("limit", 100))
        bus = LiveEventBus(root / ".agentflow" / "state.sqlite3")
        raw = bus.fetch_events(run_id, since_seq=since_seq, limit=limit)
        optimized = CommunicationOptimizer().optimize_messages(raw)
        return {"run_id": run_id, "events": optimized}
    if name == "agentflow_context_deltas":
        from .shared_context import SharedContextRegistry
        run_id = arguments["run_id"]
        project_id = str(arguments.get("project_id") or root)
        since_version = int(arguments.get("since_version", 0))
        registry = SharedContextRegistry(root / ".agentflow" / "state.sqlite3", project_id, run_id)
        return {"run_id": run_id, "deltas": registry.get_deltas(since_version=since_version)}
    raise ValueError("unknown AgentFlow MCP tool: " + name)


def _respond(message):
    if "id" not in message:
        return None
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "agentflow", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tools()}
    else:
        result = None
    if method == "tools/call":
        return "call"
    if result is None:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def serve_stdio(root: Path):
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            if request.get("method") == "tools/call":
                params = request.get("params", {})
                value = _call(root, params.get("name", ""), params.get("arguments", {}))
                response = {"jsonrpc": "2.0", "id": request.get("id"), "result": {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}], "structuredContent": value}}
            else:
                response = _respond(request)
                if response is None:
                    continue
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                        "error": {"code": -32000, "message": str(exc)[:512]}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0
