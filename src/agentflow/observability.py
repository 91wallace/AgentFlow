"""Live Execution Observability (K): structured events, heartbeats, and reconnectable cursors."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


EVENT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS live_execution_events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        worker_id TEXT NOT NULL DEFAULT '',
        task_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_live_events_run_seq ON live_execution_events(run_id, seq)",
)


class LiveEventBus:
    """Append-only SQLite event stream supporting incremental polling and reconnect recovery."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in EVENT_SCHEMA:
                    conn.execute(stmt)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def emit(
        self,
        run_id: str,
        event_type: str,
        *,
        worker_id: str = "",
        task_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> int:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    INSERT INTO live_execution_events(run_id, event_type, worker_id, task_id, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, event_type, worker_id, task_id, json.dumps(payload or {}), now),
                )
                seq = int(cur.lastrowid)
                conn.execute("COMMIT")
                return seq
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def fetch_events(self, run_id: str, *, since_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT seq, run_id, event_type, worker_id, task_id, payload_json, created_at
                FROM live_execution_events
                WHERE run_id=? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (run_id, since_seq, limit),
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "run_id": r["run_id"],
                "event_type": r["event_type"],
                "worker_id": r["worker_id"],
                "task_id": r["task_id"],
                "payload": json.loads(r["payload_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
