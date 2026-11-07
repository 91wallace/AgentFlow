import json
import hashlib
import multiprocessing
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import agentflow
from agentflow.approval.persistence import BrokerStore
from agentflow.approval.protocol import OperationIntent, build_contract, canonical_json
from agentflow.recovery import (
    RecoveryAuditError, build_recovery_export, write_recovery_export,
)
from agentflow.teg import (
    GatewayHandlerRegistry, GatewayIdentity, GatewayIntegrityError, GatewayStore,
    GatewayStoreError, TrustedExecutionGateway,
)
from agentflow.teg.protocol import (
    AuthorizationQuery, ConsumptionReservationRequest, ExecutionBinding,
    GatewayExecutionReceipt, build_message,
)
from test_approval_identity import _stamp
from test_teg import (
    SimulatedAuthority, SimulatedHandler, _broker_submission, _confirm_for_test,
    _gateway, _prepare_reserved_for_test, gateway_preparation_for_test,
)


def _race_test_authority(db_path, action, operation_id, request_digest, idempotency_key,
                        ready, start, results):
    """Multiprocess-only authority fixture; it is never imported by runtime code."""
    database = sqlite3.connect(db_path, timeout=20, isolation_level=None)
    database.execute("PRAGMA busy_timeout=20000")
    ready.put("ready")
    if not start.wait(20):
        results.put("barrier_timeout")
        database.close()
        return
    try:
        database.execute("BEGIN IMMEDIATE")
        if action == "reserve":
            prior = database.execute(
                "SELECT request_digest,reservation_digest FROM test_reservations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if prior is None:
                reservation_digest = "sha256:" + hashlib.sha256(
                    (operation_id + request_digest).encode()
                ).hexdigest()
                database.execute("INSERT INTO test_reservations VALUES(?,?,?)",
                                 (operation_id, request_digest, reservation_digest))
                result = "reserved"
            elif prior[0] == request_digest:
                result = "reserved_idempotently"
            else:
                result = "divergent_request_rejected"
        elif action == "confirm":
            reservation = database.execute(
                "SELECT request_digest FROM test_reservations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            prior = database.execute(
                "SELECT idempotency_key FROM test_confirmations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if reservation is None or reservation[0] != request_digest:
                result = "reservation_binding_rejected"
            elif prior is None:
                database.execute("INSERT INTO test_confirmations VALUES(?,?,?)",
                                 (operation_id, idempotency_key, "consumed"))
                result = "confirmed"
            elif prior[0] == idempotency_key:
                result = "confirmed_idempotently"
            else:
                result = "already_consumed"
        else:
            raise AssertionError("unknown test authority action")
        database.commit()
        results.put(result)
    except Exception as exc:  # reported to parent; never hidden in a child process
        database.rollback()
        results.put("error:" + type(exc).__name__)
    finally:
        database.close()


def _run_authority_race(tmp_path, action, keys, *, operation_id="same-operation",
                        request_digest="request-digest"):
    authority_path = tmp_path / "race-authority.sqlite"
    with sqlite3.connect(authority_path) as database:
        database.execute(
            "CREATE TABLE IF NOT EXISTS test_reservations("
            "operation_id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,reservation_digest TEXT NOT NULL UNIQUE)"
        )
        database.execute(
            "CREATE TABLE IF NOT EXISTS test_confirmations("
            "operation_id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL,status TEXT NOT NULL)"
        )
    ctx = multiprocessing.get_context("spawn")
    ready, start, results = ctx.Queue(), ctx.Event(), ctx.Queue()
    processes = [ctx.Process(
        target=_race_test_authority,
        args=(str(authority_path), action, operation_id,
              request_digest, key, ready, start, results),
    ) for key in keys]
    for process in processes:
        process.start()
    try:
        for _ in processes:
            assert ready.get(timeout=25) == "ready"
        start.set()
        for process in processes:
            process.join(timeout=25)
            assert not process.is_alive(), "authority race child did not finish"
            assert process.exitcode == 0
        return [results.get(timeout=5) for _ in processes]
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def _independent_store_worker(kind, source, destination, identity_json, ready, start, results):
    identity = GatewayIdentity(**json.loads(identity_json)) if identity_json else None
    store_type = BrokerStore if kind == "broker" else GatewayStore
    store = store_type(source) if kind == "broker" else store_type(source, identity)
    try:
        store.verify_integrity()
        ready.put(kind)
        if not start.wait(20):
            results.put("barrier_timeout")
            return
        store.backup_to(destination)
        results.put(kind + ":backup_ok")
    except Exception as exc:
        results.put(kind + ":error:" + type(exc).__name__)
    finally:
        store.close()


def test_gateway_backup_restore_is_consistent_create_only_and_keeps_unknown(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "rb08-unknown-backup")
    store, identity, gateway = _gateway(
        tmp_path, submission, authority=SimulatedAuthority(query_status="unknown"),
        handler=SimulatedHandler(),
    )
    try:
        assert gateway.submit(submission) == "unknown"
        assert gateway.handlers.select(submission.intent).calls == 0
        backup = store.backup_to(tmp_path / "backups" / "teg.bak")
        with pytest.raises(GatewayStoreError, match="new and distinct"):
            store.backup_to(backup)
    finally:
        store.close()

    restored_path = GatewayStore.restore_backup(backup, tmp_path / "restore" / "teg.sqlite")
    with GatewayStore(restored_path, identity) as restored:
        assert restored.state(submission.operation_id) == "unknown"
        assert restored.verify_integrity() is None
        restored_gateway = TrustedExecutionGateway(
            restored, identity, authority=SimulatedAuthority(query_status="approved"),
            handlers=None,
        )
        assert restored_gateway.submit(submission) == "unknown"
        assert restored.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        with pytest.raises(GatewayStoreError, match="new and distinct"):
            GatewayStore.restore_backup(backup, restored_path)


def test_gateway_restore_rejects_corrupt_or_divergent_schema_without_target(tmp_path):
    identity = GatewayIdentity.create("gateway.backup", "1.0", _stamp(-1))
    source = GatewayStore(tmp_path / "source.sqlite", identity)
    backup = source.backup_to(tmp_path / "backup.sqlite")
    source.close()

    bad_version = tmp_path / "bad-version.sqlite"
    with sqlite3.connect(backup) as database:
        database.execute("PRAGMA user_version=999")
    with pytest.raises(GatewayIntegrityError, match="schema version mismatch"):
        GatewayStore.restore_backup(backup, bad_version)
    assert not bad_version.exists()

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    target = tmp_path / "corrupt-restore.sqlite"
    with pytest.raises(GatewayIntegrityError, match="backup"):
        GatewayStore.restore_backup(corrupt, target)
    assert not target.exists()


def test_gateway_process_lock_survives_concurrent_process_attempt(tmp_path):
    db_path = tmp_path / "gateway.sqlite"
    identity = GatewayIdentity.create("gateway.locked", "1.0", _stamp(-1))
    with GatewayStore(db_path, identity):
        child = (
            "import json,sys; sys.path.insert(0,sys.argv[1]); "
            "from agentflow.teg import GatewayIdentity,GatewayStore,GatewayStoreLocked; "
            "identity=GatewayIdentity(**json.loads(sys.argv[3])); "
            "\ntry: GatewayStore(sys.argv[2],identity)\n"
            "except GatewayStoreLocked: print('LOCKED')\n"
            "else: print('UNEXPECTEDLY_ACQUIRED')"
        )
        package_root = str(Path(agentflow.__file__).resolve().parent.parent)
        commands = [subprocess.Popen(
            [sys.executable, "-c", child, package_root, str(db_path),
             json.dumps(identity.__dict__)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        ) for _ in range(4)]
        outputs = [process.communicate(timeout=20) for process in commands]
    assert all(process.returncode == 0 for process in commands), outputs
    assert [stdout.strip() for stdout, _ in outputs] == ["LOCKED"] * 4


def test_multiprocess_reservation_confirmation_and_consumption_are_single_use(tmp_path):
    reservation_results = _run_authority_race(
        tmp_path, "reserve", ["reserve-key"] * 6, operation_id="reservation-race"
    )
    assert reservation_results.count("reserved") == 1
    assert reservation_results.count("reserved_idempotently") == 5
    with sqlite3.connect(tmp_path / "race-authority.sqlite") as database:
        assert database.execute("SELECT count(*) FROM test_reservations").fetchone()[0] == 1

    confirmation_results = _run_authority_race(
        tmp_path, "confirm", ["same-confirm-key"] * 6, operation_id="reservation-race"
    )
    assert confirmation_results.count("confirmed") == 1
    assert confirmation_results.count("confirmed_idempotently") == 5
    with sqlite3.connect(tmp_path / "race-authority.sqlite") as database:
        assert database.execute("SELECT count(*) FROM test_confirmations").fetchone()[0] == 1

    _run_authority_race(
        tmp_path, "reserve", ["reserve-key"] * 6, operation_id="consume-race"
    )
    consumption_results = _run_authority_race(
        tmp_path, "confirm", [f"distinct-confirm-{i}" for i in range(6)],
        operation_id="consume-race",
    )
    assert consumption_results.count("confirmed") == 1
    assert consumption_results.count("already_consumed") == 5
    with sqlite3.connect(tmp_path / "race-authority.sqlite") as database:
        assert database.execute(
            "SELECT count(*) FROM test_confirmations WHERE operation_id='consume-race'"
        ).fetchone()[0] == 1


def test_broker_and_gateway_stores_operate_concurrently_and_back_up_independently(tmp_path):
    broker_path = tmp_path / "concurrent-broker.sqlite"
    gateway_path = tmp_path / "concurrent-gateway.sqlite"
    identity = GatewayIdentity.create("gateway.parallel-stores", "1.0", _stamp(-1))
    with BrokerStore(broker_path):
        pass
    with GatewayStore(gateway_path, identity):
        pass

    ctx = multiprocessing.get_context("spawn")
    ready, start, results = ctx.Queue(), ctx.Event(), ctx.Queue()
    jobs = (
        ("broker", str(broker_path), str(tmp_path / "broker-backup.sqlite"), None),
        ("gateway", str(gateway_path), str(tmp_path / "gateway-backup.sqlite"),
         json.dumps(identity.__dict__)),
    )
    processes = [ctx.Process(target=_independent_store_worker,
                             args=(*job, ready, start, results)) for job in jobs]
    for process in processes:
        process.start()
    try:
        assert {ready.get(timeout=25) for _ in processes} == {"broker", "gateway"}
        start.set()
        for process in processes:
            process.join(timeout=25)
            assert not process.is_alive(), "independent store child did not finish"
            assert process.exitcode == 0
        assert {results.get(timeout=5) for _ in processes} == {
            "broker:backup_ok", "gateway:backup_ok",
        }
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    BrokerStore._verify_path(tmp_path / "broker-backup.sqlite")
    GatewayStore._verify_path(tmp_path / "gateway-backup.sqlite")


def test_process_crash_during_execution_recovers_unknown_after_restart(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "rb09-process-crash")
    identity = GatewayIdentity.create("gateway.process.crash", "1.0", _stamp(-1))
    gateway_path = tmp_path / "gateway.sqlite"
    authority = SimulatedAuthority()
    store = GatewayStore(gateway_path, identity)
    handler = SimulatedHandler()
    binding, reservation = _prepare_reserved_for_test(store, submission, identity, authority, handler)
    confirmation = _confirm_for_test(store, submission, binding, reservation, authority)
    store.record_authorization(submission.operation_id, confirmation)
    store.close()

    package_root = str(Path(agentflow.__file__).resolve().parent.parent)
    crash_process = (
        "import json,os,sys; sys.path.insert(0,sys.argv[1]); "
        "from agentflow.teg import GatewayIdentity,GatewayStore; "
        "identity=GatewayIdentity(**json.loads(sys.argv[3])); "
        "store=GatewayStore(sys.argv[2],identity); store.mark_executing(sys.argv[4]); os._exit(0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", crash_process, package_root, str(gateway_path),
         json.dumps(identity.__dict__), submission.operation_id],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr

    with GatewayStore(gateway_path, identity) as recovered:
        assert recovered.state(submission.operation_id) == "unknown"
        assert recovered.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        assert recovered.verify_integrity() is None
        assert handler.calls == 0


def test_reservation_committed_before_lost_reply_is_reconciled_without_effect(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "rb08-reservation-response-loss")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    authority = SimulatedAuthority()
    store = GatewayStore(path, identity)
    store.receive(submission)
    store.mark_validated(submission.operation_id)
    store.await_authorization(submission.operation_id)
    binding = ExecutionBinding.from_submission(submission, identity)
    suffix = binding.binding_digest.removeprefix("sha256:")[:32]
    query = build_message(AuthorizationQuery, protocol_version=1, request_id="query." + suffix,
                          binding=binding, expires_at=submission.intent.expires_at)
    store.record_protocol_message(submission.operation_id, "authorization_query", query)
    result = authority.query_authorization(query)
    store.record_protocol_message(submission.operation_id, "authorization_query_result", result)
    request = build_message(
        ConsumptionReservationRequest, protocol_version=1, request_id="reserve." + suffix,
        binding=binding, query_result_digest=result.result_digest,
        expires_at=min(result.expires_at, submission.intent.expires_at),
    )
    store.record_protocol_message(submission.operation_id, "reservation_request", request)
    committed_reservation = authority.reserve_consumption(request)
    assert committed_reservation.status == "reserved"
    # Simulate process loss after authority commit but before the response is journaled locally.
    store.close()

    reopened = GatewayStore(path, identity)
    handler = SimulatedHandler()
    gateway = TrustedExecutionGateway(
        reopened, identity, GatewayHandlerRegistry((handler,)), authority,
    )
    try:
        assert reopened.state(submission.operation_id) == "unknown"
        assert gateway.reconcile(submission.operation_id) == "reserved"
        assert reopened.state(submission.operation_id) == "unknown"
        assert gateway.submit(submission) == "unknown"
        assert handler.calls == 0
        assert reopened.get_reservation(submission.operation_id) is None
        assert reopened.verify_integrity() is None
    finally:
        reopened.close()


def test_effect_completed_before_receipt_crash_stays_unknown_and_is_not_repeated(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "rb08-effect-before-receipt")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    authority = SimulatedAuthority()
    store = GatewayStore(path, identity)
    handler = SimulatedHandler()
    binding, reservation = _prepare_reserved_for_test(store, submission, identity, authority, handler)
    confirmation = _confirm_for_test(store, submission, binding, reservation, authority)
    store.record_authorization(submission.operation_id, confirmation)
    store.mark_executing(submission.operation_id)
    preparation = gateway_preparation_for_test(submission, handler)
    effect = handler.execute(submission, preparation)
    assert effect.status == "succeeded" and handler.calls == 1
    # Crash after the simulated effect but before persisting its receipt.
    store.close()

    reopened = GatewayStore(path, identity)
    recovered = TrustedExecutionGateway(
        reopened, identity, GatewayHandlerRegistry((handler,)), authority,
    )
    try:
        assert reopened.state(submission.operation_id) == "unknown"
        assert recovered.submit(submission) == "unknown"
        assert handler.calls == 1
        assert reopened.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        assert reopened.verify_integrity() is None
    finally:
        reopened.close()


def test_durable_receipt_is_recovered_without_reexecuting_effect(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "rb08-durable-receipt")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    authority = SimulatedAuthority()
    store = GatewayStore(path, identity)
    handler = SimulatedHandler()
    binding, reservation = _prepare_reserved_for_test(store, submission, identity, authority, handler)
    confirmation = _confirm_for_test(store, submission, binding, reservation, authority)
    store.record_authorization(submission.operation_id, confirmation)
    store.mark_executing(submission.operation_id)
    effect = handler.execute(submission, gateway_preparation_for_test(submission, handler))
    receipt = build_message(
        GatewayExecutionReceipt, protocol_version=1,
        receipt_id="receipt.recovered." + submission.submission_digest.removeprefix("sha256:")[:24],
        binding=binding, reservation_digest=reservation.reservation_digest,
        confirmation_digest=confirmation.confirmation_digest, status=effect.status,
        started_at=_stamp(-1), completed_at=_stamp(),
        effect_idempotency=handler.descriptor.provider_idempotency,
        effect_key=effect.effect_key, result_digest=effect.result_digest,
    )
    store.record_protocol_message(submission.operation_id, "execution_receipt", receipt)
    assert handler.calls == 1
    store.close()  # crash after durable receipt journal, before state projection

    reopened = GatewayStore(path, identity)
    recovered = TrustedExecutionGateway(
        reopened, identity, GatewayHandlerRegistry((handler,)), authority,
    )
    try:
        assert reopened.state(submission.operation_id) == "succeeded"
        assert reopened.db.execute("SELECT receipt_digest FROM gateway_receipts").fetchone()[0] == receipt.receipt_digest
        assert recovered.submit(submission) == "succeeded"
        assert handler.calls == 1
        assert reopened.verify_integrity() is None
    finally:
        reopened.close()


def test_recovery_export_is_read_only_redacted_and_never_claims_validation(tmp_path):
    sentinel = "provider-secret-export-sentinel"
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(
            broker, "rb08-audit-export", provider_payload={"private": sentinel}
        )
        gateway_store, _, _ = _gateway(tmp_path, submission)
        try:
            gateway_store.receive(submission)
            broker_changes = broker.db.total_changes
            gateway_changes = gateway_store.db.total_changes
            report = json.loads(build_recovery_export(broker, gateway_store, submission.operation_id))
            assert report["classification"] == "incomplete"
            assert report["references"]["operation_intent"]["digest"] == submission.intent_digest
            assert all(item["status"] == "verified"
                       for item in report["references"]["accepted_snapshots"].values())
            assert report["execution_effect_validation"] == "not_proven_by_this_report"
            assert report["references"]["gateway_execution_receipt"]["status"] == "absent"
            encoded = json.dumps(report)
            assert sentinel not in encoded
            assert "intent_json" not in encoded and "payload_json" not in encoded
            assert broker.db.total_changes == broker_changes
            assert gateway_store.db.total_changes == gateway_changes

            path = write_recovery_export(
                broker, gateway_store, submission.operation_id, tmp_path / "exports" / "audit.json"
            )
            assert json.loads(path.read_text(encoding="utf-8"))["classification"] == "incomplete"
            with pytest.raises(RecoveryAuditError, match="exists"):
                write_recovery_export(broker, gateway_store, submission.operation_id, path)
        finally:
            gateway_store.close()


def test_recovery_export_detects_broker_gateway_intent_divergence(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "rb08-divergent")
        gateway_store, _, _ = _gateway(tmp_path, submission)
        try:
            changed_fields = submission.intent.to_dict()
            changed_fields.pop("intent_digest")
            changed_fields["budget_json"] = canonical_json({"cost_estimate": "different"})
            divergent_intent = build_contract(OperationIntent, **changed_fields)
            divergent_submission = type(submission).create(
                submission.request_id, divergent_intent, submission.snapshots
            )
            gateway_store.receive(divergent_submission)
            report = json.loads(build_recovery_export(broker, gateway_store, submission.operation_id))
            assert report["classification"] == "divergent"
            assert "intent_digest_or_content" in report["mismatches"]
        finally:
            gateway_store.close()
