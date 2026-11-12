"""Fail-closed, grant-scoped continuous execution for an immutable APL plan.

This module deliberately has no default grant validator or executor. Applications
must inject both, and the production CLI remains unavailable until the local
Broker/TEG dispatch gates have been homologated.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
import time
from typing import Protocol
from uuid import uuid4

from .contracts import APLState, CycleSnapshot, ExecutionOutcome, JobProposal


class AuthorizedExecutionError(RuntimeError):
    """A plan could not be dispatched within its exact persisted grant."""


class NoEffectExecutionError(RuntimeError):
    """Trusted executor confirms it did not start an external effect."""


@dataclass(frozen=True)
class ExecutionLimits:
    max_jobs: int
    max_attempts_per_job: int
    max_seconds: float
    max_estimated_budget: float = 0.0
    max_iterations: int = 100

    def __post_init__(self) -> None:
        if (type(self.max_jobs) is not int or self.max_jobs < 1
                or type(self.max_attempts_per_job) is not int or self.max_attempts_per_job < 1
                or type(self.max_seconds) not in (float, int) or self.max_seconds <= 0
                or type(self.max_estimated_budget) not in (float, int)
                or self.max_estimated_budget < 0
                or type(self.max_iterations) is not int or self.max_iterations < 1):
            raise ValueError("execution_limits_invalid")


@dataclass(frozen=True)
class VerifiedExecutionGrant:
    """Authority-resolved grant and approved plan binding.

    The resolver must verify grant signature/status/expiry/revocation and resolve
    the proposal from authoritative stores on every call. It must not accept a
    caller-created grant or claims supplied by the executor.
    """

    grant_id: str
    request_digest: str
    proposal_id: str
    approved_plan_digest: str
    revision: int
    plan_revision_id: str
    accepted_plan_digest: str
    workspace_id: str
    workspace_path: str
    workspace_identity_digest: str
    allowed_operations: tuple[str, ...]
    authorized_scope: tuple[str, ...]
    expires_at: str
    limits: ExecutionLimits
    state: str = "approved"

    def __post_init__(self) -> None:
        for value in (self.grant_id, self.proposal_id, self.plan_revision_id, self.workspace_id,
                      self.workspace_path):
            if type(value) is not str or not value.strip():
                raise ValueError("grant_binding_identifier_invalid")
        for value in (self.request_digest, self.approved_plan_digest, self.accepted_plan_digest,
                      self.workspace_identity_digest):
            if type(value) is not str or not value.startswith("sha256:") or len(value) != 71:
                raise ValueError("grant_binding_digest_invalid")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("grant_binding_revision_invalid")
        if (not self.allowed_operations or len(set(self.allowed_operations)) != len(self.allowed_operations)
                or not self.authorized_scope or len(set(self.authorized_scope)) != len(self.authorized_scope)):
            raise ValueError("grant_binding_scope_invalid")
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("grant_binding_expiry_invalid") from exc
        if expiry.tzinfo is None:
            raise ValueError("grant_binding_expiry_must_be_aware")
        if not isinstance(self.limits, ExecutionLimits):
            raise ValueError("grant_binding_limits_invalid")


class ExecutionGrantResolver(Protocol):
    def resolve_and_validate(self, grant_id: str, cycle_id: str) -> VerifiedExecutionGrant: ...


class AuthorizedJobExecutor(Protocol):
    def execute(self, cycle_id: str, job: JobProposal, authorized_scope: tuple[str, ...],
                operation_id: str) -> ExecutionOutcome: ...


class ResultEvidenceValidator(Protocol):
    def validate(self, cycle_id: str, job: JobProposal, outcome: ExecutionOutcome) -> bool: ...


class SQLiteAuthorizedExecutionStore:
    """Append-only execution journal sharing the APL SQLite database."""

    def __init__(self, database_path: str):
        self.database_path = str(database_path)
        with sqlite3.connect(self.database_path, timeout=10) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS apl_authorized_runs(
                    run_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, grant_id TEXT NOT NULL UNIQUE,
                    binding_json TEXT NOT NULL, binding_digest TEXT NOT NULL,
                    plan_json TEXT NOT NULL, plan_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ready','running','blocked','completed','cancelled')),
                    started_at TEXT NOT NULL, finished_at TEXT, reason TEXT NOT NULL DEFAULT '',
                    jobs_started INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    consumed_seconds REAL NOT NULL DEFAULT 0, consumed_budget REAL NOT NULL DEFAULT 0,
                    owner_pid INTEGER, owner_marker TEXT
                );
                CREATE TABLE IF NOT EXISTS apl_authorized_run_tasks(
                    run_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ready','reserved','running','retryable','completed','failed','unknown')),
                    operation_id TEXT NOT NULL UNIQUE, attempts INTEGER NOT NULL DEFAULT 0,
                    outcome_json TEXT, evidence_digest TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,job_id),
                    FOREIGN KEY(run_id) REFERENCES apl_authorized_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS apl_authorized_run_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    job_id TEXT, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, previous_digest TEXT NOT NULL DEFAULT '',
                    event_digest TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES apl_authorized_runs(run_id)
                );
                CREATE TRIGGER IF NOT EXISTS apl_authorized_run_events_no_update
                BEFORE UPDATE ON apl_authorized_run_events BEGIN
                    SELECT RAISE(ABORT,'authorized execution events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_run_events_no_delete
                BEFORE DELETE ON apl_authorized_run_events BEGIN
                    SELECT RAISE(ABORT,'authorized execution events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_run_binding_immutable
                BEFORE UPDATE OF cycle_id,grant_id,binding_json,binding_digest,plan_json,plan_digest,started_at
                ON apl_authorized_runs BEGIN
                    SELECT RAISE(ABORT,'authorized execution binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_task_binding_immutable
                BEFORE UPDATE OF operation_id ON apl_authorized_run_tasks BEGIN
                    SELECT RAISE(ABORT,'authorized task operation is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS apl_authorized_task_outcome_immutable
                BEFORE UPDATE OF outcome_json,evidence_digest ON apl_authorized_run_tasks
                WHEN OLD.status='completed' BEGIN
                    SELECT RAISE(ABORT,'verified task outcome is immutable'); END;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(apl_authorized_runs)")}
            if "owner_pid" not in columns:
                db.execute("ALTER TABLE apl_authorized_runs ADD COLUMN owner_pid INTEGER")
            if "owner_marker" not in columns:
                db.execute("ALTER TABLE apl_authorized_runs ADD COLUMN owner_marker TEXT")
            event_columns = {row[1] for row in db.execute("PRAGMA table_info(apl_authorized_run_events)")}
            if "previous_digest" not in event_columns:
                db.execute("ALTER TABLE apl_authorized_run_events ADD COLUMN previous_digest TEXT NOT NULL DEFAULT ''")
            if "event_digest" not in event_columns:
                db.execute("ALTER TABLE apl_authorized_run_events ADD COLUMN event_digest TEXT NOT NULL DEFAULT ''")

    def create(self, *, cycle_id: str, grant: VerifiedExecutionGrant,
               snapshot: CycleSnapshot) -> str:
        now = _utc_now()
        jobs = [asdict(job) for job in snapshot.jobs]
        plan_json = _canonical(jobs)
        plan_digest = _sha(plan_json)
        binding_json = _canonical(asdict(grant))
        binding_digest = _sha(binding_json)
        run_id = "apl-run-" + uuid4().hex
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT run_id,binding_digest,plan_digest FROM apl_authorized_runs WHERE grant_id=?", (grant.grant_id,)).fetchone()
                if prior:
                    if prior[1:] != (binding_digest, plan_digest):
                        raise AuthorizedExecutionError("grant_already_bound_to_different_execution")
                    db.commit()
                    return prior[0]
                db.execute("INSERT INTO apl_authorized_runs(run_id,cycle_id,grant_id,binding_json,binding_digest,plan_json,plan_digest,status,started_at) VALUES(?,?,?,?,?,?,?,'ready',?)",
                           (run_id, cycle_id, grant.grant_id, binding_json, binding_digest, plan_json, plan_digest, now))
                for job in snapshot.jobs:
                    operation_id = _operation_id(run_id, job.job_id, plan_digest)
                    prior = dict(snapshot.outcomes).get(job.job_id)
                    already_verified = prior is not None and prior.status == "succeeded"
                    db.execute("INSERT INTO apl_authorized_run_tasks(run_id,job_id,status,operation_id,outcome_json,evidence_digest,updated_at) VALUES(?,?,?,?,?,?,?)",
                               (run_id, job.job_id, "completed" if already_verified else "ready",
                                operation_id, _canonical(asdict(prior)) if already_verified else None,
                                _sha(_canonical(prior.evidence)) if already_verified else None, now))
                self._event(db, run_id, None, "run.created", {
                    "cycle_id": cycle_id, "grant_id": grant.grant_id,
                    "binding_digest": binding_digest, "plan_digest": plan_digest,
                    "jobs": [job.job_id for job in snapshot.jobs],
                }, now)
                db.commit()
                return run_id
            except Exception:
                db.rollback()
                raise

    @staticmethod
    def _event(db, run_id: str, job_id: str | None, event_type: str, payload: dict, occurred_at: str) -> None:
        prior = db.execute("SELECT event_digest FROM apl_authorized_run_events WHERE run_id=? ORDER BY event_id DESC LIMIT 1", (run_id,)).fetchone()
        previous_digest = prior[0] if prior else ""
        payload_json = _canonical(payload)
        digest_payload = _canonical({"run_id": run_id, "job_id": job_id,
                                     "event_type": event_type, "payload_json": payload_json,
                                     "occurred_at": occurred_at, "previous_digest": previous_digest})
        event_digest = _sha(digest_payload)
        db.execute("INSERT INTO apl_authorized_run_events(run_id,job_id,event_type,payload_json,occurred_at,previous_digest,event_digest) VALUES(?,?,?,?,?,?,?)",
                   (run_id, job_id, event_type, payload_json, occurred_at, previous_digest, event_digest))

    def load(self, run_id: str) -> dict:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            result = dict(row)
            tasks = db.execute("SELECT * FROM apl_authorized_run_tasks WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            result["tasks"] = [dict(item) for item in tasks]
            result["events"] = [dict(item) for item in db.execute("SELECT * FROM apl_authorized_run_events WHERE run_id=? ORDER BY event_id", (run_id,))]
        if _sha(result["binding_json"]) != result["binding_digest"] or _sha(result["plan_json"]) != result["plan_digest"]:
            raise AuthorizedExecutionError("persisted_execution_binding_integrity_failure")
        previous = ""
        for event in result["events"]:
            expected = _sha(_canonical({"run_id": event["run_id"], "job_id": event["job_id"],
                                        "event_type": event["event_type"],
                                        "payload_json": event["payload_json"],
                                        "occurred_at": event["occurred_at"],
                                        "previous_digest": previous}))
            if event["previous_digest"] != previous or event["event_digest"] != expected:
                raise AuthorizedExecutionError("execution_journal_integrity_failure")
            previous = expected
        for task in result["tasks"]:
            if task["status"] == "completed":
                try:
                    evidence = json.loads(task["outcome_json"])["evidence"]
                except (TypeError, KeyError, json.JSONDecodeError) as exc:
                    raise AuthorizedExecutionError("verified_task_checkpoint_invalid") from exc
                if _sha(_canonical(evidence)) != task["evidence_digest"]:
                    raise AuthorizedExecutionError("verified_task_evidence_integrity_failure")
        return result

    def claim_run(self, run_id: str, *, now: str) -> None:
        marker = _current_process_marker()
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status,owner_pid,owner_marker FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                if row["status"] not in {"ready", "running"}:
                    raise AuthorizedExecutionError(f"execution_{row['status']}")
                if row["owner_pid"] is not None and _process_matches(row["owner_pid"], row["owner_marker"]):
                    raise AuthorizedExecutionError("execution_already_owned_by_live_process")
                if row["owner_pid"] is not None:
                    self._mark_dead_owner_unknown(db, run_id, now)
                    pending = db.execute("SELECT 1 FROM apl_authorized_run_tasks WHERE run_id=? AND status='unknown' LIMIT 1", (run_id,)).fetchone()
                    if pending:
                        db.commit()
                        raise AuthorizedExecutionError("recovery_found_ambiguous_task")
                db.execute("UPDATE apl_authorized_runs SET owner_pid=?,owner_marker=? WHERE run_id=?", (os.getpid(), marker, run_id))
                self._event(db, run_id, None, "run.claimed", {"pid": os.getpid(), "process_marker": marker}, now)
                db.commit()
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise

    def release_run(self, run_id: str) -> None:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            db.execute("UPDATE apl_authorized_runs SET owner_pid=NULL,owner_marker=NULL WHERE run_id=? AND owner_pid=? AND owner_marker=?",
                       (run_id, os.getpid(), _current_process_marker()))

    def request_cancel(self, run_id: str, *, now: str) -> None:
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT status FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(run_id)
                if row[0] in {"completed", "cancelled"}:
                    db.commit()
                    return
                exists = db.execute("SELECT 1 FROM apl_authorized_run_events WHERE run_id=? AND event_type='run.cancel_requested'", (run_id,)).fetchone()
                if not exists:
                    self._event(db, run_id, None, "run.cancel_requested", {"requested_at": now}, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def cancel_requested(self, run_id: str) -> bool:
        with sqlite3.connect(self.database_path, timeout=10) as db:
            return db.execute("SELECT 1 FROM apl_authorized_run_events WHERE run_id=? AND event_type='run.cancel_requested' LIMIT 1", (run_id,)).fetchone() is not None

    def status(self, run_id: str) -> dict:
        return self.load(run_id)

    @classmethod
    def _mark_dead_owner_unknown(cls, db, run_id: str, now: str) -> None:
        rows = db.execute("SELECT job_id,status FROM apl_authorized_run_tasks WHERE run_id=? AND status IN ('reserved','running')", (run_id,)).fetchall()
        for job_id, previous in rows:
            db.execute("UPDATE apl_authorized_run_tasks SET status='unknown',updated_at=? WHERE run_id=? AND job_id=?", (now, run_id, job_id))
            cls._event(db, run_id, job_id, "task.recovered_unknown", {"previous_status": previous, "automatic_retry": False}, now)
        if rows:
            db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='recovery_found_ambiguous_task',owner_pid=NULL,owner_marker=NULL WHERE run_id=?", (run_id,))
        else:
            db.execute("UPDATE apl_authorized_runs SET owner_pid=NULL,owner_marker=NULL WHERE run_id=?", (run_id,))

    def reserve_next(self, run_id: str, job: JobProposal, *, max_attempts: int, now: str) -> dict | None:
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                run = db.execute("SELECT status,plan_json FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if run is None:
                    raise KeyError(run_id)
                if run[0] not in {"ready", "running"}:
                    raise AuthorizedExecutionError(f"execution_{run[0]}")
                plan = json.loads(run[1])
                by_id = {item["job_id"]: item for item in plan}
                task = db.execute("SELECT status,operation_id,attempts FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?", (run_id, job.job_id)).fetchone()
                if task is None or task[0] not in {"ready", "retryable"}:
                    raise AuthorizedExecutionError("task_not_reservable")
                if task[2] >= max_attempts:
                    db.execute("UPDATE apl_authorized_run_tasks SET status='failed',updated_at=? WHERE run_id=? AND job_id=?", (now, run_id, job.job_id))
                    db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='task_attempt_limit' WHERE run_id=?", (run_id,))
                    self._event(db, run_id, job.job_id, "task.blocked", {"reason": "task_attempt_limit"}, now)
                    db.commit()
                    raise AuthorizedExecutionError("task_attempt_limit")
                deps = tuple(by_id[job.job_id].get("depends_on", ()))
                completed = {row[0] for row in db.execute("SELECT job_id FROM apl_authorized_run_tasks WHERE run_id=? AND status='completed'", (run_id,))}
                if not set(deps) <= completed:
                    raise AuthorizedExecutionError("task_dependencies_incomplete")
                db.execute("UPDATE apl_authorized_run_tasks SET status='reserved',attempts=attempts+1,updated_at=? WHERE run_id=? AND job_id=?", (now, run_id, job.job_id))
                db.execute("UPDATE apl_authorized_runs SET status='running',jobs_started=jobs_started+CASE WHEN ?=0 THEN 1 ELSE 0 END,attempts=attempts+1 WHERE run_id=?", (task[2], run_id))
                self._event(db, run_id, job.job_id, "task.reserved", {"operation_id": task[1], "attempt": task[2] + 1}, now)
                db.commit()
                return {"operation_id": task[1], "attempt": task[2] + 1}
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise

    def mark_running(self, run_id: str, job_id: str, *, now: str) -> None:
        self._task_transition(run_id, job_id, "reserved", "running", "task.dispatched", {}, now)

    def record_outcome(self, run_id: str, job_id: str, outcome: ExecutionOutcome, *,
                       validated: bool, elapsed_seconds: float, now: str) -> None:
        outcome_data = asdict(outcome)
        encoded = _canonical(outcome_data)
        status = "completed" if outcome.status == "succeeded" and validated else (
            "unknown" if outcome.status == "unknown" else "failed")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                task = db.execute("SELECT status FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
                if task is None or task[0] != "running":
                    raise AuthorizedExecutionError("task_outcome_without_running_reservation")
                db.execute("UPDATE apl_authorized_run_tasks SET status=?,outcome_json=?,evidence_digest=?,updated_at=? WHERE run_id=? AND job_id=?",
                           (status, encoded, _sha(_canonical(outcome.evidence)), now, run_id, job_id))
                db.execute("UPDATE apl_authorized_runs SET consumed_seconds=consumed_seconds+?,consumed_budget=consumed_budget+? WHERE run_id=?",
                           (max(0.0, elapsed_seconds), max(0.0, outcome.cost), run_id))
                reason = "" if status == "completed" else ("unknown_execution_result" if status == "unknown" else "execution_or_evidence_validation_failed")
                if status != "completed":
                    db.execute("UPDATE apl_authorized_runs SET status='blocked',reason=? WHERE run_id=?", (reason, run_id))
                self._event(db, run_id, job_id, f"task.{status}", {
                    "operation_id": db.execute("SELECT operation_id FROM apl_authorized_run_tasks WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()[0],
                    "outcome_digest": _sha(encoded), "evidence_digest": _sha(_canonical(outcome.evidence)),
                }, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def mark_unknown(self, run_id: str, job_id: str, reason: str, *, now: str) -> None:
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                changed = db.execute("UPDATE apl_authorized_run_tasks SET status='unknown',updated_at=? WHERE run_id=? AND job_id=? AND status IN ('reserved','running')", (now, run_id, job_id)).rowcount
                if changed:
                    db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='unknown_execution_result' WHERE run_id=?", (run_id,))
                    self._event(db, run_id, job_id, "task.unknown", {"reason": reason[:256]}, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def mark_retryable_no_effect(self, run_id: str, job_id: str, reason: str, *, now: str) -> None:
        self._task_transition(run_id, job_id, "running", "retryable", "task.no_effect_failure", {"reason": reason[:256]}, now)

    def record_no_effect_time(self, run_id: str, elapsed_seconds: float, *, now: str) -> None:
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("UPDATE apl_authorized_runs SET consumed_seconds=consumed_seconds+? WHERE run_id=?",
                           (max(0.0, elapsed_seconds), run_id))
                self._event(db, run_id, None, "run.no_effect_attempt", {"elapsed_seconds": max(0.0, elapsed_seconds)}, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _task_transition(self, run_id, job_id, expected, target, event_type, payload, now):
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("UPDATE apl_authorized_run_tasks SET status=?,updated_at=? WHERE run_id=? AND job_id=? AND status=?", (target, now, run_id, job_id, expected)).rowcount != 1:
                    raise AuthorizedExecutionError("task_transition_conflict")
                self._event(db, run_id, job_id, event_type, payload, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def finish(self, run_id: str, *, status: str, reason: str = "", now: str) -> None:
        if status not in {"completed", "blocked", "cancelled"}:
            raise ValueError("run_terminal_state_invalid")
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT status FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if prior is None:
                    raise KeyError(run_id)
                if prior[0] == status:
                    db.commit()
                    return
                if prior[0] in {"completed", "cancelled"}:
                    raise AuthorizedExecutionError("run_terminal_transition_conflict")
                db.execute("UPDATE apl_authorized_runs SET status=?,finished_at=?,reason=? WHERE run_id=?", (status, now, reason[:512], run_id))
                self._event(db, run_id, None, f"run.{status}", {"reason": reason[:512]}, now)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def recover_unfinished(self, *, now: str) -> tuple[str, ...]:
        """Mark tasks unknown only when their owning process is confirmed absent."""
        recovered: list[str] = []
        with sqlite3.connect(self.database_path, timeout=10, isolation_level=None) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("BEGIN IMMEDIATE")
            try:
                owners = db.execute("SELECT run_id,owner_pid,owner_marker FROM apl_authorized_runs WHERE status IN ('ready','running') AND owner_pid IS NOT NULL").fetchall()
                for run_id, pid, marker in owners:
                    if not _process_matches(pid, marker):
                        rows = db.execute("SELECT job_id FROM apl_authorized_run_tasks WHERE run_id=? AND status IN ('reserved','running')", (run_id,)).fetchall()
                        self._mark_dead_owner_unknown(db, run_id, now)
                        recovered.extend(f"{run_id}:{row[0]}" for row in rows)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return tuple(recovered)


class AuthorizedExecutionController:
    """Sequentially execute only a complete immutable plan covered by one grant."""

    def __init__(self, *, cycle_store, execution_store: SQLiteAuthorizedExecutionStore,
                 grant_resolver: ExecutionGrantResolver, executor: AuthorizedJobExecutor,
                 evidence_validator: ResultEvidenceValidator, clock=None):
        if any(item is None for item in (cycle_store, execution_store, grant_resolver, executor, evidence_validator)):
            raise ValueError("authorized_execution_dependencies_required")
        if (not callable(getattr(grant_resolver, "resolve_and_validate", None))
                or not callable(getattr(executor, "execute", None))
                or not callable(getattr(evidence_validator, "validate", None))):
            raise ValueError("authorized_execution_contract_unavailable")
        self.cycles = cycle_store
        self.runs = execution_store
        self.grants = grant_resolver
        self.executor = executor
        self.evidence = evidence_validator
        self.clock = clock or _utc_now

    def start(self, cycle_id: str, grant_id: str) -> dict:
        snapshot = self.cycles.load(cycle_id)
        grant = self.grants.resolve_and_validate(grant_id, cycle_id)
        self._validate_binding(snapshot, grant)
        run_id = self.runs.create(cycle_id=cycle_id, grant=grant, snapshot=snapshot)
        return self.run(run_id)

    def resume(self, run_id: str) -> dict:
        run = self.runs.load(run_id)
        if run["status"] not in {"ready", "running"}:
            if run["status"] == "completed":
                return run
            raise AuthorizedExecutionError(f"execution_{run['status']}")
        return self.run(run_id)

    def cancel(self, run_id: str) -> None:
        self.runs.request_cancel(run_id, now=self.clock())

    def status(self, run_id: str) -> dict:
        return self.runs.status(run_id)

    def run(self, run_id: str) -> dict:
        self.runs.claim_run(run_id, now=self.clock())
        try:
            return self._run_claimed(run_id)
        finally:
            self.runs.release_run(run_id)

    def _run_claimed(self, run_id: str) -> dict:
        state = self.runs.load(run_id)
        if state["status"] not in {"ready", "running"}:
            if state["status"] == "completed":
                return state
            raise AuthorizedExecutionError(f"execution_{state['status']}")
        cycle_id = state["cycle_id"]
        if all(task["status"] == "completed" for task in state["tasks"]):
            snapshot = self.cycles.load(cycle_id)
            self._reconcile_apl_checkpoint(snapshot, state)
            self.runs.finish(run_id, status="completed", now=self.clock())
            return self.runs.load(run_id)
        try:
            binding = self.grants.resolve_and_validate(state["grant_id"], cycle_id)
            snapshot = self.cycles.load(cycle_id)
            self._validate_binding(snapshot, binding, persisted=state)
        except Exception as exc:
            self.runs.finish(run_id, status="blocked", reason="grant_validation_failed", now=self.clock())
            raise AuthorizedExecutionError("grant_validation_failed; dispatch_suspended") from exc
        started = time.monotonic()
        initial_consumed_seconds = state["consumed_seconds"]
        spent = state["consumed_budget"]
        outcomes = {job_id: outcome for job_id, outcome in snapshot.outcomes}
        if snapshot.state is not APLState.READY:
            raise AuthorizedExecutionError(f"cycle_state_{snapshot.state.value.lower()}")
        while True:
            state = self.runs.load(run_id)
            if state["status"] == "blocked":
                raise AuthorizedExecutionError(state["reason"] or "execution_blocked")
            if self.runs.cancel_requested(run_id):
                self.runs.finish(run_id, status="cancelled", reason="operator_cancelled", now=self.clock())
                return self.runs.load(run_id)
            completed = {task["job_id"] for task in state["tasks"] if task["status"] == "completed"}
            if len(completed) == len(snapshot.jobs):
                self._reconcile_apl_checkpoint(snapshot, state)
                self.runs.finish(run_id, status="completed", now=self.clock())
                return self.runs.load(run_id)
            if state["jobs_started"] >= binding.limits.max_jobs or state["attempts"] >= binding.limits.max_jobs * binding.limits.max_attempts_per_job:
                self.runs.finish(run_id, status="blocked", reason="execution_job_or_attempt_limit", now=self.clock())
                raise AuthorizedExecutionError("execution_job_or_attempt_limit")
            if initial_consumed_seconds + time.monotonic() - started >= binding.limits.max_seconds:
                self.runs.finish(run_id, status="blocked", reason="execution_time_limit", now=self.clock())
                raise AuthorizedExecutionError("execution_time_limit")
            if state["attempts"] >= binding.limits.max_iterations:
                self.runs.finish(run_id, status="blocked", reason="execution_iteration_limit", now=self.clock())
                raise AuthorizedExecutionError("execution_iteration_limit")
            if binding.limits.max_estimated_budget and spent > binding.limits.max_estimated_budget:
                self.runs.finish(run_id, status="blocked", reason="estimated_budget_limit", now=self.clock())
                raise AuthorizedExecutionError("estimated_budget_limit")
            ready = next((job for job in snapshot.jobs if job.job_id not in completed
                          and set(job.depends_on) <= completed), None)
            if ready is None:
                self.runs.finish(run_id, status="blocked", reason="no_eligible_job_or_dependency_cycle", now=self.clock())
                raise AuthorizedExecutionError("no_eligible_job_or_dependency_cycle")
            # Revalidate revocation, expiry, proposal digest, workspace and operation immediately before reservation/dispatch.
            try:
                binding = self.grants.resolve_and_validate(state["grant_id"], cycle_id)
                self._validate_binding(snapshot, binding, persisted=state)
            except Exception as exc:
                self.runs.finish(run_id, status="blocked", reason="grant_validation_failed", now=self.clock())
                raise AuthorizedExecutionError("grant_validation_failed; dispatch_suspended") from exc
            reservation = self.runs.reserve_next(run_id, ready,
                                                  max_attempts=binding.limits.max_attempts_per_job,
                                                  now=self.clock())
            self.runs.mark_running(run_id, ready.job_id, now=self.clock())
            attempt_started = time.monotonic()
            try:
                outcome = self.executor.execute(cycle_id, ready, binding.authorized_scope,
                                                reservation["operation_id"])
            except NoEffectExecutionError as exc:
                elapsed = time.monotonic() - attempt_started
                self.runs.record_no_effect_time(run_id, elapsed, now=self.clock())
                self.runs.mark_retryable_no_effect(run_id, ready.job_id, str(exc), now=self.clock())
                continue
            except Exception as exc:
                self.runs.mark_unknown(run_id, ready.job_id, type(exc).__name__, now=self.clock())
                raise AuthorizedExecutionError("unknown_execution_result; automatic_retry_forbidden") from exc
            try:
                valid = outcome.status == "succeeded" and self.evidence.validate(cycle_id, ready, outcome)
            except Exception:
                valid = False
            elapsed = time.monotonic() - attempt_started
            self.runs.record_outcome(run_id, ready.job_id, outcome, validated=valid,
                                     elapsed_seconds=elapsed, now=self.clock())
            if not valid:
                if outcome.status == "unknown":
                    raise AuthorizedExecutionError("unknown_execution_result; automatic_retry_forbidden")
                raise AuthorizedExecutionError("execution_or_evidence_validation_failed")
            spent += outcome.cost
            if binding.limits.max_estimated_budget and spent > binding.limits.max_estimated_budget:
                self.runs.finish(run_id, status="blocked", reason="estimated_budget_limit", now=self.clock())
                raise AuthorizedExecutionError("estimated_budget_limit")
            try:
                self._reconcile_apl_checkpoint(self.cycles.load(cycle_id), self.runs.load(run_id))
            except Exception as exc:
                # The executor outcome is durable in the run journal; failure to update APL is recoverable from this verified checkpoint.
                raise AuthorizedExecutionError("verified_outcome_persisted_but_cycle_checkpoint_failed") from exc

    def _reconcile_apl_checkpoint(self, original: CycleSnapshot, run_state: dict) -> None:
        """Restore verified outcomes after a crash between the run journal and APL snapshot."""
        latest = self.cycles.load(original.cycle_id)
        if (latest.plan_revision_id != original.plan_revision_id
                or latest.plan_digest != original.plan_digest):
            raise AuthorizedExecutionError("approved_plan_changed_during_recovery")
        persisted = {job_id: outcome for job_id, outcome in latest.outcomes}
        for task in run_state["tasks"]:
            if task["status"] != "completed" or not task["outcome_json"]:
                continue
            data = json.loads(task["outcome_json"])
            outcome = ExecutionOutcome(data["status"], data["summary"], tuple(data["evidence"]),
                                       data["cost"], data["progress_key"])
            prior = persisted.get(task["job_id"])
            if prior is not None and prior != outcome:
                raise AuthorizedExecutionError("apl_outcome_conflicts_with_verified_run_checkpoint")
            persisted[task["job_id"]] = outcome
        ordered = tuple((job.job_id, persisted[job.job_id]) for job in latest.jobs if job.job_id in persisted)
        if ordered == latest.outcomes:
            return
        already = {key for key, _ in latest.outcomes}
        newly_completed_cost = sum(outcome.cost for job_id, outcome in ordered if job_id not in already)
        updated = CycleSnapshot(latest.cycle_id, latest.objective, latest.authorized_scope,
                                APLState.COMPLETED if len(ordered) == len(latest.jobs) else APLState.READY,
                                latest.jobs, ordered,
                                latest.iterations + sum(1 for key, _ in ordered if key not in already),
                                latest.consumed_budget + newly_completed_cost, latest.version, "", latest.revision,
                                latest.plan_digest, latest.plan_revision_id)
        self.cycles.save(updated, latest.iterations, expected_revision=latest.revision)

    @staticmethod
    def _validate_binding(snapshot: CycleSnapshot, grant: VerifiedExecutionGrant,
                          persisted: dict | None = None) -> None:
        if grant.state != "approved":
            raise AuthorizedExecutionError("grant_not_approved")
        if datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00")).astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise AuthorizedExecutionError("grant_expired")
        if snapshot.state in {APLState.CANCELLED, APLState.FAILED, APLState.BLOCKED, APLState.WAITING_APPROVAL}:
            raise AuthorizedExecutionError("cycle_not_executable")
        if (snapshot.plan_revision_id != grant.plan_revision_id
                or snapshot.plan_digest != grant.accepted_plan_digest
                or snapshot.authorized_scope != grant.authorized_scope):
            raise AuthorizedExecutionError("approved_plan_binding_changed")
        job_ids = {job.job_id for job in snapshot.jobs}
        operations = {f"job:{job_id}" for job_id in job_ids}
        if not job_ids or len(job_ids) != len(snapshot.jobs) or operations != set(grant.allowed_operations):
            raise AuthorizedExecutionError("grant_does_not_cover_entire_plan")
        if any(not set(job.required_scope) <= set(grant.authorized_scope) for job in snapshot.jobs):
            raise AuthorizedExecutionError("plan_job_outside_authorized_scope")
        if len(snapshot.jobs) > grant.limits.max_jobs:
            raise AuthorizedExecutionError("plan_exceeds_authorized_job_limit")
        jobs = {job.job_id: job for job in snapshot.jobs}
        if len(jobs) != len(snapshot.jobs):
            raise AuthorizedExecutionError("duplicate_job_id")
        visiting: set[str] = set()
        visited: set[str] = set()
        def visit(job_id: str) -> None:
            if job_id in visiting:
                raise AuthorizedExecutionError("dependency_cycle")
            if job_id in visited:
                return
            visiting.add(job_id)
            for dependency in jobs[job_id].depends_on:
                if dependency not in jobs:
                    raise AuthorizedExecutionError("dependency_missing")
                visit(dependency)
            visiting.remove(job_id)
            visited.add(job_id)
        for job_id in jobs:
            visit(job_id)
        if persisted is not None:
            encoded = _canonical(asdict(grant))
            if (_sha(encoded) != persisted["binding_digest"]
                    or _sha(_canonical([asdict(job) for job in snapshot.jobs])) != persisted["plan_digest"]
                    or snapshot.plan_digest != json.loads(persisted["binding_json"])["accepted_plan_digest"]):
                raise AuthorizedExecutionError("persisted_run_binding_changed")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _operation_id(run_id: str, job_id: str, plan_digest: str) -> str:
    raw = _sha(f"{run_id}:{job_id}:{plan_digest}")[-32:]
    return "apl-op-" + raw


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _current_process_marker() -> str:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
        api.GetProcessTimes.restype = wintypes.BOOL
        values = [FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(api.GetCurrentProcess(), *(ctypes.byref(value) for value in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return str((values[0].high << 32) | values[0].low)
    stat = open(f"/proc/{os.getpid()}/stat", encoding="ascii").read().rsplit(")", 1)[1].split()[19]
    boot_id = open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
    return f"{boot_id}:{stat}"


def _process_matches(pid: int, marker: str | None) -> bool:
    if not marker:
        return False
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
            api.GetProcessTimes.restype = wintypes.BOOL
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = api.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                values = [FILETIME() for _ in range(4)]
                if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                    return False
                return str((values[0].high << 32) | values[0].low) == marker
            finally:
                api.CloseHandle(handle)
        stat = open(f"/proc/{pid}/stat", encoding="ascii").read().rsplit(")", 1)[1].split()[19]
        boot_id = open("/proc/sys/kernel/random/boot_id", encoding="ascii").read().strip()
        return f"{boot_id}:{stat}" == marker
    except (OSError, IndexError):
        return False
