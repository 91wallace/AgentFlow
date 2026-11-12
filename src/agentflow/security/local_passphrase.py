"""Cross-platform local operator authentication for LOCAL_SUPERVISED.

This backend proves knowledge of an enrolled passphrase against a one-time,
persisted consent challenge. It is intentionally not an independent authority:
the OS account and its local administrators remain inside the trust boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sys
from threading import Lock
from typing import Callable

from .authorization import AuthenticatedOperator

_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_MIN_PASSPHRASE_LENGTH = 12


class LocalPassphraseError(ValueError):
    """Local credential enrollment or authentication failed closed."""


def current_local_operator_principal(*, platform: str | None = None,
                                     uid_provider: Callable[[], int] | None = None,
                                     sid_provider: Callable[[], str] | None = None) -> str:
    """Return an opaque identity derived from the current OS access token/account."""
    selected = platform or sys.platform
    if selected == "win32":
        if sid_provider is None:
            from .windows_webauthn import windows_operator_principal
            principal = windows_operator_principal()
        else:
            sid = sid_provider()
            if type(sid) is not str or not sid:
                raise LocalPassphraseError("windows_operator_sid_unavailable")
            principal = "win-sid-" + hashlib.sha256(sid.encode("ascii")).hexdigest()[:32]
        if not re.fullmatch(r"win-sid-[0-9a-f]{32}", principal):
            raise LocalPassphraseError("windows_operator_principal_invalid")
        return principal
    if selected.startswith("linux"):
        uid = (uid_provider or getattr(os, "geteuid", None))
        if not callable(uid):
            raise LocalPassphraseError("linux_operator_uid_unavailable")
        value = uid()
        if type(value) is not int or value < 0:
            raise LocalPassphraseError("linux_operator_uid_invalid")
        return f"linux-uid-{value}"
    raise LocalPassphraseError("local_authentication_platform_unsupported")


def _derive(passphrase: str, salt: bytes, *, n: int = _SCRYPT_N,
            r: int = _SCRYPT_R, p: int = _SCRYPT_P) -> bytes:
    if (type(passphrase) is not str or len(passphrase) < _MIN_PASSPHRASE_LENGTH
            or len(passphrase.encode("utf-8")) > 1024):
        raise LocalPassphraseError("local_passphrase_policy_failed")
    try:
        return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                              dklen=32, maxmem=_SCRYPT_MAXMEM)
    except (ValueError, MemoryError) as exc:
        raise LocalPassphraseError("local_passphrase_kdf_unavailable") from exc


def _proof_message(nonce: str) -> bytes:
    if type(nonce) is not str or not 32 <= len(nonce) <= 256:
        raise LocalPassphraseError("local_authentication_challenge_invalid")
    return b"AgentFlow LOCAL_SUPERVISED consent proof v1\0" + nonce.encode("ascii")


def _private_key(passphrase: str, salt: bytes, *, n: int = _SCRYPT_N,
                 r: int = _SCRYPT_R, p: int = _SCRYPT_P):
    """Derive an ephemeral signing key. Only its public half is persisted."""
    seed = _derive(passphrase, salt, n=n, r=r, p=p)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(seed)
    except (ImportError, ValueError) as exc:
        raise LocalPassphraseError("local_authentication_crypto_unavailable") from exc


def _public_key_bytes(private_key) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class LocalPassphraseSigningSession:
    """Short-lived in-memory key handoff from operator proof to grant signer."""

    def __init__(self):
        self._lock = Lock()
        self._credential_id = None
        self._private_key = None

    def set_key(self, credential_id: str, private_key) -> None:
        with self._lock:
            if self._private_key is not None:
                raise LocalPassphraseError("local_signing_session_already_active")
            self._credential_id = credential_id
            self._private_key = private_key

    def sign_digest(self, key_id: str, credential_id: str, grant_digest: str) -> bytes:
        with self._lock:
            if self._private_key is None or self._credential_id != credential_id:
                raise LocalPassphraseError("local_signing_session_unavailable")
            private_key = self._private_key
            self._private_key = None
            self._credential_id = None
        return private_key.sign(grant_digest.encode("ascii"))

    def clear(self) -> None:
        with self._lock:
            self._private_key = None
            self._credential_id = None


@dataclass(frozen=True)
class LocalPassphraseGrantSigner:
    key_id: str
    credential_id: str
    session: LocalPassphraseSigningSession = field(repr=False, compare=False)

    def sign_digest(self, grant_digest: str) -> bytes:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", grant_digest):
            raise LocalPassphraseError("local_grant_digest_invalid")
        return self.session.sign_digest(self.key_id, self.credential_id, grant_digest)


@dataclass(frozen=True)
class LocalPassphraseProof:
    """Challenge-bound proof; raw passphrases and derived keys are never carried."""

    credential_id: str
    challenge_nonce: str = field(repr=False)
    signature: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (not re.fullmatch(r"local-credential-[0-9a-f]{32}", self.credential_id)
                or type(self.challenge_nonce) is not str
                or type(self.signature) is not bytes or len(self.signature) != 64):
            raise LocalPassphraseError("local_passphrase_proof_invalid")


class SQLiteLocalOperatorCredentialStore:
    """Append-only credential records in the existing AgentFlow State Store."""

    def __init__(self, apl_store):
        if not callable(getattr(apl_store, "_connect", None)):
            raise LocalPassphraseError("agentflow_state_store_required")
        self.apl_store = apl_store

    @staticmethod
    def _ensure_schema(db) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS apl_local_operator_credentials(
            credential_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
            salt BLOB NOT NULL, public_key BLOB NOT NULL, kdf_n INTEGER NOT NULL,
            kdf_r INTEGER NOT NULL, kdf_p INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS apl_local_operator_credential_events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, credential_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('enrolled','revoked')),
            operation_id TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL,
            FOREIGN KEY(credential_id) REFERENCES apl_local_operator_credentials(credential_id))""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_operator_credentials_no_update
            BEFORE UPDATE ON apl_local_operator_credentials BEGIN
            SELECT RAISE(ABORT,'local operator credentials are immutable'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_operator_credentials_no_delete
            BEFORE DELETE ON apl_local_operator_credentials BEGIN
            SELECT RAISE(ABORT,'local operator credentials are immutable'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_operator_credential_events_no_update
            BEFORE UPDATE ON apl_local_operator_credential_events BEGIN
            SELECT RAISE(ABORT,'local operator credential events are append-only'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_operator_credential_events_no_delete
            BEFORE DELETE ON apl_local_operator_credential_events BEGIN
            SELECT RAISE(ABORT,'local operator credential events are append-only'); END""")

    def _protect_database_file(self) -> None:
        # Unix file ACLs are the OS boundary between local accounts. The
        # The database stores only a salt and public key. On Unix, limit the
        # operational DB file to the current OS account. Do not chmod its parent.
        path = getattr(self.apl_store, "path", None)
        if os.name != "nt" and type(path) is str and os.path.isfile(path):
            os.chmod(path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = path + suffix
                if os.path.isfile(sidecar):
                    os.chmod(sidecar, 0o600)

    def _enroll(self, principal_id: str, passphrase: str, confirmation: str, *,
                now: datetime | None = None) -> str:
        if (type(principal_id) is not str or not re.fullmatch(
                r"(?:win-sid-[0-9a-f]{32}|linux-uid-[0-9]{1,20})", principal_id)):
            raise LocalPassphraseError("local_operator_principal_invalid")
        _derive(passphrase, b"validation-salt", n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        if not hmac.compare_digest(passphrase.encode("utf-8"),
                                   confirmation.encode("utf-8") if type(confirmation) is str else b""):
            raise LocalPassphraseError("local_passphrase_confirmation_mismatch")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise LocalPassphraseError("aware_local_auth_clock_required")
        credential_id = "local-credential-" + secrets.token_hex(16)
        salt = secrets.token_bytes(16)
        public_key = _public_key_bytes(_private_key(passphrase, salt))
        stamp = instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self._protect_database_file()
        with self.apl_store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_schema(db)
            active = db.execute("""SELECT c.credential_id FROM apl_local_operator_credentials c
                JOIN apl_local_operator_credential_events e ON e.credential_id=c.credential_id
                WHERE c.principal_id=? AND e.event_id=(SELECT MAX(x.event_id)
                    FROM apl_local_operator_credential_events x WHERE x.credential_id=c.credential_id)
                    AND e.event_type='enrolled' LIMIT 1""", (principal_id,)).fetchone()
            if active:
                raise LocalPassphraseError("local_operator_credential_already_enrolled")
            db.execute("INSERT INTO apl_local_operator_credentials VALUES(?,?,?,?,?,?,?,?)",
                       (credential_id, principal_id, salt, public_key,
                        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, stamp))
            db.execute("""INSERT INTO apl_local_operator_credential_events(
                credential_id,event_type,operation_id,occurred_at) VALUES(?,?,?,?)""",
                       (credential_id, "enrolled", "enroll-" + credential_id, stamp))
            db.commit()
        return credential_id

    def active(self, principal_id: str):
        with self.apl_store._connect(readonly=True) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "apl_local_operator_credentials" not in tables:
                return None
            row = db.execute("""SELECT c.credential_id,c.salt,c.public_key,c.kdf_n,c.kdf_r,c.kdf_p
                FROM apl_local_operator_credentials c JOIN apl_local_operator_credential_events e
                ON e.credential_id=c.credential_id WHERE c.principal_id=? AND e.event_id=(
                    SELECT MAX(x.event_id) FROM apl_local_operator_credential_events x
                    WHERE x.credential_id=c.credential_id) AND e.event_type='enrolled'
                ORDER BY e.event_id DESC LIMIT 1""", (principal_id,)).fetchone()
        return tuple(row) if row else None

    def public_key_record(self, credential_id: str):
        """Read a credential's public verifier material, including after revocation."""
        if (type(credential_id) is not str
                or not re.fullmatch(r"local-credential-[0-9a-f]{32}", credential_id)):
            return None
        with self.apl_store._connect(readonly=True) as db:
            row = db.execute(
                "SELECT principal_id,public_key FROM apl_local_operator_credentials "
                "WHERE credential_id=?", (credential_id,)
            ).fetchone()
        return (row[0], bytes(row[1])) if row else None

    def verify_passphrase(self, principal_id: str, passphrase: str) -> bool:
        record = self.active(principal_id)
        if record is None:
            return False
        _credential_id, salt, expected_public, n, r, p = record
        try:
            private = _private_key(passphrase, salt, n=n, r=r, p=p)
            challenge = _proof_message(secrets.token_urlsafe(32))
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(expected_public).verify(private.sign(challenge), challenge)
        except Exception:
            return False
        return True

    def revoke(self, principal_id: str, *, credential_id: str | None = None,
               now: datetime | None = None) -> bool:
        record = self.active(principal_id)
        if record is None or (credential_id is not None and record[0] != credential_id):
            return False
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise LocalPassphraseError("aware_local_auth_clock_required")
        stamp = instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self.apl_store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_schema(db)
            latest = db.execute("SELECT event_type FROM apl_local_operator_credential_events "
                                "WHERE credential_id=? ORDER BY event_id DESC LIMIT 1",
                                (record[0],)).fetchone()
            if latest is None or latest[0] != "enrolled":
                db.rollback()
                return False
            db.execute("""INSERT INTO apl_local_operator_credential_events(
                credential_id,event_type,operation_id,occurred_at) VALUES(?,?,?,?)""",
                       (record[0], "revoked", "revoke-" + record[0], stamp))
            db.commit()
        return True


@dataclass(frozen=True)
class LocalPassphraseProofProvider:
    credential_store: SQLiteLocalOperatorCredentialStore
    principal_id: str
    prompt: Callable[[str], str] = field(default=getpass.getpass, repr=False, compare=False)
    confirm: Callable[[str], str] = field(default=input, repr=False, compare=False)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc),
                                         repr=False, compare=False)
    signing_session: LocalPassphraseSigningSession | None = field(default=None, repr=False, compare=False)

    def prove(self, request, receipt, challenge_nonce: str) -> LocalPassphraseProof:
        from .profile import inspect_consent_receipt
        checked = inspect_consent_receipt(request, receipt, now=self.clock())
        if (not checked.request_digest_matches or not checked.operator_identity_matches
                or not checked.explicit_action_present or not checked.within_validity
                or request.operator_identity != self.principal_id):
            raise LocalPassphraseError("local_authentication_request_mismatch")
        if not callable(self.prompt):
            raise LocalPassphraseError("interactive_local_passphrase_input_unavailable")
        if not callable(self.confirm):
            raise LocalPassphraseError("interactive_local_consent_confirmation_unavailable")
        limits = request.limits
        print("Confirmação vinculada à proposta persistida:")
        print(f"  ID: {request.proposal_id}")
        print(f"  Digest: {request.proposal_digest}")
        print(f"  Revisão: {request.revision}; workspace: {request.workspace_id}")
        print(f"  Operações: {', '.join(request.allowed_operations)}")
        print(f"  Limites: jobs={limits.max_jobs}, iterações={limits.max_iterations}, "
              f"timeout={limits.timeout_seconds}s, orçamento estimado="
              f"{limits.estimated_budget} {limits.budget_currency}")
        print(f"  Validade: {request.expires_at}; decisão: {receipt.decision.value}")
        expected_action = ("APROVAR" if receipt.decision.value == "approve" else "REJEITAR")
        token = f"{expected_action} {request.proposal_digest[7:19]}"
        entered = self.confirm(f"Digite exatamente '{token}' para confirmar: ")
        if type(entered) is not str or not hmac.compare_digest(entered.strip(), token):
            raise LocalPassphraseError("explicit_challenge_confirmation_failed")
        record = self.credential_store.active(self.principal_id)
        if record is None:
            raise LocalPassphraseError("local_operator_credential_unavailable")
        credential_id, salt, _public_key, n, r, p = record
        prompt_text = (f"AgentFlow confirmar {receipt.decision.value} da proposta "
                       f"{request.proposal_id} ({request.proposal_digest[:23]}…); senha local: ")
        private = _private_key(self.prompt(prompt_text), salt,
                               n=n, r=r, p=p)
        if not hmac.compare_digest(_public_key_bytes(private), _public_key):
            raise LocalPassphraseError("local_operator_authentication_failed")
        if self.signing_session is not None:
            self.signing_session.set_key(credential_id, private)
        return LocalPassphraseProof(
            credential_id, challenge_nonce, private.sign(_proof_message(challenge_nonce)))

    def clear_session(self) -> None:
        if self.signing_session is not None:
            self.signing_session.clear()


@dataclass(frozen=True)
class PassphraseLocalOperatorAuthenticator:
    credential_store: SQLiteLocalOperatorCredentialStore
    principal_id: str

    def authenticate(self, challenge_nonce: str, proof: object, *,
                     now: datetime) -> AuthenticatedOperator | None:
        if (not isinstance(proof, LocalPassphraseProof)
                or proof.challenge_nonce != challenge_nonce
                or proof.credential_id != (self.credential_store.active(self.principal_id) or (None,))[0]):
            return None
        record = self.credential_store.active(self.principal_id)
        if record is None:
            return None
        credential_id, _salt, public_key, _n, _r, _p = record
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                proof.signature, _proof_message(challenge_nonce))
        except Exception:
            return None
        if now.tzinfo is None:
            return None
        proof_digest = "sha256:" + hashlib.sha256(
            credential_id.encode("ascii") + _proof_message(challenge_nonce) + proof.signature
        ).hexdigest()
        return AuthenticatedOperator(
            self.principal_id, proof_digest, "local_passphrase_scrypt",
            now.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        )


def enroll_local_operator_interactive(credential_store: SQLiteLocalOperatorCredentialStore,
                                     principal_id: str, *, prompt: Callable[[str], str] = getpass.getpass
                                     ) -> str:
    if not callable(prompt):
        raise LocalPassphraseError("interactive_local_passphrase_input_unavailable")
    first = prompt("Criar senha local do operador (mínimo 12 caracteres): ")
    second = prompt("Confirmar senha local do operador: ")
    return credential_store._enroll(principal_id, first, second)


def revoke_local_operator_interactive(credential_store: SQLiteLocalOperatorCredentialStore,
                                      principal_id: str, *, prompt: Callable[[str], str] = getpass.getpass
                                      ) -> bool:
    if not callable(prompt):
        raise LocalPassphraseError("interactive_local_passphrase_input_unavailable")
    if not credential_store.verify_passphrase(
            principal_id, prompt("Confirmar revogação — senha local do operador: ")):
        raise LocalPassphraseError("local_operator_authentication_failed")
    record = credential_store.active(principal_id)
    return bool(record and credential_store.revoke(principal_id, credential_id=record[0]))
