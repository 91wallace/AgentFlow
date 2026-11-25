"""Persistent parallel worker pool backed by the operational SQLite database."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .global_config import load_global_config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


POOL_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS worker_pool_tasks(
        task_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        depends_on_json TEXT NOT NULL DEFAULT '[]',
        payload_json TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL CHECK(state IN ('pending','ready','claimed','running','completed','failed','cancelled','paused')),
        worker_id TEXT NOT NULL DEFAULT '',
        attempt INTEGER NOT NULL DEFAULT 0,
        claimed_at TEXT NOT NULL DEFAULT '',
        heartbeat_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        result_json TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_worker_pool_tasks_run_state ON worker_pool_tasks(run_id, state)",
    """
    CREATE TABLE IF NOT EXISTS worker_pool_workers(
        worker_id TEXT PRIMARY KEY,
        status TEXT NOT NULL CHECK(status IN ('idle','busy','stale','stopped')),
        active_tasks INTEGER NOT NULL DEFAULT 0,
        last_heartbeat TEXT NOT NULL
    )
    """,
)


class WorkerPool:
    """Coordinates persistent task queuing, atomic claiming, and bounded parallel execution."""

    def __init__(
        self,
        db_path: Path,
        *,
        max_concurrent_workers: int | None = None,
        distribution_policy: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        cfg = load_global_config()
        if max_concurrent_workers is not None:
            self.max_concurrent_workers = max(1, int(max_concurrent_workers))
        elif cfg is not None:
            self.max_concurrent_workers = max(1, int(cfg.max_concurrent_workers))
        else:
            self.max_concurrent_workers = 2

        if distribution_policy is not None:
            self.distribution_policy = distribution_policy
        elif cfg is not None:
            self.distribution_policy = cfg.worker_distribution_policy
        else:
            self.distribution_policy = "round-robin"

        self._rr_index = 0
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in POOL_SCHEMA:
                    conn.execute(stmt)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def enqueue_tasks(self, run_id: str, tasks: list[dict[str, Any]]) -> None:
        """Persist tasks with dependencies; tasks without dependencies become ready immediately."""
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for task in tasks:
                    task_id = str(task["task_id"])
                    deps = [str(d) for d in task.get("depends_on", [])]
                    state = "ready" if not deps else "pending"
                    payload = {k: v for k, v in task.items() if k not in ("task_id", "depends_on")}
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO worker_pool_tasks(
                            task_id, run_id, depends_on_json, payload_json, state, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (task_id, run_id, json.dumps(deps), json.dumps(payload), state, now),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def refresh_ready_tasks(self, run_id: str) -> int:
        """Promote pending tasks whose dependencies are all completed to ready."""
        now = _now_iso()
        promoted = 0
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    "SELECT task_id, depends_on_json FROM worker_pool_tasks WHERE run_id=? AND state='pending'",
                    (run_id,),
                ).fetchall()
                completed_rows = conn.execute(
                    "SELECT task_id FROM worker_pool_tasks WHERE run_id=? AND state='completed'",
                    (run_id,),
                ).fetchall()
                completed = {r["task_id"] for r in completed_rows}
                for row in rows:
                    deps = json.loads(row["depends_on_json"])
                    if all(d in completed for d in deps):
                        conn.execute(
                            "UPDATE worker_pool_tasks SET state='ready', updated_at=? WHERE task_id=? AND state='pending'",
                            (now, row["task_id"]),
                        )
                        promoted += 1
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return promoted

    def register_worker(self, worker_id: str) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT INTO worker_pool_workers(worker_id, status, active_tasks, last_heartbeat)
                    VALUES (?, 'idle', 0, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET status='idle', last_heartbeat=excluded.last_heartbeat
                    """,
                    (worker_id, now),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def heartbeat_worker(self, worker_id: str) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE worker_pool_workers SET last_heartbeat=? WHERE worker_id=?",
                    (now, worker_id),
                )
                conn.execute(
                    "UPDATE worker_pool_tasks SET heartbeat_at=? WHERE worker_id=? AND state IN ('claimed','running')",
                    (now, worker_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def select_worker(self, workers: list[str]) -> str:
        """Select a worker according to worker_distribution_policy."""
        if not workers:
            raise ValueError("No workers available")
        if self.distribution_policy == "least-loaded":
            with closing(_connect(self.db_path)) as conn:
                rows = conn.execute(
                    "SELECT worker_id, active_tasks FROM worker_pool_workers WHERE status != 'stale'"
                ).fetchall()
                loads = {r["worker_id"]: r["active_tasks"] for r in rows}
            return min(workers, key=lambda w: loads.get(w, 0))
        with self._lock:
            chosen = workers[self._rr_index % len(workers)]
            self._rr_index += 1
            return chosen

    def claim_next_task(self, worker_id: str, run_id: str | None = None) -> dict[str, Any] | None:
        """Atomically claim one ready task if concurrency limit allows."""
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                active_count = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM worker_pool_tasks WHERE state IN ('claimed','running')"
                    + (" AND run_id=?" if run_id else ""),
                    ((run_id,) if run_id else ()),
                ).fetchone()["cnt"]
                if active_count >= self.max_concurrent_workers:
                    conn.execute("COMMIT")
                    return None

                query = "SELECT * FROM worker_pool_tasks WHERE state='ready'"
                params: tuple[Any, ...] = ()
                if run_id:
                    query += " AND run_id=?"
                    params = (run_id,)
                query += " ORDER BY rowid ASC LIMIT 1"
                row = conn.execute(query, params).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None

                conn.execute(
                    """
                    UPDATE worker_pool_tasks
                    SET state='claimed', worker_id=?, attempt=attempt+1, claimed_at=?, heartbeat_at=?, updated_at=?
                    WHERE task_id=? AND state='ready'
                    """,
                    (worker_id, now, now, now, row["task_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO worker_pool_workers(worker_id, status, active_tasks, last_heartbeat)
                    VALUES (?, 'busy', 1, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        status='busy',
                        active_tasks=worker_pool_workers.active_tasks+1,
                        last_heartbeat=excluded.last_heartbeat
                    """,
                    (worker_id, now),
                )
                conn.execute("COMMIT")
                return {
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "depends_on": json.loads(row["depends_on_json"]),
                    "payload": json.loads(row["payload_json"]),
                    "attempt": row["attempt"] + 1,
                }
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_running(self, task_id: str, worker_id: str) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE worker_pool_tasks SET state='running', heartbeat_at=?, updated_at=? WHERE task_id=? AND worker_id=?",
                    (now, now, task_id, worker_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def complete_task(self, task_id: str, worker_id: str, result: Any = None) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT run_id FROM worker_pool_tasks WHERE task_id=?", (task_id,)).fetchone()
                conn.execute(
                    "UPDATE worker_pool_tasks SET state='completed', result_json=?, updated_at=? WHERE task_id=?",
                    (json.dumps(result if result is not None else {}), now, task_id),
                )
                conn.execute(
                    """
                    UPDATE worker_pool_workers
                    SET active_tasks=MAX(0, active_tasks-1),
                        status=CASE WHEN active_tasks-1 <= 0 THEN 'idle' ELSE 'busy' END,
                        last_heartbeat=?
                    WHERE worker_id=?
                    """,
                    (now, worker_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        if row is not None:
            self.refresh_ready_tasks(row["run_id"])

    def fail_task(self, task_id: str, worker_id: str, error: str) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE worker_pool_tasks SET state='failed', error=?, updated_at=? WHERE task_id=?",
                    (error, now, task_id),
                )
                conn.execute(
                    """
                    UPDATE worker_pool_workers
                    SET active_tasks=MAX(0, active_tasks-1),
                        status=CASE WHEN active_tasks-1 <= 0 THEN 'idle' ELSE 'busy' END,
                        last_heartbeat=?
                    WHERE worker_id=?
                    """,
                    (now, worker_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def reap_stale_workers(self, timeout_seconds: float = 30.0) -> int:
        """Mark stale workers and requeue their claimed/running tasks as ready."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        now = _now_iso()
        requeued = 0
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                stale_rows = conn.execute(
                    "SELECT worker_id FROM worker_pool_workers WHERE status != 'stopped' AND last_heartbeat < ?",
                    (cutoff,),
                ).fetchall()
                stale_ids = [r["worker_id"] for r in stale_rows]
                for wid in stale_ids:
                    conn.execute(
                        "UPDATE worker_pool_workers SET status='stale', active_tasks=0 WHERE worker_id=?",
                        (wid,),
                    )
                    cur = conn.execute(
                        """
                        UPDATE worker_pool_tasks
                        SET state='ready', worker_id='', updated_at=?
                        WHERE worker_id=? AND state IN ('claimed','running')
                        """,
                        (now, wid),
                    )
                    requeued += cur.rowcount
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return requeued

    def pause_run(self, run_id: str) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE worker_pool_tasks SET state='paused', updated_at=? WHERE run_id=? AND state IN ('pending','ready')",
                    (now, run_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def resume_run(self, run_id: str) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE worker_pool_tasks SET state='pending', updated_at=? WHERE run_id=? AND state='paused'",
                    (now, run_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        self.refresh_ready_tasks(run_id)

    def cancel_run(self, run_id: str) -> None:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "UPDATE worker_pool_tasks SET state='cancelled', updated_at=? WHERE run_id=? AND state IN ('pending','ready','paused')",
                    (now, run_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def run_parallel(
        self,
        run_id: str,
        executor_fn: Callable[[dict[str, Any], str], Any],
    ) -> dict[str, str]:
        """Execute all ready/pending tasks for run_id using up to max_concurrent_workers threads."""
        workers = [f"worker-{i + 1}" for i in range(self.max_concurrent_workers)]
        for w in workers:
            self.register_worker(w)

        def _worker_loop(worker_id: str) -> None:
            while True:
                task = self.claim_next_task(worker_id, run_id=run_id)
                if task is None:
                    with closing(_connect(self.db_path)) as conn:
                        remaining = conn.execute(
                            "SELECT COUNT(*) AS cnt FROM worker_pool_tasks WHERE run_id=? AND state IN ('pending','ready','claimed','running')",
                            (run_id,),
                        ).fetchone()["cnt"]
                    if remaining == 0:
                        return
                    threading.Event().wait(0.02)
                    continue
                self.mark_running(task["task_id"], worker_id)
                try:
                    res = executor_fn(task, worker_id)
                    self.complete_task(task["task_id"], worker_id, res)
                except Exception as exc:
                    self.fail_task(task["task_id"], worker_id, str(exc))

        with ThreadPoolExecutor(max_workers=self.max_concurrent_workers) as pool:
            futures = [pool.submit(_worker_loop, w) for w in workers]
            for fut in as_completed(futures):
                fut.result()

        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT task_id, state FROM worker_pool_tasks WHERE run_id=?",
                (run_id,),
            ).fetchall()
        return {r["task_id"]: r["state"] for r in rows}
