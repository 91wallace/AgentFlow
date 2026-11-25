"""Role metadata in the existing operational database; no task queue here."""

import json
import sqlite3
import time
from contextlib import closing

from agentflow.agents.roles import ExecutionRoles
from agentflow.planning import canonical, digest

from .contracts import ConflictError, IntegrityError
from .sqlite import SQLiteStore, now


class RoleConfigurationStore(SQLiteStore):
    def __init__(self, path):
        super().__init__(path, execution=True, git=True, validation=True,
                         agent_models=True, sdd=True, roles=True)

    def request(self, request_id, input_data):
        encoded, fingerprint = canonical(input_data), digest(input_data)
        with self.transaction() as db:
            prior = db.execute("SELECT input_digest,input_json FROM role_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if prior is not None:
                if prior != (fingerprint, encoded):
                    raise ConflictError("role_request_binding_conflict")
            else:
                db.execute("INSERT INTO role_requests VALUES(?,?,?,?)", (request_id, encoded, fingerprint, now()))
                self._event(db, request_id, "AWAITING_CONFIGURATION", {})
        return self.configuration(request_id)

    @staticmethod
    def _event(db, request_id, kind, data):
        db.execute("INSERT INTO role_request_events(request_id,kind,payload_json,created_at) VALUES(?,?,?,?)",
                   (request_id, kind, canonical(data), now()))

    def record_inventory(self, request_id, reports):
        with self.transaction() as db:
            self._event(db, request_id, "DISCOVERY", reports)

    def draft_selections(self, request_id):
        selected = {}
        for encoded, in self.db.execute(
                "SELECT payload_json FROM role_request_events WHERE request_id=? AND kind='ROLE_SELECTED' ORDER BY event_id",
                (request_id,)):
            value = json.loads(encoded)
            selected[value["role"]] = value["binding"]
        return selected

    def save_draft(self, request_id, role, binding):
        from dataclasses import asdict
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM role_configurations WHERE request_id=?", (request_id,)).fetchone():
                raise ConflictError("role_configuration_immutable")
            self._event(db, request_id, "ROLE_SELECTED", {"role": role, "binding": asdict(binding)})

    def configure(self, request_id, roles):
        encoded, fingerprint = canonical(roles.to_mapping()), digest(roles.to_mapping())
        with self.transaction() as db:
            prior = db.execute("SELECT snapshot_json,snapshot_digest FROM role_configurations WHERE request_id=?",
                               (request_id,)).fetchone()
            if prior is not None:
                if prior != (encoded, fingerprint):
                    raise ConflictError("role_configuration_immutable")
            else:
                db.execute("INSERT INTO role_configurations VALUES(?,?,?,?)", (request_id, encoded, fingerprint, now()))
                self._event(db, request_id, "CONFIGURED", {"roles_digest": fingerprint})
        return roles

    def configuration(self, request_id):
        row = self.db.execute("SELECT snapshot_json,snapshot_digest FROM role_configurations WHERE request_id=?",
                              (request_id,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
            if canonical(data) != row[0] or digest(data) != row[1]:
                raise ValueError("role digest mismatch")
            return ExecutionRoles.from_mapping(data)
        except (ValueError, TypeError, KeyError) as exc:
            raise IntegrityError("role_configuration_corrupt") from exc

    def input_data(self, request_id):
        row = self.db.execute("SELECT input_json,input_digest FROM role_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise IntegrityError("role_request_missing")
        value = json.loads(row[0])
        if canonical(value) != row[0] or digest(value) != row[1]:
            raise IntegrityError("role_request_corrupt")
        return value

    def bind_cycle(self, request_id, cycle_id):
        with self.transaction() as db:
            rows = db.execute("SELECT cycle_id,request_id FROM role_cycle_bindings WHERE cycle_id=? OR request_id=?",
                              (cycle_id, request_id)).fetchall()
            if rows and rows != [(cycle_id, request_id)]:
                raise ConflictError("role_cycle_binding_conflict")
            db.execute("INSERT OR IGNORE INTO role_cycle_bindings VALUES(?,?)", (cycle_id, request_id))

    def for_cycle(self, cycle_id):
        row = self.db.execute("SELECT request_id FROM role_cycle_bindings WHERE cycle_id=?", (cycle_id,)).fetchone()
        return (row[0], self.configuration(row[0])) if row else None

    def call(self, request_id, kind, input_data, invoke, *, on_claimed=None):
        fingerprint = digest(input_data)
        call_id = digest({"request_id": request_id, "kind": kind, "input_digest": fingerprint})
        with self.transaction() as db:
            prior = db.execute("SELECT result_json,result_digest FROM role_call_results WHERE call_id=?", (call_id,)).fetchone()
            if prior is not None:
                result = json.loads(prior[0])
                if canonical(result) != prior[0] or digest(result) != prior[1]:
                    raise IntegrityError("orchestrator_result_corrupt")
                return result
            if db.execute("SELECT 1 FROM role_calls WHERE call_id=?", (call_id,)).fetchone():
                raise ConflictError("orchestrator_call_unknown_or_running: automatic replay forbidden")
            db.execute("INSERT INTO role_calls VALUES(?,?,?,?,?,?)",
                       (call_id, request_id, kind, fingerprint, canonical(input_data), now()))
        # Intent is durable BEFORE calling a model. Ambiguous interrupted calls
        # remain claimed; no lease expiry silently reissues them.
        started = time.monotonic()
        if on_claimed is not None:
            on_claimed(call_id)
        result = invoke()
        encoded = canonical(result)
        with self.transaction() as db:
            db.execute("INSERT INTO role_call_results VALUES(?,?,?,?,?)",
                       (call_id, encoded, digest(result), max(0.0, time.monotonic() - started), now()))
        return result


def roles_for_cycle(path, cycle_id):
    """Inspect role metadata without migrating legacy databases."""
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='role_cycle_bindings'").fetchone():
            return None
        row = db.execute("SELECT b.request_id,c.snapshot_json,c.snapshot_digest FROM role_cycle_bindings b "
                         "JOIN role_configurations c USING(request_id) WHERE b.cycle_id=?", (cycle_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row[1])
        if canonical(data) != row[1] or digest(data) != row[2]:
            raise IntegrityError("role_configuration_corrupt")
        return row[0], ExecutionRoles.from_mapping(data)


def request_input(path, request_id):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT input_json,input_digest FROM role_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise IntegrityError("role_request_missing")
        data = json.loads(row[0])
        if canonical(data) != row[0] or digest(data) != row[1]:
            raise IntegrityError("role_request_corrupt")
        return data


class RoleCallJournal:
    """Short-lived connections around calls, including after a client restart."""

    def __init__(self, path):
        self.path = path

    def call(self, *args, **kwargs):
        with RoleConfigurationStore(self.path) as store:
            return store.call(*args, **kwargs)

    def completed_decision_seconds(self, request_id):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute(
                "SELECT COALESCE(SUM(r.duration_seconds),0) FROM role_call_results r "
                "JOIN role_calls c USING(call_id) WHERE c.request_id=? AND c.kind='result_decision'",
                (request_id,)).fetchone()[0]

    def usage(self, request_id, prefix):
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(c.input_json AS BLOB))+"
                "COALESCE(LENGTH(CAST(r.result_json AS BLOB)),0)),0),COALESCE(SUM(r.duration_seconds),0) "
                "FROM role_calls c LEFT JOIN role_call_results r USING(call_id) "
                "WHERE c.request_id=? AND substr(c.kind,1,?)=?", (request_id, len(prefix), prefix)).fetchone()
            return {"calls": row[0], "payload_bytes": row[1], "seconds": row[2]}
