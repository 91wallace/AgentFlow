"""Review metadata in the existing SQLite, with append-only, idempotent events."""

import json
import sqlite3
from contextlib import closing

from agentflow.planning import canonical, digest
from .contracts import ConflictError, IntegrityError
from .sqlite import SQLiteStore, now


class ReviewStore(SQLiteStore):
    def __init__(self, path):
        super().__init__(path, execution=True, git=True, validation=True,
                         agent_models=True, sdd=True, roles=True, review=True)

    def begin(self, operation_id, cycle_id, job_id, attempt_id, binding, baseline):
        with self.transaction() as db:
            row = db.execute("SELECT binding_json,baseline_digest FROM review_sessions WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                if row != (canonical(binding), digest(baseline)):
                    raise ConflictError("review_baseline_replay_conflict")
                return
            owner = db.execute("SELECT t.run_id,r.cycle_id,t.job_id FROM apl_authorized_run_tasks t "
                               "JOIN apl_authorized_runs r USING(run_id) WHERE t.operation_id=?", (operation_id,)).fetchone()
            if owner is None or owner[1:] != (cycle_id, job_id):
                raise ConflictError("review_task_binding_invalid")
            db.execute("INSERT INTO review_sessions VALUES(?,?,?,?,?,?,?,?,?)", (
                operation_id, owner[0], cycle_id, job_id, attempt_id, canonical(binding), canonical(baseline), digest(baseline), now()))

    def event(self, operation_id, key, state, payload):
        encoded, fingerprint = canonical(payload), digest(payload)
        with self.transaction() as db:
            prior = db.execute("SELECT state,payload_json,payload_digest FROM review_events WHERE operation_id=? AND event_key=?", (operation_id, key)).fetchone()
            if prior:
                if prior != (state, encoded, fingerprint):
                    raise ConflictError("review_event_replay_conflict")
                return payload
            db.execute("INSERT INTO review_events(operation_id,event_key,state,payload_json,payload_digest,created_at) VALUES(?,?,?,?,?,?)",
                       (operation_id, key, state, encoded, fingerprint, now()))
        return payload

    def load(self, operation_id):
        row = self.db.execute("SELECT cycle_id,job_id,attempt_id,binding_json,baseline_json,baseline_digest,run_id FROM review_sessions WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        baseline = json.loads(row[4])
        if digest(baseline) != row[5]:
            raise IntegrityError("review_baseline_corrupt")
        events = []
        for key, state, encoded, fingerprint in self.db.execute(
                "SELECT event_key,state,payload_json,payload_digest FROM review_events WHERE operation_id=? ORDER BY event_id", (operation_id,)):
            payload = json.loads(encoded)
            if canonical(payload) != encoded or digest(payload) != fingerprint:
                raise IntegrityError("review_event_corrupt")
            events.append({"key": key, "state": state, "payload": payload})
        return {"cycle_id": row[0], "job_id": row[1], "attempt_id": row[2], "run_id": row[6],
                "binding": json.loads(row[3]), "baseline": baseline, "events": events}


class ReviewJournal:
    def __init__(self, path):
        self.path = path

    def begin(self, *args):
        with ReviewStore(self.path) as store:
            return store.begin(*args)

    def event(self, *args):
        with ReviewStore(self.path) as store:
            return store.event(*args)

    def load(self, operation_id):
        with ReviewStore(self.path) as store:
            return store.load(operation_id)

    def owned_paths(self, cycle_id, current):
        owned = set()
        with ReviewStore(self.path) as store:
            rows = store.db.execute("SELECT s.operation_id FROM review_sessions s JOIN apl_authorized_run_tasks t "
                "ON t.operation_id=s.operation_id WHERE s.cycle_id=? AND t.status='completed'", (cycle_id,)).fetchall()
            for operation_id, in rows:
                session = store.load(operation_id)
                if not session["events"] or session["events"][-1]["state"] != "APPROVED":
                    continue
                snapshot = next((event["payload"] for event in reversed(session["events"]) if event["key"].startswith("snapshot-")), None)
                if not snapshot or any(snapshot["snapshot"][key] != current[key] for key in ("head", "head_ref", "root_identity")):
                    continue
                for change in snapshot["changes"]:
                    name = change["path"]
                    if (change["attribution"] == "executor_candidate" and change["after"] is not None
                            and current["files"].get(name) == change["after"]
                            and current["index"].get(name) == snapshot["snapshot"]["index"].get(name)):
                        owned.add(name)
        return owned


def inspect_review(path, operation_id):
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='review_sessions' AND type='table'").fetchone():
            return None
        row = db.execute("SELECT cycle_id,job_id,attempt_id,baseline_digest,run_id FROM review_sessions WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            return None
        events = []
        for state, payload, fingerprint in db.execute("SELECT state,payload_json,payload_digest FROM review_events WHERE operation_id=? ORDER BY event_id", (operation_id,)):
            value = json.loads(payload)
            if canonical(value) != payload or digest(value) != fingerprint:
                raise IntegrityError("review_event_corrupt")
            events.append({"state": state, "payload": value})
        calls = [{"call_id": call_id, "kind": kind, "complete": bool(complete)}
                 for call_id, kind, complete in db.execute("SELECT c.call_id,c.kind,r.call_id IS NOT NULL FROM role_calls c "
                    "LEFT JOIN role_call_results r USING(call_id) WHERE substr(c.kind,1,?)=?",
                    (len("review." + operation_id + "."), "review." + operation_id + "."))]
        finalized = db.execute("SELECT 1 FROM review_events WHERE operation_id=? AND event_key='final'", (operation_id,)).fetchone()
        return {"operation_id": operation_id, "cycle_id": row[0], "job_id": row[1], "run_id": row[4],
                "attempt_id": row[2], "baseline_digest": row[3], "events": events,
                "calls": calls, "pending_or_unknown": not finalized or any(not call["complete"] for call in calls),
                "state": events[-1]["state"] if events else "BASELINED"}
