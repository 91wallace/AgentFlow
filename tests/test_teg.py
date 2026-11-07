from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
import threading

import pytest

from agentflow.approval.persistence import BrokerStore
from agentflow.approval.protocol import AcceptedSnapshot, canonical_json, digest_json
from agentflow.teg import (
    AuthorizationConsumption, GatewayAuthorityStatus, GatewayConflict, GatewayContractError,
    GatewayEffectResult, GatewayHandlerDescriptor, GatewayHandlerRegistry,
    GatewayIdentity, GatewayStore, GatewayStoreError, GatewaySubmission,
    GatewayStoreLocked, TrustedExecutionGateway, UnavailableGatewayAuthority, build_contract,
)
from agentflow.teg.protocol import (
    AuthorizationQuery, AuthorizationQueryResult, AuthorizationState,
    ConsumptionConfirmation, ConsumptionConfirmationRequest, ConsumptionReservation,
    ConsumptionReservationRequest, ExecutionBinding,
    ReconciliationResult, build_message,
)
from agentflow.teg.store import SCHEMA_VERSION
from test_approval_identity import _intent, _stamp


CAPABILITIES = tuple(sorted(GatewayAuthorityStatus.REQUIRED_CAPABILITIES))


def _broker_submission(broker, operation_id="teg-op", *, intent=None, provider_payload=None):
    if intent is None:
        budget = {"status": "estimated", "currency": "USD", "cost_estimate": "0.20",
                  "token_limit": 100, "time_limit_seconds": 30, "constraints": ["estimated_cost"]}
        intent = _intent(broker, operation_id, include_approval_context=True,
                         provider_payload=provider_payload,
                         budget_json=canonical_json(budget))
    snapshots = []
    for row in broker.db.execute(
        "SELECT b.snapshot_id,b.kind,b.project_id,b.revision_id,b.schema_version,b.content_json,"
        "b.canonical_digest,b.provenance_json,b.parent_digest,s.actor_id,s.changed_at "
        "FROM broker_snapshots b JOIN snapshot_state s USING(snapshot_id) "
        "WHERE s.status='accepted' AND b.project_id=? AND b.snapshot_id LIKE ? ORDER BY b.kind",
        (intent.project_id, f"snapshot-{intent.operation_id}-%")
    ):
        snapshots.append(AcceptedSnapshot(
            1, row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[9], row[10], row[8]
        ))
    order = {"specification": 0, "plan": 1, "checkpoint": 2, "payload": 3}
    snapshots.sort(key=lambda item: order[item.kind])
    return GatewaySubmission.create(f"request-{operation_id}", intent, tuple(snapshots))


def _descriptor():
    return build_contract(
        GatewayHandlerDescriptor, protocol_version=1, handler_id="test-provider-handler",
        handler_version="1", action_codes=("provider_call",), target_ids=("trusted-provider-gateway",),
        provider_ids=("provider-ci",), model_ids=("model-ci",),
        allowed_scopes=("execution:propose",), required_scopes=("execution:propose",),
        allowed_budget_statuses=("estimated",),
        required_budget_fields=("cost_estimate", "currency", "status", "time_limit_seconds", "token_limit"),
        currency="USD", max_cost_estimate="1.00", max_tokens=500, max_time_seconds=60,
        allowed_parameter_keys=("mode",), required_parameter_keys=("mode",),
        max_payload_bytes=64 * 1024, provider_idempotency="unavailable",
    )


class SimulatedHandler:
    """Test fixture. No production handler is implemented or registered."""

    descriptor = _descriptor()

    def __init__(self, result="succeeded"):
        self.result = result
        self.calls = 0

    def execute(self, submission, preparation):
        self.calls += 1
        assert preparation.operation_id == submission.operation_id
        if self.result == "raise":
            raise RuntimeError("simulated ambiguous test effect")
        status = self.result
        digest = digest_json({"operation_id": submission.operation_id, "test_result": status})
        return GatewayEffectResult(1, submission.operation_id, status,
                                   "test_result_" + status, "effect.test." + submission.operation_id,
                                   digest if status != "unknown" else None)


class SimulatedAuthority:
    """Test-only stateful authority; never imported or selectable by runtime code."""

    def __init__(self, query_status="approved", *, reservation_status="reserved",
                 confirmation_status="confirmed", wrong_binding=None,
                 fail_reserve_before=False, lose_reserve_response=False,
                 lose_confirmation_response=False):
        self.query_status = query_status
        self.reservation_status = reservation_status
        self.confirmation_status = confirmation_status
        self.wrong_binding = wrong_binding
        self.fail_reserve_before = fail_reserve_before
        self.lose_reserve_response = lose_reserve_response
        self.lose_confirmation_response = lose_confirmation_response
        self.query_calls = self.reserve_calls = self.confirm_calls = 0
        self._reservations = {}
        self._confirmations = {}
        self._confirmation_keys = {}
        self._lock = threading.Lock()

    def status(self):
        return GatewayAuthorityStatus(1, True, "test_authority_available", CAPABILITIES)

    def query_authorization(self, query):
        self.query_calls += 1
        binding = query.binding.binding_digest
        if self.wrong_binding == "query":
            binding = digest_json({"wrong": "binding"})
        return build_message(AuthorizationQueryResult, protocol_version=1,
            request_id=query.request_id, binding_digest=binding, status=self.query_status,
            authorization_digest=(digest_json({"test_authority": query.binding.operation_id})
                                  if self.query_status == "approved" else None),
            expires_at=_stamp(120))

    def reserve_consumption(self, request):
        self.reserve_calls += 1
        with self._lock:
            prior = self._reservations.get(request.binding.operation_id)
            if prior:
                if prior.request_digest != request.request_digest:
                    raise ValueError("divergent reservation request")
                return prior
            if self.fail_reserve_before:
                raise TimeoutError("reservation failed before commit")
            binding = request.binding.binding_digest
            if self.wrong_binding == "reservation":
                binding = digest_json({"wrong": "binding"})
            reservation = build_message(ConsumptionReservation, protocol_version=1,
                reservation_id="reservation." + request.binding.operation_id,
                request_id=request.request_id, binding_digest=binding,
                request_digest=request.request_digest, status=self.reservation_status,
                expires_at=request.expires_at)
            if reservation.status == "reserved":
                self._reservations[request.binding.operation_id] = reservation
            if self.lose_reserve_response:
                raise TimeoutError("reservation committed; response lost")
            return reservation

    def confirm_consumption(self, request):
        self.confirm_calls += 1
        with self._lock:
            prior = self._confirmations.get(request.binding.operation_id)
            if prior:
                if self._confirmation_keys[request.binding.operation_id] == request.idempotency_key:
                    return prior
                return build_message(ConsumptionConfirmation, protocol_version=1,
                    confirmation_id="confirmation.duplicate." + request.binding.operation_id,
                    request_id=request.request_id, reservation_digest=request.reservation.reservation_digest,
                    binding_digest=request.binding.binding_digest, status="already_consumed",
                    consumed_at=None, expires_at=request.reservation.expires_at)
            status = self.confirmation_status
            binding = request.binding.binding_digest
            if self.wrong_binding == "confirmation":
                binding = digest_json({"wrong": "binding"})
            confirmation = build_message(ConsumptionConfirmation, protocol_version=1,
                confirmation_id="confirmation." + request.binding.operation_id,
                request_id=request.request_id, reservation_digest=request.reservation.reservation_digest,
                binding_digest=binding, status=status,
                consumed_at=_stamp() if status == "confirmed" else None,
                expires_at=request.reservation.expires_at)
            if status == "confirmed":
                self._confirmations[request.binding.operation_id] = confirmation
                self._confirmation_keys[request.binding.operation_id] = request.idempotency_key
            if self.lose_confirmation_response:
                raise TimeoutError("consumption committed; response lost")
            return confirmation

    def query_state(self, query):
        with self._lock:
            reservation = self._reservations.get(query.binding.operation_id)
            confirmation = self._confirmations.get(query.binding.operation_id)
        status = "consumed" if confirmation else "reserved" if reservation else "not_found"
        binding = query.binding.binding_digest
        if self.wrong_binding == "state":
            binding = digest_json({"wrong": "binding"})
        return build_message(AuthorizationState, protocol_version=1, request_id=query.request_id,
            binding_digest=binding, status=status,
            reservation_digest=reservation.reservation_digest if reservation else None,
            confirmation_digest=confirmation.confirmation_digest if confirmation else None,
            expires_at=reservation.expires_at if reservation else None)

    def reconcile(self, request):
        with self._lock:
            reservation = self._reservations.get(request.binding.operation_id)
            confirmation = self._confirmations.get(request.binding.operation_id)
        status = "consumed" if confirmation else "reserved" if reservation else "not_found"
        binding = request.binding.binding_digest
        if self.wrong_binding == "reconciliation":
            binding = digest_json({"wrong": "binding"})
        return build_message(ReconciliationResult, protocol_version=1, request_id=request.request_id,
            binding_digest=binding, status=status,
            reservation_digest=reservation.reservation_digest if reservation else None,
            confirmation_digest=confirmation.confirmation_digest if confirmation else None,
            observed_at=_stamp())


def _gateway(tmp_path, submission, *, authority=None, handler=None):
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    store = GatewayStore(tmp_path / "gateway.sqlite", identity)
    registry = GatewayHandlerRegistry((handler,)) if handler else GatewayHandlerRegistry()
    return store, identity, TrustedExecutionGateway(store, identity, registry, authority)


def _reserve_for_test(store, submission, identity, authority):
    store.receive(submission)
    store.mark_validated(submission.operation_id)
    store.await_authorization(submission.operation_id)
    binding = ExecutionBinding.from_submission(submission, identity)
    suffix = binding.binding_digest.removeprefix("sha256:")[:24]
    query = build_message(AuthorizationQuery, protocol_version=1, request_id="manual.query." + suffix,
        binding=binding, expires_at=submission.intent.expires_at)
    store.record_protocol_message(submission.operation_id, "authorization_query", query)
    query_result = authority.query_authorization(query)
    store.record_protocol_message(submission.operation_id, "authorization_query_result", query_result)
    reserve_request = build_message(ConsumptionReservationRequest, protocol_version=1,
        request_id="manual.reserve." + suffix, binding=binding,
        query_result_digest=query_result.result_digest,
        expires_at=min(query_result.expires_at, submission.intent.expires_at))
    store.record_protocol_message(submission.operation_id, "reservation_request", reserve_request)
    reservation = authority.reserve_consumption(reserve_request)
    store.record_protocol_message(submission.operation_id, "reservation", reservation)
    store.record_reservation(submission.operation_id, reservation)
    return binding, reservation


def _prepare_reserved_for_test(store, submission, identity, authority, handler):
    binding, reservation = _reserve_for_test(store, submission, identity, authority)
    preparation = gateway_preparation_for_test(submission, handler)
    store.record_preparation(preparation)
    return binding, reservation


def _confirmation_request_for_test(binding, reservation, suffix):
    idempotency_key = digest_json({"operation_id": binding.operation_id,
        "gateway_id": binding.gateway_id, "reservation_digest": reservation.reservation_digest,
        "binding_digest": binding.binding_digest})
    return build_message(ConsumptionConfirmationRequest, protocol_version=1,
        request_id="manual.confirm." + suffix, reservation=reservation,
        binding=binding, idempotency_key=idempotency_key)


def _confirm_for_test(store, submission, binding, reservation, authority, *, persist_intent=True):
    request = _confirmation_request_for_test(
        binding, reservation, binding.binding_digest.removeprefix("sha256:")[:24])
    store.record_protocol_message(submission.operation_id, "confirmation_request", request)
    if persist_intent:
        store.mark_authority_call_started(submission.operation_id)
    response = authority.confirm_consumption(request)
    store.record_protocol_message(submission.operation_id, "confirmation", response)
    return response


def test_valid_allowlisted_operation_consumes_authorization_and_executes_only_test_handler(tmp_path):
    handler = SimulatedHandler()
    authority = SimulatedAuthority()
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker)
        store, identity, gateway = _gateway(tmp_path, submission, authority=authority, handler=handler)
        try:
            state = gateway.submit(submission)
            assert state == "succeeded"
            assert store.state(submission.operation_id) == "succeeded"
            assert (authority.query_calls, authority.reserve_calls, authority.confirm_calls,
                    handler.calls) == (1, 1, 1, 1)
            events = [event["event_type"] for event in store.events(submission.operation_id)]
            assert "gateway.authorization.reserved" in events
            assert "gateway.authorization.confirm_intent" in events
            assert "gateway.authorization.consumption_confirmed" in events
            assert "gateway.execution.prepared" in events
            receipt = json.loads(store.db.execute("SELECT receipt_json FROM gateway_receipts").fetchone()[0])
            assert receipt["effect_idempotency"] == "unavailable"
            confirmation_request = ConsumptionConfirmationRequest.from_json(
                store.protocol_records(submission.operation_id, "confirmation_request")[0]["record_json"])
            repeated_confirmation = authority.confirm_consumption(confirmation_request)
            assert repeated_confirmation.confirmation_digest == receipt["confirmation_digest"]
            assert handler.calls == 1
            store.verify_integrity()
        finally:
            store.close()


def test_default_authority_is_unavailable_and_never_runs_a_handler(tmp_path):
    handler = SimulatedHandler()
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "no-authority")
        store, _, gateway = _gateway(tmp_path, submission, handler=handler)
        try:
            assert isinstance(gateway.authority, UnavailableGatewayAuthority)
            assert gateway.submit(submission) == "awaiting_authorization"
            assert handler.calls == 0
            assert store.db.execute("SELECT count(*) FROM gateway_authorizations").fetchone()[0] == 0
            assert store.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        finally:
            store.close()


@pytest.mark.parametrize("auth_status", ["expired", "revoked", "rejected", "pending"])
def test_non_consumed_authorization_is_terminal_failure_and_no_effect(tmp_path, auth_status):
    handler, authority = SimulatedHandler(), SimulatedAuthority(query_status=auth_status)
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "auth-" + auth_status)
        store, _, gateway = _gateway(tmp_path, submission, authority=authority, handler=handler)
        try:
            assert gateway.submit(submission) == "failed"
            assert handler.calls == 0
            assert store.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        finally:
            store.close()


@pytest.mark.parametrize("status", ["expired", "revoked", "rejected"])
def test_reservation_confirmation_denial_never_executes(status, tmp_path):
    handler = SimulatedHandler()
    authority = SimulatedAuthority(confirmation_status=status)
    with BrokerStore(tmp_path / f"broker-confirm-{status}.sqlite") as broker:
        submission = _broker_submission(broker, f"confirm-{status}")
        store, _, gateway = _gateway(tmp_path / status, submission, authority=authority, handler=handler)
        try:
            assert gateway.submit(submission) == "failed"
            assert handler.calls == 0
            assert store.state(submission.operation_id) == "failed"
            assert store.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        finally:
            store.close()


def test_authority_response_loss_or_digest_mismatch_becomes_unknown_without_effect(tmp_path):
    cases = [("lost-reservation", SimulatedAuthority(lose_reserve_response=True)),
             ("lost-confirmation", SimulatedAuthority(lose_confirmation_response=True))]
    cases += [(binding, SimulatedAuthority(wrong_binding=binding))
              for binding in ("query", "reservation", "confirmation")]
    for label, authority in cases:
        handler = SimulatedHandler()
        with BrokerStore(tmp_path / f"broker-{label}.sqlite") as broker:
            submission = _broker_submission(broker, "unknown-" + label)
            store, _, gateway = _gateway(tmp_path / label, submission, authority=authority, handler=handler)
            try:
                assert gateway.submit(submission) == "unknown"
                assert handler.calls == 0
                assert store.state(submission.operation_id) == "unknown"
                assert gateway.submit(submission) == "unknown"
                assert handler.calls == 0
            finally:
                store.close()


@pytest.mark.parametrize(("authority", "expected_state"), [
    (SimulatedAuthority(fail_reserve_before=True), "not_found"),
    (SimulatedAuthority(lose_reserve_response=True), "reserved"),
    (SimulatedAuthority(confirmation_status="unknown"), "reserved"),
    (SimulatedAuthority(lose_confirmation_response=True), "consumed"),
])
def test_unknown_reconciliation_is_read_only_and_never_replays_effect(tmp_path, authority, expected_state):
    handler = SimulatedHandler()
    with BrokerStore(tmp_path / f"broker-{expected_state}-{id(authority)}.sqlite") as broker:
        submission = _broker_submission(broker, f"reconcile-{id(authority)}")
        store, _, gateway = _gateway(tmp_path / str(id(authority)), submission,
                                     authority=authority, handler=handler)
        try:
            assert gateway.submit(submission) == "unknown"
            assert handler.calls == 0
            reconciled = gateway.reconcile(submission.operation_id)
            assert reconciled == expected_state
            assert gateway.submit(submission) == "unknown"
            assert handler.calls == 0
            assert store.state(submission.operation_id) == "unknown"
            assert store.protocol_records(submission.operation_id, "reconciliation_result")
            store.verify_integrity()
        finally:
            store.close()


def test_unknown_handler_and_digest_divergent_snapshot_fail_closed(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "unlisted")
        store, _, gateway = _gateway(tmp_path / "unlisted", submission)
        try:
            assert gateway.submit(submission) == "failed"
            assert store.state(submission.operation_id) == "failed"
        finally:
            store.close()

        changed = list(submission.snapshots)
        changed[0] = replace(changed[0], revision_id="different-spec-revision")
        divergent = GatewaySubmission.create("request-divergent", submission.intent, tuple(changed))
        handler = SimulatedHandler()
        store2, _, gateway2 = _gateway(tmp_path / "divergent", divergent,
                                       authority=SimulatedAuthority(), handler=handler)
        try:
            assert gateway2.submit(divergent) == "failed"
            assert handler.calls == 0
        finally:
            store2.close()


def test_unknown_operations_mixed_fields_budget_and_scope_are_rejected(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        good = _broker_submission(broker, "policy-good")
        payload = json.loads(good.snapshots[-1].content_json)
        # The operation intent is immutable; a descriptor mismatch tests the explicit registry boundary.
        descriptor_fields = _descriptor().to_dict()
        descriptor_fields["target_ids"] = ("some-other-target",)
        handler = SimulatedHandler()
        handler.descriptor = build_contract(GatewayHandlerDescriptor, **descriptor_fields)
        store, _, gateway = _gateway(tmp_path, good, authority=SimulatedAuthority(), handler=handler)
        try:
            assert gateway.submit(good) == "failed"
            assert handler.calls == 0
        finally:
            store.close()
        assert "approval_context" in payload


@pytest.mark.parametrize("change", ["budget", "scope"])
def test_budget_over_limit_or_scope_mismatch_fails_before_authority(tmp_path, change):
    with BrokerStore(tmp_path / f"broker-{change}.sqlite") as broker:
        submission = _broker_submission(broker, f"limit-{change}")
        handler = SimulatedHandler()
        fields = handler.descriptor.to_dict()
        if change == "budget":
            fields["max_cost_estimate"] = "0.10"
        else:
            fields["allowed_scopes"] = ("other:scope",)
            fields["required_scopes"] = ("other:scope",)
        handler.descriptor = build_contract(GatewayHandlerDescriptor, **fields)
        authority = SimulatedAuthority()
        store, _, gateway = _gateway(tmp_path / change, submission, authority=authority,
                                     handler=handler)
        try:
            assert gateway.submit(submission) == "failed"
            assert authority.query_calls == authority.reserve_calls == authority.confirm_calls == handler.calls == 0
        finally:
            store.close()


def test_idempotency_replay_divergence_and_concurrent_submission(tmp_path):
    handler, authority = SimulatedHandler(), SimulatedAuthority()
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "concurrent-teg")
        store, _, gateway = _gateway(tmp_path, submission, authority=authority, handler=handler)
        second_registry = GatewayHandlerRegistry((handler,))
        second_gateway = TrustedExecutionGateway(store, gateway.identity, second_registry, authority)
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                gateways = (gateway, second_gateway)
                outcomes = list(pool.map(lambda i: gateways[i % 2].submit(submission), range(8)))
            assert set(outcomes) <= {"received", "validated", "awaiting_authorization", "authorized",
                                     "execution_prepared", "executing", "succeeded"}
            assert store.state(submission.operation_id) == "succeeded"
            assert (authority.query_calls, authority.reserve_calls, authority.confirm_calls,
                    handler.calls) == (1, 1, 1, 1)
            confirmation_request = json.loads(store.protocol_records(
                submission.operation_id, "confirmation_request")[0]["record_json"])
            assert confirmation_request["idempotency_key"] == digest_json({
                "operation_id": submission.operation_id, "gateway_id": gateway.identity.gateway_id,
                "reservation_digest": store.get_reservation(submission.operation_id).reservation_digest,
                "binding_digest": confirmation_request["binding"]["binding_digest"],
            })
            assert gateway.submit(submission) == "succeeded"
            assert (authority.query_calls, authority.reserve_calls, authority.confirm_calls,
                    handler.calls) == (1, 1, 1, 1)
            divergent = GatewaySubmission.create("different-request", submission.intent, submission.snapshots)
            with pytest.raises(GatewayConflict, match="different submission"):
                gateway.submit(divergent)
            store.verify_integrity()
        finally:
            store.close()


def test_authority_call_crash_recovers_unknown_and_never_retries(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "crash-authority")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    authority = SimulatedAuthority()
    store = GatewayStore(path, identity)
    binding, reservation = _prepare_reserved_for_test(
        store, submission, identity, authority, SimulatedHandler())
    request = _confirmation_request_for_test(
        binding, reservation, binding.binding_digest.removeprefix("sha256:")[:24])
    store.record_protocol_message(submission.operation_id, "confirmation_request", request)
    store.mark_authority_call_started(submission.operation_id)
    store.close()
    reopened = GatewayStore(path, identity)
    try:
        assert reopened.state(submission.operation_id) == "unknown"
        assert reopened.db.execute("SELECT count(*) FROM gateway_authorizations").fetchone()[0] == 0
        reopened.verify_integrity()
    finally:
        reopened.close()


def test_store_requires_durable_authorization_intent_before_response(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "intent-before-proof")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    store = GatewayStore(tmp_path / "gateway.sqlite", identity)
    authority = SimulatedAuthority()
    try:
        binding, reservation = _prepare_reserved_for_test(
            store, submission, identity, authority, SimulatedHandler())
        request = _confirmation_request_for_test(
            binding, reservation, binding.binding_digest.removeprefix("sha256:")[:24])
        store.record_protocol_message(submission.operation_id, "confirmation_request", request)
        response = authority.confirm_consumption(request)
        store.record_protocol_message(submission.operation_id, "confirmation", response)
        with pytest.raises(GatewayConflict, match="durable confirmation intent"):
            store.record_authorization(submission.operation_id, response)
        assert store.state(submission.operation_id) == "execution_prepared"
        assert store.db.execute("SELECT count(*) FROM gateway_authorizations").fetchone()[0] == 0
        store.verify_integrity()
    finally:
        store.close()


def test_legacy_single_step_consumption_cannot_authorize_nivel2(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "legacy-consumption-denied")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    authority = SimulatedAuthority()
    store = GatewayStore(tmp_path / "gateway.sqlite", identity)
    try:
        binding, reservation = _prepare_reserved_for_test(
            store, submission, identity, authority, SimulatedHandler())
        request = _confirmation_request_for_test(
            binding, reservation, binding.binding_digest.removeprefix("sha256:")[:24])
        store.record_protocol_message(submission.operation_id, "confirmation_request", request)
        store.mark_authority_call_started(submission.operation_id)
        legacy = build_contract(AuthorizationConsumption, protocol_version=1,
            consumption_id="legacy.consume", authorization_digest=digest_json({"legacy": 1}),
            request_id=submission.request_id, operation_id=submission.operation_id,
            intent_digest=submission.intent_digest, audience=identity.gateway_id,
            target_id=submission.intent.target_id, payload_digest=submission.intent.payload_digest,
            status="consumed", reason_code="legacy", consumed_at=_stamp(), expires_at=_stamp(120))
        with pytest.raises(GatewayStoreError, match="typed consumption confirmation required"):
            store.record_authorization(submission.operation_id, legacy)
        assert store.state(submission.operation_id) == "execution_prepared"
        assert store.db.execute("SELECT count(*) FROM gateway_authorizations").fetchone()[0] == 0
        store.verify_integrity()
    finally:
        store.close()


def test_crash_before_and_after_preparation_never_dispatch_automatically(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission1 = _broker_submission(broker, "crash-before-preparation")
        submission2 = _broker_submission(broker, "crash-after-preparation")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    authority = SimulatedAuthority()
    store = GatewayStore(path, identity)
    _reserve_for_test(store, submission1, identity, authority)
    _prepare_reserved_for_test(store, submission2, identity, authority, SimulatedHandler())
    store.close()
    reopened = GatewayStore(path, identity)
    try:
        assert reopened.state(submission1.operation_id) == "awaiting_authorization"
        assert reopened.state(submission2.operation_id) == "execution_prepared"
        assert reopened.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        reopened.verify_integrity()
    finally:
        reopened.close()


def gateway_preparation_for_test(submission, handler):
    from agentflow.teg.gateway import _validate_submission
    from agentflow.teg.gateway import TrustedExecutionGateway
    descriptor = handler.descriptor
    scope, impact, budget = _validate_submission(submission, descriptor, now=datetime.now(timezone.utc))
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    gateway = TrustedExecutionGateway.__new__(TrustedExecutionGateway)
    gateway.identity = identity
    return gateway._prepare(submission, handler, scope, impact, budget)


def test_crash_during_execution_becomes_unknown_on_reopen(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "crash-executing")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    authority = SimulatedAuthority()
    store = GatewayStore(path, identity)
    handler = SimulatedHandler()
    binding, reservation = _prepare_reserved_for_test(store, submission, identity, authority, handler)
    confirmation = _confirm_for_test(store, submission, binding, reservation, authority)
    store.record_authorization(submission.operation_id, confirmation)
    store.mark_executing(submission.operation_id)
    store.close()
    reopened = GatewayStore(path, identity)
    try:
        assert reopened.state(submission.operation_id) == "unknown"
        assert reopened.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        reopened.verify_integrity()
    finally:
        reopened.close()


def test_ambiguous_handler_failure_records_unknown_receipt(tmp_path):
    handler = SimulatedHandler(result="raise")
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "handler-unknown")
        store, _, gateway = _gateway(tmp_path, submission, authority=SimulatedAuthority(), handler=handler)
        try:
            assert gateway.submit(submission) == "unknown"
            receipt = store.db.execute("SELECT receipt_json FROM gateway_receipts").fetchone()[0]
            assert json.loads(receipt)["status"] == "unknown"
            store.verify_integrity()
        finally:
            store.close()


def test_gateway_store_is_independent_and_rejects_broker_database(tmp_path):
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "broker.sqlite"
    with BrokerStore(path):
        with pytest.raises(GatewayStoreError, match="non-Gateway database"):
            GatewayStore(path, identity)


def test_only_one_gateway_process_can_own_its_journal(tmp_path):
    identity = GatewayIdentity.create("gateway.lock", "1.0", _stamp(-1))
    first = GatewayStore(tmp_path / "locked-gateway.sqlite", identity)
    try:
        with pytest.raises(GatewayStoreLocked):
            GatewayStore(tmp_path / "locked-gateway.sqlite", identity)
    finally:
        first.close()


def test_protocol_binding_and_canonical_digest_vectors_are_stable_and_strict(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "protocol-vector")
    identity = GatewayIdentity.create("gateway.protocol", "1.0", _stamp(-1))
    binding = ExecutionBinding.from_submission(submission, identity)
    query = build_message(AuthorizationQuery, protocol_version=1,
        request_id="query.vector", binding=binding, expires_at=submission.intent.expires_at)
    first = query.to_json()
    second = build_message(AuthorizationQuery, protocol_version=1,
        request_id="query.vector", binding=binding, expires_at=submission.intent.expires_at)
    assert first == second.to_json()
    assert AuthorizationQuery.from_json(first) == query
    with pytest.raises(GatewayContractError, match="query_digest_mismatch"):
        replace(query, expires_at=_stamp(60))
    raw = json.loads(first)
    raw["unrecognized"] = True
    with pytest.raises(ValueError, match="fields_mismatch"):
        AuthorizationQuery.from_json(canonical_json(raw))
    other_identity = GatewayIdentity.create("gateway.other", "1.0", _stamp(-1))
    assert ExecutionBinding.from_submission(submission, other_identity).binding_digest != binding.binding_digest


def _downgrade_empty_gateway_to_v1(path, *, obstruct_migration=False):
    with sqlite3.connect(path) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute("DROP INDEX IF EXISTS gateway_protocol_operation_idx")
        db.execute("DROP TABLE IF EXISTS gateway_protocol_records")
        db.execute("DROP TABLE IF EXISTS gateway_reservations")
        if obstruct_migration:
            db.execute("CREATE TABLE gateway_reservations(obstruction INTEGER)")
        db.execute("PRAGMA user_version=1")


def test_v1_to_v2_migration_preserves_operations_and_integrity(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "migration-v1")
    identity = GatewayIdentity.create("gateway.migration", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    store = GatewayStore(path, identity)
    store.receive(submission)
    store.close()
    _downgrade_empty_gateway_to_v1(path)
    migrated = GatewayStore(path, identity)
    try:
        assert SCHEMA_VERSION == 2
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert migrated.state(submission.operation_id) == "received"
        migrated.verify_integrity()
    finally:
        migrated.close()


def test_v1_to_v2_migration_failure_rolls_back_without_losing_operation(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "migration-rollback")
    identity = GatewayIdentity.create("gateway.migration.rollback", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    store = GatewayStore(path, identity)
    store.receive(submission)
    store.close()
    _downgrade_empty_gateway_to_v1(path, obstruct_migration=True)
    with pytest.raises(GatewayStoreError, match="schema creation failed and rolled back"):
        GatewayStore(path, identity)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM gateway_operations WHERE operation_id=?",
                          (submission.operation_id,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM gateway_events").fetchone()[0] == 1


def test_reservation_survives_restart_and_reconciliation_does_not_dispatch(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "reservation-restart")
    identity = GatewayIdentity.create("gateway.reservation.restart", "1.0", _stamp(-1))
    authority = SimulatedAuthority()
    path = tmp_path / "gateway.sqlite"
    store = GatewayStore(path, identity)
    store.receive(submission)
    store.mark_validated(submission.operation_id)
    store.await_authorization(submission.operation_id)
    binding = ExecutionBinding.from_submission(submission, identity)
    query = build_message(AuthorizationQuery, protocol_version=1, request_id="q.restart",
        binding=binding, expires_at=submission.intent.expires_at)
    store.record_protocol_message(submission.operation_id, "authorization_query", query)
    query_result = authority.query_authorization(query)
    store.record_protocol_message(submission.operation_id, "authorization_query_result", query_result)
    request = build_message(ConsumptionReservationRequest, protocol_version=1,
        request_id="r.restart", binding=binding, query_result_digest=query_result.result_digest,
        expires_at=min(query_result.expires_at, submission.intent.expires_at))
    store.record_protocol_message(submission.operation_id, "reservation_request", request)
    reservation = authority.reserve_consumption(request)
    store.record_protocol_message(submission.operation_id, "reservation", reservation)
    store.record_reservation(submission.operation_id, reservation)
    store.close()
    reopened = GatewayStore(path, identity)
    gateway = TrustedExecutionGateway(reopened, identity, authority=authority)
    try:
        assert reopened.state(submission.operation_id) == "awaiting_authorization"
        assert gateway.reconcile(submission.operation_id) == "reserved"
        assert reopened.state(submission.operation_id) == "awaiting_authorization"
        assert reopened.db.execute("SELECT count(*) FROM gateway_receipts").fetchone()[0] == 0
        reopened.verify_integrity()
    finally:
        reopened.close()


def test_gateway_event_chain_tampering_is_detected_on_reopen(tmp_path):
    with BrokerStore(tmp_path / "broker.sqlite") as broker:
        submission = _broker_submission(broker, "audit-tamper")
    identity = GatewayIdentity.create("gateway.test", "1.0", _stamp(-1))
    path = tmp_path / "gateway.sqlite"
    store = GatewayStore(path, identity)
    store.receive(submission)
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER gateway_event_no_update")
        db.execute("UPDATE gateway_events SET payload_json='{}' WHERE sequence=1")
    from agentflow.teg import GatewayIntegrityError
    with pytest.raises(GatewayIntegrityError, match="event hash chain invalid"):
        GatewayStore(path, identity)
