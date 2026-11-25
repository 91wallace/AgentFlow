"""Managed worker metadata shares the existing operational SQLite database."""

WORKER_SCHEMA = (
    "CREATE TABLE managed_workers(workspace_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL UNIQUE, state_db TEXT NOT NULL, active_run_id TEXT, root_path TEXT NOT NULL, root_identity TEXT NOT NULL, pid INTEGER, process_marker TEXT, started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE managed_worker_runs(run_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES managed_workers(workspace_id), start_digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','running','pause_requested','paused','cancel_requested','cancelled','completed','failed','blocked','unknown')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE INDEX managed_worker_runs_by_workspace ON managed_worker_runs(workspace_id,created_at)",
    "CREATE TABLE worker_commands(command_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES managed_workers(workspace_id), kind TEXT NOT NULL CHECK(kind IN ('start','resume','pause','cancel')), payload_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed')), result_json TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)",
    "CREATE INDEX worker_commands_queue ON worker_commands(workspace_id,state,created_at)",
)
