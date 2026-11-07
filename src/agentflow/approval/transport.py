"""Loopback-only development TLS transport for the dormant Broker protocol.

This module provides TLS contexts and a client channel, not a listener or service.
Applications cannot disable certificate checks or widen this transport off loopback.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import socket
import ssl
import struct
import time
from typing import Any, ClassVar
from uuid import uuid4

from .identity import ClientRecord, certificate_fingerprint, new_operation_nonce
from .protocol import (PROTOCOL_VERSION, ProtocolError, WireContract, _check_version,
                       _identifier, _digest, canonical_json, digest_json, parse_canonical_json)

ALPN_PROTOCOL = "agentflow-broker/1"
MAX_MESSAGE_BYTES = 64 * 1024
DEFAULT_TIMEOUT_SECONDS = 5.0


class BrokerTransportError(RuntimeError):
    """TLS, framing, identity, timeout, or protocol failure; safe for logs."""


@dataclass(frozen=True)
class BrokerEnvelope(WireContract):
    protocol_version: int
    message_type: str
    message_id: str
    operation_id: str
    nonce: str
    body_json: str
    body_digest: str

    DIGEST_FIELD: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        _check_version(self.protocol_version)
        if self.message_type not in {"request", "response"}:
            raise ProtocolError("unknown message_type")
        _identifier(self.message_id, "message_id")
        _identifier(self.operation_id, "operation_id")
        if type(self.nonce) is not str or len(self.nonce) != 64 or any(
            char not in "0123456789abcdef" for char in self.nonce
        ):
            raise ProtocolError("nonce must be 32 random bytes encoded as lowercase hex")
        body = parse_canonical_json(self.body_json)
        if type(body) is not dict:
            raise ProtocolError("message body must be an object")
        if body.get("operation_id") != self.operation_id:
            raise ProtocolError("envelope and body operation_id differ or are missing")
        if "nonce" in body:
            raise ProtocolError("nonce must appear only in the envelope")
        if ("protocol_version" in body
                and body["protocol_version"] != self.protocol_version):
            raise ProtocolError("envelope and body protocol versions differ")
        _digest(self.body_digest, "body_digest")
        if digest_json(body) != self.body_digest:
            raise ProtocolError("message body digest mismatch")

    @classmethod
    def create(cls, *, message_type: str, operation_id: str, nonce: str,
               body: dict[str, Any], message_id: str | None = None) -> "BrokerEnvelope":
        body_json = canonical_json(body)
        return cls(PROTOCOL_VERSION, message_type, message_id or uuid4().hex,
                   operation_id, nonce, body_json, digest_json(body))

    @classmethod
    def new_request(cls, *, operation_id: str, body: dict[str, Any]) -> "BrokerEnvelope":
        return cls.create(message_type="request", operation_id=operation_id,
                          nonce=new_operation_nonce(), body=body)


def _loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _set_deadline(sock: socket.socket, deadline: float | None) -> None:
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerTransportError("Broker communication timed out; outcome is unknown")
        sock.settimeout(remaining)


def _read_exact(sock: socket.socket, length: int, deadline: float | None = None) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        _set_deadline(sock, deadline)
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise BrokerTransportError("connection closed during framed message")
        chunks.extend(chunk)
    return bytes(chunks)


def _send_frame(sock: socket.socket, envelope: BrokerEnvelope,
                deadline: float | None = None) -> None:
    payload = envelope.to_json().encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise BrokerTransportError("Broker message exceeds configured limit")
    _set_deadline(sock, deadline)
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _receive_frame(sock: socket.socket, deadline: float | None = None) -> BrokerEnvelope:
    size = struct.unpack("!I", _read_exact(sock, 4, deadline))[0]
    if size < 2 or size > MAX_MESSAGE_BYTES:
        raise BrokerTransportError("Broker frame size is invalid")
    payload = _read_exact(sock, size, deadline)
    try:
        value = parse_canonical_json(payload.decode("utf-8"))
        return BrokerEnvelope.from_dict(value)
    except (UnicodeError, ProtocolError, TypeError) as exc:
        raise BrokerTransportError("Broker response is malformed or incompatible") from exc


class LoopbackTLSClient:
    """One-request TLS client restricted to loopback and a pinned Broker identity."""

    def __init__(self, *, host: str, port: int, server_name: str,
                 trust_anchor_pem: str, client_certificate_pem: str,
                 client_private_key_pem: str, expected_server_fingerprint: str,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        if not _loopback_host(host):
            raise BrokerTransportError("development Broker transport is restricted to loopback")
        if type(port) is not int or not 1 <= port <= 65535:
            raise BrokerTransportError("Broker port is invalid")
        if type(timeout_seconds) not in (int, float) or not 0.1 <= timeout_seconds <= 30:
            raise BrokerTransportError("Broker timeout must be between 0.1 and 30 seconds")
        if not _loopback_host(server_name):
            raise BrokerTransportError("TLS server name must identify loopback")
        try:
            _digest(expected_server_fingerprint, "expected_server_fingerprint")
        except ProtocolError as exc:
            raise BrokerTransportError("pinned Broker certificate fingerprint is invalid") from exc
        self.host = host
        self.port = port
        self.server_name = server_name
        self.trust_anchor_pem = trust_anchor_pem
        self.client_certificate_pem = client_certificate_pem
        self.client_private_key_pem = client_private_key_pem
        self.expected_server_fingerprint = expected_server_fingerprint
        self.timeout_seconds = float(timeout_seconds)

    def _context(self) -> ssl.SSLContext:
        try:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH,
                                                 cafile=self.trust_anchor_pem)
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols([ALPN_PROTOCOL])
            context.load_cert_chain(self.client_certificate_pem, self.client_private_key_pem)
            return context
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise BrokerTransportError("TLS trust or client credential configuration failed") from exc

    def request(self, envelope: BrokerEnvelope) -> BrokerEnvelope:
        if not isinstance(envelope, BrokerEnvelope) or envelope.message_type != "request":
            raise BrokerTransportError("only a versioned request envelope can be sent")
        raw = None
        deadline = time.monotonic() + self.timeout_seconds
        try:
            addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0].split("%", 1)[0]).is_loopback
                                    for item in addresses):
                raise BrokerTransportError("Broker address resolved outside loopback")
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout_seconds)
            _set_deadline(raw, deadline)
            with self._context().wrap_socket(raw, server_hostname=self.server_name) as secure:
                raw = None
                peer_der = secure.getpeercert(binary_form=True)
                if certificate_fingerprint(peer_der) != self.expected_server_fingerprint:
                    raise BrokerTransportError("Broker certificate identity pin mismatch")
                if secure.selected_alpn_protocol() != ALPN_PROTOCOL:
                    raise BrokerTransportError("Broker ALPN protocol mismatch; downgrade rejected")
                _send_frame(secure, envelope, deadline)
                response = _receive_frame(secure, deadline)
                if (response.message_type != "response"
                        or response.protocol_version != envelope.protocol_version
                        or response.message_id != envelope.message_id
                        or response.operation_id != envelope.operation_id
                        or response.nonce != envelope.nonce):
                    raise BrokerTransportError("Broker response correlation failed")
                return response
        except BrokerTransportError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise BrokerTransportError("Broker communication timed out; outcome is unknown") from exc
        except (OSError, ssl.SSLError, ValueError, json.JSONDecodeError) as exc:
            raise BrokerTransportError("Broker TLS connection failed; outcome is unknown") from exc
        finally:
            if raw is not None:
                raw.close()


def create_broker_server_context(*, server_certificate_pem: str,
                                server_private_key_pem: str,
                                trusted_client_ca_pem: str) -> ssl.SSLContext:
    """Build a strict mTLS server context; does not open a socket or start a service."""
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=trusted_client_ca_pem)
        context.load_cert_chain(server_certificate_pem, server_private_key_pem)
        context.set_alpn_protocols([ALPN_PROTOCOL])
        return context
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise BrokerTransportError("mTLS server context configuration failed") from exc


def authenticate_tls_peer(secure_socket: ssl.SSLSocket, store: Any,
                          *, now: Any = None) -> ClientRecord:
    """Map a CA-validated mTLS leaf to one active, explicitly registered client."""
    if not isinstance(secure_socket, ssl.SSLSocket):
        raise BrokerTransportError("authenticated TLS socket is required")
    if secure_socket.context.verify_mode != ssl.CERT_REQUIRED:
        raise BrokerTransportError("Broker TLS context must require verified client certificates")
    if secure_socket.selected_alpn_protocol() != ALPN_PROTOCOL:
        raise BrokerTransportError("TLS peer negotiated an incompatible protocol")
    peer_der = secure_socket.getpeercert(binary_form=True)
    if not peer_der:
        raise BrokerTransportError("TLS peer supplied no client certificate")
    fingerprint = certificate_fingerprint(peer_der)
    record = store.get_client_credential(fingerprint)
    if record is None or record.status != "active":
        raise BrokerTransportError("unknown or revoked client identity")
    client = store.get_client(record.credential.client_id)
    if client is None or client.status != "active":
        raise BrokerTransportError("unknown or revoked client identity")
    from datetime import datetime, timezone
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise BrokerTransportError("client identity verification requires an aware UTC clock")
    current = current.astimezone(timezone.utc)
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    if not (parse(record.credential.not_before) <= current < parse(record.credential.expires_at)
            and parse(client.identity.created_at) <= current < parse(client.identity.expires_at)):
        raise BrokerTransportError("client certificate identity is outside its registered validity")
    return client
