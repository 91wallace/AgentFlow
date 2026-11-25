"""Shared context registry and incremental delta tracking isolated by project and run."""

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


CONTEXT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS shared_context_deltas(
        version INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shared_context_scope ON shared_context_deltas(project_id, run_id, version)",
)


class SharedContextRegistry:
    """Stores incremental context updates isolated by (project_id, run_id)."""

    def __init__(self, db_path: Path, project_id: str, run_id: str) -> None:
        self.db_path = Path(db_path)
        self.project_id = project_id
        self.run_id = run_id
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stmt in CONTEXT_SCHEMA:
                    conn.execute(stmt)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def put_delta(self, key: str, value: Any) -> int:
        now = _now_iso()
        with closing(_connect(self.db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    INSERT INTO shared_context_deltas(project_id, run_id, key, value_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.project_id, self.run_id, key, json.dumps(value), now),
                )
                version = int(cur.lastrowid)
                conn.execute("COMMIT")
                return version
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_deltas(self, since_version: int = 0) -> list[dict[str, Any]]:
        with closing(_connect(self.db_path)) as conn:
            rows = conn.execute(
                """
                SELECT version, key, value_json, updated_at
                FROM shared_context_deltas
                WHERE project_id=? AND run_id=? AND version > ?
                ORDER BY version ASC
                """,
                (self.project_id, self.run_id, since_version),
            ).fetchall()
        return [
            {
                "version": r["version"],
                "key": r["key"],
                "value": json.loads(r["value_json"]),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]
