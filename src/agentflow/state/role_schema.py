"""Additive schema v8: role configuration and journaled orchestrator calls.

Tasks, attempts, cycles and execution outcomes retain their existing tables.
"""

ROLE_SCHEMA = (
    "CREATE TABLE role_requests(request_id TEXT PRIMARY KEY, input_json TEXT NOT NULL, input_digest TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE role_configurations(request_id TEXT PRIMARY KEY REFERENCES role_requests(request_id), snapshot_json TEXT NOT NULL, snapshot_digest TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE role_request_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL REFERENCES role_requests(request_id), kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE role_cycle_bindings(cycle_id TEXT PRIMARY KEY REFERENCES apl_cycles(cycle_id), request_id TEXT NOT NULL UNIQUE REFERENCES role_configurations(request_id))",
    "CREATE TABLE role_calls(call_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES role_configurations(request_id), kind TEXT NOT NULL, input_digest TEXT NOT NULL, input_json TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE role_call_results(call_id TEXT PRIMARY KEY REFERENCES role_calls(call_id), result_json TEXT NOT NULL, result_digest TEXT NOT NULL, duration_seconds REAL NOT NULL CHECK(duration_seconds>=0), created_at TEXT NOT NULL)",
    "CREATE INDEX role_request_history ON role_request_events(request_id,event_id)",
    *(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'role history is immutable'); END"
      for table in ("role_requests", "role_configurations", "role_request_events", "role_cycle_bindings", "role_calls", "role_call_results")
      for verb in ("UPDATE", "DELETE")),
)
