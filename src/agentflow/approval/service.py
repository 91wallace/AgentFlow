"""Loopback-only Broker service for pending proposals, without authority or dispatch."""

from __future__ import annotations

import ipaddress
import sqlite3
import socket
import ssl
import struct
import threading
from pathlib import Path
from uuid import uuid4

from .persistence import BrokerConflict, BrokerIntegrityError, BrokerStore, BrokerStoreError
from .protocol import (
    PROTOCOL_VERSION, PendingProposalCancellation, PendingProposalInvalidation,
    PendingProposalQuery, PendingProposalSnapshot, PendingProposalSubmission,
    ProposalLifecycleSnapshot, ProtocolError, SnapshotProposal, parse_canonical_json,
)
from .transport import (
    MAX_MESSAGE_BYTES,
    BrokerEnvelope,
    BrokerTransportError,
    _read_exact,
    _send_frame,
    authenticate_tls_peer,
    create_broker_server_context,
)


class BrokerServiceError(RuntimeError):
    """The local Broker service could not start or stop cleanly."""


class LocalBrokerService:
    """Single-instance loopback mTLS service with no approval or dispatch authority."""

    def __init__(self, store_path: str | Path, *, server_certificate_pem: str | Path,
                 server_private_key_pem: str | Path, trusted_client_ca_pem: str | Path,
                 port: int = 0, request_timeout_seconds: float = 3.0):
        if type(port) is not int or not 0 <= port <= 65535:
            raise BrokerServiceError("port must be between 0 and 65535")
        if (type(request_timeout_seconds) not in (int, float)
                or not 0.1 <= request_timeout_seconds <= 30):
            raise BrokerServiceError("request timeout must be between 0.1 and 30 seconds")
        self.store_path = Path(store_path)
        self.server_certificate_pem = str(server_certificate_pem)
        self.server_private_key_pem = str(server_private_key_pem)
        self.trusted_client_ca_pem = str(trusted_client_ca_pem)
        self.port = port
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._store: BrokerStore | None = None
        self._context: ssl.SSLContext | None = None
        self._failure: str | None = None
        self._started_once = False
        self.instance_id: str | None = None
        self.bound_port: int | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def authority_available(self) -> bool:
        """Always false: this foundation has no consent or grant issuer."""
        return False

    @property
    def failure(self) -> str | None:
        return self._failure

    def start(self) -> str:
        if self._started_once:
            raise BrokerServiceError("Broker service instance cannot be started twice")
        context = create_broker_server_context(
            server_certificate_pem=self.server_certificate_pem,
            server_private_key_pem=self.server_private_key_pem,
            trusted_client_ca_pem=self.trusted_client_ca_pem,
        )
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            # Numeric loopback bind is fixed; there is no configurable external interface.
            listener.bind(("127.0.0.1", self.port))
            listener.listen(8)
            listener.settimeout(0.2)
            store = BrokerStore(self.store_path)
        except Exception:
            listener.close()
            raise

        instance_id = uuid4().hex
        try:
            store.expire_pending_proposals()
            previous = store.latest_service_lifecycle()
            recovered = previous is not None and previous[0] in {"service.started", "service.failed"}
            if recovered:
                store.record_service_event("service.recovered", instance_id, {
                    "protocol_version": PROTOCOL_VERSION,
                    "previous_instance_id": previous[1],
                })
            store.record_service_event("service.started", instance_id, {
                "protocol_version": PROTOCOL_VERSION,
                "status": "running",
                "recovered": recovered,
                "previous_instance_id": previous[1] if recovered else None,
            })
        except Exception:
            store.close()
            listener.close()
            raise

        self._context = context
        self._listener = listener
        self._store = store
        self.instance_id = instance_id
        self.bound_port = listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name=f"agentflow-broker-{instance_id[:8]}",
                                        daemon=True)
        try:
            self._thread.start()
        except Exception as exc:
            self._failure = "listener_failure"
            self._started_once = True
            try:
                store.record_service_event("service.failed", instance_id, {
                    "protocol_version": PROTOCOL_VERSION,
                    "status": "failed",
                    "reason_code": "listener_failure",
                })
            except Exception:
                # The earlier durable "started" event makes the next start recover conservatively.
                pass
            finally:
                store.close()
                listener.close()
                self._store = None
                self._listener = None
                self._thread = None
            raise BrokerServiceError("Broker service listener thread failed to start") from exc
        self._started_once = True
        return instance_id

    def close(self, *, timeout_seconds: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        listener = self._listener
        if listener is not None:
            listener.close()
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise BrokerServiceError("Broker service did not stop before timeout")
        store = self._store
        event_error = None
        if store is not None:
            if self._failure is None and self.instance_id is not None:
                try:
                    store.record_service_event("service.stopped", self.instance_id, {
                        "protocol_version": PROTOCOL_VERSION,
                        "status": "stopped",
                    })
                except Exception as exc:
                    event_error = exc
            try:
                store.close()
            finally:
                self._store = None
                self._listener = None
                self._thread = None
        if event_error is not None:
            raise BrokerServiceError("Broker service stopped but its shutdown event was not persisted") from event_error

    def __enter__(self) -> "LocalBrokerService":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _audit_rejection(self, reason_code: str, message_id: str | None = None) -> None:
        if self._store is None or self.instance_id is None:
            return
        self._store.record_service_event("service.request_rejected", self.instance_id, {
            "protocol_version": PROTOCOL_VERSION,
            "message_id": message_id,
            "reason_code": reason_code,
        })

    def _read_request(self, secure: ssl.SSLSocket) -> tuple[BrokerEnvelope | None, str | None, str | None]:
        try:
            length = struct.unpack("!I", _read_exact(secure, 4))[0]
            if length < 2 or length > MAX_MESSAGE_BYTES:
                return None, "invalid_frame", None
            raw = _read_exact(secure, length)
            decoded = raw.decode("utf-8")
            value = parse_canonical_json(decoded)
            if type(value) is not dict:
                return None, "invalid_frame", None
            message_id = value.get("message_id")
            if type(message_id) is not str or len(message_id) > 200:
                message_id = None
            if (type(value.get("protocol_version")) is not int
                    or value.get("protocol_version") != PROTOCOL_VERSION):
                return None, "unsupported_protocol", message_id
            return BrokerEnvelope.from_json(decoded), None, message_id
        except (socket.timeout, TimeoutError):
            return None, "request_timeout", None
        except (OSError, UnicodeError, ValueError, ProtocolError, BrokerTransportError,
                struct.error):
            return None, "invalid_frame", None

    def _serve_client(self, raw: socket.socket) -> None:
        assert self._context is not None and self._store is not None and self.instance_id is not None
        raw.settimeout(self.request_timeout_seconds)
        try:
            secure = self._context.wrap_socket(raw, server_side=True)
        except (OSError, ssl.SSLError, TimeoutError):
            self._audit_rejection("tls_peer_unverified")
            raw.close()
            return
        with secure:
            try:
                client = authenticate_tls_peer(secure, self._store)
            except BrokerTransportError:
                self._audit_rejection("tls_peer_unverified")
                return
            request, error, message_id = self._read_request(secure)
            if error is not None or request is None:
                self._audit_rejection(error or "invalid_frame", message_id)
                return
            if request.message_type != "request":
                self._audit_rejection("unsupported_message_type", request.message_id)
                response_body = {"error_code": "unsupported_message_type",
                                 "operation_id": request.operation_id,
                                 "protocol_version": PROTOCOL_VERSION}
            else:
                try:
                    body = parse_canonical_json(request.body_json)
                except ProtocolError:
                    body = None
                if (type(body) is not dict
                        or body.get("operation_id") != request.operation_id
                        or type(body.get("protocol_version")) is not int
                        or body.get("protocol_version") != PROTOCOL_VERSION
                        or type(body.get("action")) is not str):
                    self._audit_rejection("invalid_message", request.message_id)
                    response_body = {"error_code": "invalid_message",
                                     "operation_id": request.operation_id,
                                     "protocol_version": PROTOCOL_VERSION}
                elif (body["action"] == "service_status" and set(body) == {
                        "action", "operation_id", "protocol_version"}):
                    self._store.record_service_event("service.status_queried", self.instance_id, {
                        "protocol_version": PROTOCOL_VERSION,
                        "message_id": request.message_id,
                    })
                    response_body = {
                        "authority_status": "unavailable",
                        "dispatch_allowed": False,
                        "operation_id": request.operation_id,
                        "protocol_version": PROTOCOL_VERSION,
                        "result": "service_status",
                        "service_instance_id": self.instance_id,
                        "service_status": "running",
                    }
                elif body["action"] == "service_status":
                    response_body = self._operation_error(
                        "invalid_message", request, reason="invalid_message")
                elif body["action"] == "snapshot_submit":
                    response_body = self._submit_pending_proposal(body, request, client)
                elif body["action"] == "snapshot_get":
                    response_body = self._query_pending_proposal(body, request, client)
                elif body["action"] == "snapshot_cancel":
                    response_body = self._cancel_pending_proposal(body, request, client)
                elif body["action"] == "snapshot_invalidate":
                    response_body = self._invalidate_pending_proposal(body, request, client)
                else:
                    self._audit_rejection("operation_not_exposed", request.message_id)
                    response_body = {"error_code": "operation_not_exposed",
                                     "operation_id": request.operation_id,
                                     "protocol_version": PROTOCOL_VERSION}
            response = BrokerEnvelope.create(
                message_type="response", operation_id=request.operation_id,
                nonce=request.nonce, message_id=request.message_id, body=response_body,
            )
            try:
                _send_frame(secure, response)
            except (OSError, BrokerTransportError, TimeoutError):
                # A lost proposal response can be replayed with the same immutable operation.
                return

    @staticmethod
    def _project_scope_ids(client) -> tuple[str, ...]:
        return tuple(sorted(scope.partition(":")[2] for scope in client.identity.scopes
                            if scope.startswith("project:") and scope.partition(":")[2]))

    def _operation_error(self, code: str, request: BrokerEnvelope, *, reason: str | None = None) -> dict:
        if reason is not None:
            self._audit_rejection(reason, request.message_id)
        return {"error_code": code, "operation_id": request.operation_id,
                "protocol_version": PROTOCOL_VERSION}

    @staticmethod
    def _snapshot_response(proposal: SnapshotProposal, sequence: int, client) -> dict:
        return PendingProposalSnapshot(
            protocol_version=PROTOCOL_VERSION,
            snapshot_id=proposal.snapshot_id,
            revision_id=proposal.revision_id,
            digest=proposal.canonical_digest,
            status="pending",
            client_id=client.identity.client_id,
            principal_id=client.identity.principal_id,
            journal_sequence=sequence,
            authority_status="unavailable",
            dispatch_allowed=False,
        ).to_dict()

    def _submit_pending_proposal(self, body: dict, request: BrokerEnvelope, client) -> dict:
        try:
            if set(body) != {"action", "operation_id", "protocol_version", "proposal_json"}:
                raise ProtocolError("submission envelope fields mismatch")
            operation = PendingProposalSubmission.from_dict({
                "protocol_version": body["protocol_version"],
                "operation_id": body["operation_id"],
                "proposal_json": body["proposal_json"],
            })
            proposal = SnapshotProposal.from_json(operation.proposal_json)
        except (ProtocolError, TypeError):
            return self._operation_error("invalid_proposal", request, reason="invalid_proposal")
        scopes = set(client.identity.scopes)
        if (proposal.proposed_by != client.identity.principal_id
                or "execution:propose" not in scopes
                or f"project:{proposal.project_id}" not in scopes):
            return self._operation_error("scope_denied", request, reason="scope_denied")
        assert self._store is not None
        try:
            self._store.expire_pending_proposals()
            record = self._store.propose_snapshot(proposal)
            if record.state != "pending":
                return self._operation_error("snapshot_not_pending", request,
                                             reason="lifecycle_conflict")
            pending = self._store.get_pending_snapshot_for_identity(
                proposal.snapshot_id, principal_id=client.identity.principal_id,
                authorized_project_ids=(proposal.project_id,),
            )
            if pending is None:
                raise BrokerIntegrityError("persisted proposal is not visible to its authenticated owner")
        except BrokerConflict:
            return self._operation_error("operation_conflict", request, reason="proposal_conflict")
        except (BrokerStoreError, sqlite3.Error, OSError):
            return self._operation_error("persistence_failed", request, reason="persistence_failed")
        return {
            "action": "snapshot_pending",
            "operation_id": request.operation_id,
            "protocol_version": PROTOCOL_VERSION,
            "snapshot": self._snapshot_response(pending.proposal, pending.journal_sequence, client),
        }

    def _query_pending_proposal(self, body: dict, request: BrokerEnvelope, client) -> dict:
        try:
            if set(body) != {"action", "operation_id", "protocol_version", "snapshot_id"}:
                raise ProtocolError("query envelope fields mismatch")
            query = PendingProposalQuery.from_dict({
                "protocol_version": body["protocol_version"],
                "operation_id": body["operation_id"],
                "snapshot_id": body["snapshot_id"],
            })
        except (ProtocolError, TypeError):
            return self._operation_error("invalid_message", request, reason="invalid_message")
        project_ids = self._project_scope_ids(client)
        if "execution:propose" not in client.identity.scopes or not project_ids:
            return self._operation_error("scope_denied", request, reason="scope_denied")
        assert self._store is not None
        try:
            self._store.expire_pending_proposals()
            lifecycle = self._store.get_proposal_lifecycle_for_identity(
                query.snapshot_id, principal_id=client.identity.principal_id,
                authorized_project_ids=project_ids,
            )
        except (BrokerStoreError, sqlite3.Error):
            return self._operation_error("snapshot_unavailable", request,
                                         reason="snapshot_unavailable")
        if lifecycle is None:
            return self._operation_error("snapshot_unavailable", request,
                                         reason="snapshot_unavailable")
        if lifecycle.state == "pending":
            return {
                "action": "snapshot_pending",
                "operation_id": request.operation_id,
                "protocol_version": PROTOCOL_VERSION,
                "snapshot": self._snapshot_response(
                    lifecycle.proposal, lifecycle.journal_sequence, client),
            }
        return {
            "action": "snapshot_lifecycle",
            "operation_id": request.operation_id,
            "protocol_version": PROTOCOL_VERSION,
            "snapshot": self._lifecycle_response(lifecycle),
        }

    @staticmethod
    def _lifecycle_response(record) -> dict:
        return ProposalLifecycleSnapshot(
            protocol_version=PROTOCOL_VERSION,
            snapshot_id=record.proposal.snapshot_id,
            revision_id=record.proposal.revision_id,
            digest=record.proposal.canonical_digest,
            status=record.state,
            principal_id=record.proposal.proposed_by,
            journal_sequence=record.journal_sequence,
            authority_status="unavailable",
            dispatch_allowed=False,
        ).to_dict()

    def _authorized_proposal_scope(self, client, request: BrokerEnvelope, project_id: str) -> bool:
        scopes = set(client.identity.scopes)
        if "execution:propose" in scopes and f"project:{project_id}" in scopes:
            return True
        self._operation_error("scope_denied", request, reason="scope_denied")
        return False

    def _cancel_pending_proposal(self, body: dict, request: BrokerEnvelope, client) -> dict:
        try:
            if set(body) != {"action", "operation_id", "protocol_version", "snapshot_id"}:
                raise ProtocolError("cancellation envelope fields mismatch")
            operation = PendingProposalCancellation.from_dict({
                "protocol_version": body["protocol_version"],
                "operation_id": body["operation_id"],
                "snapshot_id": body["snapshot_id"],
            })
        except (ProtocolError, TypeError):
            return self._operation_error("invalid_message", request, reason="invalid_message")
        assert self._store is not None
        try:
            current = self._store.get_proposal_lifecycle_for_identity(
                operation.snapshot_id, principal_id=client.identity.principal_id,
                authorized_project_ids=self._project_scope_ids(client),
            )
            if current is None:
                return self._operation_error("snapshot_unavailable", request,
                                             reason="snapshot_unavailable")
            if not self._authorized_proposal_scope(client, request, current.proposal.project_id):
                return {"error_code": "scope_denied", "operation_id": request.operation_id,
                        "protocol_version": PROTOCOL_VERSION}
            record = self._store.cancel_pending_proposal(
                operation.snapshot_id, principal_id=client.identity.principal_id,
                operation_id=operation.operation_id,
            )
            lifecycle = self._store.get_proposal_lifecycle_for_identity(
                operation.snapshot_id, principal_id=client.identity.principal_id,
                authorized_project_ids=(current.proposal.project_id,),
            )
            if lifecycle is None or lifecycle.state != record.state:
                raise BrokerIntegrityError("cancelled proposal lifecycle is inconsistent")
        except BrokerConflict:
            return self._operation_error("lifecycle_conflict", request, reason="lifecycle_conflict")
        except (BrokerStoreError, sqlite3.Error, OSError):
            return self._operation_error("snapshot_unavailable", request,
                                         reason="snapshot_unavailable")
        return {"action": "snapshot_lifecycle", "operation_id": request.operation_id,
                "protocol_version": PROTOCOL_VERSION,
                "snapshot": self._lifecycle_response(lifecycle)}

    def _invalidate_pending_proposal(self, body: dict, request: BrokerEnvelope, client) -> dict:
        try:
            if set(body) != {"action", "operation_id", "protocol_version", "snapshot_id",
                             "superseding_snapshot_id"}:
                raise ProtocolError("invalidation envelope fields mismatch")
            operation = PendingProposalInvalidation.from_dict({
                "protocol_version": body["protocol_version"],
                "operation_id": body["operation_id"],
                "snapshot_id": body["snapshot_id"],
                "superseding_snapshot_id": body["superseding_snapshot_id"],
            })
        except (ProtocolError, TypeError):
            return self._operation_error("invalid_message", request, reason="invalid_message")
        assert self._store is not None
        try:
            previous = self._store.get_proposal_lifecycle_for_identity(
                operation.snapshot_id, principal_id=client.identity.principal_id,
                authorized_project_ids=self._project_scope_ids(client),
            )
            if previous is None:
                return self._operation_error("snapshot_unavailable", request,
                                             reason="snapshot_unavailable")
            if not self._authorized_proposal_scope(client, request, previous.proposal.project_id):
                return {"error_code": "scope_denied", "operation_id": request.operation_id,
                        "protocol_version": PROTOCOL_VERSION}
            record = self._store.obsolete_pending_proposal(
                operation.snapshot_id, operation.superseding_snapshot_id,
                principal_id=client.identity.principal_id, operation_id=operation.operation_id,
            )
            lifecycle = self._store.get_proposal_lifecycle_for_identity(
                operation.snapshot_id, principal_id=client.identity.principal_id,
                authorized_project_ids=(previous.proposal.project_id,),
            )
            if lifecycle is None or lifecycle.state != record.state:
                raise BrokerIntegrityError("invalidated proposal lifecycle is inconsistent")
        except BrokerConflict:
            return self._operation_error("lifecycle_conflict", request, reason="lifecycle_conflict")
        except (BrokerStoreError, sqlite3.Error, OSError):
            return self._operation_error("snapshot_unavailable", request,
                                         reason="snapshot_unavailable")
        return {"action": "snapshot_lifecycle", "operation_id": request.operation_id,
                "protocol_version": PROTOCOL_VERSION,
                "snapshot": self._lifecycle_response(lifecycle)}

    def _serve(self) -> None:
        assert self._listener is not None
        try:
            while not self._stop.is_set():
                try:
                    raw, address = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop.is_set():
                        break
                    raise
                if not ipaddress.ip_address(address[0]).is_loopback:
                    raw.close()
                    continue
                self._serve_client(raw)
        except Exception:
            self._failure = "listener_failure"
            if self._store is not None and self.instance_id is not None:
                self._store.record_service_event("service.failed", self.instance_id, {
                    "protocol_version": PROTOCOL_VERSION,
                    "status": "failed",
                    "reason_code": "listener_failure",
                })
