"""Isolated single-instance SQLite persistence for the dormant Broker protocol.

This is a storage primitive, not an Approval Broker service. It has no network listener,
grant issuer, consent interface, or provider execution path. WebAuthn proof verification
only authenticates an assertion and cannot create an approval or execution authorization.
State transition helpers that accept authority results are private and require their
caller to have verified an external proof; no such caller exists in AgentFlow yet.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
from typing import Iterator
from uuid import uuid4

from .protocol import (
    AcceptedSnapshot,
    ApprovalDecision,
    ApprovalRequest,
    ExecutionAuthorization,
    ExecutionReceipt,
    OperationIntent,
    PROTOCOL_VERSION,
    ProtocolError,
    SnapshotProposal,
    canonical_json,
    digest_json,
    parse_canonical_json,
    utc_now,
)
from .identity import (
    ClientAuthorizationDecision, ClientCredential, ClientIdentity, ClientPolicy,
    ClientRecord, CredentialRecord,
    PolicyRecord, certificate_fingerprint, evaluate_client_operation,
)
from .webauthn import (
    CredentialStateRecord, WebAuthnAssertion, WebAuthnChallenge,
    WebAuthnChallengeBinding, WebAuthnCredential, WebAuthnError,
    WebAuthnVerificationError, WebAuthnVerificationResult, b64url_encode, build_binding, challenge_digest,
    verify_assertion_signature, WEBAUTHN_BINDING_VERSION,
)


SCHEMA_VERSION = 3
APPLICATION_ID = int.from_bytes(b"AFBR", "big")
BUSY_TIMEOUT_MS = 5_000
_REQUEST_TRANSITIONS = {
    "pending": {"approved", "rejected", "expired", "unknown"},
    "approved": {"revoked", "expired", "unknown"},
    "rejected": set(), "expired": set(), "revoked": set(), "unknown": set(),
}
_SNAPSHOT_TRANSITIONS = {
    "pending": {"accepted", "rejected", "expired"},
    "accepted": {"revoked"},
    "rejected": set(), "expired": set(), "revoked": set(),
}
_AUTHORIZATION_TRANSITIONS = {
    "approved": {"consumed", "revoked", "expired", "unknown"},
    "consumed": set(), "rejected": set(), "expired": set(), "revoked": set(), "unknown": set(),
}


class BrokerStoreError(RuntimeError):
    """The isolated Broker store is unavailable or invalid."""


class BrokerConflict(BrokerStoreError):
    """An operation ID or immutable record was reused with different parameters."""


class BrokerIntegrityError(BrokerStoreError):
    """SQLite, digest, event chain, or materialized state failed verification."""


class BrokerStoreLocked(BrokerStoreError):
    """Another process already owns the single-instance Broker store lock."""


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: str
    state: str
    canonical_digest: str
    event_id: str


@dataclass(frozen=True)
class RequestRecord:
    request: ApprovalRequest
    state: str
    last_event_id: str


class _ProcessLock:
    """Small non-blocking OS file lock used to enforce a single store owner."""

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
            raise BrokerStoreLocked("Broker store is already locked by another process") from exc

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


def _safe_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if any(part.casefold() == ".agentflow" for part in resolved.parts):
        raise BrokerStoreError("Broker data must not be stored inside .agentflow/")
    return resolved


_SCHEMA = (
    "CREATE TABLE broker_snapshots (snapshot_id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('specification','plan','checkpoint','payload')), project_id TEXT NOT NULL, revision_id TEXT NOT NULL, schema_version INTEGER NOT NULL CHECK(schema_version>0), content_json TEXT NOT NULL, canonical_digest TEXT NOT NULL, provenance_json TEXT NOT NULL, parent_digest TEXT, operation_id TEXT NOT NULL UNIQUE, proposed_by TEXT NOT NULL, proposed_at TEXT NOT NULL)",
    "CREATE TABLE snapshot_state (snapshot_id TEXT PRIMARY KEY REFERENCES broker_snapshots(snapshot_id), status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','expired','revoked')), actor_id TEXT NOT NULL, changed_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TABLE operation_intents (operation_id TEXT PRIMARY KEY, intent_json TEXT NOT NULL, intent_digest TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, specification_snapshot_id TEXT NOT NULL REFERENCES broker_snapshots(snapshot_id), plan_snapshot_id TEXT NOT NULL REFERENCES broker_snapshots(snapshot_id), checkpoint_snapshot_id TEXT NOT NULL REFERENCES broker_snapshots(snapshot_id), payload_snapshot_id TEXT NOT NULL REFERENCES broker_snapshots(snapshot_id))",
    "CREATE TABLE approval_requests (request_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, action_code TEXT NOT NULL, request_json TEXT NOT NULL, request_digest TEXT NOT NULL, nonce_digest TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, UNIQUE(operation_id,action_code), FOREIGN KEY(operation_id) REFERENCES operation_intents(operation_id))",
    "CREATE TABLE approval_request_state (request_id TEXT PRIMARY KEY REFERENCES approval_requests(request_id), status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired','revoked','unknown')), changed_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TABLE approval_decisions (decision_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES approval_requests(request_id), operation_id TEXT NOT NULL, intent_digest TEXT NOT NULL, request_digest TEXT NOT NULL, challenge_digest TEXT NOT NULL UNIQUE, decision_json TEXT NOT NULL, decision_digest TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL)",
    "CREATE TABLE execution_authorizations (authorization_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES approval_requests(request_id), operation_id TEXT NOT NULL UNIQUE, nonce_digest TEXT NOT NULL UNIQUE, authorization_json TEXT NOT NULL, authorization_digest TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL)",
    "CREATE TABLE authorization_state (authorization_id TEXT PRIMARY KEY REFERENCES execution_authorizations(authorization_id), status TEXT NOT NULL CHECK(status IN ('approved','rejected','expired','revoked','consumed','unknown')), changed_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TABLE execution_receipts (receipt_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operation_intents(operation_id), authorization_id TEXT NOT NULL REFERENCES execution_authorizations(authorization_id), receipt_json TEXT NOT NULL, receipt_digest TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL)",
    "CREATE TABLE execution_receipt_state (operation_id TEXT PRIMARY KEY REFERENCES operation_intents(operation_id), receipt_id TEXT NOT NULL REFERENCES execution_receipts(receipt_id), status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed_before_effect','failed_verified','unknown')), changed_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TABLE broker_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL, previous_digest TEXT NOT NULL, event_digest TEXT NOT NULL UNIQUE)",
    "CREATE INDEX broker_events_aggregate ON broker_events(aggregate_type,aggregate_id,sequence)",
    "CREATE TRIGGER broker_snapshots_no_update BEFORE UPDATE ON broker_snapshots BEGIN SELECT RAISE(ABORT,'Broker snapshots are immutable'); END",
    "CREATE TRIGGER broker_snapshots_no_delete BEFORE DELETE ON broker_snapshots BEGIN SELECT RAISE(ABORT,'Broker snapshots are immutable'); END",
    "CREATE TRIGGER broker_intents_no_update BEFORE UPDATE ON operation_intents BEGIN SELECT RAISE(ABORT,'operation intents are immutable'); END",
    "CREATE TRIGGER broker_intents_no_delete BEFORE DELETE ON operation_intents BEGIN SELECT RAISE(ABORT,'operation intents are immutable'); END",
    "CREATE TRIGGER broker_decisions_no_update BEFORE UPDATE ON approval_decisions BEGIN SELECT RAISE(ABORT,'approval decisions are append-only'); END",
    "CREATE TRIGGER broker_decisions_no_delete BEFORE DELETE ON approval_decisions BEGIN SELECT RAISE(ABORT,'approval decisions are append-only'); END",
    "CREATE TRIGGER broker_events_no_update BEFORE UPDATE ON broker_events BEGIN SELECT RAISE(ABORT,'Broker events are append-only'); END",
    "CREATE TRIGGER broker_events_no_delete BEFORE DELETE ON broker_events BEGIN SELECT RAISE(ABORT,'Broker events are append-only'); END",
    "CREATE TRIGGER broker_authz_no_update BEFORE UPDATE ON execution_authorizations BEGIN SELECT RAISE(ABORT,'execution authorizations are immutable'); END",
    "CREATE TRIGGER broker_authz_no_delete BEFORE DELETE ON execution_authorizations BEGIN SELECT RAISE(ABORT,'execution authorizations are immutable'); END",
    "CREATE TRIGGER broker_receipts_no_update BEFORE UPDATE ON execution_receipts BEGIN SELECT RAISE(ABORT,'execution receipts are immutable'); END",
    "CREATE TRIGGER broker_receipts_no_delete BEFORE DELETE ON execution_receipts BEGIN SELECT RAISE(ABORT,'execution receipts are immutable'); END",
)

_SCHEMA_V2 = (
    "CREATE TABLE broker_clients (client_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, identity_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','revoked')), created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TRIGGER broker_clients_identity_immutable BEFORE UPDATE OF client_id,principal_id,identity_json,created_at,expires_at ON broker_clients BEGIN SELECT RAISE(ABORT,'Broker client identity is immutable'); END",
    "CREATE TABLE broker_client_credentials (fingerprint TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES broker_clients(client_id), credential_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','rotated','revoked')), issued_at TEXT NOT NULL, not_before TEXT NOT NULL, expires_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TRIGGER broker_client_credentials_immutable BEFORE UPDATE OF fingerprint,client_id,credential_json,issued_at,not_before,expires_at ON broker_client_credentials BEGIN SELECT RAISE(ABORT,'Broker credential identity is immutable'); END",
    "CREATE UNIQUE INDEX broker_one_active_credential ON broker_client_credentials(client_id) WHERE status='active'",
    "CREATE TABLE broker_client_policies (client_id TEXT NOT NULL REFERENCES broker_clients(client_id), policy_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), policy_json TEXT NOT NULL, policy_digest TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','revoked')), created_at TEXT NOT NULL, last_event_id TEXT NOT NULL, PRIMARY KEY(client_id,policy_id,version), UNIQUE(policy_digest))",
    "CREATE UNIQUE INDEX broker_one_active_policy ON broker_client_policies(client_id,policy_id) WHERE status='active'",
    "CREATE TABLE broker_client_operations (operation_id TEXT PRIMARY KEY REFERENCES operation_intents(operation_id), client_id TEXT NOT NULL REFERENCES broker_clients(client_id), request_digest TEXT NOT NULL, nonce_digest TEXT NOT NULL UNIQUE, policy_id TEXT NOT NULL, policy_version INTEGER NOT NULL, claimed_at TEXT NOT NULL, FOREIGN KEY(client_id,policy_id,policy_version) REFERENCES broker_client_policies(client_id,policy_id,version))",
    "CREATE TRIGGER broker_clients_no_delete BEFORE DELETE ON broker_clients BEGIN SELECT RAISE(ABORT,'Broker client identity history is retained'); END",
    "CREATE TRIGGER broker_client_credentials_no_delete BEFORE DELETE ON broker_client_credentials BEGIN SELECT RAISE(ABORT,'Broker credential history is retained'); END",
    "CREATE TRIGGER broker_client_policies_no_update BEFORE UPDATE OF policy_json,policy_digest,version,client_id,policy_id,created_at ON broker_client_policies BEGIN SELECT RAISE(ABORT,'Broker client policies are immutable'); END",
    "CREATE TRIGGER broker_client_policies_no_delete BEFORE DELETE ON broker_client_policies BEGIN SELECT RAISE(ABORT,'Broker policy history is retained'); END",
    "CREATE TRIGGER broker_client_operations_no_update BEFORE UPDATE ON broker_client_operations BEGIN SELECT RAISE(ABORT,'Broker operation replay records are immutable'); END",
    "CREATE TRIGGER broker_client_operations_no_delete BEFORE DELETE ON broker_client_operations BEGIN SELECT RAISE(ABORT,'Broker operation replay records are retained'); END",
)

_SCHEMA_V3 = (
    "CREATE TABLE webauthn_credentials (credential_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, rp_id TEXT NOT NULL, credential_json TEXT NOT NULL, credential_digest TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)",
    "CREATE TABLE webauthn_credential_state (credential_id TEXT PRIMARY KEY REFERENCES webauthn_credentials(credential_id), status TEXT NOT NULL CHECK(status IN ('active','revoked')), sign_count INTEGER NOT NULL CHECK(sign_count BETWEEN 0 AND 4294967295), counter_mode TEXT NOT NULL CHECK(counter_mode IN ('zero','monotonic')), changed_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TABLE webauthn_challenges (challenge_id TEXT PRIMARY KEY, challenge_digest TEXT NOT NULL UNIQUE, binding_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL)",
    "CREATE TABLE webauthn_challenge_state (challenge_id TEXT PRIMARY KEY REFERENCES webauthn_challenges(challenge_id), status TEXT NOT NULL CHECK(status IN ('pending','verifying','consumed','rejected','expired','unknown')), changed_at TEXT NOT NULL, last_event_id TEXT NOT NULL)",
    "CREATE TRIGGER webauthn_credentials_no_update BEFORE UPDATE ON webauthn_credentials BEGIN SELECT RAISE(ABORT,'WebAuthn credential records are immutable'); END",
    "CREATE TRIGGER webauthn_credentials_no_delete BEFORE DELETE ON webauthn_credentials BEGIN SELECT RAISE(ABORT,'WebAuthn credential history is retained'); END",
    "CREATE TRIGGER webauthn_challenges_no_update BEFORE UPDATE ON webauthn_challenges BEGIN SELECT RAISE(ABORT,'WebAuthn challenge bindings are immutable'); END",
    "CREATE TRIGGER webauthn_challenges_no_delete BEFORE DELETE ON webauthn_challenges BEGIN SELECT RAISE(ABORT,'WebAuthn challenge history is retained'); END",
)

_TABLES = (
    "execution_receipt_state", "execution_receipts", "authorization_state",
    "execution_authorizations", "approval_decisions", "approval_request_state",
    "approval_requests", "operation_intents", "snapshot_state", "broker_events",
    "broker_snapshots", "broker_client_operations", "broker_client_policies",
    "broker_client_credentials", "broker_clients", "webauthn_challenge_state",
    "webauthn_challenges", "webauthn_credential_state", "webauthn_credentials",
)


class BrokerStore:
    """Dedicated durable store; deliberately disconnected from AgentFlow SQLiteStore."""

    def __init__(self, path: str | Path):
        self.path = _safe_path(path)
        self._mutex = threading.RLock()
        self._process_lock = _ProcessLock(self.path.with_name(self.path.name + ".lock"))
        self._validate_existing_path_identity()
        self._process_lock.acquire()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000,
                                      isolation_level=None, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            self.db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._validate_identity_before_mutation()
            self.db.execute("PRAGMA foreign_keys=ON")
            if self.db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise BrokerStoreError("SQLite foreign key enforcement unavailable")
            journal_mode = self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if journal_mode.casefold() != "wal":
                raise BrokerStoreError("SQLite WAL mode is unavailable for Broker storage")
            self.db.execute("PRAGMA synchronous=FULL")
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                os.chmod(self._process_lock.path, 0o600)
            self._migrate()
            self.verify_integrity()
            self._recover_webauthn_verifications()
            self.verify_integrity()
        except Exception:
            if hasattr(self, "db"):
                self.db.close()
            self._process_lock.release()
            raise

    def _validate_identity_before_mutation(self) -> None:
        application_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        tables = self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if application_id not in (0, APPLICATION_ID):
            raise BrokerStoreError("database belongs to a different application")
        if application_id == 0 and tables:
            raise BrokerStoreError("refusing to reuse a non-empty database without Broker identity")

    def _validate_existing_path_identity(self) -> None:
        """Reject a foreign DB before creating the Broker lock sidecar beside it."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        try:
            uri = self.path.as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
            try:
                app_id = db.execute("PRAGMA application_id").fetchone()[0]
                tables = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if app_id not in (0, APPLICATION_ID) or (app_id == 0 and tables):
                    raise BrokerStoreError("refusing to reuse a non-empty database without Broker identity")
            finally:
                db.close()
        except sqlite3.DatabaseError as exc:
            raise BrokerStoreError("existing database is unreadable and was not modified") from exc

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        app_id = self.db.execute("PRAGMA application_id").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise BrokerStoreError(f"unsupported Broker schema version: {version}")
        if version == SCHEMA_VERSION:
            if app_id != APPLICATION_ID:
                raise BrokerStoreError("Broker schema identity mismatch")
            return
        if version not in (0, 1, 2) or app_id not in (0, APPLICATION_ID):
            raise BrokerStoreError("unsupported Broker schema migration path")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            if version == 0:
                for statement in _SCHEMA:
                    self.db.execute(statement)
                for statement in _SCHEMA_V2:
                    self.db.execute(statement)
                self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                version = 2
            if version == 1:
                for statement in _SCHEMA_V2:
                    self.db.execute(statement)
                version = 2
            if version == 2:
                for statement in _SCHEMA_V3:
                    self.db.execute(statement)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()
        except sqlite3.Error as exc:
            self.db.rollback()
            raise BrokerStoreError("Broker schema migration failed and was rolled back") from exc

    def close(self) -> None:
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        self._process_lock.release()

    def __enter__(self) -> "BrokerStore":
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

    def _store_webauthn_credential(self, credential: WebAuthnCredential) -> CredentialStateRecord:
        """Persist public credential metadata; no ceremony/enrollment endpoint calls this."""
        if not isinstance(credential, WebAuthnCredential):
            raise BrokerStoreError("WebAuthn credential contract is required")
        with self.transaction() as db:
            prior = db.execute(
                "SELECT credential_json FROM webauthn_credentials WHERE credential_id=?",
                (credential.credential_id,),
            ).fetchone()
            if prior:
                if prior[0] != credential.to_json():
                    raise BrokerConflict("credential ID reused with different public-key metadata")
                row = db.execute(
                    "SELECT status,sign_count,counter_mode,last_event_id FROM webauthn_credential_state WHERE credential_id=?",
                    (credential.credential_id,),
                ).fetchone()
                return CredentialStateRecord(credential, *row[:3], row[3])
            event_id, _ = self._append_event(
                db, "webauthn_credential", credential.credential_id,
                "webauthn_credential.registered",
                {"status": "active", "credential_digest": credential.credential_digest},
                credential.created_at,
            )
            db.execute(
                "INSERT INTO webauthn_credentials VALUES(?,?,?,?,?,?)",
                (credential.credential_id, credential.principal_id, credential.rp_id,
                 credential.to_json(), credential.credential_digest, credential.created_at),
            )
            db.execute(
                "INSERT INTO webauthn_credential_state VALUES(?,?,?,?,?,?)",
                (credential.credential_id, "active", 0, "zero", credential.created_at, event_id),
            )
            return CredentialStateRecord(credential, "active", 0, "zero", event_id)

    def build_consent_presentation(self, request_id: str, *, now: datetime | None = None):
        """Build a deterministic display model in a read transaction; no state is changed."""
        from .presentation import build_presentation_from_connection
        with self._mutex:
            self.db.execute("BEGIN")
            try:
                value = build_presentation_from_connection(self.db, request_id, now=now)
                self.db.rollback()
                return value
            except Exception:
                self.db.rollback()
                raise

    def revoke_webauthn_credential(self, credential_id: str, *, occurred_at: str | None = None) -> CredentialStateRecord:
        """Revoke a public credential record idempotently; never creates approval."""
        now = occurred_at or utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT c.credential_json,s.status,s.sign_count,s.counter_mode,s.last_event_id "
                "FROM webauthn_credentials c JOIN webauthn_credential_state s USING(credential_id) "
                "WHERE c.credential_id=?", (credential_id,),
            ).fetchone()
            if row is None:
                raise BrokerStoreError("unknown WebAuthn credential")
            credential = WebAuthnCredential.from_json(row[0])
            if row[1] == "revoked":
                return CredentialStateRecord(credential, row[1], row[2], row[3], row[4])
            event_id, _ = self._append_event(
                db, "webauthn_credential", credential_id, "webauthn_credential.revoked",
                {"from": "active", "to": "revoked", "credential_digest": credential.credential_digest}, now,
            )
            db.execute(
                "UPDATE webauthn_credential_state SET status='revoked',changed_at=?,last_event_id=? WHERE credential_id=?",
                (now, event_id, credential_id),
            )
            return CredentialStateRecord(credential, "revoked", row[2], row[3], event_id)

    def _create_webauthn_challenge(self, request_id: str, approver_principal: str,
                                   rp_id: str, allowed_origins: tuple[str, ...], *,
                                   presentation_digest: str,
                                   now: datetime | None = None, ttl_seconds: int = 120) -> WebAuthnChallenge:
        """Create an in-memory ceremony challenge bound to a pending operation request."""
        instant = now or datetime.now(timezone.utc)
        if (instant.tzinfo is None or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120
                or type(approver_principal) is not str or not approver_principal):
            raise BrokerStoreError("invalid WebAuthn challenge clock or lifetime")
        if not _is_digest(presentation_digest):
            raise BrokerStoreError("a valid presentation digest is required before WebAuthn challenge creation")
        created = instant.astimezone(timezone.utc)
        created_text = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
        raw_challenge = secrets.token_bytes(32)
        challenge_id = uuid4().hex
        challenge_value = b64url_encode(raw_challenge)
        expiry = created + timedelta(seconds=ttl_seconds)
        with self.transaction() as db:
            from .presentation import build_presentation_from_connection
            presentation = build_presentation_from_connection(db, request_id, now=created)
            if presentation.presentation_digest != presentation_digest:
                raise BrokerConflict("displayed presentation changed before challenge creation")
            request_row = db.execute(
                "SELECT r.request_json,r.request_digest,r.operation_id,r.expires_at,s.status "
                "FROM approval_requests r JOIN approval_request_state s USING(request_id) WHERE r.request_id=?",
                (request_id,),
            ).fetchone()
            if request_row is None or request_row[4] != "pending":
                raise BrokerStoreError("approval request is missing or not pending")
            request_data = json.loads(request_row[0])
            request_data["status"] = request_row[4]
            request = ApprovalRequest.from_dict(request_data)
            intent_row = db.execute(
                "SELECT intent_json,intent_digest FROM operation_intents WHERE operation_id=?",
                (request_row[2],),
            ).fetchone()
            if intent_row is None:
                raise BrokerIntegrityError("approval request has no operation intent")
            intent = OperationIntent.from_json(intent_row[0])
            if intent.intent_digest != request.intent_digest or intent.intent_digest != intent_row[1]:
                raise BrokerIntegrityError("approval request and intent digests diverge")
            if request.requester_principal != intent.principal_id:
                raise BrokerIntegrityError("approval requester diverges from operation principal")
            if approver_principal == intent.principal_id:
                raise BrokerStoreError("requester cannot serve as the independent approver")
            policy_claim = db.execute(
                "SELECT op.client_id,op.policy_id,op.policy_version,p.policy_digest,p.policy_json "
                "FROM broker_client_operations op JOIN broker_client_policies p "
                "ON p.client_id=op.client_id AND p.policy_id=op.policy_id AND p.version=op.policy_version "
                "JOIN broker_clients c ON c.client_id=op.client_id "
                "WHERE op.operation_id=? AND p.status='active' AND c.status='active' "
                "AND EXISTS (SELECT 1 FROM broker_client_credentials cc WHERE cc.client_id=c.client_id "
                "AND cc.status='active' AND cc.expires_at>?)", (intent.operation_id, created_text),
            ).fetchone()
            if policy_claim is None:
                raise BrokerStoreError("no authorized client policy is linked to this operation")
            if policy_claim[0] != intent.client_id:
                raise BrokerIntegrityError("operation client identity diverges from authorization claim")
            policy = ClientPolicy.from_json(policy_claim[4])
            credential_row = db.execute(
                "SELECT c.credential_json,s.status FROM webauthn_credentials c "
                "JOIN webauthn_credential_state s USING(credential_id) "
                "WHERE c.principal_id=? AND c.rp_id=? AND s.status='active'",
                (approver_principal, rp_id),
            ).fetchall()
            if not credential_row:
                raise BrokerStoreError("no active WebAuthn credential for approver and RP")
            credentials = [WebAuthnCredential.from_json(row[0]) for row in credential_row]
            origins = tuple(sorted(set(allowed_origins)))
            if not origins or any(origin not in credential.allowed_origins for credential in credentials
                                  for origin in origins):
                raise BrokerStoreError("challenge origins do not match registered credential policy")
            snapshot_digests = {}
            for kind, snapshot_id in (("specification", intent.specification_snapshot_id),
                                      ("plan", intent.plan_snapshot_id),
                                      ("checkpoint", intent.checkpoint_snapshot_id),
                                      ("payload", intent.payload_snapshot_id)):
                snap = db.execute(
                    "SELECT b.canonical_digest,s.status FROM broker_snapshots b JOIN snapshot_state s USING(snapshot_id) "
                    "WHERE b.snapshot_id=? AND b.kind=?", (snapshot_id, kind),
                ).fetchone()
                if snap is None or snap[1] != "accepted":
                    raise BrokerStoreError("operation snapshot is missing or not accepted")
                snapshot_digests[kind] = snap[0]
            remaining = min((_dt(request.expires_at) - created).total_seconds(),
                            (_dt(intent.expires_at) - created).total_seconds())
            effective_ttl = min(ttl_seconds, int(remaining))
            if effective_ttl < 1:
                raise BrokerStoreError("operation approval window expired")
            expiry = created + timedelta(seconds=effective_ttl)
            existing = db.execute(
                "SELECT 1 FROM webauthn_challenges c JOIN webauthn_challenge_state s USING(challenge_id) "
                "WHERE json_extract(c.binding_json,'$.request_id')=? AND s.status IN ('pending','verifying')",
                (request_id,),
            ).fetchone()
            if existing:
                raise BrokerConflict("an unconsumed WebAuthn challenge already exists for request")
            binding = build_binding(
                WebAuthnChallengeBinding, protocol_version=PROTOCOL_VERSION,
                challenge_id=challenge_id, challenge_digest=challenge_digest(raw_challenge),
                approver_principal=approver_principal, requester_principal=intent.principal_id,
                operation_id=intent.operation_id, request_id=request.request_id,
                request_digest=request.request_digest, intent_digest=intent.intent_digest,
                action_code=intent.action_code, target_id=intent.target_id,
                scope_digest=digest_json(list(policy.required_scopes)),
                client_policy_digest=policy.policy_digest,
                specification_digest=snapshot_digests["specification"],
                plan_digest=snapshot_digests["plan"], checkpoint_digest=snapshot_digests["checkpoint"],
                payload_digest=snapshot_digests["payload"], catalog_digest=intent.catalog_digest,
                policy_digest=intent.policy_digest, rp_id=rp_id, allowed_origins=origins,
                created_at=created_text,
                expires_at=expiry.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                binding_version=WEBAUTHN_BINDING_VERSION, presentation_version=presentation.presentation_version,
                presentation_digest=presentation.presentation_digest,
            )
            event_id, _ = self._append_event(
                db, "webauthn_challenge", challenge_id, "webauthn_challenge.created",
                {"status": "pending", "binding_digest": binding.binding_digest,
                 "challenge_digest": binding.challenge_digest, "request_id": request_id}, created_text,
            )
            db.execute("INSERT INTO webauthn_challenges VALUES(?,?,?,?,?)",
                       (challenge_id, binding.challenge_digest, binding.to_json(), created_text, binding.expires_at))
            db.execute("INSERT INTO webauthn_challenge_state VALUES(?,?,?,?)",
                       (challenge_id, "pending", created_text, event_id))
        return WebAuthnChallenge(binding, challenge_value)

    def verify_webauthn_assertion(self, assertion: WebAuthnAssertion, *,
                                  now: datetime | None = None):
        """Verify one assertion once; returns authentication only, never consent or a grant."""
        if not isinstance(assertion, WebAuthnAssertion):
            raise WebAuthnVerificationError("assertion_contract_invalid")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise WebAuthnVerificationError("invalid_clock")
        now_text = instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        expired = False
        with self.transaction() as db:
            row = db.execute(
                "SELECT c.binding_json,s.status FROM webauthn_challenges c "
                "JOIN webauthn_challenge_state s USING(challenge_id) WHERE c.challenge_id=?",
                (assertion.challenge_id,),
            ).fetchone()
            if row is None:
                raise WebAuthnVerificationError("challenge_unknown")
            binding = WebAuthnChallengeBinding.from_json(row[0])
            if (binding.binding_version != WEBAUTHN_BINDING_VERSION
                    or binding.presentation_version != 1 or binding.presentation_digest is None):
                raise WebAuthnVerificationError("presentation_binding_required")
            if row[1] != "pending":
                raise WebAuthnVerificationError("challenge_replay_or_not_pending")
            if _dt(binding.expires_at) <= instant.astimezone(timezone.utc):
                self._change_webauthn_challenge(db, binding.challenge_id, "pending", "expired", now_text,
                                                "webauthn_challenge.expired", "challenge_expired")
                expired = True
                credential_row = None
            else:
                self._change_webauthn_challenge(db, binding.challenge_id, "pending", "verifying", now_text,
                                                "webauthn_challenge.verification_started", None)
                credential_row = db.execute(
                    "SELECT c.credential_json,s.status,s.sign_count,s.counter_mode FROM webauthn_credentials c "
                    "JOIN webauthn_credential_state s USING(credential_id) WHERE c.credential_id=?",
                    (assertion.credential_id,),
                ).fetchone()
        if expired:
            raise WebAuthnVerificationError("challenge_expired")
        try:
            if credential_row is None:
                raise WebAuthnVerificationError("credential_unknown")
            credential = WebAuthnCredential.from_json(credential_row[0])
            if credential_row[1] != "active":
                raise WebAuthnVerificationError("credential_revoked")
            from .webauthn import _parse_client_data, b64url_decode
            client_data = _parse_client_data(assertion.client_data_json)
            raw_challenge = b64url_decode(client_data.get("challenge", ""), maximum=64, label="challenge")
            if len(raw_challenge) != 32 or challenge_digest(raw_challenge) != binding.challenge_digest:
                raise WebAuthnVerificationError("challenge_mismatch")
            counter, _signed_message = verify_assertion_signature(
                assertion, binding, credential, challenge_bytes=raw_challenge, now=instant)
            previous_counter = credential_row[2]
            mode = credential_row[3]
            if mode == "monotonic" and counter <= previous_counter:
                raise WebAuthnVerificationError("signature_counter_regressed")
            next_mode = "monotonic" if counter or previous_counter else "zero"
        except (WebAuthnError, ProtocolError) as exc:
            reason = str(exc).split(":", 1)[0]
            self._reject_webauthn_attempt(binding, reason, now_text)
            raise WebAuthnVerificationError(reason) from exc
        with self.transaction() as db:
            state = db.execute(
                "SELECT status FROM webauthn_challenge_state WHERE challenge_id=?", (binding.challenge_id,)
            ).fetchone()
            if state is None or state[0] != "verifying":
                raise WebAuthnVerificationError("challenge_state_changed")
            self._revalidate_webauthn_binding(db, binding, instant.astimezone(timezone.utc))
            credential_state = db.execute(
                "SELECT status,sign_count,counter_mode FROM webauthn_credential_state WHERE credential_id=?",
                (credential.credential_id,),
            ).fetchone()
            if credential_state is None or credential_state[0] != "active":
                raise WebAuthnVerificationError("credential_revoked")
            if credential_state[2] == "monotonic" and counter <= credential_state[1]:
                self._change_webauthn_challenge(db, binding.challenge_id, "verifying", "rejected", now_text,
                                                "webauthn_challenge.rejected", "signature_counter_regressed")
                raise WebAuthnVerificationError("signature_counter_regressed")
            cred_event, _ = self._append_event(
                db, "webauthn_credential", credential.credential_id,
                "webauthn_credential.counter_updated",
                {"status": "active", "from": credential_state[1], "to": counter,
                 "counter_mode": next_mode, "credential_digest": credential.credential_digest}, now_text,
            )
            db.execute(
                "UPDATE webauthn_credential_state SET sign_count=?,counter_mode=?,changed_at=?,last_event_id=? "
                "WHERE credential_id=?", (counter, next_mode, now_text, cred_event, credential.credential_id),
            )
            self._change_webauthn_challenge(db, binding.challenge_id, "verifying", "consumed", now_text,
                                            "webauthn_challenge.consumed", None)
        return WebAuthnVerificationResult(
            authenticated_principal=credential.principal_id, operation_id=binding.operation_id,
            challenge_id=binding.challenge_id, credential_digest=credential.credential_digest,
            binding_digest=binding.binding_digest, user_present=True, user_verified=True,
        )

    def _change_webauthn_challenge(self, db, challenge_id: str, source: str, target: str,
                                   when: str, event_type: str, reason: str | None) -> None:
        row = db.execute("SELECT status FROM webauthn_challenge_state WHERE challenge_id=?", (challenge_id,)).fetchone()
        if row is None or row[0] != source:
            raise BrokerConflict("WebAuthn challenge state changed")
        payload = {"from": source, "to": target}
        if reason:
            payload["reason"] = reason
        event_id, _ = self._append_event(db, "webauthn_challenge", challenge_id, event_type, payload, when)
        db.execute("UPDATE webauthn_challenge_state SET status=?,changed_at=?,last_event_id=? WHERE challenge_id=?",
                   (target, when, event_id, challenge_id))

    def _reject_webauthn_attempt(self, binding: WebAuthnChallengeBinding, reason: str, when: str) -> None:
        allowed = {"credential_unknown", "credential_revoked", "challenge_mismatch", "origin_mismatch",
                   "rp_id_hash_mismatch", "signature_invalid", "signature_format_invalid",
                   "user_presence_required", "user_verification_required", "credential_expired",
                   "challenge_expired", "client_data_type_mismatch", "cross_origin_not_supported",
                   "authenticator_data_size_or_extension_unsupported", "signature_counter_regressed",
                   "credential_binding_mismatch", "protocol_version_mismatch", "challenge_id_mismatch",
                   "credential_id_mismatch", "invalid_clock", "public_key_der_invalid",
                   "public_key_algorithm_mismatch", "algorithm_unsupported", "client_data_invalid_json",
                   "client_data_duplicate_key", "client_data_not_object", "client_data_size_invalid",
                   "approval_window_expired", "operation_binding_changed"}
        safe_reason = reason if reason in allowed else "assertion_rejected"
        with self.transaction() as db:
            state = db.execute("SELECT status FROM webauthn_challenge_state WHERE challenge_id=?",
                               (binding.challenge_id,)).fetchone()
            if state and state[0] == "verifying":
                self._change_webauthn_challenge(db, binding.challenge_id, "verifying", "rejected", when,
                                                "webauthn_challenge.rejected", safe_reason)

    def _revalidate_webauthn_binding(self, db, binding: WebAuthnChallengeBinding, now: datetime) -> None:
        request = db.execute(
            "SELECT r.request_json,r.request_digest,s.status FROM approval_requests r "
            "JOIN approval_request_state s USING(request_id) WHERE r.request_id=?", (binding.request_id,),
        ).fetchone()
        intent = db.execute("SELECT intent_json,intent_digest FROM operation_intents WHERE operation_id=?",
                            (binding.operation_id,)).fetchone()
        if (request is None or request[2] != "pending" or request[1] != binding.request_digest
                or intent is None or intent[1] != binding.intent_digest):
            raise WebAuthnVerificationError("operation_binding_changed")
        if _dt(binding.expires_at) <= now:
            raise WebAuthnVerificationError("challenge_expired")
        operation = OperationIntent.from_json(intent[0])
        if _dt(json.loads(request[0])["expires_at"]) <= now or _dt(operation.expires_at) <= now:
            raise WebAuthnVerificationError("approval_window_expired")
        req_data = json.loads(request[0])
        if (req_data["operation_id"] != binding.operation_id
                or req_data["action_code"] != binding.action_code
                or req_data["intent_digest"] != binding.intent_digest
                or operation.target_id != binding.target_id
                or operation.action_code != binding.action_code
                or operation.principal_id != binding.requester_principal
                or operation.policy_digest != binding.policy_digest
                or operation.catalog_digest != binding.catalog_digest):
            raise WebAuthnVerificationError("operation_binding_changed")
        policy = db.execute(
            "SELECT p.policy_digest FROM broker_client_operations o JOIN broker_client_policies p "
            "ON p.client_id=o.client_id AND p.policy_id=o.policy_id AND p.version=o.policy_version "
            "JOIN broker_clients c ON c.client_id=o.client_id "
            "WHERE o.operation_id=? AND p.status='active' AND c.status='active' "
            "AND EXISTS (SELECT 1 FROM broker_client_credentials cc WHERE cc.client_id=c.client_id "
            "AND cc.status='active' AND cc.expires_at>?)",
            (binding.operation_id, now.isoformat(timespec="microseconds").replace("+00:00", "Z")),
        ).fetchone()
        if policy is None or policy[0] != binding.client_policy_digest:
            raise WebAuthnVerificationError("operation_binding_changed")
        for snapshot_id, expected, kind in (
            (operation.specification_snapshot_id, binding.specification_digest, "specification"),
            (operation.plan_snapshot_id, binding.plan_digest, "plan"),
            (operation.checkpoint_snapshot_id, binding.checkpoint_digest, "checkpoint"),
            (operation.payload_snapshot_id, binding.payload_digest, "payload"),
        ):
            row = db.execute("SELECT b.canonical_digest,s.status FROM broker_snapshots b "
                             "JOIN snapshot_state s USING(snapshot_id) WHERE b.snapshot_id=? AND b.kind=?",
                             (snapshot_id, kind)).fetchone()
            if row is None or row[0] != expected or row[1] != "accepted":
                raise WebAuthnVerificationError("operation_binding_changed")
        from .presentation import build_presentation_from_connection
        presentation = build_presentation_from_connection(db, binding.request_id, now=now)
        if (presentation.presentation_version != binding.presentation_version
                or presentation.presentation_digest != binding.presentation_digest):
            raise WebAuthnVerificationError("presentation_digest_mismatch")

    def _recover_webauthn_verifications(self) -> None:
        with self.transaction() as db:
            rows = db.execute("SELECT challenge_id FROM webauthn_challenge_state WHERE status='verifying'").fetchall()
            for row in rows:
                self._change_webauthn_challenge(db, row[0], "verifying", "unknown", utc_now(),
                                                "webauthn_challenge.recovered_unknown", "interrupted_verification")

    def _append_event(self, db: sqlite3.Connection, aggregate_type: str, aggregate_id: str,
                      event_type: str, payload: dict, occurred_at: str) -> tuple[str, int]:
        previous = db.execute(
            "SELECT event_digest FROM broker_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_digest = previous[0] if previous else "sha256:" + "0" * 64
        event_id = uuid4().hex
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM broker_events").fetchone()[0]
        body = {"sequence": sequence, "event_id": event_id, "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id, "event_type": event_type,
                "payload": payload, "occurred_at": occurred_at,
                "previous_digest": previous_digest}
        event_digest = digest_json(body)
        db.execute(
            "INSERT INTO broker_events(sequence,event_id,aggregate_type,aggregate_id,event_type,"
            "payload_json,occurred_at,previous_digest,event_digest) VALUES(?,?,?,?,?,?,?,?,?)",
            (sequence, event_id, aggregate_type, aggregate_id, event_type,
             canonical_json(payload), occurred_at, previous_digest, event_digest),
        )
        return event_id, sequence

    def propose_snapshot(self, proposal: SnapshotProposal) -> SnapshotRecord:
        """Persist immutable candidate bytes as pending; this never accepts a snapshot."""
        if not isinstance(proposal, SnapshotProposal):
            raise BrokerStoreError("snapshot must be a SnapshotProposal")
        now = proposal.proposed_at
        with self.transaction() as db:
            prior = db.execute(
                "SELECT snapshot_id,canonical_digest FROM broker_snapshots WHERE operation_id=?",
                (proposal.operation_id,),
            ).fetchone()
            if prior:
                if prior[0] != proposal.snapshot_id or prior[1] != proposal.canonical_digest:
                    raise BrokerConflict("snapshot operation_id reused with different parameters")
                state = db.execute("SELECT status,last_event_id FROM snapshot_state WHERE snapshot_id=?",
                                   (prior[0],)).fetchone()
                return SnapshotRecord(prior[0], state[0], prior[1], state[1])
            if db.execute("SELECT 1 FROM broker_snapshots WHERE snapshot_id=?",
                          (proposal.snapshot_id,)).fetchone():
                raise BrokerConflict("snapshot_id already belongs to another operation")
            db.execute(
                "INSERT INTO broker_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal.snapshot_id, proposal.kind, proposal.project_id, proposal.revision_id,
                 proposal.schema_version, proposal.content_json, proposal.canonical_digest,
                 proposal.provenance_json, proposal.parent_digest, proposal.operation_id,
                 proposal.proposed_by, proposal.proposed_at),
            )
            event_id, _ = self._append_event(
                db, "snapshot", proposal.snapshot_id, "snapshot.proposed",
                {"status": "pending", "digest": proposal.canonical_digest,
                 "operation_id": proposal.operation_id}, now,
            )
            db.execute("INSERT INTO snapshot_state VALUES(?,?,?,?,?)",
                       (proposal.snapshot_id, "pending", proposal.proposed_by, now, event_id))
            return SnapshotRecord(proposal.snapshot_id, "pending", proposal.canonical_digest, event_id)

    def get_snapshot(self, snapshot_id: str) -> SnapshotProposal | AcceptedSnapshot:
        row = self.db.execute(
            "SELECT s.*, st.status,st.actor_id,st.changed_at FROM broker_snapshots s "
            "JOIN snapshot_state st USING(snapshot_id) WHERE s.snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise BrokerStoreError("snapshot not found")
        if row["status"] == "accepted":
            return AcceptedSnapshot(
                PROTOCOL_VERSION, row["snapshot_id"], row["kind"], row["project_id"],
                row["revision_id"], row["schema_version"], row["content_json"],
                row["canonical_digest"], row["provenance_json"], row["actor_id"],
                row["changed_at"], row["parent_digest"],
            )
        return SnapshotProposal(
            PROTOCOL_VERSION, row["snapshot_id"], row["kind"], row["project_id"],
            row["revision_id"], row["schema_version"], row["content_json"],
            row["canonical_digest"], row["provenance_json"], row["proposed_by"],
            row["operation_id"], row["proposed_at"], row["parent_digest"],
        )

    def _transition_snapshot(self, snapshot_id: str, target: str, actor_id: str,
                              occurred_at: str | None = None, *, proof_digest: str | None = None) -> None:
        """Internal projection primitive; only a future verified authority may call it."""
        occurred_at = occurred_at or utc_now()
        if target == "accepted" and not _is_digest(proof_digest):
            raise BrokerStoreError("snapshot acceptance requires verified authority proof")
        with self.transaction() as db:
            row = db.execute("SELECT status FROM snapshot_state WHERE snapshot_id=?",
                             (snapshot_id,)).fetchone()
            if row is None:
                raise BrokerStoreError("snapshot not found")
            current = row[0]
            if target not in _SNAPSHOT_TRANSITIONS.get(current, set()):
                raise BrokerConflict(f"invalid snapshot state transition: {current} -> {target}")
            event_id, _ = self._append_event(
                db, "snapshot", snapshot_id, "snapshot.status_changed",
                {"from": current, "to": target, "actor_id": actor_id,
                 "proof_digest": proof_digest}, occurred_at,
            )
            db.execute("UPDATE snapshot_state SET status=?,actor_id=?,changed_at=?,last_event_id=? "
                       "WHERE snapshot_id=?", (target, actor_id, occurred_at, event_id, snapshot_id))

    def store_intent(self, intent: OperationIntent) -> OperationIntent:
        if not isinstance(intent, OperationIntent):
            raise BrokerStoreError("intent has an invalid contract type")
        snapshot_refs = (
            (intent.specification_snapshot_id, intent.specification_digest, "specification",
             intent.specification_revision_id),
            (intent.plan_snapshot_id, intent.plan_digest, "plan", intent.plan_revision_id),
            (intent.checkpoint_snapshot_id, intent.checkpoint_digest, "checkpoint", None),
            (intent.payload_snapshot_id, intent.payload_digest, "payload", None),
        )
        with self.transaction() as db:
            prior = db.execute("SELECT intent_digest,intent_json FROM operation_intents WHERE operation_id=?",
                               (intent.operation_id,)).fetchone()
            if prior:
                if prior[0] != intent.intent_digest:
                    raise BrokerConflict("operation_id reused with divergent intent")
                return OperationIntent.from_json(prior[1])
            digest_owner = db.execute("SELECT operation_id FROM operation_intents WHERE intent_digest=?",
                                      (intent.intent_digest,)).fetchone()
            if digest_owner:
                raise BrokerConflict("intent digest already bound to a different operation_id")
            for snapshot_id, expected_digest, expected_kind, expected_revision in snapshot_refs:
                row = db.execute(
                    "SELECT s.kind,s.canonical_digest,st.status,s.project_id,s.revision_id FROM broker_snapshots s "
                    "JOIN snapshot_state st USING(snapshot_id) WHERE s.snapshot_id=?", (snapshot_id,)
                ).fetchone()
                if (row is None or row[0] != expected_kind or row[1] != expected_digest
                        or row[2] != "accepted" or row[3] != intent.project_id
                        or (expected_revision is not None and row[4] != expected_revision)):
                    raise BrokerIntegrityError("intent references missing, unaccepted, or divergent snapshot")
            # The same operation ID is intentionally not shared with snapshot proposal IDs.
            existing = db.execute("SELECT operation_id FROM operation_intents WHERE operation_id=?",
                                  (intent.operation_id,)).fetchone()
            if existing:
                raise BrokerConflict("operation_id already exists")
            db.execute("INSERT INTO operation_intents VALUES(?,?,?,?,?,?,?,?)",
                       (intent.operation_id, intent.to_json(), intent.intent_digest, intent.created_at,
                        intent.specification_snapshot_id, intent.plan_snapshot_id,
                        intent.checkpoint_snapshot_id, intent.payload_snapshot_id))
            self._append_event(db, "operation", intent.operation_id, "operation.intent_recorded",
                               {"intent_digest": intent.intent_digest}, intent.created_at)
            return intent

    def create_request(self, request: ApprovalRequest) -> RequestRecord:
        """Create only a pending request. There is deliberately no public approval method."""
        if not isinstance(request, ApprovalRequest) or request.status != "pending":
            raise BrokerStoreError("new approval requests must start pending")
        if _dt(request.expires_at) <= _dt(utc_now()):
            raise BrokerStoreError("expired requests cannot be persisted as pending")
        with self.transaction() as db:
            intent = db.execute("SELECT intent_digest,intent_json FROM operation_intents WHERE operation_id=?",
                                (request.operation_id,)).fetchone()
            intent_contract = OperationIntent.from_json(intent[1]) if intent else None
            if (intent is None or intent[0] != request.intent_digest
                    or _dt(request.created_at) < _dt(intent_contract.created_at)
                    or _dt(request.expires_at) > _dt(intent_contract.expires_at)):
                raise BrokerIntegrityError("request intent is missing or divergent")
            prior = db.execute(
                "SELECT r.request_digest,r.request_id,s.status,s.last_event_id FROM approval_requests r "
                "JOIN approval_request_state s USING(request_id) WHERE r.operation_id=? AND r.action_code=?",
                (request.operation_id, request.action_code),
            ).fetchone()
            if prior:
                if prior[0] != request.request_digest:
                    raise BrokerConflict("operation_id reused with divergent approval parameters")
                stored = db.execute("SELECT request_json FROM approval_requests WHERE request_id=?",
                                    (prior[1],)).fetchone()[0]
                value = json.loads(stored)
                value["status"] = prior[2]
                return RequestRecord(ApprovalRequest.from_dict(value), prior[2], prior[3])
            collision = db.execute("SELECT request_id FROM approval_requests WHERE request_id=? OR nonce_digest=?",
                                   (request.request_id, request.nonce_digest)).fetchone()
            if collision:
                raise BrokerConflict("request_id or nonce_digest already used")
            db.execute("INSERT INTO approval_requests VALUES(?,?,?,?,?,?,?,?)",
                       (request.request_id, request.operation_id, request.action_code,
                        request.to_json(), request.request_digest, request.nonce_digest,
                        request.created_at, request.expires_at))
            event_id, _ = self._append_event(
                db, "request", request.request_id, "approval_request.created",
                {"operation_id": request.operation_id, "intent_digest": request.intent_digest,
                 "status": "pending", "request_digest": request.request_digest}, request.created_at,
            )
            db.execute("INSERT INTO approval_request_state VALUES(?,?,?,?)",
                       (request.request_id, "pending", request.created_at, event_id))
            return RequestRecord(request, "pending", event_id)

    def get_request(self, request_id: str) -> RequestRecord:
        row = self.db.execute(
            "SELECT r.request_json,s.status,s.last_event_id FROM approval_requests r "
            "JOIN approval_request_state s USING(request_id) WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise BrokerStoreError("approval request not found")
        value = json.loads(row[0])
        value["status"] = row[1]
        return RequestRecord(ApprovalRequest.from_dict(value), row[1], row[2])

    def expire_pending_requests(self, as_of: str | None = None) -> int:
        """Deterministically expire only pending requests whose deadline has passed."""
        as_of = as_of or utc_now()
        _dt(as_of)
        pending = self.db.execute(
            "SELECT r.request_id,r.expires_at FROM approval_requests r "
            "JOIN approval_request_state s USING(request_id) WHERE s.status='pending' "
            "ORDER BY r.request_id"
        ).fetchall()
        expired = [row[0] for row in pending if _dt(row[1]) <= _dt(as_of)]
        for request_id in expired:
            self._transition_request(request_id, "expired", "broker-expiry-worker", as_of)
        return len(expired)

    def list_events(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> tuple[dict, ...]:
        query = "SELECT sequence,event_id,aggregate_type,aggregate_id,event_type,payload_json,occurred_at,previous_digest,event_digest FROM broker_events"
        clauses, args = [], []
        if aggregate_type is not None:
            clauses.append("aggregate_type=?")
            args.append(aggregate_type)
        if aggregate_id is not None:
            clauses.append("aggregate_id=?")
            args.append(aggregate_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY sequence"
        return tuple({**dict(row), "payload": json.loads(row["payload_json"])}
                     for row in self.db.execute(query, args).fetchall())

    def _transition_request(self, request_id: str, target: str, actor_id: str,
                            occurred_at: str | None = None, *, proof_digest: str | None = None) -> RequestRecord:
        """Internal projection primitive; callers must authenticate/verify authority first."""
        occurred_at = occurred_at or utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT r.operation_id,r.request_digest,r.request_json,s.status "
                "FROM approval_requests r JOIN approval_request_state s USING(request_id) "
                "WHERE r.request_id=?", (request_id,),
            ).fetchone()
            if row is None:
                raise BrokerStoreError("approval request not found")
            current = row[3]
            if target == current:
                return self.get_request(request_id)
            if target not in _REQUEST_TRANSITIONS.get(current, set()):
                raise BrokerConflict(f"invalid request state transition: {current} -> {target}")
            if target == "approved" and proof_digest is None:
                raise BrokerStoreError("approval transition requires verified proof digest")
            if proof_digest is not None and not _is_digest(proof_digest):
                raise BrokerStoreError("verified proof digest is invalid")
            event_id, _ = self._append_event(
                db, "request", request_id, "approval_request.transitioned",
                {"from": current, "to": target, "actor_id": actor_id,
                 "request_digest": row[1], "proof_digest": proof_digest}, occurred_at,
            )
            db.execute("UPDATE approval_request_state SET status=?,changed_at=?,last_event_id=? WHERE request_id=?",
                       (target, occurred_at, event_id, request_id))
            if target in {"approved", "rejected"}:
                raise BrokerStoreError("record a decision through the internal decision persistence flow")
        return self.get_request(request_id)

    def _record_decision(self, decision: ApprovalDecision) -> None:
        """Persist a decision already verified by a future trusted authority.

        This private primitive performs no WebAuthn or identity verification and is not
        called by the AgentFlow facade, CLI, or any runtime path.
        """
        if not isinstance(decision, ApprovalDecision):
            raise BrokerStoreError("decision contract invalid")
        if _dt(decision.expires_at) <= _dt(utc_now()):
            raise BrokerStoreError("expired decisions cannot be persisted as current")
        target = decision.decision
        with self.transaction() as db:
            row = db.execute(
                "SELECT r.operation_id,r.request_digest,s.status,i.intent_digest "
                "FROM approval_requests r JOIN approval_request_state s USING(request_id) "
                "JOIN operation_intents i USING(operation_id) WHERE r.request_id=?",
                (decision.request_id,),
            ).fetchone()
            if (row is None or row[0] != decision.operation_id or row[1] != decision.request_digest
                    or row[3] != decision.intent_digest):
                raise BrokerIntegrityError("decision does not match stored request and intent")
            prior = db.execute("SELECT decision_digest FROM approval_decisions WHERE decision_id=?",
                               (decision.decision_id,)).fetchone()
            if prior:
                if prior[0] != decision.decision_digest:
                    raise BrokerConflict("decision_id reused with divergent content")
                return
            if db.execute("SELECT 1 FROM approval_decisions WHERE challenge_digest=?",
                          (decision.challenge_digest,)).fetchone():
                raise BrokerConflict("WebAuthn challenge digest already used")
            if row[2] != "pending" or target not in {"approved", "rejected"}:
                raise BrokerConflict("decision is not valid for current request state")
            db.execute("INSERT INTO approval_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                       (decision.decision_id, decision.request_id, decision.operation_id,
                        decision.intent_digest, decision.request_digest, decision.challenge_digest,
                        decision.to_json(), decision.decision_digest,
                        decision.decided_at))
            event_id, _ = self._append_event(
                db, "request", decision.request_id, "approval_decision.recorded",
                {"decision_id": decision.decision_id, "decision_digest": decision.decision_digest,
                 "decision": target, "request_digest": row[1]}, decision.decided_at,
            )
            db.execute("UPDATE approval_request_state SET status=?,changed_at=?,last_event_id=? WHERE request_id=?",
                       (target, decision.decided_at, event_id, decision.request_id))

    def _record_authorization(self, authorization: ExecutionAuthorization) -> None:
        """Persist a verified Broker-to-TEG authorization; never returns a bearer grant."""
        if not isinstance(authorization, ExecutionAuthorization) or authorization.status != "approved":
            raise BrokerStoreError("authorization must be an approved protocol record")
        if _dt(authorization.expires_at) <= _dt(utc_now()):
            raise BrokerStoreError("expired execution authorization cannot be persisted")
        with self.transaction() as db:
            request = db.execute(
                "SELECT r.operation_id,s.status,i.intent_digest,r.expires_at FROM approval_requests r "
                "JOIN approval_request_state s USING(request_id) JOIN operation_intents i USING(operation_id) "
                "WHERE r.request_id=?", (authorization.request_id,),
            ).fetchone()
            if (request is None or request[0] != authorization.operation_id or request[1] != "approved"
                    or request[2] != authorization.intent_digest
                    or _dt(authorization.expires_at) > _dt(request[3])):
                raise BrokerIntegrityError("authorization does not match an approved request")
            decision_row = db.execute(
                "SELECT decision_json FROM approval_decisions WHERE request_id=? ORDER BY occurred_at DESC LIMIT 1",
                (authorization.request_id,),
            ).fetchone()
            if decision_row is None:
                raise BrokerIntegrityError("approved request has no durable decision")
            decision = ApprovalDecision.from_json(decision_row[0])
            if (decision.decision != "approved" or decision.intent_digest != authorization.intent_digest
                    or _dt(authorization.issued_at) < _dt(decision.decided_at)
                    or _dt(authorization.issued_at) >= _dt(decision.expires_at)):
                raise BrokerIntegrityError("authorization is outside the approved decision scope")
            prior = db.execute("SELECT authorization_digest FROM execution_authorizations WHERE operation_id=?",
                               (authorization.operation_id,)).fetchone()
            if prior:
                if prior[0] != authorization.authorization_digest:
                    raise BrokerConflict("operation already has a different authorization")
                return
            if db.execute("SELECT 1 FROM execution_authorizations WHERE authorization_id=?",
                          (authorization.authorization_id,)).fetchone():
                raise BrokerConflict("authorization_id already belongs to another operation")
            if db.execute("SELECT 1 FROM execution_authorizations WHERE nonce_digest=?",
                          (authorization.nonce_digest,)).fetchone():
                raise BrokerConflict("authorization nonce already used")
            db.execute("INSERT INTO execution_authorizations VALUES(?,?,?,?,?,?,?)",
                       (authorization.authorization_id, authorization.request_id, authorization.operation_id,
                        authorization.nonce_digest, authorization.to_json(),
                        authorization.authorization_digest, authorization.expires_at))
            event_id, _ = self._append_event(
                db, "authorization", authorization.authorization_id, "execution_authorization.created",
                {"operation_id": authorization.operation_id,
                 "authorization_digest": authorization.authorization_digest, "status": "approved"}, utc_now(),
            )
            changed_at = utc_now()
            db.execute("INSERT INTO authorization_state VALUES(?,?,?,?)",
                       (authorization.authorization_id, "approved", changed_at, event_id))

    def _transition_authorization(self, authorization_id: str, target: str, actor_id: str,
                                  occurred_at: str | None = None) -> None:
        occurred_at = occurred_at or utc_now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT s.status,a.operation_id,a.authorization_digest FROM authorization_state s "
                "JOIN execution_authorizations a USING(authorization_id) WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            if row is None:
                raise BrokerStoreError("authorization not found")
            if target == row[0]:
                return
            if target not in _AUTHORIZATION_TRANSITIONS.get(row[0], set()):
                raise BrokerConflict(f"invalid authorization transition: {row[0]} -> {target}")
            event_id, _ = self._append_event(
                db, "authorization", authorization_id, "execution_authorization.transitioned",
                {"from": row[0], "to": target, "actor_id": actor_id,
                 "operation_id": row[1], "authorization_digest": row[2]}, occurred_at,
            )
            db.execute("UPDATE authorization_state SET status=?,changed_at=?,last_event_id=? WHERE authorization_id=?",
                       (target, occurred_at, event_id, authorization_id))

    def _record_receipt(self, receipt: ExecutionReceipt) -> None:
        if not isinstance(receipt, ExecutionReceipt):
            raise BrokerStoreError("receipt contract invalid")
        with self.transaction() as db:
            authorization = db.execute(
                "SELECT a.authorization_id,a.authorization_digest,a.operation_id,s.status,a.authorization_json "
                "FROM execution_authorizations a JOIN authorization_state s USING(authorization_id) "
                "WHERE a.authorization_digest=?", (receipt.authorization_digest,),
            ).fetchone()
            authorization_contract = (ExecutionAuthorization.from_json(authorization[4])
                                      if authorization else None)
            intent = db.execute("SELECT intent_digest FROM operation_intents WHERE operation_id=?",
                                (receipt.operation_id,)).fetchone()
            if (authorization is None or authorization[2] != receipt.operation_id
                    or authorization_contract.target_id != receipt.target_id
                    or authorization[3] not in {"consumed", "unknown"}
                    or intent is None or intent[0] != receipt.intent_digest):
                raise BrokerIntegrityError("receipt diverges from authorization or operation")
            prior = db.execute("SELECT receipt_digest FROM execution_receipts WHERE receipt_id=?",
                               (receipt.receipt_id,)).fetchone()
            if prior:
                if prior[0] != receipt.receipt_digest:
                    raise BrokerConflict("receipt_id reused with divergent content")
                return
            current = db.execute("SELECT status FROM execution_receipt_state WHERE operation_id=?",
                                 (receipt.operation_id,)).fetchone()
            if current and current[0] != "pending":
                raise BrokerConflict("terminal receipt state cannot be overwritten")
            db.execute("INSERT INTO execution_receipts VALUES(?,?,?,?,?,?)",
                       (receipt.receipt_id, receipt.operation_id, authorization[0],
                        receipt.to_json(), receipt.receipt_digest, receipt.started_at))
            event_id, _ = self._append_event(
                db, "receipt", receipt.operation_id, "execution_receipt.recorded",
                {"receipt_id": receipt.receipt_id, "receipt_digest": receipt.receipt_digest,
                 "status": receipt.status, "authorization_digest": receipt.authorization_digest},
                receipt.completed_at or receipt.started_at,
            )
            db.execute(
                "INSERT INTO execution_receipt_state VALUES(?,?,?,?,?) "
                "ON CONFLICT(operation_id) DO UPDATE SET receipt_id=excluded.receipt_id,"
                "status=excluded.status,changed_at=excluded.changed_at,last_event_id=excluded.last_event_id",
                (receipt.operation_id, receipt.receipt_id, receipt.status,
                 receipt.completed_at or receipt.started_at, event_id),
            )

    def register_client(self, identity: ClientIdentity, credential: ClientCredential,
                        policy: ClientPolicy) -> None:
        """Register a client, initial public certificate identity and least-privilege policy."""
        if (credential.client_id != identity.client_id or policy.client_id != identity.client_id
                or identity.expires_at < credential.expires_at):
            raise BrokerStoreError("client, credential, and policy identity bindings disagree")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM broker_clients WHERE client_id=?", (identity.client_id,)).fetchone():
                raise BrokerConflict("client_id is already registered")
            now = identity.created_at
            db.execute("INSERT INTO broker_clients VALUES(?,?,?,?,?,?,?)",
                       (identity.client_id, identity.principal_id, identity.to_json(), "active",
                        identity.created_at, identity.expires_at, "pending"))
            event_id, _ = self._append_event(
                db, "client", identity.client_id, "client.registered",
                {"identity_digest": identity.identity_digest, "status": "active"}, now,
            )
            db.execute("UPDATE broker_clients SET last_event_id=? WHERE client_id=?",
                       (event_id, identity.client_id))
            self._insert_client_credential(db, credential)
            self._insert_client_policy(db, policy)

    def register_client_policy(self, policy: ClientPolicy) -> None:
        """Append a policy revision, revoking its former active revision atomically."""
        with self.transaction() as db:
            client = db.execute("SELECT status FROM broker_clients WHERE client_id=?",
                                (policy.client_id,)).fetchone()
            if client is None or client[0] != "active":
                raise BrokerStoreError("policy client is unknown or revoked")
            self._insert_client_policy(db, policy)

    def _insert_client_credential(self, db: sqlite3.Connection, credential: ClientCredential) -> None:
        if db.execute("SELECT 1 FROM broker_clients WHERE client_id=?", (credential.client_id,)).fetchone() is None:
            raise BrokerStoreError("credential refers to an unknown client")
        db.execute("INSERT INTO broker_client_credentials VALUES(?,?,?,?,?,?,?,?)",
                   (credential.fingerprint, credential.client_id, credential.to_json(), "active",
                    credential.issued_at, credential.not_before, credential.expires_at, "pending"))
        event_id, _ = self._append_event(
            db, "credential", credential.fingerprint, "credential.registered",
            {"client_id": credential.client_id, "credential_digest": credential.credential_digest,
             "status": "active"}, credential.issued_at,
        )
        db.execute("UPDATE broker_client_credentials SET last_event_id=? WHERE fingerprint=?",
                   (event_id, credential.fingerprint))

    def _insert_client_policy(self, db: sqlite3.Connection, policy: ClientPolicy) -> None:
        if db.execute("SELECT status FROM broker_clients WHERE client_id=?", (policy.client_id,)).fetchone() is None:
            raise BrokerStoreError("policy refers to an unknown client")
        versions = db.execute(
            "SELECT MAX(version) FROM broker_client_policies WHERE client_id=? AND policy_id=?",
            (policy.client_id, policy.policy_id),
        ).fetchone()[0]
        if versions is not None and policy.version <= versions:
            raise BrokerConflict("policy versions must increase monotonically")
        prior = db.execute(
            "SELECT version,last_event_id FROM broker_client_policies "
            "WHERE client_id=? AND policy_id=? AND status='active'",
            (policy.client_id, policy.policy_id),
        ).fetchone()
        if prior:
            event_id, _ = self._append_event(
                db, "policy", f"{policy.client_id}:{policy.policy_id}:{prior[0]}",
                "policy.status_changed", {"from": "active", "to": "revoked",
                                             "reason": "superseded_by_new_version"},
                policy.created_at,
            )
            db.execute("UPDATE broker_client_policies SET status='revoked',last_event_id=? "
                       "WHERE client_id=? AND policy_id=? AND version=?",
                       (event_id, policy.client_id, policy.policy_id, prior[0]))
        db.execute("INSERT INTO broker_client_policies VALUES(?,?,?,?,?,?,?,?)",
                   (policy.client_id, policy.policy_id, policy.version, policy.to_json(),
                    policy.policy_digest, "active", policy.created_at, "pending"))
        event_id, _ = self._append_event(
            db, "policy", f"{policy.client_id}:{policy.policy_id}:{policy.version}",
            "policy.registered", {"policy_digest": policy.policy_digest,
                                  "status": "active"}, policy.created_at,
        )
        db.execute("UPDATE broker_client_policies SET last_event_id=? "
                   "WHERE client_id=? AND policy_id=? AND version=?",
                   (event_id, policy.client_id, policy.policy_id, policy.version))

    def rotate_client_credential(self, client_id: str, old_fingerprint: str,
                                 replacement: ClientCredential) -> None:
        if replacement.client_id != client_id:
            raise BrokerStoreError("replacement credential belongs to a different client")
        with self.transaction() as db:
            row = db.execute("SELECT status FROM broker_client_credentials "
                             "WHERE fingerprint=? AND client_id=?", (old_fingerprint, client_id)).fetchone()
            if row is None or row[0] != "active":
                raise BrokerConflict("active credential for rotation was not found")
            event_id, _ = self._append_event(
                db, "credential", old_fingerprint, "credential.status_changed",
                {"from": "active", "to": "rotated"}, replacement.issued_at,
            )
            db.execute("UPDATE broker_client_credentials SET status='rotated',last_event_id=? "
                       "WHERE fingerprint=?", (event_id, old_fingerprint))
            self._insert_client_credential(db, replacement)

    def revoke_client_credential(self, client_id: str, fingerprint: str,
                                 occurred_at: str | None = None) -> None:
        occurred_at = occurred_at or utc_now()
        with self.transaction() as db:
            row = db.execute("SELECT status FROM broker_client_credentials "
                             "WHERE fingerprint=? AND client_id=?", (fingerprint, client_id)).fetchone()
            if row is None:
                raise BrokerStoreError("client credential not found")
            if row[0] != "active":
                return
            event_id, _ = self._append_event(
                db, "credential", fingerprint, "credential.status_changed",
                {"from": "active", "to": "revoked"}, occurred_at,
            )
            db.execute("UPDATE broker_client_credentials SET status='revoked',last_event_id=? "
                       "WHERE fingerprint=?", (event_id, fingerprint))

    def revoke_client(self, client_id: str, occurred_at: str | None = None) -> None:
        occurred_at = occurred_at or utc_now()
        with self.transaction() as db:
            client = db.execute("SELECT status FROM broker_clients WHERE client_id=?", (client_id,)).fetchone()
            if client is None:
                raise BrokerStoreError("client not found")
            if client[0] == "revoked":
                return
            event_id, _ = self._append_event(
                db, "client", client_id, "client.status_changed",
                {"from": "active", "to": "revoked"}, occurred_at,
            )
            db.execute("UPDATE broker_clients SET status='revoked',last_event_id=? WHERE client_id=?",
                       (event_id, client_id))
            for row in db.execute("SELECT fingerprint FROM broker_client_credentials "
                                  "WHERE client_id=? AND status='active'", (client_id,)).fetchall():
                fingerprint = row[0]
                credential_event, _ = self._append_event(
                    db, "credential", fingerprint, "credential.status_changed",
                    {"from": "active", "to": "revoked"}, occurred_at,
                )
                db.execute("UPDATE broker_client_credentials SET status='revoked',last_event_id=? "
                           "WHERE fingerprint=?", (credential_event, fingerprint))
            for row in db.execute("SELECT policy_id,version FROM broker_client_policies "
                                  "WHERE client_id=? AND status='active'", (client_id,)).fetchall():
                policy_id, version = row
                policy_event, _ = self._append_event(
                    db, "policy", f"{client_id}:{policy_id}:{version}", "policy.status_changed",
                    {"from": "active", "to": "revoked", "reason": "client_revoked"}, occurred_at,
                )
                db.execute("UPDATE broker_client_policies SET status='revoked',last_event_id=? "
                           "WHERE client_id=? AND policy_id=? AND version=?",
                           (policy_event, client_id, policy_id, version))

    def get_client(self, client_id: str) -> ClientRecord | None:
        row = self.db.execute("SELECT identity_json,status FROM broker_clients WHERE client_id=?",
                              (client_id,)).fetchone()
        return ClientRecord(ClientIdentity.from_json(row[0]), row[1]) if row else None

    def get_client_credential(self, fingerprint: str) -> CredentialRecord | None:
        row = self.db.execute("SELECT credential_json,status FROM broker_client_credentials "
                              "WHERE fingerprint=?", (fingerprint,)).fetchone()
        return CredentialRecord(ClientCredential.from_json(row[0]), row[1]) if row else None

    def get_client_policy(self, client_id: str, policy_id: str,
                          version: int | None = None) -> PolicyRecord | None:
        if version is None:
            row = self.db.execute("SELECT policy_json,status FROM broker_client_policies "
                                  "WHERE client_id=? AND policy_id=? AND status='active'",
                                  (client_id, policy_id)).fetchone()
        else:
            row = self.db.execute("SELECT policy_json,status FROM broker_client_policies "
                                  "WHERE client_id=? AND policy_id=? AND version=?",
                                  (client_id, policy_id, version)).fetchone()
        return PolicyRecord(ClientPolicy.from_json(row[0]), row[1]) if row else None

    def authorize_client_operation(self, *, client_id: str, fingerprint: str,
                                   policy_id: str, intent: OperationIntent,
                                   nonce_digest: str, now: datetime | None = None
                                   ) -> ClientAuthorizationDecision:
        """Check TLS identity, policy, exact accepted snapshots, then durably claim once.

        Even a positive result means only that an authenticated client may submit a
        proposal. Human consent remains required, and this method emits no grant.
        """
        now = now or datetime.now(timezone.utc)
        if not _is_digest(nonce_digest):
            reason = "invalid_operation_nonce"
            with self.transaction() as db:
                self._record_client_denial(db, intent, client_id, reason, None)
            return ClientAuthorizationDecision(False, True, reason, client_id,
                                               intent.operation_id, intent.intent_digest, None)
        with self.transaction() as db:
            stored_intent = db.execute("SELECT intent_digest,intent_json FROM operation_intents "
                                       "WHERE operation_id=?", (intent.operation_id,)).fetchone()
            if (stored_intent is None or stored_intent[0] != intent.intent_digest
                    or stored_intent[1] != intent.to_json()):
                self._record_client_denial(db, intent, client_id,
                                           "operation_intent_missing_or_divergent", None)
                return ClientAuthorizationDecision(
                    False, True, "operation_intent_missing_or_divergent", client_id,
                    intent.operation_id, intent.intent_digest, None,
                )
            client_row = db.execute("SELECT identity_json,status FROM broker_clients WHERE client_id=?",
                                    (client_id,)).fetchone()
            credential_row = db.execute("SELECT credential_json,status FROM broker_client_credentials "
                                        "WHERE fingerprint=?", (fingerprint,)).fetchone()
            policy_row = db.execute("SELECT policy_json,status FROM broker_client_policies "
                                    "WHERE client_id=? AND policy_id=? AND status='active'",
                                    (client_id, policy_id)).fetchone()
            identity_record = (ClientRecord(ClientIdentity.from_json(client_row[0]), client_row[1])
                               if client_row else None)
            credential_record = (CredentialRecord(ClientCredential.from_json(credential_row[0]), credential_row[1])
                                 if credential_row else None)
            policy_record = (PolicyRecord(ClientPolicy.from_json(policy_row[0]), policy_row[1])
                             if policy_row else None)
            accepted: dict[str, str] = {}
            if identity_record and credential_record and policy_record:
                for kind, snapshot_id, expected_digest in (
                    ("specification", intent.specification_snapshot_id, intent.specification_digest),
                    ("plan", intent.plan_snapshot_id, intent.plan_digest),
                    ("checkpoint", intent.checkpoint_snapshot_id, intent.checkpoint_digest),
                    ("payload", intent.payload_snapshot_id, intent.payload_digest),
                ):
                    row = db.execute("SELECT s.*,st.status,st.actor_id,st.changed_at FROM broker_snapshots s "
                                     "JOIN snapshot_state st USING(snapshot_id) WHERE s.snapshot_id=?",
                                     (snapshot_id,)).fetchone()
                    if row is None or row["status"] != "accepted":
                        break
                    snapshot = AcceptedSnapshot(
                        PROTOCOL_VERSION, row["snapshot_id"], row["kind"], row["project_id"],
                        row["revision_id"], row["schema_version"], row["content_json"],
                        row["canonical_digest"], row["provenance_json"], row["actor_id"],
                        row["changed_at"], row["parent_digest"],
                    )
                    if snapshot.kind != kind or snapshot.canonical_digest != expected_digest:
                        break
                    accepted[kind] = snapshot.canonical_digest
            result = evaluate_client_operation(
                identity=(identity_record.identity if identity_record and identity_record.status == "active"
                          else None),
                credential=(credential_record.credential if credential_record and credential_record.status == "active"
                            else None),
                policy=(policy_record.policy if policy_record and policy_record.status == "active" else None),
                intent=intent, presented_fingerprint=fingerprint,
                accepted_snapshot_digests=accepted, now=now,
            )
            if not result.permitted_to_propose:
                self._record_client_denial(
                    db, intent, client_id, result.reason,
                    policy_record.policy.policy_digest if policy_record else None,
                )
                return result
            assert policy_record is not None
            existing = db.execute("SELECT client_id,request_digest,nonce_digest,policy_id,policy_version "
                                  "FROM broker_client_operations WHERE operation_id=?",
                                  (intent.operation_id,)).fetchone()
            if existing is not None:
                reason = ("operation_replay_detected" if tuple(existing) == (
                    client_id, intent.intent_digest, nonce_digest, policy_id, policy_record.policy.version
                ) else "operation_id_reused_with_different_parameters")
                self._record_client_denial(db, intent, client_id, reason,
                                           policy_record.policy.policy_digest)
                return ClientAuthorizationDecision(False, True, reason, client_id,
                                                   intent.operation_id, intent.intent_digest,
                                                   policy_record.policy.policy_digest)
            if db.execute("SELECT 1 FROM broker_client_operations WHERE nonce_digest=?",
                          (nonce_digest,)).fetchone():
                self._record_client_denial(db, intent, client_id, "operation_nonce_replay",
                                           policy_record.policy.policy_digest)
                return ClientAuthorizationDecision(False, True, "operation_nonce_replay", client_id,
                                                   intent.operation_id, intent.intent_digest,
                                                   policy_record.policy.policy_digest)
            db.execute("INSERT INTO broker_client_operations VALUES(?,?,?,?,?,?,?)",
                       (intent.operation_id, client_id, intent.intent_digest, nonce_digest,
                        policy_id, policy_record.policy.version, utc_now()))
            self._append_event(
                db, "client_operation", intent.operation_id, "client_operation.claimed",
                {"client_id": client_id, "request_digest": intent.intent_digest,
                 "nonce_digest": nonce_digest, "policy_id": policy_id,
                 "policy_version": policy_record.policy.version,
                 "eligible_to_propose": True, "consent_required": True}, utc_now(),
            )
            return result

    def _record_client_denial(self, db: sqlite3.Connection, intent: OperationIntent,
                              client_id: str, reason: str, policy_digest: str | None) -> None:
        """Append a sanitized denial event without raw nonce/certificate data."""
        allowed_reasons = {
            "invalid_operation_nonce", "operation_intent_missing_or_divergent",
            "unknown_client_credential_or_policy", "invalid_clock",
            "client_identity_mismatch", "client_principal_mismatch",
            "certificate_identity_mismatch", "client_identity_inactive_or_expired",
            "client_credential_inactive_or_expired", "policy_client_mismatch",
            "action_not_allowed", "target_not_allowed", "required_scope_missing",
            "operation_ttl_exceeds_policy", "accepted_snapshot_digest_mismatch",
            "operation_replay_detected", "operation_id_reused_with_different_parameters",
            "operation_nonce_replay",
        }
        if reason not in allowed_reasons:
            reason = "unknown_client_credential_or_policy"
        client_reference = client_id[:512] if type(client_id) is str else "<invalid>"
        client_id_digest = "sha256:" + hashlib.sha256(client_reference.encode("utf-8")).hexdigest()
        self._append_event(
            db, "client_authorization", intent.operation_id,
            "client_authorization.denied",
            {"operation_id": intent.operation_id,
             "client_id_digest": client_id_digest,
             "intent_digest": intent.intent_digest,
             "policy_digest": policy_digest,
             "reason": reason, "consent_required": True},
            utc_now(),
        )

    def verify_integrity(self) -> None:
        """Validate SQLite, FK, canonical digests, event chain, and state projections."""
        try:
            if self.db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise BrokerIntegrityError("database application identity mismatch")
            if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise BrokerIntegrityError("database schema version mismatch")
            if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BrokerIntegrityError("SQLite integrity_check failed")
            if self.db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BrokerIntegrityError("SQLite foreign_key_check failed")
            previous = "sha256:" + "0" * 64
            expected_sequence = 1
            projected: dict[tuple[str, str], tuple[str, str]] = {}
            seen_operations: set[str] = set()
            seen_client_operations: set[str] = set()
            denial_reasons = {
                "invalid_operation_nonce", "operation_intent_missing_or_divergent",
                "unknown_client_credential_or_policy", "invalid_clock",
                "client_identity_mismatch", "client_principal_mismatch",
                "certificate_identity_mismatch", "client_identity_inactive_or_expired",
                "client_credential_inactive_or_expired", "policy_client_mismatch",
                "action_not_allowed", "target_not_allowed", "required_scope_missing",
                "operation_ttl_exceeds_policy", "accepted_snapshot_digest_mismatch",
                "operation_replay_detected", "operation_id_reused_with_different_parameters",
                "operation_nonce_replay",
            }
            for row in self.db.execute("SELECT * FROM broker_events ORDER BY sequence"):
                payload = parse_canonical_json(row["payload_json"])
                if row["sequence"] != expected_sequence:
                    raise BrokerIntegrityError("Broker event sequence has a gap")
                expected_sequence += 1
                body = {"sequence": row["sequence"], "event_id": row["event_id"],
                        "aggregate_type": row["aggregate_type"], "aggregate_id": row["aggregate_id"],
                        "event_type": row["event_type"], "payload": payload,
                        "occurred_at": row["occurred_at"], "previous_digest": row["previous_digest"]}
                if row["previous_digest"] != previous or digest_json(body) != row["event_digest"]:
                    raise BrokerIntegrityError("Broker event hash chain mismatch")
                previous = row["event_digest"]
                key = (row["aggregate_type"], row["aggregate_id"])
                current = projected.get(key)
                event_type = row["event_type"]
                status = None
                if event_type == "snapshot.proposed" and key[0] == "snapshot":
                    if current is not None or payload.get("status") != "pending":
                        raise BrokerIntegrityError("invalid snapshot proposal event sequence")
                    status = "pending"
                elif event_type == "snapshot.status_changed" and key[0] == "snapshot":
                    target = payload.get("to")
                    if (current is None or payload.get("from") != current[0]
                            or target not in _SNAPSHOT_TRANSITIONS.get(current[0], set())):
                        raise BrokerIntegrityError("invalid snapshot state event sequence")
                    status = target
                elif event_type == "approval_request.created" and key[0] == "request":
                    if current is not None or payload.get("status") != "pending":
                        raise BrokerIntegrityError("invalid approval request creation event")
                    status = "pending"
                elif event_type == "approval_decision.recorded" and key[0] == "request":
                    target = payload.get("decision")
                    if current is None or current[0] != "pending" or target not in {"approved", "rejected"}:
                        raise BrokerIntegrityError("invalid approval decision event sequence")
                    status = target
                elif event_type == "approval_request.transitioned" and key[0] == "request":
                    target = payload.get("to")
                    if (current is None or payload.get("from") != current[0]
                            or target not in _REQUEST_TRANSITIONS.get(current[0], set())):
                        raise BrokerIntegrityError("invalid approval request state event sequence")
                    status = target
                elif event_type == "execution_authorization.created" and key[0] == "authorization":
                    if current is not None or payload.get("status") != "approved":
                        raise BrokerIntegrityError("invalid authorization creation event")
                    status = "approved"
                elif event_type == "execution_authorization.transitioned" and key[0] == "authorization":
                    target = payload.get("to")
                    if (current is None or payload.get("from") != current[0]
                            or target not in _AUTHORIZATION_TRANSITIONS.get(current[0], set())):
                        raise BrokerIntegrityError("invalid authorization state event sequence")
                    status = target
                elif event_type == "execution_receipt.recorded" and key[0] == "receipt":
                    target = payload.get("status")
                    if target not in ExecutionReceipt.STATUSES:
                        raise BrokerIntegrityError("invalid receipt event status")
                    if current is not None and current[0] != "pending":
                        raise BrokerIntegrityError("receipt event followed a terminal receipt")
                    status = target
                elif event_type == "operation.intent_recorded" and key[0] == "operation":
                    if key[1] in seen_operations:
                        raise BrokerIntegrityError("duplicate operation intent event")
                    seen_operations.add(key[1])
                elif event_type == "client.registered" and key[0] == "client":
                    if current is not None or payload.get("status") != "active":
                        raise BrokerIntegrityError("invalid client registration event")
                    status = "active"
                elif event_type == "client.status_changed" and key[0] == "client":
                    if current is None or current[0] != "active" or payload.get("from") != "active" or payload.get("to") != "revoked":
                        raise BrokerIntegrityError("invalid client revocation event")
                    status = "revoked"
                elif event_type == "credential.registered" and key[0] == "credential":
                    if current is not None or payload.get("status") != "active":
                        raise BrokerIntegrityError("invalid client credential registration event")
                    status = "active"
                elif event_type == "credential.status_changed" and key[0] == "credential":
                    target = payload.get("to")
                    if (current is None or current[0] != "active" or payload.get("from") != "active"
                            or target not in {"rotated", "revoked"}):
                        raise BrokerIntegrityError("invalid client credential state event")
                    status = target
                elif event_type == "policy.registered" and key[0] == "policy":
                    if current is not None or payload.get("status") != "active":
                        raise BrokerIntegrityError("invalid client policy registration event")
                    status = "active"
                elif event_type == "policy.status_changed" and key[0] == "policy":
                    if current is None or current[0] != "active" or payload.get("from") != "active" or payload.get("to") != "revoked":
                        raise BrokerIntegrityError("invalid client policy state event")
                    status = "revoked"
                elif event_type == "client_operation.claimed" and key[0] == "client_operation":
                    if key[1] in seen_client_operations:
                        raise BrokerIntegrityError("duplicate client operation claim event")
                    seen_client_operations.add(key[1])
                    if payload.get("consent_required") is not True or payload.get("eligible_to_propose") is not True:
                        raise BrokerIntegrityError("client operation audit event overstates consent")
                elif event_type == "client_authorization.denied" and key[0] == "client_authorization":
                    if (payload.get("operation_id") != key[1]
                            or payload.get("reason") not in denial_reasons
                            or payload.get("consent_required") is not True
                            or not _is_digest(payload.get("intent_digest", ""))
                            or not _is_digest(payload.get("client_id_digest", ""))
                            or (payload.get("policy_digest") is not None
                                and not _is_digest(payload.get("policy_digest")))):
                        raise BrokerIntegrityError("client authorization denial audit event is invalid")
                elif event_type == "webauthn_credential.registered" and key[0] == "webauthn_credential":
                    if current is not None or payload.get("status") != "active" or not _is_digest(payload.get("credential_digest", "")):
                        raise BrokerIntegrityError("invalid WebAuthn credential registration event")
                    status = "active"
                elif event_type == "webauthn_credential.revoked" and key[0] == "webauthn_credential":
                    if current is None or current[0] != "active" or payload.get("from") != "active" or payload.get("to") != "revoked":
                        raise BrokerIntegrityError("invalid WebAuthn credential revocation event")
                    status = "revoked"
                elif event_type == "webauthn_credential.counter_updated" and key[0] == "webauthn_credential":
                    if (current is None or current[0] != "active" or payload.get("status") != "active"
                            or type(payload.get("from")) is not int or type(payload.get("to")) is not int
                            or not 0 <= payload["from"] <= payload["to"] <= 0xFFFFFFFF
                            or payload.get("counter_mode") not in {"zero", "monotonic"}
                            or not _is_digest(payload.get("credential_digest", ""))):
                        raise BrokerIntegrityError("invalid WebAuthn signature counter event")
                    status = "active"
                elif event_type == "webauthn_challenge.created" and key[0] == "webauthn_challenge":
                    if (current is not None or payload.get("status") != "pending"
                            or not _is_digest(payload.get("binding_digest", ""))
                            or not _is_digest(payload.get("challenge_digest", ""))):
                        raise BrokerIntegrityError("invalid WebAuthn challenge event")
                    status = "pending"
                elif event_type in {"webauthn_challenge.verification_started",
                                    "webauthn_challenge.consumed", "webauthn_challenge.rejected",
                                    "webauthn_challenge.expired", "webauthn_challenge.recovered_unknown"}:
                    target_by_event = {
                        "webauthn_challenge.verification_started": ("pending", "verifying"),
                        "webauthn_challenge.consumed": ("verifying", "consumed"),
                        "webauthn_challenge.rejected": ("verifying", "rejected"),
                        "webauthn_challenge.expired": ("pending", "expired"),
                        "webauthn_challenge.recovered_unknown": ("verifying", "unknown"),
                    }
                    source, target = target_by_event[event_type]
                    if current is None or current[0] != source or payload.get("from") != source or payload.get("to") != target:
                        raise BrokerIntegrityError("invalid WebAuthn challenge transition")
                    if event_type in {"webauthn_challenge.rejected", "webauthn_challenge.expired",
                                      "webauthn_challenge.recovered_unknown"} and type(payload.get("reason")) is not str:
                        raise BrokerIntegrityError("WebAuthn challenge terminal event lacks a reason code")
                    status = target
                else:
                    raise BrokerIntegrityError("unknown Broker event type")
                if status is not None:
                    projected[key] = (status, row["event_id"])
            for row in self.db.execute("SELECT client_id,identity_json,status,last_event_id FROM broker_clients"):
                identity = ClientIdentity.from_json(row[1])
                if (identity.client_id != row[0]
                        or projected.get(("client", row[0])) != (row[2], row[3])):
                    raise BrokerIntegrityError("client materialized identity diverges from event history")
                event = self.db.execute("SELECT payload_json FROM broker_events WHERE event_id=?", (row[3],)).fetchone()
                if event is None or json.loads(event[0]).get("identity_digest") not in (None, identity.identity_digest):
                    raise BrokerIntegrityError("client state has no matching identity event")
            for row in self.db.execute("SELECT fingerprint,client_id,credential_json,status,last_event_id "
                                       "FROM broker_client_credentials"):
                credential = ClientCredential.from_json(row[2])
                if (credential.client_id != row[1] or credential.fingerprint != row[0]
                        or projected.get(("credential", row[0])) != (row[3], row[4])):
                    raise BrokerIntegrityError("client credential state diverges from event history")
                event = self.db.execute("SELECT aggregate_type,aggregate_id FROM broker_events WHERE event_id=?",
                                        (row[4],)).fetchone()
                if event is None or tuple(event) != ("credential", row[0]):
                    raise BrokerIntegrityError("client credential state has no matching event")
            for row in self.db.execute("SELECT client_id,policy_id,version,policy_json,policy_digest,status,last_event_id "
                                       "FROM broker_client_policies"):
                policy = ClientPolicy.from_json(row[3])
                aggregate_id = f"{row[0]}:{row[1]}:{row[2]}"
                if (policy.client_id != row[0] or policy.policy_id != row[1]
                        or policy.version != row[2] or policy.policy_digest != row[4]
                        or projected.get(("policy", aggregate_id)) != (row[5], row[6])):
                    raise BrokerIntegrityError("client policy state diverges from event history")
            for row in self.db.execute("SELECT operation_id,client_id,request_digest,nonce_digest,policy_id,policy_version "
                                       "FROM broker_client_operations"):
                event = self.db.execute("SELECT payload_json FROM broker_events WHERE aggregate_type='client_operation' "
                                        "AND aggregate_id=? AND event_type='client_operation.claimed'",
                                        (row[0],)).fetchone()
                if event is None:
                    raise BrokerIntegrityError("client operation claim has no audit event")
                payload = json.loads(event[0])
                if payload != {"client_id": row[1], "request_digest": row[2], "nonce_digest": row[3],
                               "policy_id": row[4], "policy_version": row[5],
                               "eligible_to_propose": True, "consent_required": True}:
                    raise BrokerIntegrityError("client operation claim diverges from audit event")
            for row in self.db.execute(
                "SELECT c.credential_id,c.principal_id,c.rp_id,c.credential_json,c.credential_digest,"
                "s.status,s.sign_count,s.counter_mode,s.last_event_id FROM webauthn_credentials c "
                "JOIN webauthn_credential_state s USING(credential_id)"
            ):
                credential = WebAuthnCredential.from_json(row[3])
                if (credential.credential_id != row[0] or credential.principal_id != row[1]
                        or credential.rp_id != row[2] or credential.credential_digest != row[4]
                        or projected.get(("webauthn_credential", row[0])) != (row[5], row[8])
                        or type(row[6]) is not int or not 0 <= row[6] <= 0xFFFFFFFF
                        or row[7] not in {"zero", "monotonic"}):
                    raise BrokerIntegrityError("WebAuthn credential state diverges from immutable record/events")
            for row in self.db.execute(
                "SELECT c.challenge_id,c.challenge_digest,c.binding_json,c.expires_at,s.status,s.last_event_id "
                "FROM webauthn_challenges c JOIN webauthn_challenge_state s USING(challenge_id)"
            ):
                binding = WebAuthnChallengeBinding.from_json(row[2])
                if (binding.challenge_id != row[0] or binding.challenge_digest != row[1]
                        or binding.expires_at != row[3]
                        or projected.get(("webauthn_challenge", row[0])) != (row[4], row[5])):
                    raise BrokerIntegrityError("WebAuthn challenge state diverges from immutable binding/events")
            for row in self.db.execute("SELECT * FROM broker_snapshots"):
                proposal = SnapshotProposal(
                    PROTOCOL_VERSION, row["snapshot_id"], row["kind"], row["project_id"],
                    row["revision_id"], row["schema_version"], row["content_json"],
                    row["canonical_digest"], row["provenance_json"], row["proposed_by"],
                    row["operation_id"], row["proposed_at"], row["parent_digest"],
                )
                if digest_json(parse_canonical_json(proposal.content_json)) != proposal.canonical_digest:
                    raise BrokerIntegrityError("snapshot digest mismatch")
            for row in self.db.execute(
                "SELECT s.snapshot_id,s.status,s.last_event_id FROM snapshot_state s"
            ):
                if projected.get(("snapshot", row[0])) != (row[1], row[2]):
                    raise BrokerIntegrityError("snapshot materialized state diverges from event history")
                event = self.db.execute("SELECT aggregate_type,aggregate_id,event_type,payload_json "
                                        "FROM broker_events WHERE event_id=?", (row[2],)).fetchone()
                if (event is None or event[0] != "snapshot" or event[1] != row[0]
                        or event[2] not in {"snapshot.proposed", "snapshot.status_changed"}):
                    raise BrokerIntegrityError("snapshot state does not point to its event")
                event_payload = json.loads(event[3])
                if event_payload.get("to", event_payload.get("status")) != row[1]:
                    raise BrokerIntegrityError("snapshot state/event status diverged")
            for row in self.db.execute("SELECT operation_id,intent_json,intent_digest,"
                                       "specification_snapshot_id,plan_snapshot_id,"
                                       "checkpoint_snapshot_id,payload_snapshot_id FROM operation_intents"):
                intent = OperationIntent.from_json(row[1])
                if (intent.operation_id != row[0] or intent.intent_digest != row[2]
                        or intent.specification_snapshot_id != row[3]
                        or intent.plan_snapshot_id != row[4]
                        or intent.checkpoint_snapshot_id != row[5]
                        or intent.payload_snapshot_id != row[6]):
                    raise BrokerIntegrityError("stored intent digest mismatch")
                event = self.db.execute(
                    "SELECT payload_json FROM broker_events WHERE aggregate_type='operation' "
                    "AND aggregate_id=? AND event_type='operation.intent_recorded'", (intent.operation_id,),
                ).fetchone()
                if event is None or json.loads(event[0]).get("intent_digest") != intent.intent_digest:
                    raise BrokerIntegrityError("operation intent has no matching append-only event")
            for row in self.db.execute(
                "SELECT r.request_id,r.operation_id,r.action_code,r.request_json,r.request_digest,"
                "r.nonce_digest,r.expires_at,s.status,s.last_event_id "
                "FROM approval_requests r JOIN approval_request_state s USING(request_id)"
            ):
                if projected.get(("request", row[0])) != (row[7], row[8]):
                    raise BrokerIntegrityError("request materialized state diverges from event history")
                value = json.loads(row[3])
                value["status"] = row[7]
                request = ApprovalRequest.from_dict(value)
                if (request.request_id != row[0] or request.operation_id != row[1]
                        or request.action_code != row[2] or request.request_digest != row[4]
                        or request.nonce_digest != row[5] or request.expires_at != row[6]):
                    raise BrokerIntegrityError("stored approval request digest mismatch")
                event = self.db.execute("SELECT aggregate_type,aggregate_id,event_type,payload_json "
                                        "FROM broker_events WHERE event_id=?", (row[8],)).fetchone()
                allowed_events = {"approval_request.transitioned", "approval_request.created",
                                  "approval_decision.recorded"}
                if (event is None or event[0] != "request" or event[1] != row[0]
                        or event[2] not in allowed_events):
                    raise BrokerIntegrityError("request state does not point to its event")
                event_payload = json.loads(event[3])
                event_status = event_payload.get("to", event_payload.get("status", event_payload.get("decision")))
                if event_status != row[7]:
                    raise BrokerIntegrityError("request state/event status diverged")
            for row in self.db.execute("SELECT decision_id,request_id,operation_id,intent_digest,"
                                       "request_digest,challenge_digest,decision_json,decision_digest "
                                       "FROM approval_decisions"):
                decision = ApprovalDecision.from_json(row[6])
                if (decision.decision_id != row[0] or decision.request_id != row[1]
                        or decision.operation_id != row[2] or decision.intent_digest != row[3]
                        or decision.request_digest != row[4] or decision.challenge_digest != row[5]
                        or decision.decision_digest != row[7]):
                    raise BrokerIntegrityError("decision digest mismatch")
                events = self.db.execute(
                    "SELECT payload_json FROM broker_events WHERE aggregate_type='request' "
                    "AND aggregate_id=? AND event_type='approval_decision.recorded'", (decision.request_id,),
                ).fetchall()
                if not any(json.loads(item[0]).get("decision_digest") == decision.decision_digest
                           for item in events):
                    raise BrokerIntegrityError("decision has no matching append-only event")
            for row in self.db.execute("SELECT authorization_id,request_id,operation_id,nonce_digest,"
                                       "authorization_json,authorization_digest FROM execution_authorizations"):
                auth = ExecutionAuthorization.from_json(row[4])
                if (auth.authorization_id != row[0] or auth.request_id != row[1]
                        or auth.operation_id != row[2] or auth.nonce_digest != row[3]
                        or auth.authorization_digest != row[5]):
                    raise BrokerIntegrityError("authorization digest mismatch")
                event = self.db.execute(
                    "SELECT payload_json FROM broker_events WHERE aggregate_type='authorization' "
                    "AND aggregate_id=? AND event_type='execution_authorization.created'",
                    (auth.authorization_id,),
                ).fetchone()
                if event is None or json.loads(event[0]).get("authorization_digest") != auth.authorization_digest:
                    raise BrokerIntegrityError("authorization has no matching append-only event")
            for row in self.db.execute(
                "SELECT s.authorization_id,s.status,s.last_event_id FROM authorization_state s"
            ):
                if projected.get(("authorization", row[0])) != (row[1], row[2]):
                    raise BrokerIntegrityError("authorization materialized state diverges from event history")
                event = self.db.execute("SELECT aggregate_type,aggregate_id,event_type,payload_json "
                                        "FROM broker_events WHERE event_id=?", (row[2],)).fetchone()
                if (event is None or event[0] != "authorization" or event[1] != row[0]
                        or event[2] not in {"execution_authorization.created", "execution_authorization.transitioned"}):
                    raise BrokerIntegrityError("authorization state does not point to its event")
                event_payload = json.loads(event[3])
                if event_payload.get("to", event_payload.get("status")) != row[1]:
                    raise BrokerIntegrityError("authorization state/event status diverged")
            for row in self.db.execute("SELECT receipt_id,operation_id,authorization_id,receipt_json,"
                                       "receipt_digest FROM execution_receipts"):
                receipt = ExecutionReceipt.from_json(row[3])
                if (receipt.receipt_id != row[0] or receipt.operation_id != row[1]
                        or receipt.authorization_digest != self.db.execute(
                            "SELECT authorization_digest FROM execution_authorizations WHERE authorization_id=?",
                            (row[2],)).fetchone()[0]
                        or receipt.receipt_digest != row[4]):
                    raise BrokerIntegrityError("receipt digest mismatch")
                events = self.db.execute(
                    "SELECT payload_json FROM broker_events WHERE aggregate_type='receipt' "
                    "AND aggregate_id=? AND event_type='execution_receipt.recorded'", (receipt.operation_id,),
                ).fetchall()
                if not any(json.loads(item[0]).get("receipt_digest") == receipt.receipt_digest
                           for item in events):
                    raise BrokerIntegrityError("receipt has no matching append-only event")
            for row in self.db.execute(
                "SELECT s.operation_id,s.receipt_id,s.status,s.last_event_id FROM execution_receipt_state s"
            ):
                if projected.get(("receipt", row[0])) != (row[2], row[3]):
                    raise BrokerIntegrityError("receipt materialized state diverges from event history")
                event = self.db.execute("SELECT aggregate_type,aggregate_id,event_type,payload_json "
                                        "FROM broker_events WHERE event_id=?", (row[3],)).fetchone()
                if (event is None or event[0] != "receipt" or event[1] != row[0]
                        or event[2] != "execution_receipt.recorded"):
                    raise BrokerIntegrityError("receipt state does not point to its event")
                if json.loads(event[3]).get("status") != row[2]:
                    raise BrokerIntegrityError("receipt state/event status diverged")
        except sqlite3.DatabaseError as exc:
            raise BrokerIntegrityError("Broker database is corrupt or unreadable") from exc
        except ProtocolError as exc:
            raise BrokerIntegrityError("stored protocol record is invalid") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BrokerIntegrityError("Broker database contains inconsistent records") from exc

    def backup_to(self, destination: str | Path) -> Path:
        target = _safe_path(destination)
        if target == self.path or target.exists():
            raise BrokerStoreError("backup destination must be new and distinct")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp-" + uuid4().hex)
        try:
            backup_db = sqlite3.connect(temp, isolation_level=None)
            try:
                with self._mutex:
                    self.db.backup(backup_db)
                backup_db.execute("PRAGMA integrity_check")
            finally:
                backup_db.close()
            self._verify_path(temp)
            os.link(temp, target)  # atomic create-if-absent; never replaces user data
            temp.unlink()
            return target
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def restore_backup(cls, backup: str | Path, destination: str | Path) -> Path:
        source = _safe_path(backup)
        target = _safe_path(destination)
        if source == target or target.exists():
            raise BrokerStoreError("restore destination must be new and distinct")
        cls._verify_path(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".restore-" + uuid4().hex)
        try:
            src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            dst = sqlite3.connect(temp, isolation_level=None)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            cls._verify_path(temp)
            os.link(temp, target)
            temp.unlink()
            return target
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def _verify_path(cls, path: Path) -> None:
        uri = path.as_uri() + "?mode=ro"
        try:
            db = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
            db.row_factory = sqlite3.Row
            if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise BrokerIntegrityError("backup is not an AgentFlow Broker database")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise BrokerIntegrityError("backup schema version mismatch")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BrokerIntegrityError("backup integrity_check failed")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BrokerIntegrityError("backup foreign_key_check failed")
            checker = object.__new__(cls)
            checker.db = db
            checker.verify_integrity()
        except sqlite3.DatabaseError as exc:
            raise BrokerIntegrityError("backup is corrupt or unreadable") from exc
        finally:
            if "db" in locals():
                db.close()

    def rollback_empty_schema(self) -> None:
        """Revert initial migration only while no domain event or record exists."""
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM broker_events LIMIT 1").fetchone():
                raise BrokerStoreError("cannot roll back a Broker schema containing history")
            triggers = db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'broker_%' OR name LIKE 'webauthn_%')"
            ).fetchall()
            indexes = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('broker_one_active_credential','broker_one_active_policy')"
            ).fetchall()
            for row in triggers:
                db.execute(f'DROP TRIGGER "{row[0]}"')
            for row in indexes:
                db.execute(f'DROP INDEX "{row[0]}"')
            for table in _TABLES:
                db.execute(f'DROP TABLE IF EXISTS "{table}"')
            db.execute("PRAGMA user_version=0")
            db.execute("PRAGMA application_id=0")

    def rollback_identity_schema_if_empty(self) -> None:
        """Revert v2/v3 additive identity tables only while no Broker history exists."""
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (2, 3):
                raise BrokerStoreError("identity schema rollback requires Broker schema v2 or v3")
            if db.execute("SELECT 1 FROM broker_events LIMIT 1").fetchone():
                raise BrokerStoreError("cannot roll back identity schema after Broker history exists")
            triggers = db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND "
                "(name LIKE 'broker_client%' OR name LIKE 'webauthn_%')"
            ).fetchall()
            indexes = db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IN ('broker_one_active_credential','broker_one_active_policy')"
            ).fetchall()
            for row in triggers:
                db.execute(f'DROP TRIGGER "{row[0]}"')
            for row in indexes:
                db.execute(f'DROP INDEX "{row[0]}"')
            if version == 3:
                for table in ("webauthn_challenge_state", "webauthn_challenges",
                              "webauthn_credential_state", "webauthn_credentials"):
                    db.execute(f'DROP TABLE "{table}"')
            for table in ("broker_client_operations", "broker_client_policies",
                          "broker_client_credentials", "broker_clients"):
                db.execute(f'DROP TABLE "{table}"')
            db.execute("PRAGMA user_version=1")

    def rollback_webauthn_schema_if_empty(self) -> None:
        """Revert v3 to v2 only before any event or WebAuthn record exists."""
        with self.transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 3:
                raise BrokerStoreError("WebAuthn schema rollback requires Broker schema v3")
            if db.execute("SELECT 1 FROM broker_events LIMIT 1").fetchone():
                raise BrokerStoreError("cannot roll back WebAuthn schema after Broker history exists")
            for table in ("webauthn_challenge_state", "webauthn_challenges",
                          "webauthn_credential_state", "webauthn_credentials"):
                db.execute(f'DROP TABLE "{table}"')
            for name in ("webauthn_credentials_no_update", "webauthn_credentials_no_delete",
                         "webauthn_challenges_no_update", "webauthn_challenges_no_delete"):
                db.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            db.execute("PRAGMA user_version=2")


def _is_digest(value: str) -> bool:
    return (isinstance(value, str) and len(value) == 71 and value.startswith("sha256:")
            and all(char in "0123456789abcdef" for char in value[7:]))


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")
