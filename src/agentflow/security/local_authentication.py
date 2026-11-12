"""Platform authentication capability discovery and consent challenge records.

The probe never presents UI or authenticates a person. Finding an OS API is not
evidence that enrollment, user verification, or a consent ceremony is configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import sys
from typing import Callable
from uuid import uuid4


@dataclass(frozen=True)
class LocalAuthenticationCapability:
    schema_version: int
    platform: str
    mechanism: str
    native_api_available: bool
    ceremony_integrated: bool
    authentication_operational: bool
    reason: str
    local_passphrase_available: bool = False


@dataclass(frozen=True)
class LocalAuthenticationChallenge:
    """One-time proof challenge bound to a single consent decision."""

    schema_version: int
    challenge_id: str
    request_digest: str
    receipt_digest: str
    challenge_digest: str
    nonce: str = field(repr=False)
    expires_at: str

    def __post_init__(self) -> None:
        if (self.schema_version != 1 or not re.fullmatch(r"local-auth-[0-9a-f]{32}", self.challenge_id)
                or not self.nonce or not self.request_digest.startswith("sha256:")
                or not self.receipt_digest.startswith("sha256:")
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_digest)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.receipt_digest)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.challenge_digest)):
            raise ValueError("local_authentication_challenge_invalid")
        try:
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("local_authentication_challenge_expiry_invalid") from exc
        if expiry.tzinfo is None:
            raise ValueError("local_authentication_challenge_expiry_invalid")


class SQLiteLocalAuthenticationChallengeStore:
    """Append-only one-time challenge journal in the existing AgentFlow State Store."""

    def __init__(self, apl_store):
        if not callable(getattr(apl_store, "_connect", None)):
            raise ValueError("agentflow_state_store_required")
        self.apl_store = apl_store

    @staticmethod
    def _receipt_digest(receipt) -> str:
        payload = {
            "request_digest": receipt.request_digest,
            "decision": receipt.decision.value,
            "operator_identity": receipt.operator_identity,
            "explicit_action": receipt.explicit_action,
            "recorded_at": receipt.recorded_at,
            "expires_at": receipt.expires_at,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _ensure_schema(db) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS apl_local_auth_challenges(
            challenge_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
            receipt_digest TEXT NOT NULL, challenge_digest TEXT NOT NULL,
            nonce_digest TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS apl_local_auth_challenge_events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, challenge_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('issued','consumed','expired','failed')),
            operation_id TEXT NOT NULL UNIQUE, proof_digest TEXT, occurred_at TEXT NOT NULL,
            reason_code TEXT,
            FOREIGN KEY(challenge_id) REFERENCES apl_local_auth_challenges(challenge_id))""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_auth_challenges_no_update
            BEFORE UPDATE ON apl_local_auth_challenges BEGIN
            SELECT RAISE(ABORT,'authentication challenge bindings are immutable'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_auth_challenges_no_delete
            BEFORE DELETE ON apl_local_auth_challenges BEGIN
            SELECT RAISE(ABORT,'authentication challenge bindings are immutable'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_auth_events_no_update
            BEFORE UPDATE ON apl_local_auth_challenge_events BEGIN
            SELECT RAISE(ABORT,'authentication challenge events are append-only'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_local_auth_events_no_delete
            BEFORE DELETE ON apl_local_auth_challenge_events BEGIN
            SELECT RAISE(ABORT,'authentication challenge events are append-only'); END""")
        columns = {row[1] for row in db.execute(
            "PRAGMA table_info(apl_local_auth_challenge_events)")}
        if "reason_code" not in columns:
            db.execute("ALTER TABLE apl_local_auth_challenge_events ADD COLUMN reason_code TEXT")

    def issue(self, request, receipt, *, now: datetime | None = None,
              ttl_seconds: int = 120) -> LocalAuthenticationChallenge:
        from .profile import ConsentDecision, LocalConsentReceipt, LocalConsentRequest
        from .profile import inspect_consent_receipt
        if not isinstance(request, LocalConsentRequest) or not isinstance(receipt, LocalConsentReceipt):
            raise ValueError("typed_local_consent_contracts_required")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 120:
            raise ValueError("authentication_challenge_clock_or_ttl_invalid")
        instant = instant.astimezone(timezone.utc)
        checked = inspect_consent_receipt(request, receipt, now=instant)
        if (receipt.decision not in (ConsentDecision.APPROVE, ConsentDecision.REJECT)
                or not checked.request_digest_matches or not checked.operator_identity_matches
                or not checked.explicit_action_present or not checked.within_validity):
            raise ValueError("authentication_challenge_consent_binding_invalid")
        expires = min(datetime.fromisoformat(request.expires_at.replace("Z", "+00:00")),
                      datetime.fromisoformat(receipt.expires_at.replace("Z", "+00:00")),
                      instant + timedelta(seconds=ttl_seconds))
        challenge_id = "local-auth-" + uuid4().hex
        nonce = secrets.token_urlsafe(32)
        receipt_digest = self._receipt_digest(receipt)
        fields = {"schema_version": 1, "challenge_id": challenge_id,
                  "request_digest": request.request_digest, "receipt_digest": receipt_digest,
                  "nonce": nonce, "expires_at": expires.astimezone(timezone.utc).isoformat(
                      timespec="microseconds").replace("+00:00", "Z")}
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        challenge_digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        result = LocalAuthenticationChallenge(1, challenge_id, request.request_digest,
                                              receipt_digest, challenge_digest, nonce,
                                              fields["expires_at"])
        with self.apl_store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_schema(db)
            prior = db.execute("""SELECT c.challenge_id FROM apl_local_auth_challenges c
                JOIN apl_local_auth_challenge_events e ON e.challenge_id=c.challenge_id
                WHERE c.request_digest=? AND c.receipt_digest=?
                AND e.event_id=(SELECT MAX(x.event_id) FROM apl_local_auth_challenge_events x
                                WHERE x.challenge_id=c.challenge_id)
                AND e.event_type='issued' LIMIT 1""",
                (request.request_digest, receipt_digest)).fetchone()
            if prior is not None:
                pending = db.execute("""SELECT expires_at FROM apl_local_auth_challenges
                    WHERE challenge_id=?""", (prior[0],)).fetchone()
                if datetime.fromisoformat(pending[0].replace("Z", "+00:00")) > instant:
                    raise ValueError("authentication_challenge_replay")
                db.execute("""INSERT INTO apl_local_auth_challenge_events(
                    challenge_id,event_type,operation_id,occurred_at) VALUES(?,?,?,?)""",
                    (prior[0], "expired", "expire-" + prior[0],
                     instant.isoformat(timespec="microseconds").replace("+00:00", "Z")))
            db.execute("INSERT INTO apl_local_auth_challenges VALUES(?,?,?,?,?,?,?)",
                (challenge_id, request.request_digest, receipt_digest, challenge_digest,
                 hashlib.sha256(nonce.encode("ascii")).hexdigest(), fields["expires_at"],
                 instant.isoformat(timespec="microseconds").replace("+00:00", "Z")))
            db.execute("""INSERT INTO apl_local_auth_challenge_events(
                challenge_id,event_type,operation_id,occurred_at) VALUES(?,?,?,?)""",
                (challenge_id, "issued", "issue-" + challenge_id,
                 instant.isoformat(timespec="microseconds").replace("+00:00", "Z")))
            db.commit()
        return result

    def validate(self, challenge: LocalAuthenticationChallenge, request, receipt, *,
                 now: datetime | None = None) -> None:
        from .profile import LocalConsentReceipt, LocalConsentRequest
        if (not isinstance(challenge, LocalAuthenticationChallenge)
                or not isinstance(request, LocalConsentRequest)
                or not isinstance(receipt, LocalConsentReceipt)):
            raise ValueError("typed_authentication_challenge_required")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("aware_authentication_clock_required")
        expected_receipt = self._receipt_digest(receipt)
        fields = {"schema_version": 1, "challenge_id": challenge.challenge_id,
                  "request_digest": request.request_digest, "receipt_digest": expected_receipt,
                  "nonce": challenge.nonce, "expires_at": challenge.expires_at}
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected_challenge = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.apl_store._connect(readonly=True) as db:
            row = db.execute("""SELECT request_digest,receipt_digest,challenge_digest,nonce_digest,
                expires_at FROM apl_local_auth_challenges WHERE challenge_id=?""",
                (challenge.challenge_id,)).fetchone()
        if (row is None or row[0] != request.request_digest or row[1] != expected_receipt
                or row[2] != expected_challenge or challenge.challenge_digest != expected_challenge
                or row[3] != hashlib.sha256(challenge.nonce.encode("ascii")).hexdigest()
                or row[4] != challenge.expires_at):
            raise ValueError("authentication_challenge_binding_mismatch")
        if datetime.fromisoformat(row[4].replace("Z", "+00:00")) <= instant.astimezone(timezone.utc):
            raise ValueError("authentication_challenge_expired")

    def consume(self, challenge: LocalAuthenticationChallenge, proof_digest: str, *,
                operation_id: str, now: datetime | None = None) -> bool:
        if (not isinstance(challenge, LocalAuthenticationChallenge)
                or type(proof_digest) is not str
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", proof_digest)
                or type(operation_id) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", operation_id)):
            raise ValueError("authentication_challenge_consumption_invalid")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("aware_authentication_clock_required")
        instant = instant.astimezone(timezone.utc)
        with self.apl_store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_schema(db)
            row = db.execute("""SELECT request_digest,receipt_digest,challenge_digest,nonce_digest,
                expires_at FROM apl_local_auth_challenges WHERE challenge_id=?""",
                (challenge.challenge_id,)).fetchone()
            if row is None:
                raise ValueError("authentication_challenge_unknown")
            if (row[0] != challenge.request_digest or row[1] != challenge.receipt_digest
                    or not hmac.compare_digest(row[2], challenge.challenge_digest)
                    or not hmac.compare_digest(row[3], hashlib.sha256(
                        challenge.nonce.encode("ascii")).hexdigest())):
                raise ValueError("authentication_challenge_binding_mismatch")
            expiry = datetime.fromisoformat(row[4].replace("Z", "+00:00"))
            if instant >= expiry:
                raise ValueError("authentication_challenge_expired")
            prior = db.execute("""SELECT event_type,proof_digest,operation_id FROM
                apl_local_auth_challenge_events WHERE challenge_id=? ORDER BY event_id DESC LIMIT 1""",
                (challenge.challenge_id,)).fetchone()
            if prior is None or prior[0] != "issued":
                if prior and prior[0] == "consumed" and prior[1] == proof_digest and prior[2] == operation_id:
                    db.commit()
                    return True
                raise ValueError("authentication_challenge_replay")
            db.execute("""INSERT INTO apl_local_auth_challenge_events(
                challenge_id,event_type,operation_id,proof_digest,occurred_at) VALUES(?,?,?,?,?)""",
                (challenge.challenge_id, "consumed", operation_id, proof_digest,
                 instant.isoformat(timespec="microseconds").replace("+00:00", "Z")))
            db.commit()
            return True

    def fail(self, challenge: LocalAuthenticationChallenge, *, reason: str,
             now: datetime | None = None) -> None:
        """Permanently close a challenge after a failed or unavailable ceremony."""
        if (not isinstance(challenge, LocalAuthenticationChallenge)
                or not re.fullmatch(r"[a-z0-9_]{1,64}", reason)):
            raise ValueError("authentication_challenge_failure_invalid")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ValueError("aware_authentication_clock_required")
        instant = instant.astimezone(timezone.utc)
        with self.apl_store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_schema(db)
            row = db.execute("""SELECT challenge_digest FROM apl_local_auth_challenges
                WHERE challenge_id=?""", (challenge.challenge_id,)).fetchone()
            if row is None or row[0] != challenge.challenge_digest:
                raise ValueError("authentication_challenge_binding_mismatch")
            prior = db.execute("""SELECT event_type FROM apl_local_auth_challenge_events
                WHERE challenge_id=? ORDER BY event_id DESC LIMIT 1""",
                (challenge.challenge_id,)).fetchone()
            if prior is None or prior[0] != "issued":
                db.commit()
                return
            event_type = "expired" if datetime.fromisoformat(
                challenge.expires_at.replace("Z", "+00:00")) <= instant else "failed"
            db.execute("""INSERT INTO apl_local_auth_challenge_events(
                challenge_id,event_type,operation_id,occurred_at,reason_code) VALUES(?,?,?,?,?)""",
                (challenge.challenge_id, event_type,
                 event_type + "-" + challenge.challenge_id,
                 instant.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 reason if event_type == "failed" else "challenge_expired"))
            db.commit()


def inspect_local_authentication(*, platform: str | None = None,
                                 library_loader: Callable[[str], object] | None = None
                                 ) -> LocalAuthenticationCapability:
    """Read-only capability probe; never invokes an authenticator ceremony."""
    selected = platform or sys.platform
    if selected == "win32":
        try:
            loader = library_loader or ctypes.WinDLL
            api = loader("webauthn.dll")
            required = ("WebAuthNGetApiVersionNumber",
                        "WebAuthNAuthenticatorGetAssertion",
                        "WebAuthNFreeAssertion")
            if not all(hasattr(api, symbol) for symbol in required):
                raise OSError("required WebAuthn entry point unavailable")
            version_fn = api.WebAuthNGetApiVersionNumber
            try:
                version_fn.restype = ctypes.c_uint32
            except AttributeError:
                pass  # Allows a read-only test double; native ctypes functions expose restype.
            version = int(version_fn())
            if version < 1:
                raise OSError("unsupported WebAuthn API version")
            return LocalAuthenticationCapability(
                1, "windows", "local passphrase (scrypt/Ed25519, Windows SID); optional WebAuthn",
                True, False, False,
                "local_passphrase_backend_available; enrollment_and_real_consent_not_verified",
                True,
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return LocalAuthenticationCapability(
                1, "windows", "local passphrase (scrypt/Ed25519, Windows SID); optional WebAuthn",
                False, False, False,
                "local_passphrase_backend_available; enrollment_and_real_consent_not_verified",
                True,
            )
    if selected.startswith("linux"):
        return LocalAuthenticationCapability(
            1, "linux", "local passphrase (scrypt/Ed25519, OS UID); optional WebAuthn",
            False, False, False,
            "local_passphrase_backend_available; enrollment_and_real_consent_not_verified",
            True,
        )
    return LocalAuthenticationCapability(
        1, selected, "unsupported local authentication backend",
        False, False, False, "platform_backend_unsupported",
    )
