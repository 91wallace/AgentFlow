from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import json
from pathlib import Path
import queue
import sqlite3
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

import pytest

from agentflow.approval.identity import certificate_fingerprint
from agentflow.approval.identity import ClientCredential, ClientIdentity, ClientPolicy
from agentflow.approval.persistence import BrokerConflict, BrokerStore
from agentflow.approval import BrokerServiceError, LocalBrokerService
from agentflow.approval.protocol import SnapshotProposal, canonical_json, digest_json
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
        self.request_received = False
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
                    # Complete the TLS close-notify handshake instead of closing
                    # one peer while the client context is still open.
                    raw = secure.unwrap()
                    raw.close()
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
                self.request_received = True
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


def _register_test_client(store, fingerprint, *, client_id="client-test", expires_seconds=1800,
                          project_ids=()):
    from datetime import datetime, timedelta, timezone
    def stamp(delta):
        return (datetime.now(timezone.utc) + timedelta(seconds=delta)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
    identity = build_contract(
        ClientIdentity, protocol_version=1, client_id=client_id, principal_id="principal-test",
        scopes=tuple(sorted(("execution:propose", *(f"project:{item}" for item in project_ids)))),
        created_at=stamp(-5), expires_at=stamp(3600),
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


def test_preparation_pin_check_only_reports_tls_peer_identity(certificates):
    from agentflow.approval.preparation import (
        AuthorityPreparationError, ServiceDomainConfig, verify_tls_peer_pin,
    )

    server = _LocalTLSServer(certificates, response="drop")
    context = ssl.create_default_context(cafile=str(certificates["ca"]))
    context.load_cert_chain(str(certificates["client_cert"]), str(certificates["client_key"]))
    context.set_alpn_protocols([ALPN_PROTOCOL])
    service = ServiceDomainConfig(
        domain_id="synthetic-broker-domain", service_id="synthetic-broker",
        operator_principal="synthetic-broker-operator",
        endpoint="https://broker.example.invalid/api",
        trust_root_fingerprint=_fp_for_test("1"),
        service_certificate_fingerprint=certificates["fingerprint"],
        key_custody_reference="synthetic-custody",
        recovery_reference="synthetic-recovery",
    )
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as secure:
                result = verify_tls_peer_pin(secure, service)
                assert result.tls_chain_verified and result.hostname_verified and result.pin_matched
                assert result.administrative_independence_verified is False
                assert result.accepted_for_authority is False
                with pytest.raises(AuthorityPreparationError, match="pin_mismatch"):
                    verify_tls_peer_pin(secure, replace_service_pin_for_test(service, "f"))
                raw = secure.unwrap()
                raw.close()
    finally:
        server.close()
    assert server.error is None
    assert server.request_received is False


def _fp_for_test(character):
    return "sha256:" + character * 64


def replace_service_pin_for_test(service, character):
    from dataclasses import replace
    return replace(service, service_certificate_fingerprint=_fp_for_test(character))


def _service(certificates, store_path, *, project_ids=()):
    fingerprint = certificate_fingerprint(
        ssl.PEM_cert_to_DER_cert(certificates["client_cert"].read_text(encoding="ascii"))
    )
    with BrokerStore(store_path) as store:
        if store.get_client("client-test") is None:
            _register_test_client(store, fingerprint, project_ids=project_ids)
    return LocalBrokerService(
        store_path, server_certificate_pem=certificates["server_cert"],
        server_private_key_pem=certificates["server_key"],
        trusted_client_ca_pem=certificates["ca"], request_timeout_seconds=2,
    )


def _service_client(certificates, service):
    return LoopbackTLSClient(
        host="127.0.0.1", port=service.bound_port, server_name="localhost",
        trust_anchor_pem=str(certificates["ca"]),
        client_certificate_pem=str(certificates["client_cert"]),
        client_private_key_pem=str(certificates["client_key"]),
        expected_server_fingerprint=certificates["fingerprint"], timeout_seconds=2,
    )


def _service_status_request(operation_id="service-status"):
    return BrokerEnvelope.new_request(operation_id=operation_id, body={
        "action": "service_status", "operation_id": operation_id,
        "protocol_version": 1,
    })


def _proposal(operation_id="proposal-operation", *, snapshot_id="proposal-snapshot",
              project_id="project-test", revision_id="revision-1", content=None,
              proposed_by="principal-test", parent_digest=None):
    content_json = canonical_json(content if content is not None else {"tasks": ["synthetic"]})
    return SnapshotProposal(
        protocol_version=1, snapshot_id=snapshot_id, kind="plan", project_id=project_id,
        revision_id=revision_id, schema_version=1, content_json=content_json,
        canonical_digest=digest_json(json.loads(content_json)),
        provenance_json=canonical_json({"source": "disposable-test"}),
        proposed_by=proposed_by, operation_id=operation_id,
        proposed_at=datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"),
        parent_digest=parent_digest,
    )


def _submit_snapshot_request(proposal):
    return BrokerEnvelope.new_request(operation_id=proposal.operation_id, body={
        "action": "snapshot_submit", "operation_id": proposal.operation_id,
        "protocol_version": 1, "proposal_json": proposal.to_json(),
    })


def _query_snapshot_request(operation_id, snapshot_id):
    return BrokerEnvelope.new_request(operation_id=operation_id, body={
        "action": "snapshot_get", "operation_id": operation_id,
        "protocol_version": 1, "snapshot_id": snapshot_id,
    })


def _cancel_snapshot_request(operation_id, snapshot_id):
    return BrokerEnvelope.new_request(operation_id=operation_id, body={
        "action": "snapshot_cancel", "operation_id": operation_id,
        "protocol_version": 1, "snapshot_id": snapshot_id,
    })


def _invalidate_snapshot_request(operation_id, snapshot_id, superseding_snapshot_id):
    return BrokerEnvelope.new_request(operation_id=operation_id, body={
        "action": "snapshot_invalidate", "operation_id": operation_id,
        "protocol_version": 1, "snapshot_id": snapshot_id,
        "superseding_snapshot_id": superseding_snapshot_id,
    })


def test_local_broker_service_start_status_audit_and_controlled_shutdown(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    from agentflow.approval.persistence import BrokerStore
    path = certificates["folder"] / "operational-broker.sqlite"
    service = _service(certificates, path)
    instance_id = service.start()
    try:
        assert service.running and service.bound_port > 0
        assert instance_id == service.instance_id
        assert service.authority_available is False
        response = _service_client(certificates, service).request(_service_status_request())
        body = parse_canonical_json(response.body_json)
        assert body["service_status"] == "running"
        assert body["service_instance_id"] == instance_id
        assert body["authority_status"] == "unavailable"
        assert body["dispatch_allowed"] is False
    finally:
        service.close()
    assert not service.running
    with pytest.raises(BrokerServiceError, match="cannot be started twice"):
        service.start()
    with BrokerStore(path) as store:
        events = store.list_events("broker_service", instance_id)
        assert [item["event_type"] for item in events] == [
            "service.started", "service.status_queried", "service.stopped",
        ]
        store.verify_integrity()


def test_local_broker_service_rejects_unexposed_and_malformed_requests(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    from agentflow.approval.persistence import BrokerStore
    path = certificates["folder"] / "rejected-broker.sqlite"
    service = _service(certificates, path)
    instance_id = service.start()
    try:
        client = _service_client(certificates, service)
        unauthorized = BrokerEnvelope.new_request(operation_id="do-not-authorize", body={
            "action": "approve", "operation_id": "do-not-authorize", "protocol_version": 1,
        })
        rejected = parse_canonical_json(client.request(unauthorized).body_json)
        assert rejected["error_code"] == "operation_not_exposed"
        malformed = BrokerEnvelope.new_request(operation_id="bad-schema", body={
            "action": "service_status", "extra": "not accepted",
            "operation_id": "bad-schema", "protocol_version": 1,
        })
        rejected = parse_canonical_json(client.request(malformed).body_json)
        assert rejected["error_code"] == "invalid_message"
    finally:
        service.close()
    with BrokerStore(path) as store:
        reasons = [event["payload"].get("reason_code")
                   for event in store.list_events("broker_service", instance_id)
                   if event["event_type"] == "service.request_rejected"]
        assert reasons == ["operation_not_exposed", "invalid_message"]


def test_local_broker_service_rejects_incompatible_protocol_version(certificates):
    from agentflow.approval.persistence import BrokerStore
    from agentflow.approval.protocol import canonical_json, digest_json
    path = certificates["folder"] / "version-broker.sqlite"
    service = _service(certificates, path)
    instance_id = service.start()
    try:
        body = {"action": "service_status", "operation_id": "bad-version",
                "protocol_version": 99}
        request = {
            "protocol_version": 99, "message_type": "request", "message_id": "bad-version-message",
            "operation_id": "bad-version", "nonce": "a" * 64,
            "body_json": canonical_json(body), "body_digest": digest_json(body),
        }
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(certificates["ca"]))
        context.load_cert_chain(str(certificates["client_cert"]), str(certificates["client_key"]))
        context.set_alpn_protocols([ALPN_PROTOCOL])
        with socket.create_connection(("127.0.0.1", service.bound_port), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as secure:
                payload = canonical_json(request).encode("utf-8")
                secure.sendall(struct.pack("!I", len(payload)) + payload)
                assert secure.recv(1) == b""
    finally:
        service.close()
    with BrokerStore(path) as store:
        rejected = [event["payload"] for event in store.list_events("broker_service", instance_id)
                    if event["event_type"] == "service.request_rejected"]
        assert rejected == [{"message_id": "bad-version-message", "protocol_version": 1,
                             "reason_code": "unsupported_protocol"}]


def test_local_broker_service_records_partial_message_timeout(certificates):
    from agentflow.approval.persistence import BrokerStore
    path = certificates["folder"] / "timeout-broker.sqlite"
    service = LocalBrokerService(
        path, server_certificate_pem=certificates["server_cert"],
        server_private_key_pem=certificates["server_key"],
        trusted_client_ca_pem=certificates["ca"], request_timeout_seconds=0.3,
    )
    fingerprint = certificate_fingerprint(
        ssl.PEM_cert_to_DER_cert(certificates["client_cert"].read_text(encoding="ascii"))
    )
    with BrokerStore(path) as store:
        _register_test_client(store, fingerprint)
    instance_id = service.start()
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(certificates["ca"]))
    context.load_cert_chain(str(certificates["client_cert"]), str(certificates["client_key"]))
    context.set_alpn_protocols([ALPN_PROTOCOL])
    try:
        with socket.create_connection(("127.0.0.1", service.bound_port), timeout=2) as raw:
            with context.wrap_socket(raw, server_hostname="localhost") as secure:
                secure.sendall(b"\x00")  # Incomplete 4-byte frame length.
                secure.settimeout(2)
                assert secure.recv(1) == b""
    finally:
        service.close()
    with BrokerStore(path) as store:
        rejected = [event["payload"] for event in store.list_events("broker_service", instance_id)
                    if event["event_type"] == "service.request_rejected"]
        assert rejected == [{"message_id": None, "protocol_version": 1,
                             "reason_code": "request_timeout"}]


def test_local_broker_service_restart_and_unclean_process_recovery(certificates):
    from agentflow.approval.persistence import BrokerStore
    path = certificates["folder"] / "restart-broker.sqlite"
    _service(certificates, path).close()  # initialize schema and test identity only
    repository = Path(__file__).resolve().parents[1]
    child_code = (
        "import sys,time,json; from agentflow.approval.service import LocalBrokerService; "
        "s=LocalBrokerService(sys.argv[1],server_certificate_pem=sys.argv[2],"
        "server_private_key_pem=sys.argv[3],trusted_client_ca_pem=sys.argv[4]); "
        "print(json.dumps([s.start(),s.bound_port]),flush=True); time.sleep(60)"
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", child_code, str(path), str(certificates["server_cert"]),
         str(certificates["server_key"]), str(certificates["ca"])],
        cwd=repository, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready = queue.Queue()
    reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    service = None
    try:
        line = ready.get(timeout=15)
        assert line, process.stderr.read()
        previous_instance, previous_port = __import__("json").loads(line)
        assert previous_port > 0 and process.poll() is None
        process.terminate()
        process.wait(timeout=10)

        service = _service(certificates, path)
        instance_id = service.start()
        assert instance_id != previous_instance
        response = _service_client(certificates, service).request(
            _service_status_request("after-restart")
        )
        assert response.message_type == "response"
        service.close()
        service = None
        with BrokerStore(path) as store:
            events = store.list_events("broker_service")
            recovered = [item for item in events if item["event_type"] == "service.recovered"]
            assert recovered[-1]["aggregate_id"] == instance_id
            assert recovered[-1]["payload"]["previous_instance_id"] == previous_instance
            assert store.latest_service_lifecycle() == ("service.stopped", instance_id)
            store.verify_integrity()
    finally:
        if service is not None:
            service.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def test_local_broker_service_reports_communication_failure_after_shutdown(certificates):
    path = certificates["folder"] / "closed-broker.sqlite"
    service = _service(certificates, path)
    service.start()
    client = _service_client(certificates, service)
    service.close()
    with pytest.raises(BrokerTransportError):
        client.request(_service_status_request("after-shutdown"))


def test_local_broker_service_releases_resources_when_listener_thread_cannot_start(
        certificates, monkeypatch):
    from agentflow.approval.persistence import BrokerStore
    import threading
    path = certificates["folder"] / "thread-start-failure.sqlite"
    service = _service(certificates, path)
    original_start = threading.Thread.start

    def fail_broker_thread(thread, *args, **kwargs):
        if thread.name.startswith("agentflow-broker-"):
            raise RuntimeError("injected thread startup failure")
        return original_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", fail_broker_thread)
    with pytest.raises(BrokerServiceError, match="listener thread failed to start"):
        service.start()
    assert service._store is None and service._listener is None and service._thread is None
    monkeypatch.setattr(threading.Thread, "start", original_start)
    with BrokerStore(path) as store:
        assert store.latest_service_lifecycle() == ("service.failed", service.instance_id)
        store.verify_integrity()
    restarted = _service(certificates, path)
    new_instance = restarted.start()
    try:
        # A new instance sees the durable failed lifecycle and remains non-authoritative.
        assert new_instance != service.instance_id
        assert restarted.authority_available is False
    finally:
        restarted.close()
    with BrokerStore(path) as store:
        recovered = [event for event in store.list_events("broker_service")
                     if event["event_type"] == "service.recovered"]
        assert recovered[-1]["payload"]["previous_instance_id"] == service.instance_id
        store.verify_integrity()


def test_local_broker_service_closes_store_if_shutdown_event_write_fails(certificates, monkeypatch):
    from agentflow.approval.persistence import BrokerStore
    path = certificates["folder"] / "shutdown-event-failure.sqlite"
    service = _service(certificates, path)
    service.start()
    store = service._store
    original_record = store.record_service_event

    def fail_stopped(event_type, instance_id, payload):
        if event_type == "service.stopped":
            raise OSError("injected event persistence failure")
        return original_record(event_type, instance_id, payload)

    monkeypatch.setattr(store, "record_service_event", fail_stopped)
    with pytest.raises(BrokerServiceError, match="shutdown event was not persisted"):
        service.close()
    assert service._store is None and service._listener is None and service._thread is None
    with BrokerStore(path) as reopened:
        assert reopened.latest_service_lifecycle() == ("service.started", service.instance_id)
        reopened.verify_integrity()


def test_mtls_pending_proposal_persists_idempotently_and_is_queryable_after_restart(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "pending-proposal-restart.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    proposal = _proposal()
    request = _submit_snapshot_request(proposal)
    try:
        client = _service_client(certificates, service)
        submitted = parse_canonical_json(client.request(request).body_json)
        assert submitted["action"] == "snapshot_pending"
        snapshot = submitted["snapshot"]
        assert snapshot["snapshot_id"] == proposal.snapshot_id
        assert snapshot["revision_id"] == proposal.revision_id
        assert snapshot["digest"] == proposal.canonical_digest
        assert snapshot["status"] == "pending"
        assert snapshot["client_id"] == "client-test"
        assert snapshot["principal_id"] == "principal-test"
        assert snapshot["authority_status"] == "unavailable"
        assert snapshot["dispatch_allowed"] is False
        assert snapshot["journal_sequence"] > 0
        replay = parse_canonical_json(client.request(request).body_json)
        assert replay["snapshot"] == snapshot
        proposal_events = [event for event in service._store.list_events("snapshot", proposal.snapshot_id)
                           if event["event_type"] == "snapshot.proposed"]
        assert len(proposal_events) == 1
        assert proposal_events[0]["sequence"] == snapshot["journal_sequence"]
        service._store.verify_integrity()
    finally:
        service.close()

    restarted = _service(certificates, path, project_ids=("project-test",))
    restarted.start()
    try:
        response = parse_canonical_json(_service_client(certificates, restarted).request(
            _query_snapshot_request("query-after-restart", proposal.snapshot_id)
        ).body_json)
        assert response["action"] == "snapshot_pending"
        assert response["snapshot"] == snapshot
        assert restarted.authority_available is False
    finally:
        restarted.close()
    with BrokerStore(path) as store:
        persisted = store.get_snapshot(proposal.snapshot_id)
        assert isinstance(persisted, SnapshotProposal)
        assert persisted.to_json() == proposal.to_json()
        store.verify_integrity()


def test_mtls_pending_proposal_rejects_scope_and_conflicting_replay(certificates):
    from dataclasses import replace
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "pending-proposal-conflict.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    try:
        client = _service_client(certificates, service)
        out_of_scope = _proposal(operation_id="outside-op", snapshot_id="outside-snapshot",
                                 project_id="other-project")
        denied = parse_canonical_json(client.request(_submit_snapshot_request(out_of_scope)).body_json)
        assert denied["error_code"] == "scope_denied"

        proposal = _proposal(operation_id="conflict-op", snapshot_id="conflict-snapshot")
        accepted_for_persistence = parse_canonical_json(
            client.request(_submit_snapshot_request(proposal)).body_json)
        assert accepted_for_persistence["snapshot"]["status"] == "pending"
        conflicting = replace(
            proposal,
            content_json=canonical_json({"tasks": ["different"]}),
            canonical_digest=digest_json({"tasks": ["different"]}),
        )
        conflict = parse_canonical_json(
            client.request(_submit_snapshot_request(conflicting)).body_json)
        assert conflict["error_code"] == "operation_conflict"
        events = [event for event in service._store.list_events("snapshot", proposal.snapshot_id)
                  if event["event_type"] == "snapshot.proposed"]
        assert len(events) == 1
        assert service._store.get_snapshot(proposal.snapshot_id).canonical_digest == proposal.canonical_digest
    finally:
        service.close()


def test_mtls_pending_proposal_query_hides_out_of_scope_and_inconsistent_state(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "pending-proposal-scope.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    try:
        client = _service_client(certificates, service)
        other = _proposal(operation_id="other-project-op", snapshot_id="other-project-snapshot",
                          project_id="other-project")
        service._store.propose_snapshot(other)
        hidden = parse_canonical_json(client.request(
            _query_snapshot_request("hidden-query", other.snapshot_id)).body_json)
        assert hidden["error_code"] == "snapshot_unavailable"
        assert "snapshot" not in hidden

        owned = _proposal(operation_id="owned-op", snapshot_id="owned-snapshot")
        result = parse_canonical_json(client.request(_submit_snapshot_request(owned)).body_json)
        assert result["snapshot"]["status"] == "pending"
        service._store.db.execute(
            "UPDATE snapshot_state SET actor_id=? WHERE snapshot_id=?",
            ("divergent-principal", owned.snapshot_id),
        )
        inconsistent = parse_canonical_json(client.request(
            _query_snapshot_request("inconsistent-query", owned.snapshot_id)).body_json)
        assert inconsistent["error_code"] == "snapshot_unavailable"
        assert "snapshot" not in inconsistent
    finally:
        service.close()


def test_mtls_pending_proposal_persistence_failure_is_not_acknowledged(certificates, monkeypatch):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "pending-proposal-failure.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    proposal = _proposal(operation_id="persistence-failure-op", snapshot_id="persistence-failure")
    original = service._store.propose_snapshot

    def fail_persistence(_proposal):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(service._store, "propose_snapshot", fail_persistence)
    try:
        response = parse_canonical_json(_service_client(certificates, service).request(
            _submit_snapshot_request(proposal)).body_json)
        assert response["error_code"] == "persistence_failed"
        assert "snapshot" not in response
    finally:
        monkeypatch.setattr(service._store, "propose_snapshot", original)
        service.close()
    with BrokerStore(path) as store:
        with pytest.raises(Exception, match="snapshot not found"):
            store.get_snapshot(proposal.snapshot_id)
        store.verify_integrity()


def test_mtls_pending_proposal_rolls_back_partial_sqlite_write(certificates, monkeypatch):
    import sqlite3
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "pending-proposal-rollback.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    proposal = _proposal(operation_id="rollback-op", snapshot_id="rollback-snapshot")
    store = service._store
    original_append = store._append_event

    def fail_after_proposal_event(db, aggregate_type, aggregate_id, event_type, payload, occurred_at):
        event_id, sequence = original_append(
            db, aggregate_type, aggregate_id, event_type, payload, occurred_at)
        if event_type == "snapshot.proposed":
            raise sqlite3.OperationalError("synthetic commit-path failure")
        return event_id, sequence

    monkeypatch.setattr(store, "_append_event", fail_after_proposal_event)
    try:
        response = parse_canonical_json(_service_client(certificates, service).request(
            _submit_snapshot_request(proposal)).body_json)
        assert response["error_code"] == "persistence_failed"
        assert store.db.execute("SELECT 1 FROM broker_snapshots WHERE snapshot_id=?",
                                (proposal.snapshot_id,)).fetchone() is None
        assert store.db.execute("SELECT 1 FROM broker_events WHERE aggregate_id=?",
                                (proposal.snapshot_id,)).fetchone() is None
    finally:
        monkeypatch.setattr(store, "_append_event", original_append)
        service.close()
    with BrokerStore(path) as reopened:
        reopened.verify_integrity()


def test_mtls_pending_proposal_rejects_malformed_contract_and_identity_spoof(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "pending-proposal-invalid.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    try:
        client = _service_client(certificates, service)
        malformed = BrokerEnvelope.new_request(operation_id="malformed-proposal", body={
            "action": "snapshot_submit", "operation_id": "malformed-proposal",
            "protocol_version": 1, "proposal_json": "{not-json",
        })
        assert parse_canonical_json(client.request(malformed).body_json)["error_code"] == "invalid_proposal"
        spoofed = _proposal(operation_id="spoofed-proposal", snapshot_id="spoofed-snapshot",
                            proposed_by="some-other-principal")
        response = parse_canonical_json(client.request(_submit_snapshot_request(spoofed)).body_json)
        assert response["error_code"] == "scope_denied"
        assert service._store.db.execute(
            "SELECT 1 FROM broker_snapshots WHERE snapshot_id=?", (spoofed.snapshot_id,)
        ).fetchone() is None
    finally:
        service.close()


def test_mtls_owner_cancellation_is_persisted_idempotent_and_never_dispatches(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "proposal-cancellation.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    proposal = _proposal(operation_id="cancelled-proposal-op", snapshot_id="cancelled-proposal")
    try:
        client = _service_client(certificates, service)
        submitted = parse_canonical_json(client.request(_submit_snapshot_request(proposal)).body_json)
        assert submitted["snapshot"]["status"] == "pending"
        cancel_request = _cancel_snapshot_request("cancel-proposal-op", proposal.snapshot_id)
        cancelled = parse_canonical_json(client.request(cancel_request).body_json)
        assert cancelled["action"] == "snapshot_lifecycle"
        assert cancelled["snapshot"]["status"] == "cancelled"
        assert cancelled["snapshot"]["principal_id"] == "principal-test"
        assert cancelled["snapshot"]["authority_status"] == "unavailable"
        assert cancelled["snapshot"]["dispatch_allowed"] is False
        assert parse_canonical_json(client.request(cancel_request).body_json) == cancelled
        retry_submit = parse_canonical_json(client.request(_submit_snapshot_request(proposal)).body_json)
        assert retry_submit["error_code"] == "snapshot_not_pending"
        with pytest.raises(BrokerConflict):
            service._store.cancel_pending_proposal(
                proposal.snapshot_id, principal_id="principal-test", operation_id="different-cancel")
        service._store.verify_integrity()
        assert service._store.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        assert service._store.db.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0] == 0
    finally:
        service.close()
    with BrokerStore(path) as reopened:
        record = reopened.get_proposal_lifecycle_for_identity(
            proposal.snapshot_id, principal_id="principal-test", authorized_project_ids=("project-test",))
        assert record is not None and record.state == "cancelled"
        reopened.verify_integrity()
    restarted = _service(certificates, path, project_ids=("project-test",))
    restarted.start()
    try:
        replay = parse_canonical_json(_service_client(certificates, restarted).request(
            cancel_request).body_json)
        assert replay["snapshot"]["status"] == "cancelled"
    finally:
        restarted.close()


def test_mtls_cancellation_denies_non_owner_and_unknown_scope(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "proposal-cancel-denied.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    other = _proposal(operation_id="other-owner-op", snapshot_id="other-owner",
                      proposed_by="different-principal")
    try:
        service._store.propose_snapshot(other)
        response = parse_canonical_json(_service_client(certificates, service).request(
            _cancel_snapshot_request("unauthorized-cancel", other.snapshot_id)).body_json)
        assert response["error_code"] == "snapshot_unavailable"
        state = service._store.db.execute(
            "SELECT status FROM snapshot_state WHERE snapshot_id=?", (other.snapshot_id,)).fetchone()[0]
        assert state == "pending"
    finally:
        service.close()


def test_mtls_lifecycle_write_failure_rolls_back_and_allows_safe_retry(certificates, monkeypatch):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "proposal-lifecycle-write-failure.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    proposal = _proposal(operation_id="lifecycle-write-failure-proposal",
                         snapshot_id="lifecycle-write-failure")
    request = _cancel_snapshot_request("retryable-cancel-op", proposal.snapshot_id)
    try:
        client = _service_client(certificates, service)
        client.request(_submit_snapshot_request(proposal))
        store = service._store
        original_append = store._append_event

        def fail_terminal_transition(db, aggregate_type, aggregate_id, event_type, payload, occurred_at):
            event_id, sequence = original_append(
                db, aggregate_type, aggregate_id, event_type, payload, occurred_at)
            if event_type == "snapshot.status_changed":
                raise sqlite3.OperationalError("injected terminal transition failure")
            return event_id, sequence

        monkeypatch.setattr(store, "_append_event", fail_terminal_transition)
        failed = parse_canonical_json(client.request(request).body_json)
        assert failed["error_code"] == "snapshot_unavailable"
        state = store.db.execute(
            "SELECT status FROM snapshot_state WHERE snapshot_id=?", (proposal.snapshot_id,)).fetchone()[0]
        assert state == "pending"
        assert not [event for event in store.list_events("snapshot", proposal.snapshot_id)
                    if event["event_type"] == "snapshot.status_changed"]
        monkeypatch.setattr(store, "_append_event", original_append)
        retried = parse_canonical_json(client.request(request).body_json)
        assert retried["snapshot"]["status"] == "cancelled"
        store.verify_integrity()
    finally:
        service.close()


def test_mtls_revision_invalidation_requires_digest_link_and_is_recoverable(certificates):
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "proposal-invalidation.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    previous = _proposal(operation_id="revision-one-op", snapshot_id="revision-one",
                         revision_id="r1")
    successor = _proposal(operation_id="revision-two-op", snapshot_id="revision-two",
                          revision_id="r2", parent_digest=previous.canonical_digest)
    try:
        client = _service_client(certificates, service)
        for proposal in (previous, successor):
            assert parse_canonical_json(client.request(_submit_snapshot_request(proposal)).body_json)[
                "snapshot"]["status"] == "pending"
        invalid = _proposal(operation_id="unlinked-revision-op", snapshot_id="unlinked-revision",
                            revision_id="r3", parent_digest=digest_json({"unrelated": True}))
        client.request(_submit_snapshot_request(invalid))
        failed = parse_canonical_json(client.request(_invalidate_snapshot_request(
            "invalid-invalidation-op", previous.snapshot_id, invalid.snapshot_id)).body_json)
        assert failed["error_code"] == "lifecycle_conflict"
        state = service._store.db.execute(
            "SELECT status FROM snapshot_state WHERE snapshot_id=?", (previous.snapshot_id,)).fetchone()[0]
        assert state == "pending"

        invalidation_request = _invalidate_snapshot_request(
            "valid-invalidation-op", previous.snapshot_id, successor.snapshot_id)
        obsolete = parse_canonical_json(client.request(invalidation_request).body_json)
        assert obsolete["snapshot"]["status"] == "obsolete"
        assert obsolete["snapshot"]["authority_status"] == "unavailable"
        assert obsolete["snapshot"]["dispatch_allowed"] is False
        assert parse_canonical_json(client.request(invalidation_request).body_json) == obsolete
        queried = parse_canonical_json(client.request(
            _query_snapshot_request("query-obsolete", previous.snapshot_id)).body_json)
        assert queried["action"] == "snapshot_lifecycle"
        assert queried["snapshot"]["status"] == "obsolete"
        service._store.verify_integrity()
    finally:
        service.close()
    with BrokerStore(path) as reopened:
        record = reopened.get_proposal_lifecycle_for_identity(
            previous.snapshot_id, principal_id="principal-test", authorized_project_ids=("project-test",))
        assert record is not None and record.state == "obsolete"
        reopened.verify_integrity()


def test_mtls_expired_proposal_terminal_state_survives_broker_restart(certificates):
    from datetime import timedelta
    from agentflow.approval.protocol import parse_canonical_json
    path = certificates["folder"] / "proposal-expiry-restart.sqlite"
    service = _service(certificates, path, project_ids=("project-test",))
    service.start()
    proposal = _proposal(operation_id="expiry-over-transport", snapshot_id="expired-over-transport")
    try:
        client = _service_client(certificates, service)
        client.request(_submit_snapshot_request(proposal))
        event = next(item for item in service._store.list_events("snapshot", proposal.snapshot_id)
                     if item["event_type"] == "snapshot.proposed")
        received = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        assert service._store.expire_pending_proposals(
            as_of=received + timedelta(days=7, seconds=1)) == 1
    finally:
        service.close()
    restarted = _service(certificates, path, project_ids=("project-test",))
    restarted.start()
    try:
        response = parse_canonical_json(_service_client(certificates, restarted).request(
            _query_snapshot_request("query-expired", proposal.snapshot_id)).body_json)
        assert response["action"] == "snapshot_lifecycle"
        assert response["snapshot"]["status"] == "expired"
        assert response["snapshot"]["dispatch_allowed"] is False
    finally:
        restarted.close()


def test_apl_broker_supervised_handoff_real_mtls_sqlite_and_resume(certificates, monkeypatch, capsys):
    """Demonstrate validated APL proposal -> real loopback Broker -> awaiting authorization."""
    from agentflow.apl import (BrokerHandoffService, DeterministicPlannerAdapter,
                               JobProposal, SQLiteCycleStore, SupervisedCycleService,
                               CycleSnapshot, APLState)
    from agentflow.state.execution import ExecutionStore
    from agentflow.approval.protocol import parse_canonical_json

    root = certificates["folder"]
    project_root = root / "synthetic-repository"
    state_dir = project_root / ".agentflow"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    broker_path = root / "apl-broker.sqlite3"
    broker = _service(certificates, broker_path, project_ids=("project-test",))
    broker.start()
    try:
        client = _service_client(certificates, broker)
        apl_store = SQLiteCycleStore(state_path)
        apl = SupervisedCycleService(apl_store, state_path)
        cycle = apl_store.create(CycleSnapshot(
            "cycle-handoff", "calculate a synthetic sum", ("task:first",), APLState.READY,
            (JobProposal("first", "synthetic work", (), ("task:first",), ("criterion",)),),
            (), 0, 0))
        proposed, apl_status, created = apl.replan(
            cycle.cycle_id, DeterministicPlannerAdapter(rationale="synthetic validated plan"),
            "deterministic-handoff-request")
        assert apl_status == "proposed" and created is True
        class DropFirstRequest:
            def __init__(self, delegate):
                self.delegate = delegate
                self.first = True
            def request(self, envelope):
                if self.first:
                    self.first = False
                    raise BrokerTransportError("synthetic interrupted dispatch; outcome unknown")
                return self.delegate.request(envelope)

        handoff = BrokerHandoffService(apl_store, state_path, DropFirstRequest(client),
                                       project_id="project-test", principal_id="principal-test")
        with pytest.raises(BrokerTransportError, match="outcome unknown"):
            handoff.submit(cycle.cycle_id, proposed.proposal_id)
        assert any(item["state"] == "unknown"
                   for item in handoff.history(cycle.cycle_id, proposed.proposal_id))
        result = handoff.resume(cycle.cycle_id, proposed.proposal_id)
        assert result["status"] == "awaiting_authorization"
        assert result["authority_status"] == "unavailable"
        assert result["dispatch_allowed"] is False
        assert apl_store.load(cycle.cycle_id).state is APLState.WAITING_APPROVAL
        assert apl.store.proposal_status(proposed.proposal_id) == "proposed"
        assert len(handoff.history(cycle.cycle_id, proposed.proposal_id)) >= 3
        duplicate = handoff.submit(cycle.cycle_id, proposed.proposal_id)
        assert duplicate == result

        # Exercise the public CLI command against the same real mTLS Broker.
        import agentflow.cli as cli
        from agentflow.cli import main
        monkeypatch.setattr(cli, "repository_root", lambda _path: project_root)
        cli_cycle = apl_store.create(CycleSnapshot(
            "cycle-cli-handoff", "synthetic CLI objective", ("task:first",), APLState.READY,
            (JobProposal("first", "synthetic work", (), ("task:first",), ("criterion",)),),
            (), 0, 0))
        cli_args = ["apl", "handoff", "--path", str(project_root),
                    "--cycle-id", cli_cycle.cycle_id, "--planner", "deterministic",
                    "--project-id", "project-test", "--principal-id", "principal-test",
                    "--broker-port", str(broker.bound_port), "--server-name", "localhost",
                    "--ca", str(certificates["ca"]), "--client-cert", str(certificates["client_cert"]),
                    "--client-key", str(certificates["client_key"]),
                    "--server-fingerprint", certificates["fingerprint"]]
        assert main(cli_args) == 0
        cli_result = json.loads(capsys.readouterr().out.splitlines()[0])
        assert cli_result["status"] == "awaiting_authorization"
        assert apl_store.load(cli_cycle.cycle_id).state is APLState.WAITING_APPROVAL

        # Restart the Broker and APL service objects; resume queries the same binding.
        snapshot_id = result["snapshot_id"]
    finally:
        broker.close()
    restarted = _service(certificates, broker_path, project_ids=("project-test",))
    restarted.start()
    try:
        store_after_restart = SQLiteCycleStore(state_path, initialize=False)
        resumed = BrokerHandoffService(store_after_restart, state_path,
                                       _service_client(certificates, restarted),
                                       project_id="project-test", principal_id="principal-test")
        result_again = resumed.resume("cycle-handoff", proposed.proposal_id)
        assert result_again["status"] == "awaiting_authorization"
        assert result_again["snapshot_id"] == snapshot_id
        assert restarted.authority_available is False
        assert parse_canonical_json(_service_client(certificates, restarted).request(
            _service_status_request("handoff-status-proof")).body_json)["dispatch_allowed"] is False
    finally:
        restarted.close()
    with BrokerStore(broker_path) as broker_store:
        pending = broker_store.get_proposal_lifecycle_for_identity(
            snapshot_id, principal_id="principal-test", authorized_project_ids=("project-test",))
        assert pending is not None and pending.state == "pending"
        broker_store.verify_integrity()
    with closing(sqlite3.connect(state_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM apl_broker_handoffs").fetchone()[0] == 2
    with closing(sqlite3.connect(broker_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM broker_snapshots").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 0
    from datetime import datetime, timedelta, timezone
    with BrokerStore(broker_path) as broker_store:
        broker_store.expire_pending_proposals(
            as_of=datetime.now(timezone.utc) + timedelta(days=8))
    expired_service = _service(certificates, broker_path, project_ids=("project-test",))
    expired_service.start()
    try:
        terminal_client = _service_client(certificates, expired_service)
        terminal_handoff = BrokerHandoffService(
            SQLiteCycleStore(state_path, initialize=False), state_path, terminal_client,
            project_id="project-test", principal_id="principal-test")
        expired = terminal_handoff.status("cycle-handoff", proposed.proposal_id)
        assert expired["status"] == "expired"
        assert expired["dispatch_allowed"] is False
        assert terminal_handoff.store.load("cycle-handoff").state is APLState.BLOCKED
    finally:
        expired_service.close()


def test_apl_handoff_cancel_uses_real_loopback_mtls_and_persists_terminal_state(certificates):
    from agentflow.apl import (APLState, BrokerHandoffService, CycleSnapshot,
                               DeterministicPlannerAdapter, JobProposal,
                               SQLiteCycleStore, SupervisedCycleService)
    from agentflow.state.execution import ExecutionStore

    root = certificates["folder"]
    project_root = root / "cancel-repository"
    state_dir = project_root / ".agentflow"
    state_dir.mkdir(parents=True)
    state_path = state_dir / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    broker_path = root / "cancel-broker.sqlite3"
    broker = _service(certificates, broker_path, project_ids=("project-test",))
    broker.start()
    try:
        apl_store = SQLiteCycleStore(state_path)
        cycle = apl_store.create(CycleSnapshot(
            "cycle-cancel", "synthetic objective", ("task:first",), APLState.READY,
            (JobProposal("first", "synthetic operation", (), ("task:first",), ("ok",)),),
            (), 0, 0))
        planner = SupervisedCycleService(apl_store, state_path)
        proposal, status, _ = planner.replan(
            cycle.cycle_id, DeterministicPlannerAdapter(rationale="persisted test proposal"),
            "cancel-replan")
        assert status == "proposed"
        handoff = BrokerHandoffService(
            apl_store, state_path, _service_client(certificates, broker),
            project_id="project-test", principal_id="principal-test")
        submitted = handoff.submit(cycle.cycle_id, proposal.proposal_id)
        assert submitted["status"] == "awaiting_authorization"
        cancelled = handoff.cancel(cycle.cycle_id, proposal.proposal_id)
        assert cancelled["status"] == "cancelled"
        assert cancelled["authority_status"] == "unavailable"
        assert cancelled["dispatch_allowed"] is False
        assert apl_store.load(cycle.cycle_id).state is APLState.BLOCKED
        lifecycle = broker._store.get_proposal_lifecycle_for_identity(
            submitted["snapshot_id"], principal_id="principal-test",
            authorized_project_ids=("project-test",))
        assert lifecycle.state == "cancelled"
        assert broker._store.db.execute(
            "SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        assert broker._store.db.execute(
            "SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
    finally:
        broker.close()
