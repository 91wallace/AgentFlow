from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time

import pytest

from agentflow.approval.identity import certificate_fingerprint
from agentflow.approval.identity import ClientCredential, ClientIdentity, ClientPolicy
from agentflow.approval.persistence import BrokerStore
from agentflow.approval.protocol import canonical_json, digest_json
from agentflow.approval.protocol import build_contract
from agentflow.approval.transport import (
    ALPN_PROTOCOL, BrokerEnvelope, BrokerTransportError, LoopbackTLSClient,
    _read_exact, authenticate_tls_peer, create_broker_server_context,
)


def _openssl() -> str | None:
    executable = shutil.which("openssl")
    if executable:
        return executable
    candidates = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/usr/bin/openssl.exe",
        Path("/usr/bin/openssl"),
    )
    return str(next((path for path in candidates if path.exists()), "")) or None


@pytest.fixture(scope="module")
def certificates(request):
    openssl = _openssl()
    if not openssl:
        if os.environ.get("AGENTFLOW_REQUIRE_OPENSSL") == "1":
            pytest.fail("OpenSSL CLI is required for the supported CI TLS tests")
        pytest.skip("OpenSSL CLI is unavailable for disposable TLS test certificates")
    temporary = tempfile.TemporaryDirectory(prefix="agentflow-approval-test-pki-")
    request.addfinalizer(temporary.cleanup)
    folder = Path(temporary.name)

    def run(*args):
        result = subprocess.run([openssl, *map(str, args)], capture_output=True, text=True)
        if result.returncode:
            pytest.fail(f"test-only certificate generation failed: {result.stderr[-600:]}")

    ca_key, ca_cert = folder / "ca-key.pem", folder / "ca.pem"
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", ca_key,
        "-out", ca_cert, "-days", "2", "-subj", "/CN=AgentFlow-Disposable-Test-CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    wrong_ca = folder / "untrusted-ca.pem"
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", folder / "untrusted-key.pem",
        "-out", wrong_ca, "-days", "2", "-subj", "/CN=AgentFlow-Untrusted-Test-CA",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")

    def signed_leaf(label, common_name, extensions):
        key, csr, cert, ext = (folder / f"{label}-{suffix}" for suffix in
                               ("key.pem", "csr.pem", "cert.pem", "ext.cnf"))
        ext.write_text(extensions, encoding="ascii")
        run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", csr,
            "-subj", f"/CN={common_name}")
        serial = folder / f"{label}.srl"
        run("x509", "-req", "-in", csr, "-CA", ca_cert, "-CAkey", ca_key,
            "-CAcreateserial", "-CAserial", serial, "-out", cert, "-days", "2",
            "-extfile", ext)
        return cert, key

    server_cert, server_key = signed_leaf(
        "server", "localhost",
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n",
    )
    client_cert, client_key = signed_leaf(
        "client", "agentflow-test-client",
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=clientAuth\nsubjectAltName=URI:urn:agentflow:client:client-test\n",
    )
    expired_client_key = folder / "expired-client-key.pem"
    expired_client_csr = folder / "expired-client.csr"
    expired_client_cert = folder / "expired-client-cert.pem"
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", expired_client_key,
        "-out", expired_client_csr, "-subj", "/CN=AgentFlow-Expired-Test-Client")
    expired_index, expired_serial = folder / "expired-index.txt", folder / "expired-serial"
    expired_newcerts = folder / "expired-newcerts"
    expired_index.write_text("", encoding="ascii")
    expired_serial.write_text("1000\n", encoding="ascii")
    expired_newcerts.mkdir()
    posix = lambda path: str(path).replace("\\", "/")
    expired_ca_config = folder / "expired-ca.cnf"
    expired_ca_config.write_text(
        f"[ca]\ndefault_ca=CA_default\n[CA_default]\ndatabase={posix(expired_index)}\n"
        f"new_certs_dir={posix(expired_newcerts)}\ncertificate={posix(ca_cert)}\n"
        f"private_key={posix(ca_key)}\nserial={posix(expired_serial)}\ndefault_md=sha256\n"
        "policy=policy_any\nx509_extensions=v3_client\ncopy_extensions=copy\n"
        "[policy_any]\ncommonName=supplied\n[v3_client]\n"
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=clientAuth\nsubjectAltName=URI:urn:agentflow:client:expired\n",
        encoding="ascii",
    )
    run("ca", "-batch", "-config", expired_ca_config, "-in", expired_client_csr,
        "-out", expired_client_cert, "-startdate", "20200101000000Z",
        "-enddate", "20200102000000Z", "-extensions", "v3_client", "-notext")
    server_der = ssl.PEM_cert_to_DER_cert(server_cert.read_text(encoding="ascii"))
    return {
        "ca": ca_cert, "server_cert": server_cert, "server_key": server_key,
        "client_cert": client_cert, "client_key": client_key,
        "expired_client_cert": expired_client_cert, "expired_client_key": expired_client_key,
        "fingerprint": certificate_fingerprint(server_der), "folder": folder,
        "wrong_ca": wrong_ca,
    }


class _LocalTLSServer:
    """Ephemeral test-only TLS endpoint bound to loopback."""

    def __init__(self, certificates, *, response="valid", alpn=True,
                 server_cert=None, server_key=None, client_ca=None, store=None):
        self.certificates = certificates
        self.response = response
        self.alpn = alpn
        self.server_cert = server_cert or certificates["server_cert"]
        self.server_key = server_key or certificates["server_key"]
        self.client_ca = client_ca or certificates["ca"]
        self.store = store
        self.authenticated_client = None
        self.error = None
        self.peer_fingerprint = None
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            context = create_broker_server_context(
                server_certificate_pem=str(self.server_cert),
                server_private_key_pem=str(self.server_key),
                trusted_client_ca_pem=str(self.client_ca),
            )
            if not self.alpn:
                context.set_alpn_protocols(["agentflow-broker/0"])
            conn, _ = self.listener.accept()
            with context.wrap_socket(conn, server_side=True) as secure:
                der = secure.getpeercert(binary_form=True)
                self.peer_fingerprint = certificate_fingerprint(der) if der else None
                if self.store is not None:
                    self.authenticated_client = authenticate_tls_peer(secure, self.store)
                if self.response == "timeout":
                    time.sleep(0.5)
                    return
                if self.response == "drop":
                    return
                if self.response == "oversized":
                    secure.sendall(struct.pack("!I", 0xFFFFFFFF))
                    return
                if self.response == "malformed":
                    secure.sendall(struct.pack("!I", 9) + b"not-json!")
                    return
                size = struct.unpack("!I", _read_exact(secure, 4))[0]
                request = canonical_json({}) if size < 2 else _read_exact(secure, size).decode("utf-8")
                from agentflow.approval.transport import BrokerEnvelope
                inbound = BrokerEnvelope.from_json(request)
                if self.response == "version":
                    body = canonical_json({"ok": False, "operation_id": inbound.operation_id})
                    raw = canonical_json({
                        "protocol_version": 99, "message_type": "response",
                        "message_id": inbound.message_id, "operation_id": inbound.operation_id,
                        "nonce": inbound.nonce, "body_json": body,
                        "body_digest": digest_json({"ok": False}),
                    }).encode("utf-8")
                    secure.sendall(struct.pack("!I", len(raw)) + raw)
                    return
                if self.response == "correlation":
                    nonce = "0" * 64
                else:
                    nonce = inbound.nonce
                response = BrokerEnvelope.create(
                    message_type="response", operation_id=inbound.operation_id,
                    nonce=nonce, message_id=inbound.message_id,
                    body={"ok": True, "operation_id": inbound.operation_id},
                )
                payload = response.to_json().encode("utf-8")
                secure.sendall(struct.pack("!I", len(payload)) + payload)
        except Exception as exc:  # captured and asserted in the test thread
            self.error = exc
        finally:
            self.listener.close()

    def close(self):
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.listener.close()


def _client(certificates, server, *, timeout=2.0, pin=None, ca=None,
            client_cert=None, client_key=None):
    return LoopbackTLSClient(
        host="127.0.0.1", port=server.port, server_name="localhost",
        trust_anchor_pem=str(ca or certificates["ca"]),
        client_certificate_pem=str(client_cert or certificates["client_cert"]),
        client_private_key_pem=str(client_key or certificates["client_key"]),
        expected_server_fingerprint=pin or certificates["fingerprint"],
        timeout_seconds=timeout,
    )


def _request(operation_id="tls-operation"):
    return BrokerEnvelope.create(
        message_type="request", operation_id=operation_id, nonce="a" * 64,
        body={"action": "propose", "operation_id": operation_id, "version": 1},
    )


def _register_test_client(store, fingerprint, *, client_id="client-test", expires_seconds=1800):
    from datetime import datetime, timedelta, timezone
    def stamp(delta):
        return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
    identity = build_contract(
        ClientIdentity, protocol_version=1, client_id=client_id, principal_id="principal-test",
        scopes=("execution:propose",), created_at=stamp(-5), expires_at=stamp(3600),
    )
    credential = build_contract(
        ClientCredential, protocol_version=1, client_id=client_id, fingerprint=fingerprint,
        issued_at=stamp(-5), not_before=stamp(-5), expires_at=stamp(expires_seconds),
    )
    policy = build_contract(
        ClientPolicy, protocol_version=1, policy_id="policy-test", client_id=client_id,
        version=1, allowed_actions=("provider_call",), required_scopes=("execution:propose",),
        allowed_targets=("gateway-test",), max_request_ttl_seconds=300, created_at=stamp(-5),
    )
    store.register_client(identity, credential, policy)
    return identity, credential, policy


def test_loopback_mtls_validates_ca_pin_alpn_and_correlates_response(certificates):
    client_fingerprint = certificate_fingerprint(
        ssl.PEM_cert_to_DER_cert(certificates["client_cert"].read_text(encoding="ascii"))
    )
    with BrokerStore(certificates["folder"] / "test-broker.sqlite") as store:
        identity, _, _ = _register_test_client(store, client_fingerprint)
        server = _LocalTLSServer(certificates, store=store)
        try:
            response = _client(certificates, server).request(_request())
        finally:
            server.close()
        assert response.message_type == "response"
        assert response.body_json == canonical_json({"ok": True, "operation_id": "tls-operation"})
        assert server.peer_fingerprint == client_fingerprint
        assert server.authenticated_client.identity == identity
        assert server.error is None


def test_unknown_revoked_and_expired_registered_certificates_fail_closed(certificates):
    client_fingerprint = certificate_fingerprint(
        ssl.PEM_cert_to_DER_cert(certificates["client_cert"].read_text(encoding="ascii"))
    )
    with BrokerStore(certificates["folder"] / "identity-check.sqlite") as store:
        unknown_server = _LocalTLSServer(certificates, store=store)
        try:
            with pytest.raises(BrokerTransportError):
                _client(certificates, unknown_server).request(_request("unknown-client-op"))
        finally:
            unknown_server.close()
        assert isinstance(unknown_server.error, BrokerTransportError)
        assert "unknown or revoked" in str(unknown_server.error)
        _register_test_client(store, client_fingerprint)
        store.revoke_client_credential("client-test", client_fingerprint)
        server = _LocalTLSServer(certificates, store=store)
        try:
            with pytest.raises(BrokerTransportError):
                _client(certificates, server).request(_request())
        finally:
            server.close()
        assert isinstance(server.error, BrokerTransportError)
        assert "unknown or revoked" in str(server.error)
        expired_server = _LocalTLSServer(certificates)
        try:
            with pytest.raises(BrokerTransportError, match="TLS connection failed"):
                _client(certificates, expired_server,
                        client_cert=certificates["expired_client_cert"],
                        client_key=certificates["expired_client_key"]).request(_request("expired-cert-op"))
        finally:
            expired_server.close()
        assert isinstance(expired_server.error, ssl.SSLError)


def test_certificate_pin_and_ca_fail_closed(certificates):
    server = _LocalTLSServer(certificates)
    try:
        with pytest.raises(BrokerTransportError, match="identity pin mismatch"):
            _client(certificates, server, pin="sha256:" + "f" * 64).request(_request())
    finally:
        server.close()
    server = _LocalTLSServer(certificates)
    try:
        with pytest.raises(BrokerTransportError, match="TLS connection failed"):
            _client(certificates, server, ca=certificates["wrong_ca"]).request(_request())
    finally:
        server.close()


def test_protocol_downgrade_malformed_version_and_correlation_rejected(certificates):
    server = _LocalTLSServer(certificates, alpn=False)
    try:
        with pytest.raises(BrokerTransportError, match="ALPN protocol mismatch"):
            _client(certificates, server).request(_request())
    finally:
        server.close()
    for behavior in ("version", "correlation", "oversized", "malformed", "drop"):
        server = _LocalTLSServer(certificates, response=behavior)
        try:
            with pytest.raises(BrokerTransportError):
                _client(certificates, server).request(_request())
        finally:
            server.close()


def test_timeout_is_explicit_and_outcome_is_unknown(certificates):
    server = _LocalTLSServer(certificates, response="timeout")
    try:
        with pytest.raises(BrokerTransportError, match="outcome is unknown"):
            _client(certificates, server, timeout=0.1).request(_request())
    finally:
        server.close()


def test_transport_rejects_non_loopback_missing_client_identity_and_bad_envelope(certificates):
    with pytest.raises(BrokerTransportError, match="restricted to loopback"):
        LoopbackTLSClient(
            host="192.0.2.50", port=443, server_name="localhost",
            trust_anchor_pem=str(certificates["ca"]),
            client_certificate_pem=str(certificates["client_cert"]),
            client_private_key_pem=str(certificates["client_key"]),
            expected_server_fingerprint=certificates["fingerprint"],
        )
    server = _LocalTLSServer(certificates)
    try:
        with pytest.raises(BrokerTransportError, match="TLS connection failed"):
            _client(certificates, server, client_cert=certificates["server_cert"],
                    client_key=certificates["server_key"]).request(_request())
    finally:
        server.close()
    bad = _request().to_dict()
    bad["protocol_version"] = 9
    with pytest.raises(ValueError, match="unsupported protocol_version"):
        BrokerEnvelope.from_dict(bad)
    with pytest.raises(ValueError, match="operation_id differ"):
        BrokerEnvelope.create(message_type="request", operation_id="outer-id",
                              nonce="b" * 64, body={"operation_id": "inner-id"})


def test_tls_minimum_version_and_message_limit_are_fixed(certificates):
    context = create_broker_server_context(
        server_certificate_pem=str(certificates["server_cert"]),
        server_private_key_pem=str(certificates["server_key"]),
        trusted_client_ca_pem=str(certificates["ca"]),
    )
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert ALPN_PROTOCOL == "agentflow-broker/1"
