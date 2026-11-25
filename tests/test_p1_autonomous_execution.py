"""Functional tests for P1 autonomous execution: pool, fallback, subagents, observability, and context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import time

import pytest

from agentflow.communication_optimizer import CommunicationOptimizer
from agentflow.fallback_executor import classify_error, execute_with_fallback
from agentflow.global_config import GlobalConfig, RoleModelConfig, save_global_config
from agentflow.observability import LiveEventBus
from agentflow.shared_context import SharedContextRegistry
from agentflow.subagent_dispatcher import SubagentDispatcher
from agentflow.worker_pool import WorkerPool


def test_worker_pool_respects_concurrency_limit_and_dependencies(tmp_path):
    cfg = GlobalConfig(max_concurrent_workers=2, worker_distribution_policy="round-robin")
    save_global_config(cfg)

    db_path = tmp_path / "state.sqlite3"
    pool = WorkerPool(db_path)
    assert pool.max_concurrent_workers == 2

    pool.enqueue_tasks(
        "run-1",
        [
            {"task_id": "t1", "depends_on": []},
            {"task_id": "t2", "depends_on": []},
            {"task_id": "t3", "depends_on": ["t1", "t2"]},
        ],
    )

    active = 0
    max_seen = 0
    seen_order: list[str] = []
    lock = threading.Lock()

    def _run(task, worker_id):
        nonlocal active, max_seen
        with lock:
            active += 1
            max_seen = max(max_seen, active)
        time.sleep(0.03)
        with lock:
            active -= 1
            seen_order.append(task["task_id"])
        return {"ok": True}

    results = pool.run_parallel("run-1", _run)
    assert results == {"t1": "completed", "t2": "completed", "t3": "completed"}
    assert max_seen <= 2
    assert seen_order[-1] == "t3"


def test_worker_pool_reaps_stale_workers_and_pause_resume_cancel(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    pool = WorkerPool(db_path, max_concurrent_workers=2)
    pool.enqueue_tasks("run-2", [{"task_id": "a1", "depends_on": []}, {"task_id": "a2", "depends_on": ["a1"]}])

    claimed = pool.claim_next_task("worker-dead", run_id="run-2")
    assert claimed is not None
    # Simulate stale heartbeat
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE worker_pool_workers SET last_heartbeat=? WHERE worker_id='worker-dead'", (old_ts,))
        conn.commit()

    requeued = pool.reap_stale_workers(timeout_seconds=30.0)
    assert requeued == 1

    pool.pause_run("run-2")
    assert pool.claim_next_task("worker-live", run_id="run-2") is None
    pool.resume_run("run-2")
    reclaimed = pool.claim_next_task("worker-live", run_id="run-2")
    assert reclaimed is not None and reclaimed["task_id"] == "a1"
    pool.cancel_run("run-2")


def test_fallback_executor_applies_policy_and_avoids_silent_fallback():
    category, recoverable = classify_error(TimeoutError("request timed out"))
    assert recoverable is True and "timed out" in category

    cfg_none = GlobalConfig()
    cfg_none.fallback_policy = "none"  # type: ignore[attr-defined]
    cfg_none.fallback_models = [RoleModelConfig(agent="gemini", provider="google", model="gemini-2.5-pro")]  # type: ignore[attr-defined]

    res_none = execute_with_fallback(
        RoleModelConfig(agent="codex"),
        lambda model, key: (_ for _ in ()).throw(TimeoutError("timed out")),
        task_id="t1",
        idempotency_key="k1",
        config=cfg_none,
    )
    assert res_none.succeeded is False
    assert len(res_none.attempts) == 1

    cfg_allow = GlobalConfig()
    cfg_allow.fallback_policy = "ordered"  # type: ignore[attr-defined]
    cfg_allow.fallback_models = [RoleModelConfig(agent="gemini", provider="google", model="gemini-2.5-pro")]  # type: ignore[attr-defined]

    def _runner(model: RoleModelConfig, key: str):
        if model.agent == "codex":
            raise TimeoutError("timed out")
        return f"ok:{model.agent}:{key}"

    res_ok = execute_with_fallback(
        RoleModelConfig(agent="codex"),
        _runner,
        task_id="t1",
        idempotency_key="k1",
        config=cfg_allow,
    )
    assert res_ok.succeeded is True
    assert res_ok.selected is not None and res_ok.selected.agent == "gemini"
    assert len(res_ok.attempts) == 2


def test_subagent_dispatcher_uses_global_specializations():
    cfg = GlobalConfig(
        subagents={
            "research": RoleModelConfig(agent="gemini", provider="google", model="gemini-2.5-pro"),
            "testing": RoleModelConfig(agent="codex"),
        },
        adversarial_enabled=False,
    )
    dispatcher = SubagentDispatcher(cfg)
    rec = dispatcher.dispatch("task-r", "research", lambda m, role, tid: f"{m.agent}:{role}:{tid}")
    assert rec.agent == "gemini"
    assert rec.result == "gemini:research:task-r"

    with pytest.raises(RuntimeError, match="disabled"):
        dispatcher.dispatch("task-adv", "adversarial", lambda m, role, tid: None)


def test_observability_optimizer_and_shared_context(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    bus = LiveEventBus(db_path)
    s1 = bus.emit("run-x", "heartbeat", worker_id="w1", payload={"progress": 10})
    s2 = bus.emit("run-x", "log", worker_id="w1", payload={"msg": "step 1"})
    assert s2 > s1
    assert [e["seq"] for e in bus.fetch_events("run-x", since_seq=s1)] == [s2]

    opt = CommunicationOptimizer()
    msgs = [
        {"kind": "status", "text": "working"},
        {"kind": "status", "text": "working"},
        {"kind": "evidence", "text": "sha256:abc"},
        {"kind": "evidence", "text": "sha256:abc"},
    ]
    out = opt.optimize_messages(msgs)
    assert len(out) == 3  # 1 status + 2 essential evidences preserved
    assert opt.total_tokens_saved > 0

    ctx_a = SharedContextRegistry(db_path, "proj-a", "run-1")
    ctx_b = SharedContextRegistry(db_path, "proj-b", "run-1")
    v1 = ctx_a.put_delta("plan", {"step": 1})
    v2 = ctx_a.put_delta("plan", {"step": 2})
    ctx_b.put_delta("plan", {"step": 99})

    deltas = ctx_a.get_deltas(since_version=v1)
    assert len(deltas) == 1 and deltas[0]["version"] == v2 and deltas[0]["value"] == {"step": 2}
