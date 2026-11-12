"""Namespaced APL persistence in a caller-provided operational SQLite database."""

from dataclasses import asdict
import json
import sqlite3
from threading import RLock
from uuid import uuid4
from contextlib import contextmanager

from .contracts import APLState, CycleSnapshot, JobProposal, ExecutionOutcome


class SQLiteCycleStore:
    """Append-only snapshots. Use a dedicated AgentFlow operational DB connection/path."""

    def __init__(self, path, *, initialize=True):
        self.path = str(path)
        self._lock = RLock()
        with self._connect(readonly=not initialize) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "schema_migrations" not in tables:
                raise RuntimeError("initialize the AgentFlow State Store before opening APL storage")
            if initialize:
                db.execute("CREATE TABLE IF NOT EXISTS apl_cycles(cycle_id TEXT PRIMARY KEY, version INTEGER NOT NULL, revision INTEGER NOT NULL, snapshot_json TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS apl_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL, snapshot_digest TEXT NOT NULL, UNIQUE(cycle_id,revision), FOREIGN KEY(cycle_id) REFERENCES apl_cycles(cycle_id))")
                db.execute("CREATE TABLE IF NOT EXISTS apl_result_refs(attempt_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, job_id TEXT NOT NULL, FOREIGN KEY(cycle_id) REFERENCES apl_cycles(cycle_id))")
                db.execute("CREATE TABLE IF NOT EXISTS apl_plan_revisions(cycle_id TEXT NOT NULL, sequence INTEGER NOT NULL, revision_id TEXT NOT NULL UNIQUE, proposal_id TEXT UNIQUE, plan_json TEXT NOT NULL, plan_digest TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(cycle_id,sequence), FOREIGN KEY(cycle_id) REFERENCES apl_cycles(cycle_id))")
                db.execute("CREATE TABLE IF NOT EXISTS apl_plan_proposals(proposal_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, request_digest TEXT NOT NULL, source_revision_id TEXT NOT NULL, source_cycle_revision INTEGER NOT NULL, proposal_json TEXT NOT NULL, proposal_digest TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(cycle_id,request_digest), FOREIGN KEY(cycle_id) REFERENCES apl_cycles(cycle_id))")
                db.execute("CREATE TABLE IF NOT EXISTS apl_proposal_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','stale')), occurred_at TEXT NOT NULL, FOREIGN KEY(proposal_id) REFERENCES apl_plan_proposals(proposal_id))")
                db.execute("CREATE TABLE IF NOT EXISTS apl_planner_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, cycle_revision INTEGER NOT NULL, request_digest TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('started','succeeded','failed')), detail TEXT NOT NULL, occurred_at TEXT NOT NULL, FOREIGN KEY(cycle_id) REFERENCES apl_cycles(cycle_id))")
                db.execute("CREATE TABLE IF NOT EXISTS apl_planner_requests(cycle_id TEXT NOT NULL, request_digest TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('started','succeeded','failed')), PRIMARY KEY(cycle_id,request_digest), FOREIGN KEY(cycle_id) REFERENCES apl_cycles(cycle_id))")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_plan_revisions_no_update BEFORE UPDATE ON apl_plan_revisions BEGIN SELECT RAISE(ABORT,'APL plan revisions are immutable'); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_plan_revisions_no_delete BEFORE DELETE ON apl_plan_revisions BEGIN SELECT RAISE(ABORT,'APL plan revisions are immutable'); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_plan_proposals_no_update BEFORE UPDATE ON apl_plan_proposals BEGIN SELECT RAISE(ABORT,'APL proposals are immutable'); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_plan_proposals_no_delete BEFORE DELETE ON apl_plan_proposals BEGIN SELECT RAISE(ABORT,'APL proposals are immutable'); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_proposal_events_no_update BEFORE UPDATE ON apl_proposal_events BEGIN SELECT RAISE(ABORT,'APL proposal events are append-only'); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_proposal_events_no_delete BEFORE DELETE ON apl_proposal_events BEGIN SELECT RAISE(ABORT,'APL proposal events are append-only'); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_planner_events_no_update BEFORE UPDATE ON apl_planner_events BEGIN SELECT RAISE(ABORT,'APL planner events are append-only'); END")
                db.execute("CREATE TRIGGER IF NOT EXISTS apl_planner_events_no_delete BEFORE DELETE ON apl_planner_events BEGIN SELECT RAISE(ABORT,'APL planner events are append-only'); END")
            elif not {"apl_cycles", "apl_events"} <= tables:
                raise RuntimeError("APL storage is not initialized")

    @contextmanager
    def _connect(self, readonly=False):
        if readonly:
            from pathlib import Path
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
            db.execute("PRAGMA query_only=ON")
        else:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def _encode(snapshot):
        data = asdict(snapshot)
        data["state"] = snapshot.state.value
        data["jobs"] = [asdict(j) for j in snapshot.jobs]
        data["outcomes"] = [[job, asdict(outcome)] for job, outcome in snapshot.outcomes]
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _decode(encoded):
        d = json.loads(encoded)
        jobs = tuple(JobProposal(j["job_id"], j["instruction"], tuple(j["depends_on"]), tuple(j["required_scope"]), tuple(j["acceptance_criteria"])) for j in d["jobs"])
        outcomes = tuple((job, ExecutionOutcome(o["status"], o["summary"], tuple(o["evidence"]), o["cost"], o["progress_key"])) for job, o in d["outcomes"])
        return CycleSnapshot(d["cycle_id"], d["objective"], tuple(d["authorized_scope"]), APLState(d["state"]),
                             jobs, outcomes,
                             d["iterations"], d["consumed_budget"], d["version"], d["reason"],
                             d.get("revision", 0), d.get("plan_digest", ""),
                             d.get("plan_revision_id", ""))

    def create(self, snapshot):
        from dataclasses import replace
        from datetime import datetime, timezone
        revision_id = snapshot.plan_revision_id or uuid4().hex
        snapshot = replace(snapshot, plan_revision_id=revision_id)
        encoded = self._encode(snapshot)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("INSERT INTO apl_cycles VALUES(?,?,?,?)", (snapshot.cycle_id, snapshot.version, 0, encoded))
                plan_json = json.dumps([asdict(job) for job in snapshot.jobs], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                plan_hash = __import__('hashlib').sha256(json.dumps({"source": snapshot.plan_digest, "jobs": json.loads(plan_json)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                db.execute("INSERT INTO apl_plan_revisions VALUES(?,?,?,?,?,?,?)", (snapshot.cycle_id, 1, revision_id, None, plan_json, plan_hash, datetime.now(timezone.utc).isoformat()))
                db.execute("INSERT INTO apl_events(cycle_id,revision,state,snapshot_digest) VALUES(?,?,?,?)", (snapshot.cycle_id, 0, snapshot.state.value, __import__('hashlib').sha256(encoded.encode()).hexdigest()))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return snapshot

    @staticmethod
    def _proposal_to_json(proposal):
        data = asdict(proposal)
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _proposal_from_json(encoded):
        from .contracts import PlanProposal, TaskChange
        d = json.loads(encoded)
        changes = tuple(TaskChange(c["action"], JobProposal(c["task"]["job_id"], c["task"]["instruction"],
                              tuple(c["task"]["depends_on"]), tuple(c["task"]["required_scope"]),
                              tuple(c["task"]["acceptance_criteria"]))) for c in d["changes"])
        return PlanProposal(d["proposal_id"], d["source_revision_id"], d["source_cycle_revision"], d["rationale"], changes,
                            d["completion_condition"], tuple(d["evidence_used"]),
                            tuple(d["resolves_blockers"]), d["schema_version"])

    def propose_plan(self, cycle_id, proposal, request_digest, proposal_digest, expected_revision):
        from datetime import datetime, timezone
        encoded = self._proposal_to_json(proposal)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT proposal_id,proposal_json,proposal_digest FROM apl_plan_proposals WHERE cycle_id=? AND request_digest=?", (cycle_id, request_digest)).fetchone()
                if existing:
                    if existing[2] != proposal_digest:
                        raise RuntimeError("idempotent replan request produced a divergent proposal")
                    db.commit()
                    return self._proposal_from_json(existing[1]), self.proposal_status(existing[0]), False
                snapshot = db.execute("SELECT snapshot_json FROM apl_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
                if snapshot is None:
                    raise KeyError(cycle_id)
                current = self._decode(snapshot[0])
                if (current.plan_revision_id != proposal.source_revision_id
                        or current.revision != expected_revision
                        or proposal.source_cycle_revision != expected_revision):
                    raise RuntimeError("proposal source revision is stale")
                db.execute("INSERT INTO apl_plan_proposals VALUES(?,?,?,?,?,?,?,?)",
                           (proposal.proposal_id, cycle_id, request_digest, proposal.source_revision_id,
                            proposal.source_cycle_revision, encoded, proposal_digest,
                            datetime.now(timezone.utc).isoformat()))
                db.execute("INSERT INTO apl_proposal_events(proposal_id,status,occurred_at) VALUES(?,?,?)",
                           (proposal.proposal_id, "proposed", datetime.now(timezone.utc).isoformat()))
                db.commit()
                return proposal, "proposed", True
            except Exception:
                db.rollback()
                raise

    def proposal_for_request(self, cycle_id, request_digest):
        with self._connect(readonly=True) as db:
            row = db.execute("SELECT proposal_id,proposal_json,proposal_digest FROM apl_plan_proposals WHERE cycle_id=? AND request_digest=?",
                             (cycle_id, request_digest)).fetchone()
        if row is None:
            return None
        return self._proposal_from_json(row[1]), self.proposal_status(row[0]), row[2]

    def claim_planner_request(self, cycle_id, cycle_revision, request_digest, provider):
        from datetime import datetime, timezone
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT status FROM apl_planner_requests WHERE cycle_id=? AND request_digest=?",
                                   (cycle_id, request_digest)).fetchone()
                if prior:
                    db.rollback()
                    raise RuntimeError(f"planner request already {prior[0]}; use a new request id after review")
                now = datetime.now(timezone.utc).isoformat()
                db.execute("INSERT INTO apl_planner_requests VALUES(?,?,?,'started')",
                           (cycle_id, request_digest, provider))
                db.execute("INSERT INTO apl_planner_events(cycle_id,cycle_revision,request_digest,provider,status,detail,occurred_at) VALUES(?,?,?,?,?,?,?)",
                           (cycle_id, cycle_revision, request_digest, provider, "started", "", now))
                db.commit()
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise

    def finish_planner_request(self, cycle_id, cycle_revision, request_digest, provider, status, detail):
        from datetime import datetime, timezone
        if status not in {"succeeded", "failed"}:
            raise ValueError("invalid terminal planner request status")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute("UPDATE apl_planner_requests SET status=? WHERE cycle_id=? AND request_digest=? AND status='started'",
                                    (status, cycle_id, request_digest))
                if cursor.rowcount != 1:
                    raise RuntimeError("planner request is not in started state")
                db.execute("INSERT INTO apl_planner_events(cycle_id,cycle_revision,request_digest,provider,status,detail,occurred_at) VALUES(?,?,?,?,?,?,?)",
                           (cycle_id, cycle_revision, request_digest, provider, status, detail,
                            datetime.now(timezone.utc).isoformat()))
                db.commit()
            except Exception:
                db.rollback()
                raise

    def planner_events(self, cycle_id):
        try:
            with self._connect(readonly=True) as db:
                return tuple(dict(zip(("event_id", "cycle_revision", "request_digest", "provider", "status", "detail", "occurred_at"), row))
                             for row in db.execute("SELECT event_id,cycle_revision,request_digest,provider,status,detail,occurred_at FROM apl_planner_events WHERE cycle_id=? ORDER BY event_id", (cycle_id,)))
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return ()
            raise

    def proposal_status(self, proposal_id):
        with self._connect() as db:
            row = db.execute("SELECT status FROM apl_proposal_events WHERE proposal_id=? ORDER BY event_id DESC LIMIT 1", (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return row[0]

    def proposals(self, cycle_id):
        from .replanning import proposal_digest
        with self._connect() as db:
            rows = db.execute("SELECT p.proposal_json,p.proposal_digest,(SELECT e.status FROM apl_proposal_events e WHERE e.proposal_id=p.proposal_id ORDER BY e.event_id DESC LIMIT 1) FROM apl_plan_proposals p WHERE p.cycle_id=? ORDER BY p.created_at,p.proposal_id", (cycle_id,)).fetchall()
        decoded = []
        for row in rows:
            proposal = self._proposal_from_json(row[0])
            if proposal_digest(proposal) != row[1]:
                raise RuntimeError("stored APL proposal digest mismatch")
            decoded.append((proposal, row[1], row[2]))
        return tuple(decoded)

    def plan_revisions(self, cycle_id):
        with self._connect() as db:
            return tuple(db.execute("SELECT revision_id,sequence,proposal_id,plan_digest FROM apl_plan_revisions WHERE cycle_id=? ORDER BY sequence", (cycle_id,)))

    def reject_proposal(self, cycle_id, proposal_id):
        from datetime import datetime, timezone
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT cycle_id FROM apl_plan_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
                if row != (cycle_id,):
                    raise KeyError(proposal_id)
                status = self.proposal_status(proposal_id)
                if status == "proposed":
                    db.execute("INSERT INTO apl_proposal_events(proposal_id,status,occurred_at) VALUES(?,?,?)",
                               (proposal_id, "rejected", datetime.now(timezone.utc).isoformat()))
                elif status != "rejected":
                    raise RuntimeError(f"proposal is already {status}")
                db.commit()
                return status == "proposed"
            except Exception:
                db.rollback()
                raise

    def accept_proposal(self, snapshot, proposal, jobs, proposal_digest, expected_revision):
        from dataclasses import replace
        from datetime import datetime, timezone
        from .contracts import APLState
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT revision,snapshot_json FROM apl_cycles WHERE cycle_id=?", (snapshot.cycle_id,)).fetchone()
                if row is None:
                    raise KeyError(snapshot.cycle_id)
                current = self._decode(row[1])
                status = self.proposal_status(proposal.proposal_id)
                if row[0] != expected_revision or current.revision != expected_revision:
                    raise RuntimeError("concurrent APL plan update")
                if (current.plan_revision_id != proposal.source_revision_id
                        or current.revision != proposal.source_cycle_revision or status != "proposed"):
                    raise RuntimeError("proposal is stale or no longer proposed")
                revision_id = uuid4().hex
                sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM apl_plan_revisions WHERE cycle_id=?", (snapshot.cycle_id,)).fetchone()[0]
                plan_json = json.dumps([asdict(job) for job in jobs], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                plan_hash = __import__('hashlib').sha256(plan_json.encode()).hexdigest()
                updated_state = APLState.PAUSED if current.state == APLState.PAUSED else APLState.READY
                updated = replace(snapshot, jobs=jobs, plan_revision_id=revision_id, plan_digest=plan_hash,
                                   state=updated_state, revision=expected_revision + 1,
                                   reason=current.reason if updated_state == APLState.PAUSED else "")
                encoded = self._encode(updated)
                if db.execute("UPDATE apl_cycles SET revision=?,snapshot_json=? WHERE cycle_id=? AND revision=?",
                              (updated.revision, encoded, snapshot.cycle_id, expected_revision)).rowcount != 1:
                    raise RuntimeError("concurrent APL plan update")
                db.execute("INSERT INTO apl_plan_revisions VALUES(?,?,?,?,?,?,?)",
                           (snapshot.cycle_id, sequence, revision_id, proposal.proposal_id, plan_json, plan_hash, datetime.now(timezone.utc).isoformat()))
                db.execute("INSERT INTO apl_proposal_events(proposal_id,status,occurred_at) VALUES(?,?,?)",
                           (proposal.proposal_id, "accepted", datetime.now(timezone.utc).isoformat()))
                db.execute("INSERT INTO apl_events(cycle_id,revision,state,snapshot_digest) VALUES(?,?,?,?)",
                           (snapshot.cycle_id, updated.revision, updated.state.value, __import__('hashlib').sha256(encoded.encode()).hexdigest()))
                db.commit()
                return updated
            except Exception:
                db.rollback()
                raise

    def load(self, cycle_id):
        with self._connect() as db:
            row = db.execute("SELECT snapshot_json FROM apl_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise KeyError(cycle_id)
        return self._decode(row[0])

    def revision(self, cycle_id):
        with self._connect() as db:
            row = db.execute("SELECT revision FROM apl_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise KeyError(cycle_id)
        return row[0]

    def list_cycles(self):
        with self._connect() as db:
            rows = db.execute("SELECT snapshot_json FROM apl_cycles ORDER BY cycle_id").fetchall()
        return tuple(self._decode(row[0]) for row in rows)

    def history(self, cycle_id):
        with self._connect() as db:
            rows = db.execute("SELECT revision,state,snapshot_digest FROM apl_events WHERE cycle_id=? ORDER BY revision", (cycle_id,)).fetchall()
        if not rows:
            raise KeyError(cycle_id)
        return tuple({"revision": row[0], "state": row[1], "snapshot_digest": row[2]} for row in rows)

    def result_used(self, attempt_id):
        with self._connect() as db:
            try:
                return db.execute("SELECT cycle_id,job_id FROM apl_result_refs WHERE attempt_id=?", (attempt_id,)).fetchone()
            except sqlite3.OperationalError:
                return None

    def save(self, snapshot, expected_iterations, expected_revision=None, result_ref=None):
        encoded = self._encode(snapshot)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT revision,snapshot_json FROM apl_cycles WHERE cycle_id=?", (snapshot.cycle_id,)).fetchone()
                if row is None:
                    raise KeyError(snapshot.cycle_id)
                current = self._decode(row[1])
                expected = snapshot.revision if expected_revision is None else expected_revision
                if (current.iterations != expected_iterations or current.revision != row[0]
                        or row[0] != expected
                        or (expected_revision is None and current.state in {APLState.COMPLETED, APLState.BLOCKED, APLState.FAILED, APLState.CANCELLED, APLState.WAITING_APPROVAL})):
                    raise RuntimeError("concurrent or duplicate APL cycle update")
                revision = row[0] + 1
                from dataclasses import replace
                persisted = replace(snapshot, revision=revision)
                encoded = self._encode(persisted)
                changed = db.execute("UPDATE apl_cycles SET revision=?,snapshot_json=? WHERE cycle_id=? AND revision=?", (revision, encoded, snapshot.cycle_id, row[0])).rowcount
                if changed != 1:
                    raise RuntimeError("concurrent APL cycle update")
                if result_ref is not None:
                    db.execute("INSERT INTO apl_result_refs(attempt_id,cycle_id,job_id) VALUES(?,?,?)",
                               (result_ref[0], snapshot.cycle_id, result_ref[1]))
                db.execute("INSERT INTO apl_events(cycle_id,revision,state,snapshot_digest) VALUES(?,?,?,?)", (snapshot.cycle_id, revision, snapshot.state.value, __import__('hashlib').sha256(encoded.encode()).hexdigest()))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return persisted
