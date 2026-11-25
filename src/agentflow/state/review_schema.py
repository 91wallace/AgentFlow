"""Additive v9 review journal. Existing task/attempt state remains authoritative."""

REVIEW_SCHEMA = (
    "CREATE TABLE review_sessions(operation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES apl_authorized_runs(run_id), cycle_id TEXT NOT NULL REFERENCES apl_cycles(cycle_id), job_id TEXT NOT NULL, attempt_id TEXT NOT NULL, binding_json TEXT NOT NULL, baseline_json TEXT NOT NULL, baseline_digest TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(run_id,job_id) REFERENCES apl_authorized_run_tasks(run_id,job_id))",
    "CREATE TABLE review_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL REFERENCES review_sessions(operation_id), event_key TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(operation_id,event_key))",
    "CREATE INDEX review_history ON review_events(operation_id,event_id)",
    *(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'review history is immutable'); END"
      for table in ("review_sessions", "review_events") for verb in ("UPDATE", "DELETE")),
)
