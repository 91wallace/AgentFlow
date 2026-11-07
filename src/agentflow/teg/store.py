"""Independent, single-instance SQLite journal for the dormant TEG core."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
from uuid import uuid4

from agentflow.approval.protocol import ExecutionReceipt, ProtocolError, digest_json, _timestamp

from .contracts import (
    AuthorizationConsumption, GatewayIdentity, GatewayPreparation, GatewaySubmission,
    GatewayContractError, GatewayExecutionError,
)
from .protocol import (
    AuthorizationQuery, AuthorizationQueryResult, AuthorizationState,
    AuthorizationStateQuery, ConsumptionConfirmation, ConsumptionConfirmationRequest,
    ConsumptionReservation, ConsumptionReservationRequest, ExecutionBinding,
    GatewayExecutionReceipt, ReconciliationRequest, ReconciliationResult,
)

APPLICATION_ID = int.from_bytes(b"AFTE", "big")
SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5_000
_ZERO_DIGEST = "sha256:" + "0" * 64
_TRANSITIONS = {
    "received": {"validated", "failed"},
    "validated": {"awaiting_authorization", "failed"},
    "awaiting_authorization": {"authorized", "execution_prepared", "failed", "unknown"},
    "authorized": {"execution_prepared", "executing", "failed"},
    "execution_prepared": {"authorized", "executing", "failed", "unknown"},
    "executing": {"succeeded", "failed", "unknown"},
    "succeeded": set(), "failed": set(), "unknown": set(),
}
_SCHEMA = (
    "CREATE TABLE gateway_identity(gateway_id TEXT PRIMARY KEY,identity_json TEXT NOT NULL,identity_digest TEXT NOT NULL UNIQUE)",
    "CREATE TABLE gateway_operations(operation_id TEXT PRIMARY KEY,submission_json TEXT NOT NULL,submission_digest TEXT NOT NULL UNIQUE,state TEXT NOT NULL CHECK(state IN ('received','validated','awaiting_authorization','authorized','execution_prepared','executing','succeeded','failed','unknown')),authorization_started INTEGER NOT NULL CHECK(authorization_started IN (0,1)),created_at TEXT NOT NULL,changed_at TEXT NOT NULL,last_event_id TEXT NOT NULL)",
    "CREATE TABLE gateway_authorizations(operation_id TEXT PRIMARY KEY REFERENCES gateway_operations(operation_id),consumption_json TEXT NOT NULL,consumption_digest TEXT NOT NULL UNIQUE,consumption_id TEXT NOT NULL UNIQUE)",
    "CREATE TABLE gateway_preparations(operation_id TEXT PRIMARY KEY REFERENCES gateway_operations(operation_id),preparation_json TEXT NOT NULL,preparation_digest TEXT NOT NULL UNIQUE)",
    "CREATE TABLE gateway_receipts(operation_id TEXT PRIMARY KEY REFERENCES gateway_operations(operation_id),receipt_json TEXT NOT NULL,receipt_digest TEXT NOT NULL UNIQUE)",
    "CREATE TABLE gateway_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,operation_id TEXT NOT NULL REFERENCES gateway_operations(operation_id),event_type TEXT NOT NULL,payload_json TEXT NOT NULL,occurred_at TEXT NOT NULL,previous_digest TEXT NOT NULL,event_digest TEXT NOT NULL UNIQUE)",
    "CREATE TRIGGER gateway_identity_no_update BEFORE UPDATE ON gateway_identity BEGIN SELECT RAISE(ABORT,'gateway identity is immutable'); END",
    "CREATE TRIGGER gateway_identity_no_delete BEFORE DELETE ON gateway_identity BEGIN SELECT RAISE(ABORT,'gateway identity is immutable'); END",
    "CREATE TRIGGER gateway_operation_submission_no_update BEFORE UPDATE OF submission_json,submission_digest,created_at ON gateway_operations BEGIN SELECT RAISE(ABORT,'gateway submissions are immutable'); END",
    "CREATE TRIGGER gateway_operation_no_delete BEFORE DELETE ON gateway_operations BEGIN SELECT RAISE(ABORT,'gateway operations are append-only'); END",
    "CREATE TRIGGER gateway_authorization_no_update BEFORE UPDATE ON gateway_authorizations BEGIN SELECT RAISE(ABORT,'gateway authorizations are immutable'); END",
    "CREATE TRIGGER gateway_authorization_no_delete BEFORE DELETE ON gateway_authorizations BEGIN SELECT RAISE(ABORT,'gateway authorizations are immutable'); END",
    "CREATE TRIGGER gateway_preparation_no_update BEFORE UPDATE ON gateway_preparations BEGIN SELECT RAISE(ABORT,'gateway preparations are immutable'); END",
    "CREATE TRIGGER gateway_preparation_no_delete BEFORE DELETE ON gateway_preparations BEGIN SELECT RAISE(ABORT,'gateway preparations are immutable'); END",
    "CREATE TRIGGER gateway_receipt_no_update BEFORE UPDATE ON gateway_receipts BEGIN SELECT RAISE(ABORT,'gateway receipts are immutable'); END",
    "CREATE TRIGGER gateway_receipt_no_delete BEFORE DELETE ON gateway_receipts BEGIN SELECT RAISE(ABORT,'gateway receipts are immutable'); END",
    "CREATE TRIGGER gateway_event_no_update BEFORE UPDATE ON gateway_events BEGIN SELECT RAISE(ABORT,'gateway events are append-only'); END",
    "CREATE TRIGGER gateway_event_no_delete BEFORE DELETE ON gateway_events BEGIN SELECT RAISE(ABORT,'gateway events are append-only'); END",
)
_SCHEMA_V2 = (
    "CREATE TABLE gateway_reservations(operation_id TEXT PRIMARY KEY REFERENCES gateway_operations(operation_id),reservation_id TEXT NOT NULL UNIQUE,reservation_json TEXT NOT NULL,reservation_digest TEXT NOT NULL UNIQUE,request_digest TEXT NOT NULL,binding_digest TEXT NOT NULL,created_at TEXT NOT NULL)",
    "CREATE TABLE gateway_protocol_records(record_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL REFERENCES gateway_operations(operation_id),record_type TEXT NOT NULL,record_json TEXT NOT NULL,record_digest TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(operation_id,record_type,record_digest))",
    "CREATE INDEX gateway_protocol_operation_idx ON gateway_protocol_records(operation_id,record_type)",
    "CREATE TRIGGER gateway_reservation_no_update BEFORE UPDATE ON gateway_reservations BEGIN SELECT RAISE(ABORT,'gateway reservations are immutable'); END",
    "CREATE TRIGGER gateway_reservation_no_delete BEFORE DELETE ON gateway_reservations BEGIN SELECT RAISE(ABORT,'gateway reservations are immutable'); END",
    "CREATE TRIGGER gateway_protocol_record_no_update BEFORE UPDATE ON gateway_protocol_records BEGIN SELECT RAISE(ABORT,'gateway protocol records are append-only'); END",
    "CREATE TRIGGER gateway_protocol_record_no_delete BEFORE DELETE ON gateway_protocol_records BEGIN SELECT RAISE(ABORT,'gateway protocol records are append-only'); END",
)
_PROTOCOL_CONTRACTS = {
    "authorization_query": AuthorizationQuery,
    "authorization_query_result": AuthorizationQueryResult,
    "reservation_request": ConsumptionReservationRequest,
    "reservation": ConsumptionReservation,
    "confirmation_request": ConsumptionConfirmationRequest,
    "confirmation": ConsumptionConfirmation,
    "authorization_state_query": AuthorizationStateQuery,
    "authorization_state": AuthorizationState,
    "reconciliation_request": ReconciliationRequest,
    "reconciliation_result": ReconciliationResult,
    "execution_receipt": GatewayExecutionReceipt,
}


class GatewayStoreError(RuntimeError):
    """Gateway journal could not be safely used."""


class GatewayConflict(GatewayStoreError):
    """operation_id was reused with different immutable parameters."""


class GatewayIntegrityError(GatewayStoreError):
    """Gateway journal, digest chain, or state projection is inconsistent."""


class GatewayStoreLocked(GatewayStoreError):
    """Another process owns the single-instance Gateway store."""


class _ProcessLock:
    """Non-blocking OS lock; the lock file may persist, the kernel lock does not."""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, os.SEEK_END)
                if self.file.tell() == 0:
                    self.file.write(b"\0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            self.file.close()
            self.file = None
            raise GatewayStoreLocked("Gateway store is already locked") from exc

    def release(self) -> None:
        if self.file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class GatewayStore:
    """Dedicated SQLite database; never opens or migrates Broker/AgentFlow stores."""

    def __init__(self, path: str | Path, identity: GatewayIdentity):
        self.path = Path(path).expanduser().resolve()
        if any(part.casefold() == ".agentflow" for part in self.path.parts):
            raise GatewayStoreError("Gateway database must be outside project .agentflow/ state")
        self.identity = identity
        self._mutex = threading.RLock()
        self._lock = _ProcessLock(self.path.with_name(self.path.name + ".lock"))
        self._validate_existing_identity()
        self._lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000,
                                      isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            app_id = self.db.execute("PRAGMA application_id").fetchone()[0]
            tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if app_id not in (0, APPLICATION_ID) or (app_id == 0 and tables):
                raise GatewayStoreError("database belongs to another component or is non-empty")
            self.db.execute("PRAGMA foreign_keys=ON")
            if self.db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise GatewayStoreError("SQLite foreign keys are unavailable")
            if self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0].casefold() != "wal":
                raise GatewayStoreError("Gateway requires SQLite WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                os.chmod(self._lock.path, 0o600)
            self._migrate()
            self._install_identity()
            self.verify_integrity()
            self._recover_interrupted()
            self.verify_integrity()
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self._lock.release()
            raise

    def _validate_existing_identity(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                app_id = conn.execute("PRAGMA application_id").fetchone()[0]
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                if app_id not in (0, APPLICATION_ID) or (app_id == 0 and tables):
                    raise GatewayStoreError("refusing to touch a non-Gateway database")
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise GatewayStoreError("existing Gateway path is unreadable; it was not changed") from exc

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION or version not in (0, 1, SCHEMA_VERSION):
            raise GatewayStoreError(f"unsupported Gateway schema version: {version}")
        if version == SCHEMA_VERSION:
            if self.db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise GatewayStoreError("Gateway schema identity mismatch")
            return
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if version == 0:
                for statement in _SCHEMA:
                    self.db.execute(statement)
            elif self.db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise GatewayStoreError("Gateway v1 schema identity mismatch")
            for statement in _SCHEMA_V2:
                self.db.execute(statement)
            self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            if isinstance(exc, GatewayStoreError):
                raise
            raise GatewayStoreError("Gateway schema creation failed and rolled back") from exc

    def _install_identity(self) -> None:
        row = self.db.execute("SELECT identity_json,identity_digest FROM gateway_identity").fetchone()
        if row:
            if row[0] != json.dumps(self.identity.to_dict(), ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")) or row[1] != self.identity.identity_digest:
                raise GatewayConflict("stable gateway_id already has a different identity record")
            return
        with self.transaction() as db:
            db.execute("INSERT INTO gateway_identity VALUES(?,?,?)",
                       (self.identity.gateway_id, json.dumps(self.identity.to_dict(), ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")), self.identity.identity_digest))

    def close(self) -> None:
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        self._lock.release()

    def __enter__(self) -> "GatewayStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def _append_event(self, db: sqlite3.Connection, operation_id: str, event_type: str,
                      payload: dict, occurred_at: str) -> str:
        prev_row = db.execute("SELECT event_digest FROM gateway_events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous = prev_row[0] if prev_row else _ZERO_DIGEST
        event_id = uuid4().hex
        body = {"event_id": event_id, "operation_id": operation_id, "event_type": event_type,
                "payload": payload, "occurred_at": occurred_at, "previous_digest": previous}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        db.execute("INSERT INTO gateway_events(event_id,operation_id,event_type,payload_json,occurred_at,previous_digest,event_digest) VALUES(?,?,?,?,?,?,?)",
                   (event_id, operation_id, event_type, encoded, occurred_at, previous, digest_json(body)))
        return event_id

    def _transition(self, db: sqlite3.Connection, operation_id: str, expected: str,
                    target: str, event_type: str, payload: dict | None = None,
                    *, when: str | None = None) -> None:
        row = db.execute("SELECT state FROM gateway_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None or row[0] != expected or target not in _TRANSITIONS.get(expected, set()):
            raise GatewayConflict(f"invalid Gateway transition {row[0] if row else 'missing'} -> {target}")
        at = when or _now()
        event_payload = {"from": expected, "to": target, **(payload or {})}
        event_id = self._append_event(db, operation_id, event_type, event_payload, at)
        db.execute("UPDATE gateway_operations SET state=?,changed_at=?,last_event_id=? WHERE operation_id=?",
                   (target, at, event_id, operation_id))

    def receive(self, submission: GatewaySubmission) -> tuple[str, bool]:
        if not isinstance(submission, GatewaySubmission):
            raise GatewayStoreError("versioned GatewaySubmission required")
        now = _now()
        with self.transaction() as db:
            prior = db.execute("SELECT submission_digest,state FROM gateway_operations WHERE operation_id=?",
                               (submission.operation_id,)).fetchone()
            if prior:
                if prior[0] != submission.submission_digest:
                    raise GatewayConflict("operation_id reused with different submission")
                return prior[1], False
            db.execute("INSERT INTO gateway_operations VALUES(?,?,?,?,?,?,?,?)",
                       (submission.operation_id, submission.to_json(), submission.submission_digest,
                        "received", 0, now, now, "pending"))
            event_id = self._append_event(db, submission.operation_id, "gateway.operation.received",
                                          {"submission_digest": submission.submission_digest,
                                           "intent_digest": submission.intent_digest}, now)
            db.execute("UPDATE gateway_operations SET last_event_id=? WHERE operation_id=?",
                       (event_id, submission.operation_id))
            return "received", True

    def get_submission(self, operation_id: str) -> GatewaySubmission:
        row = self.db.execute("SELECT submission_json FROM gateway_operations WHERE operation_id=?",
                              (operation_id,)).fetchone()
        if row is None:
            raise GatewayStoreError("Gateway operation not found")
        return GatewaySubmission.from_json(row[0])

    def state(self, operation_id: str) -> str:
        row = self.db.execute("SELECT state FROM gateway_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise GatewayStoreError("Gateway operation not found")
        return row[0]

    def record_protocol_message(self, operation_id: str, record_type: str, message) -> None:
        """Durably journal one versioned Broker/TEG request or response before reuse."""
        if record_type not in _PROTOCOL_CONTRACTS or not isinstance(message, _PROTOCOL_CONTRACTS[record_type]):
            raise GatewayStoreError("unsupported or untyped Gateway protocol record")
        record_json = message.to_json()
        digest_field = message.DIGEST_FIELD
        message_digest = getattr(message, digest_field)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM gateway_operations WHERE operation_id=?", (operation_id,)).fetchone() is None:
                raise GatewayStoreError("Gateway operation not found")
            prior = db.execute(
                "SELECT record_json,record_digest FROM gateway_protocol_records "
                "WHERE operation_id=? AND record_type=? ORDER BY rowid DESC LIMIT 1",
                (operation_id, record_type),
            ).fetchone()
            if prior:
                if prior[0] == record_json and prior[1] == message_digest:
                    return
                if record_type not in {"authorization_state_query", "authorization_state",
                                       "reconciliation_request", "reconciliation_result"}:
                    raise GatewayConflict("protocol operation reused with divergent parameters")
            record_id = uuid4().hex
            created_at = _now()
            db.execute("INSERT INTO gateway_protocol_records VALUES(?,?,?,?,?,?)",
                       (record_id, operation_id, record_type, record_json, message_digest, created_at))
            event_id = self._append_event(db, operation_id, "gateway.protocol.recorded",
                                          {"record_id": record_id, "record_type": record_type,
                                           "record_digest": message_digest}, created_at)
            db.execute("UPDATE gateway_operations SET changed_at=?,last_event_id=? WHERE operation_id=?",
                       (created_at, event_id, operation_id))

    def protocol_records(self, operation_id: str, record_type: str | None = None) -> tuple[dict, ...]:
        if record_type is None:
            rows = self.db.execute("SELECT * FROM gateway_protocol_records WHERE operation_id=? ORDER BY rowid",
                                   (operation_id,)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM gateway_protocol_records WHERE operation_id=? "
                                   "AND record_type=? ORDER BY rowid", (operation_id, record_type)).fetchall()
        return tuple(dict(row) for row in rows)

    def get_reservation(self, operation_id: str) -> ConsumptionReservation | None:
        row = self.db.execute("SELECT reservation_json FROM gateway_reservations WHERE operation_id=?",
                              (operation_id,)).fetchone()
        return ConsumptionReservation.from_json(row[0]) if row else None

    def get_confirmation(self, operation_id: str) -> ConsumptionConfirmation | None:
        row = self.db.execute("SELECT consumption_json FROM gateway_authorizations WHERE operation_id=?",
                              (operation_id,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        return ConsumptionConfirmation.from_json(row[0]) if "confirmation_id" in data else None

    def record_reservation(self, operation_id: str, reservation: ConsumptionReservation) -> None:
        with self.transaction() as db:
            op = db.execute("SELECT state,submission_json FROM gateway_operations WHERE operation_id=?",
                            (operation_id,)).fetchone()
            if op is None or op[0] != "awaiting_authorization":
                raise GatewayConflict("operation is not awaiting reservation")
            submission = GatewaySubmission.from_json(op[1])
            binding = ExecutionBinding.from_submission(submission, self.identity)
            request_row = db.execute(
                "SELECT record_json FROM gateway_protocol_records WHERE operation_id=? "
                "AND record_type='reservation_request' ORDER BY rowid DESC LIMIT 1", (operation_id,)
            ).fetchone()
            query_row = db.execute(
                "SELECT record_json FROM gateway_protocol_records WHERE operation_id=? "
                "AND record_type='authorization_query_result' ORDER BY rowid DESC LIMIT 1", (operation_id,)
            ).fetchone()
            if request_row is None or query_row is None:
                raise GatewayIntegrityError("reservation lacks a durable authorization query")
            request = ConsumptionReservationRequest.from_json(request_row[0])
            query = AuthorizationQueryResult.from_json(query_row[0])
            if (reservation.status != "reserved" or reservation.binding_digest != binding.binding_digest
                    or request.binding.binding_digest != binding.binding_digest
                    or reservation.request_id != request.request_id
                    or reservation.request_digest != request.request_digest
                    or reservation.expires_at > request.expires_at
                    or _timestamp(reservation.expires_at, "reservation_expiry") <= datetime.now(timezone.utc)
                    or _timestamp(query.expires_at, "query_expiry") <= datetime.now(timezone.utc)
                    or request.query_result_digest != query.result_digest
                    or query.status != "approved" or query.binding_digest != binding.binding_digest):
                raise GatewayIntegrityError("reservation does not match approved immutable operation")
            body = reservation.to_json()
            db.execute("INSERT INTO gateway_reservations VALUES(?,?,?,?,?,?,?)",
                       (operation_id, reservation.reservation_id, body, reservation.reservation_digest,
                        reservation.request_digest, reservation.binding_digest, _now()))
            at = _now()
            event_id = self._append_event(db, operation_id, "gateway.authorization.reserved",
                                          {"reservation_digest": reservation.reservation_digest,
                                           "binding_digest": reservation.binding_digest}, at)
            db.execute("UPDATE gateway_operations SET changed_at=?,last_event_id=? WHERE operation_id=?",
                       (at, event_id, operation_id))

    def mark_validated(self, operation_id: str) -> None:
        with self.transaction() as db:
            self._transition(db, operation_id, "received", "validated", "gateway.operation.validated")

    def await_authorization(self, operation_id: str) -> None:
        with self.transaction() as db:
            self._transition(db, operation_id, "validated", "awaiting_authorization",
                             "gateway.operation.awaiting_authorization")

    def mark_authority_call_started(self, operation_id: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT state,authorization_started FROM gateway_operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row is None or row[0] != "execution_prepared" or row[1]:
                raise GatewayConflict("authority call cannot be started in current state")
            has_reservation = db.execute("SELECT 1 FROM gateway_reservations WHERE operation_id=?",
                                         (operation_id,)).fetchone() is not None
            if not has_reservation:
                raise GatewayIntegrityError("consumption confirmation lacks a durable reservation")
            event_id = self._append_event(db, operation_id, "gateway.authorization.confirm_intent",
                                          {"operation_id": operation_id}, _now())
            db.execute("UPDATE gateway_operations SET authorization_started=1,changed_at=?,last_event_id=? WHERE operation_id=?",
                       (_now(), event_id, operation_id))

    def record_authorization(self, operation_id: str, consumption: ConsumptionConfirmation) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT state,authorization_started,submission_json FROM gateway_operations "
                             "WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or row[1] != 1:
                raise GatewayConflict("operation lacks durable confirmation intent")
            submission = GatewaySubmission.from_json(row[2])
            intent = submission.intent
            checked_at = datetime.now(timezone.utc)
            if not isinstance(consumption, ConsumptionConfirmation):
                raise GatewayStoreError("typed consumption confirmation required")
            reservation_row = db.execute("SELECT reservation_json FROM gateway_reservations WHERE operation_id=?",
                                         (operation_id,)).fetchone()
            request_row = db.execute("SELECT record_json FROM gateway_protocol_records WHERE operation_id=? "
                                     "AND record_type='confirmation_request' ORDER BY rowid DESC LIMIT 1",
                                     (operation_id,)).fetchone()
            if row[0] != "execution_prepared" or row[1] != 1 or reservation_row is None or request_row is None:
                raise GatewayConflict("operation lacks durable confirmation intent, preparation, or reservation")
            reservation = ConsumptionReservation.from_json(reservation_row[0])
            request = ConsumptionConfirmationRequest.from_json(request_row[0])
            binding = ExecutionBinding.from_submission(submission, self.identity)
            if (consumption.status != "confirmed" or consumption.binding_digest != binding.binding_digest
                    or consumption.reservation_digest != reservation.reservation_digest
                    or consumption.request_id != request.request_id
                    or _timestamp(consumption.expires_at, "confirmation_expiry") <= checked_at
                    or _timestamp(consumption.consumed_at, "consumed_at") > checked_at):
                raise GatewayIntegrityError("consumption confirmation is unbound, expired, or not confirmed")
            digest, record_id = consumption.confirmation_digest, consumption.confirmation_id
            transition_from = "execution_prepared"
            event = "gateway.authorization.consumption_confirmed"
            authorization_digest = consumption.confirmation_digest
            body = json.dumps(consumption.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            db.execute("INSERT INTO gateway_authorizations VALUES(?,?,?,?)",
                       (operation_id, body, digest, record_id))
            self._transition(db, operation_id, transition_from, "authorized", event,
                             {"consumption_digest": digest, "authorization_digest": authorization_digest})
            db.execute("UPDATE gateway_operations SET authorization_started=0 WHERE operation_id=?",
                       (operation_id,))

    def record_preparation(self, preparation: GatewayPreparation) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT state,submission_json FROM gateway_operations WHERE operation_id=?",
                             (preparation.operation_id,)).fetchone()
            if row is None:
                raise GatewayConflict("operation not found")
            submission = GatewaySubmission.from_json(row[1])
            reservation = db.execute("SELECT 1 FROM gateway_reservations WHERE operation_id=?",
                                     (preparation.operation_id,)).fetchone()
            if row[0] == "awaiting_authorization" and reservation is not None:
                source = "awaiting_authorization"
            elif row[0] == "authorized":
                source = "authorized"
            else:
                raise GatewayConflict("operation is not reserved or authorized")
            if (preparation.intent_digest != submission.intent_digest
                    or preparation.target_id != submission.intent.target_id
                    or preparation.payload_digest != submission.intent.payload_digest):
                raise GatewayIntegrityError("preparation does not match immutable submission")
            body = json.dumps(preparation.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            db.execute("INSERT INTO gateway_preparations VALUES(?,?,?)",
                       (preparation.operation_id, body, preparation.preparation_digest))
            self._transition(db, preparation.operation_id, source, "execution_prepared",
                             "gateway.execution.prepared",
                             {"preparation_digest": preparation.preparation_digest})

    def mark_executing(self, operation_id: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT state FROM gateway_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise GatewayConflict("operation not found")
            auth = db.execute("SELECT consumption_json FROM gateway_authorizations WHERE operation_id=?",
                              (operation_id,)).fetchone()
            reservation = db.execute("SELECT 1 FROM gateway_reservations WHERE operation_id=?",
                                     (operation_id,)).fetchone()
            if row[0] != "authorized" or auth is None or reservation is None:
                raise GatewayConflict("execution requires confirmed reserved authorization")
            if "confirmation_id" not in json.loads(auth[0]):
                raise GatewayConflict("legacy authorization cannot start Nível 2 execution")
            self._transition(db, operation_id, "authorized", "executing", "gateway.execution.started")

    def record_receipt(self, receipt: ExecutionReceipt | GatewayExecutionReceipt) -> None:
        operation_id = (receipt.binding.operation_id if isinstance(receipt, GatewayExecutionReceipt)
                        else receipt.operation_id)
        target = {"succeeded": "succeeded", "failed_before_effect": "failed",
                 "failed_verified": "failed", "unknown": "unknown"}.get(receipt.status)
        if target is None:
            raise GatewayStoreError("unsupported receipt status")
        with self.transaction() as db:
            operation = db.execute(
                "SELECT state,submission_json FROM gateway_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is None or operation[0] != "executing":
                raise GatewayConflict("operation is not executing")
            submission = GatewaySubmission.from_json(operation[1])
            auth = db.execute("SELECT consumption_json FROM gateway_authorizations WHERE operation_id=?",
                              (operation_id,)).fetchone()
            if isinstance(receipt, GatewayExecutionReceipt):
                reservation_row = db.execute("SELECT reservation_json FROM gateway_reservations WHERE operation_id=?",
                                             (operation_id,)).fetchone()
                if auth is None or reservation_row is None:
                    raise GatewayIntegrityError("protocol receipt lacks reservation or confirmation")
                confirmation = ConsumptionConfirmation.from_json(auth[0])
                reservation = ConsumptionReservation.from_json(reservation_row[0])
                binding = ExecutionBinding.from_submission(submission, self.identity)
                if (receipt.binding != binding or receipt.reservation_digest != reservation.reservation_digest
                        or receipt.confirmation_digest != confirmation.confirmation_digest
                        or confirmation.status != "confirmed"):
                    raise GatewayIntegrityError("protocol receipt does not match exact consumed operation")
            else:
                consumption = AuthorizationConsumption(**json.loads(auth[0])) if auth else None
                if (consumption is None or receipt.intent_digest != submission.intent_digest
                        or receipt.authorization_digest != consumption.authorization_digest
                        or receipt.target_id != submission.intent.target_id
                        or receipt.executor_id != self.identity.gateway_id
                        or receipt.payload_digest != submission.intent.payload_digest):
                    raise GatewayIntegrityError("receipt does not match operation and consumed authorization")
            db.execute("INSERT INTO gateway_receipts VALUES(?,?,?)",
                       (operation_id, receipt.to_json(), receipt.receipt_digest))
            self._transition(db, operation_id, "executing", target,
                             "gateway.execution.completed",
                             {"receipt_digest": receipt.receipt_digest, "receipt_status": receipt.status,
                              **({"effect_idempotency": receipt.effect_idempotency}
                                 if isinstance(receipt, GatewayExecutionReceipt) else {})})

    def record_failure(self, operation_id: str, reason_code: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT state FROM gateway_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or "failed" not in _TRANSITIONS.get(row[0], set()):
                raise GatewayConflict("operation cannot fail from current state")
            self._transition(db, operation_id, row[0], "failed", "gateway.operation.failed",
                             {"reason_code": reason_code})

    def mark_unknown(self, operation_id: str, reason_code: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT state FROM gateway_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or "unknown" not in _TRANSITIONS.get(row[0], set()):
                if row is not None and row[0] == "unknown":
                    return
                raise GatewayConflict("operation cannot become unknown from current state")
            self._transition(db, operation_id, row[0], "unknown", "gateway.operation.unknown",
                             {"reason_code": reason_code})

    def _recover_interrupted(self) -> None:
        with self.transaction() as db:
            rows = db.execute("SELECT operation_id,state,authorization_started FROM gateway_operations "
                              "WHERE state IN ('awaiting_authorization','execution_prepared','executing') "
                              "ORDER BY operation_id").fetchall()
            for operation_id, state, auth_started in rows:
                if state == "executing":
                    journaled_receipt = db.execute(
                        "SELECT record_json FROM gateway_protocol_records WHERE operation_id=? "
                        "AND record_type='execution_receipt' ORDER BY rowid DESC LIMIT 1",
                        (operation_id,),
                    ).fetchone()
                    if journaled_receipt is not None:
                        # A receipt durably journaled before the crash is a known result;
                        # materialize it without invoking the handler or authority again.
                        receipt = GatewayExecutionReceipt.from_json(journaled_receipt[0])
                        submission_row = db.execute(
                            "SELECT submission_json FROM gateway_operations WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()
                        submission = GatewaySubmission.from_json(submission_row[0])
                        reservation_row = db.execute(
                            "SELECT reservation_json FROM gateway_reservations WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()
                        confirmation_row = db.execute(
                            "SELECT consumption_json FROM gateway_authorizations WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()
                        if reservation_row is None or confirmation_row is None:
                            raise GatewayIntegrityError("journaled receipt lost authorization lineage")
                        reservation = ConsumptionReservation.from_json(reservation_row[0])
                        confirmation = ConsumptionConfirmation.from_json(confirmation_row[0])
                        binding = ExecutionBinding.from_submission(submission, self.identity)
                        if (receipt.binding != binding
                                or receipt.reservation_digest != reservation.reservation_digest
                                or receipt.confirmation_digest != confirmation.confirmation_digest):
                            raise GatewayIntegrityError("journaled receipt does not match operation binding")
                        target = {"succeeded": "succeeded", "failed_before_effect": "failed",
                                  "failed_verified": "failed", "unknown": "unknown"}.get(receipt.status)
                        if target is None:
                            raise GatewayIntegrityError("journaled receipt status is unsupported")
                        db.execute("INSERT INTO gateway_receipts VALUES(?,?,?)",
                                   (operation_id, receipt.to_json(), receipt.receipt_digest))
                        self._transition(
                            db, operation_id, "executing", target,
                            "gateway.execution.recovered_receipt",
                            {"receipt_digest": receipt.receipt_digest,
                             "receipt_status": receipt.status, "recovered": True},
                        )
                        continue
                reservation_call_unanswered = False
                if state == "awaiting_authorization":
                    request = db.execute(
                        "SELECT rowid FROM gateway_protocol_records WHERE operation_id=? "
                        "AND record_type='reservation_request' ORDER BY rowid DESC LIMIT 1",
                        (operation_id,),
                    ).fetchone()
                    if request is not None:
                        response = db.execute(
                            "SELECT 1 FROM gateway_protocol_records WHERE operation_id=? "
                            "AND record_type='reservation' AND rowid>? LIMIT 1",
                            (operation_id, request[0]),
                        ).fetchone()
                        reservation_call_unanswered = response is None
                if (state == "executing"
                        or (state in {"awaiting_authorization", "execution_prepared"} and auth_started)
                        or reservation_call_unanswered):
                    self._transition(db, operation_id, state, "unknown", "gateway.operation.recovered_unknown",
                                     {"reason_code": ("reservation_response_missing" if reservation_call_unanswered
                                                       else "interrupted_after_external_boundary")})

    def events(self, operation_id: str | None = None) -> tuple[dict, ...]:
        if operation_id is None:
            rows = self.db.execute("SELECT * FROM gateway_events ORDER BY sequence").fetchall()
        else:
            rows = self.db.execute("SELECT * FROM gateway_events WHERE operation_id=? ORDER BY sequence",
                                    (operation_id,)).fetchall()
        return tuple({**dict(row), "payload": json.loads(row["payload_json"])} for row in rows)

    def backup_to(self, destination: str | Path) -> Path:
        """Create and verify a consistent, create-only copy of the Gateway journal."""
        target = self._safe_data_path(destination)
        if target == self.path or target.exists():
            raise GatewayStoreError("backup destination must be new and distinct")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp-" + uuid4().hex)
        try:
            backup_db = sqlite3.connect(temporary, isolation_level=None)
            try:
                with self._mutex:
                    self.db.backup(backup_db)
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
            finally:
                backup_db.close()
            self._verify_path(temporary)
            os.link(temporary, target)  # create-only; never replace existing evidence/data
            temporary.unlink()
            return target
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def restore_backup(cls, backup: str | Path, destination: str | Path) -> Path:
        """Restore a verified backup to a new path; never overwrite or migrate it."""
        source = cls._safe_data_path(backup)
        target = cls._safe_data_path(destination)
        if source == target or target.exists():
            raise GatewayStoreError("restore destination must be new and distinct")
        cls._verify_path(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".restore-" + uuid4().hex)
        try:
            source_db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            restored_db = sqlite3.connect(temporary, isolation_level=None)
            try:
                source_db.backup(restored_db)
            finally:
                restored_db.close()
                source_db.close()
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            cls._verify_path(temporary)
            os.link(temporary, target)
            temporary.unlink()
            return target
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @staticmethod
    def _safe_data_path(path: str | Path) -> Path:
        resolved = Path(path).expanduser().resolve()
        if any(part.casefold() == ".agentflow" for part in resolved.parts):
            raise GatewayStoreError("Gateway data must remain outside project .agentflow/ state")
        return resolved

    @classmethod
    def _verify_path(cls, path: Path) -> None:
        """Verify a standalone store read-only, without migration or recovery writes."""
        database = None
        try:
            database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                       timeout=BUSY_TIMEOUT_MS / 1000)
            database.row_factory = sqlite3.Row
            if database.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise GatewayIntegrityError("backup is not a Gateway database")
            if database.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise GatewayIntegrityError("Gateway backup schema version mismatch")
            if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise GatewayIntegrityError("Gateway SQLite integrity_check failed")
            if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise GatewayIntegrityError("Gateway backup foreign_key_check failed")
            identity_row = database.execute(
                "SELECT identity_json FROM gateway_identity"
            ).fetchone()
            if identity_row is None:
                raise GatewayIntegrityError("Gateway backup identity is missing")
            try:
                identity = GatewayIdentity(**json.loads(identity_row[0]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise GatewayIntegrityError("Gateway backup identity is invalid") from exc
            checker = object.__new__(cls)
            checker.db = database
            checker.identity = identity
            checker.verify_integrity()
        except sqlite3.DatabaseError as exc:
            raise GatewayIntegrityError("Gateway backup is corrupt or unreadable") from exc
        finally:
            if database is not None:
                database.close()

    def verify_integrity(self) -> None:
        if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise GatewayIntegrityError("SQLite quick_check failed")
        if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise GatewayIntegrityError("Gateway foreign key check failed")
        identity_rows = self.db.execute("SELECT gateway_id,identity_json,identity_digest FROM gateway_identity").fetchall()
        if len(identity_rows) != 1:
            raise GatewayIntegrityError("Gateway identity row missing or duplicated")
        try:
            identity = GatewayIdentity(**json.loads(identity_rows[0][1]))
        except (TypeError, ValueError) as exc:
            raise GatewayIntegrityError("Gateway identity contract invalid") from exc
        if (identity.gateway_id, identity.identity_digest) != (identity_rows[0][0], identity_rows[0][2]):
            raise GatewayIntegrityError("Gateway identity projection diverged")
        previous = _ZERO_DIGEST
        projected: dict[str, tuple[str, int]] = {}
        for row in self.db.execute("SELECT * FROM gateway_events ORDER BY sequence"):
            payload = json.loads(row["payload_json"])
            body = {"event_id": row["event_id"], "operation_id": row["operation_id"],
                    "event_type": row["event_type"], "payload": payload,
                    "occurred_at": row["occurred_at"], "previous_digest": row["previous_digest"]}
            if row["previous_digest"] != previous or digest_json(body) != row["event_digest"]:
                raise GatewayIntegrityError("Gateway event hash chain invalid")
            previous = row["event_digest"]
            op, kind = row["operation_id"], row["event_type"]
            current = projected.get(op)
            if kind == "gateway.operation.received":
                if current is not None or not isinstance(payload.get("submission_digest"), str):
                    raise GatewayIntegrityError("invalid initial Gateway event")
                projected[op] = ("received", row["event_id"])
                continue
            if kind in {"gateway.authorization.consume_intent", "gateway.authorization.confirm_intent"}:
                expected_state = ("execution_prepared" if kind.endswith("confirm_intent")
                                  else "awaiting_authorization")
                if current is None or current[0] != expected_state:
                    raise GatewayIntegrityError("authorization call intent out of sequence")
                projected[op] = (current[0], row["event_id"])
                continue
            if kind == "gateway.authorization.reserved":
                if current is None or current[0] != "awaiting_authorization":
                    raise GatewayIntegrityError("authorization reservation out of sequence")
                projected[op] = (current[0], row["event_id"])
                continue
            if kind == "gateway.protocol.recorded":
                if current is None or not isinstance(payload.get("record_digest"), str):
                    raise GatewayIntegrityError("protocol record event out of sequence")
                projected[op] = (current[0], row["event_id"])
                continue
            if current is None or payload.get("from") != current[0]:
                raise GatewayIntegrityError("Gateway event transition source mismatch")
            target = payload.get("to")
            if target not in _TRANSITIONS.get(current[0], set()):
                raise GatewayIntegrityError("Gateway event transition invalid")
            projected[op] = (target, row["event_id"])
        for row in self.db.execute("SELECT * FROM gateway_operations"):
            try:
                submission = GatewaySubmission.from_json(row["submission_json"])
            except (GatewayContractError, ProtocolError) as exc:
                raise GatewayIntegrityError("stored Gateway submission invalid") from exc
            if (submission.operation_id != row["operation_id"]
                    or submission.submission_digest != row["submission_digest"]
                    or projected.get(row["operation_id"]) != (row["state"], row["last_event_id"])):
                raise GatewayIntegrityError("Gateway operation state does not match its journal")
            if row["authorization_started"] and not any(
                    event["event_type"] in {"gateway.authorization.consume_intent",
                                            "gateway.authorization.confirm_intent"}
                    for event in self.events(row["operation_id"])):
                raise GatewayIntegrityError("authorization_started lacks durable intent event")
        for row in self.db.execute("SELECT * FROM gateway_reservations"):
            try:
                reservation = ConsumptionReservation.from_json(row["reservation_json"])
            except (GatewayContractError, ValueError) as exc:
                raise GatewayIntegrityError("stored reservation contract invalid") from exc
            if (reservation.reservation_id != row["reservation_id"]
                    or reservation.reservation_digest != row["reservation_digest"]
                    or reservation.request_digest != row["request_digest"]
                    or reservation.binding_digest != row["binding_digest"]):
                raise GatewayIntegrityError("reservation projection diverged")
            submission = self.get_submission(row["operation_id"])
            if ExecutionBinding.from_submission(submission, self.identity).binding_digest != reservation.binding_digest:
                raise GatewayIntegrityError("reservation diverges from exact operation binding")
        for row in self.db.execute("SELECT * FROM gateway_protocol_records ORDER BY rowid"):
            contract_type = _PROTOCOL_CONTRACTS.get(row["record_type"])
            if contract_type is None:
                raise GatewayIntegrityError("unknown Gateway protocol record type")
            try:
                message = contract_type.from_json(row["record_json"])
            except (GatewayContractError, TypeError, ValueError) as exc:
                raise GatewayIntegrityError("stored Gateway protocol message invalid") from exc
            if getattr(message, message.DIGEST_FIELD) != row["record_digest"]:
                raise GatewayIntegrityError("Gateway protocol record digest diverged")
            event_found = self.db.execute(
                "SELECT 1 FROM gateway_events WHERE operation_id=? AND event_type='gateway.protocol.recorded' "
                "AND json_extract(payload_json,'$.record_id')=? AND json_extract(payload_json,'$.record_digest')=?",
                (row["operation_id"], row["record_id"], row["record_digest"]),
            ).fetchone()
            if event_found is None:
                raise GatewayIntegrityError("protocol record lacks matching audit event")
            if row["record_type"] == "execution_receipt":
                receipt = GatewayExecutionReceipt.from_json(row["record_json"])
                submission = self.get_submission(row["operation_id"])
                reservation_row = self.db.execute(
                    "SELECT reservation_json FROM gateway_reservations WHERE operation_id=?",
                    (row["operation_id"],),
                ).fetchone()
                confirmation_row = self.db.execute(
                    "SELECT consumption_json FROM gateway_authorizations WHERE operation_id=?",
                    (row["operation_id"],),
                ).fetchone()
                if reservation_row is None or confirmation_row is None:
                    raise GatewayIntegrityError("journaled execution receipt has orphaned authorization")
                reservation = ConsumptionReservation.from_json(reservation_row[0])
                confirmation = ConsumptionConfirmation.from_json(confirmation_row[0])
                binding = ExecutionBinding.from_submission(submission, self.identity)
                if (receipt.binding != binding
                        or receipt.reservation_digest != reservation.reservation_digest
                        or receipt.confirmation_digest != confirmation.confirmation_digest):
                    raise GatewayIntegrityError("journaled execution receipt binding diverged")
        for row in self.db.execute("SELECT operation_id,consumption_json,consumption_digest,consumption_id FROM gateway_authorizations"):
            try:
                value = json.loads(row[1])
                consumption = (ConsumptionConfirmation.from_json(row[1]) if "confirmation_id" in value
                               else AuthorizationConsumption(**value))
            except (TypeError, ValueError) as exc:
                raise GatewayIntegrityError("stored authorization consumption invalid") from exc
            operation = self.db.execute("SELECT submission_json,authorization_started FROM gateway_operations "
                                        "WHERE operation_id=?", (row[0],)).fetchone()
            submission = GatewaySubmission.from_json(operation[0])
            if isinstance(consumption, ConsumptionConfirmation):
                reservation_row = self.db.execute("SELECT reservation_json FROM gateway_reservations "
                                                  "WHERE operation_id=?", (row[0],)).fetchone()
                if reservation_row is None:
                    raise GatewayIntegrityError("consumption confirmation has no reservation")
                reservation = ConsumptionReservation.from_json(reservation_row[0])
                binding = ExecutionBinding.from_submission(submission, self.identity)
                if (consumption.confirmation_digest != row[2] or consumption.confirmation_id != row[3]
                        or consumption.status != "confirmed"
                        or consumption.binding_digest != binding.binding_digest
                        or consumption.reservation_digest != reservation.reservation_digest):
                    raise GatewayIntegrityError("stored consumption confirmation binding diverged")
            elif (consumption.operation_id != row[0] or consumption.consumption_digest != row[2]
                  or consumption.consumption_id != row[3] or consumption.status != "consumed"
                  or consumption.request_id != submission.request_id
                  or consumption.intent_digest != submission.intent_digest
                  or consumption.audience != self.identity.gateway_id
                  or consumption.target_id != submission.intent.target_id
                  or consumption.payload_digest != submission.intent.payload_digest):
                raise GatewayIntegrityError("stored authorization does not match operation")
        for row in self.db.execute("SELECT operation_id,preparation_json,preparation_digest FROM gateway_preparations"):
            try:
                preparation = GatewayPreparation(**json.loads(row[1]))
            except (TypeError, ValueError) as exc:
                raise GatewayIntegrityError("stored preparation invalid") from exc
            if preparation.operation_id != row[0] or preparation.preparation_digest != row[2]:
                raise GatewayIntegrityError("preparation projection diverged")
            submission = self.get_submission(row[0])
            if (preparation.intent_digest != submission.intent_digest
                    or preparation.target_id != submission.intent.target_id
                    or preparation.payload_digest != submission.intent.payload_digest):
                raise GatewayIntegrityError("preparation does not match immutable submission")
        for row in self.db.execute("SELECT operation_id,receipt_json,receipt_digest FROM gateway_receipts"):
            try:
                receipt_data = json.loads(row[1])
                receipt = (GatewayExecutionReceipt.from_json(row[1]) if "binding" in receipt_data
                           else ExecutionReceipt.from_json(row[1]))
            except (ProtocolError, ValueError) as exc:
                raise GatewayIntegrityError("stored execution receipt invalid") from exc
            submission = self.get_submission(row[0])
            if isinstance(receipt, GatewayExecutionReceipt):
                reservation_row = self.db.execute("SELECT reservation_json FROM gateway_reservations "
                                                  "WHERE operation_id=?", (row[0],)).fetchone()
                auth_row = self.db.execute("SELECT consumption_json FROM gateway_authorizations "
                                           "WHERE operation_id=?", (row[0],)).fetchone()
                if reservation_row is None or auth_row is None:
                    raise GatewayIntegrityError("protocol receipt lacks authorization records")
                reservation = ConsumptionReservation.from_json(reservation_row[0])
                confirmation = ConsumptionConfirmation.from_json(auth_row[0])
                binding = ExecutionBinding.from_submission(submission, self.identity)
                if (receipt.receipt_digest != row[2] or receipt.binding != binding
                        or receipt.reservation_digest != reservation.reservation_digest
                        or receipt.confirmation_digest != confirmation.confirmation_digest):
                    raise GatewayIntegrityError("protocol receipt projection diverged")
            else:
                if (receipt.operation_id != row[0] or receipt.receipt_digest != row[2]
                        or receipt.executor_id != self.identity.gateway_id
                        or receipt.executor_version != self.identity.gateway_version):
                    raise GatewayIntegrityError("receipt projection diverged")
                authorization = self.db.execute("SELECT consumption_json FROM gateway_authorizations "
                                                "WHERE operation_id=?", (row[0],)).fetchone()
                if authorization is None:
                    raise GatewayIntegrityError("receipt has no persisted authorization")
                consumption = AuthorizationConsumption(**json.loads(authorization[0]))
                if (receipt.intent_digest != submission.intent_digest
                        or receipt.authorization_digest != consumption.authorization_digest
                        or receipt.target_id != submission.intent.target_id
                        or receipt.payload_digest != submission.intent.payload_digest):
                    raise GatewayIntegrityError("receipt is not linked to immutable authorization")
            expected_state = {"succeeded": "succeeded", "failed_before_effect": "failed",
                              "failed_verified": "failed", "unknown": "unknown"}.get(receipt.status)
            operation_state = self.db.execute(
                "SELECT state FROM gateway_operations WHERE operation_id=?", (row[0],)
            ).fetchone()
            event = self.db.execute(
                "SELECT payload_json FROM gateway_events WHERE operation_id=? AND event_type IN "
                "('gateway.execution.completed','gateway.execution.recovered_receipt') "
                "ORDER BY sequence DESC LIMIT 1", (row[0],),
            ).fetchone()
            if expected_state is None or operation_state[0] != expected_state or event is None:
                raise GatewayIntegrityError("receipt state does not match materialized execution state")
            event_payload = json.loads(event[0])
            if (event_payload.get("receipt_digest") != row[2]
                    or event_payload.get("receipt_status") != receipt.status):
                raise GatewayIntegrityError("receipt state does not match its audit event")
