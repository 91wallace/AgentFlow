"""Fail-closed deterministic core for a future Trusted Execution Gateway.

No concrete provider, network, shell, subprocess, Git, or filesystem effect handler is
registered here. Production instances default to unavailable external authorization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from agentflow.approval.protocol import (
    ProtocolError, canonical_json, digest_json,
    parse_canonical_json, _timestamp,
)

from .contracts import (
    GatewayAuthorityStatus, GatewayContractError,
    GatewayEffectResult, GatewayExecutionError, GatewayHandlerDescriptor,
    GatewayIdentity, GatewayPreparation, GatewaySubmission, _BUDGET_FIELDS,
    _CONTEXT_FIELDS, _IMPACT_FIELDS, _money,
)
from .store import GatewayStore
from .protocol import (
    AuthorizationQuery, AuthorizationQueryResult, AuthorizationState,
    AuthorizationStateQuery, ConsumptionConfirmation, ConsumptionConfirmationRequest,
    ConsumptionReservation, ConsumptionReservationRequest, ExecutionBinding,
    GatewayExecutionReceipt, ReconciliationRequest, ReconciliationResult,
    build_message,
)


class GatewayHandler(Protocol):
    """Explicit typed effect adapter. Implementations belong to tests until RB-09."""

    descriptor: GatewayHandlerDescriptor

    def execute(self, submission: GatewaySubmission,
                preparation: GatewayPreparation) -> GatewayEffectResult: ...


class GatewayHandlerRegistry:
    """In-memory allowlist; no discovery, entry-point loading, or dynamic plugins."""

    def __init__(self, handlers: tuple[GatewayHandler, ...] = ()):
        self._handlers: tuple[GatewayHandler, ...] = ()
        self.register_all(handlers)

    def register_all(self, handlers: tuple[GatewayHandler, ...]) -> None:
        if type(handlers) is not tuple:
            raise GatewayContractError("handlers_must_be_explicit_tuple")
        existing = {item.descriptor.handler_id for item in self._handlers}
        additions = []
        for handler in handlers:
            descriptor = getattr(handler, "descriptor", None)
            if not isinstance(descriptor, GatewayHandlerDescriptor) or not callable(getattr(handler, "execute", None)):
                raise GatewayContractError("handler_contract_invalid")
            if descriptor.handler_id in existing or any(d.handler_id == descriptor.handler_id for d, _ in additions):
                raise GatewayContractError("duplicate_handler_id")
            existing.add(descriptor.handler_id)
            additions.append((descriptor, handler))
        self._handlers += tuple(handler for _, handler in additions)

    def select(self, intent) -> GatewayHandler:
        matches = [handler for handler in self._handlers
                   if intent.action_code in handler.descriptor.action_codes
                   and intent.target_id in handler.descriptor.target_ids
                   and intent.provider_id in handler.descriptor.provider_ids
                   and intent.model_id in handler.descriptor.model_ids]
        if not matches:
            raise GatewayExecutionError("handler_not_allowlisted")
        if len(matches) != 1:
            raise GatewayExecutionError("handler_selection_ambiguous")
        return matches[0]


class GatewayAuthority(Protocol):
    """Future authenticated Broker client; runtime implementation is not available."""

    def status(self) -> GatewayAuthorityStatus: ...

    def query_authorization(self, query: AuthorizationQuery) -> AuthorizationQueryResult: ...

    def reserve_consumption(self, request: ConsumptionReservationRequest) -> ConsumptionReservation: ...

    def confirm_consumption(self, request: ConsumptionConfirmationRequest) -> ConsumptionConfirmation: ...

    def query_state(self, query: AuthorizationStateQuery) -> AuthorizationState: ...

    def reconcile(self, request: ReconciliationRequest) -> ReconciliationResult: ...


class UnavailableGatewayAuthority:
    """Default fail-closed authority. It has no test or local approval path."""

    def status(self) -> GatewayAuthorityStatus:
        return GatewayAuthorityStatus(1, False, "broker_not_homologated", ())

    def query_authorization(self, query: AuthorizationQuery) -> AuthorizationQueryResult:
        raise GatewayExecutionError("authorization_authority_unavailable")

    def reserve_consumption(self, request: ConsumptionReservationRequest) -> ConsumptionReservation:
        raise GatewayExecutionError("authorization_authority_unavailable")

    def confirm_consumption(self, request: ConsumptionConfirmationRequest) -> ConsumptionConfirmation:
        raise GatewayExecutionError("authorization_authority_unavailable")

    def query_state(self, query: AuthorizationStateQuery) -> AuthorizationState:
        raise GatewayExecutionError("authorization_authority_unavailable")

    def reconcile(self, request: ReconciliationRequest) -> ReconciliationResult:
        raise GatewayExecutionError("authorization_authority_unavailable")


_EXPECTED_EFFECT = {
    "provider_call": "provider_request",
    "provider_model_change": "provider_selection_change",
    "model_escalation": "model_escalation",
}


def _utc(value: datetime | None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        raise GatewayContractError("gateway_clock_must_be_aware")
    return result.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_budget(intent_json: str, descriptor: GatewayHandlerDescriptor) -> dict:
    try:
        value = parse_canonical_json(intent_json)
    except ProtocolError as exc:
        raise GatewayExecutionError("budget_json_invalid") from exc
    if type(value) is not dict or "status" not in value or set(value) - _BUDGET_FIELDS:
        raise GatewayExecutionError("budget_fields_incomplete_or_unknown")
    if type(value["status"]) is not str or value["status"] not in descriptor.allowed_budget_statuses:
        raise GatewayExecutionError("budget_status_not_allowed")
    missing = set(descriptor.required_budget_fields) - set(value)
    if missing:
        raise GatewayExecutionError("required_budget_bound_missing")
    if "constraints" in value:
        constraints = value["constraints"]
        if (type(constraints) is not list or any(type(item) is not str for item in constraints)
                or constraints != sorted(set(constraints))):
            raise GatewayExecutionError("budget_constraints_invalid")
    currency = value.get("currency")
    cost = value.get("cost_estimate")
    if cost is not None:
        if currency != descriptor.currency or descriptor.max_cost_estimate is None:
            raise GatewayExecutionError("cost_limit_or_currency_unverifiable")
        if _money(cost, "cost_estimate") > _money(descriptor.max_cost_estimate, "handler_max_cost"):
            raise GatewayExecutionError("cost_budget_exceeded")
    elif currency is not None and (type(currency) is not str or len(currency) != 3 or currency.upper() != currency):
        raise GatewayExecutionError("budget_currency_invalid")
    for key, maximum in (("token_limit", descriptor.max_tokens),
                         ("time_limit_seconds", descriptor.max_time_seconds)):
        amount = value.get(key)
        if amount is not None and (type(amount) is not int or amount < 0):
            raise GatewayExecutionError("budget_bound_invalid")
        if amount is not None and maximum is not None and amount > maximum:
            raise GatewayExecutionError(f"{key}_exceeded")
    return value


def _validate_submission(submission: GatewaySubmission, descriptor: GatewayHandlerDescriptor,
                         *, now: datetime) -> tuple[tuple[str, ...], dict, dict]:
    intent = submission.intent
    snapshots = submission.snapshots
    if _timestamp(intent.expires_at, "intent_expiry") <= now:
        raise GatewayExecutionError("operation_expired")
    by_kind = {item.kind: item for item in snapshots}
    expected = {
        "specification": (intent.specification_snapshot_id, intent.specification_revision_id, intent.specification_digest),
        "plan": (intent.plan_snapshot_id, intent.plan_revision_id, intent.plan_digest),
        "checkpoint": (intent.checkpoint_snapshot_id, None, intent.checkpoint_digest),
        "payload": (intent.payload_snapshot_id, None, intent.payload_digest),
    }
    for kind, (snapshot_id, revision_id, digest) in expected.items():
        snapshot = by_kind.get(kind)
        if snapshot is None or snapshot.snapshot_id != snapshot_id or snapshot.project_id != intent.project_id:
            raise GatewayExecutionError("accepted_snapshot_identity_mismatch")
        if snapshot.canonical_digest != digest or (revision_id is not None and snapshot.revision_id != revision_id):
            raise GatewayExecutionError("accepted_snapshot_digest_or_revision_mismatch")
        if _timestamp(snapshot.accepted_at, "snapshot_accepted_at") > now:
            raise GatewayExecutionError("snapshot_acceptance_from_future")
    if (intent.action_code not in descriptor.action_codes or intent.target_id not in descriptor.target_ids
            or intent.provider_id not in descriptor.provider_ids or intent.model_id not in descriptor.model_ids):
        raise GatewayExecutionError("operation_outside_handler_allowlist")
    payload_snapshot = by_kind["payload"]
    if len(payload_snapshot.content_json.encode("utf-8")) > descriptor.max_payload_bytes:
        raise GatewayExecutionError("payload_exceeds_handler_limit")
    payload = parse_canonical_json(payload_snapshot.content_json)
    if type(payload) is not dict or set(payload) != {"provider_payload", "approval_context"}:
        raise GatewayExecutionError("payload_envelope_incomplete_or_unknown")
    context = payload["approval_context"]
    if (type(context) is not dict or set(context) != _CONTEXT_FIELDS
            or type(context.get("context_version")) is not int or context.get("context_version") != 1):
        raise GatewayExecutionError("approval_context_incomplete_or_unknown")
    linked_values = {
        "operation_id": intent.operation_id, "project_id": intent.project_id,
        "task_id": intent.task_id, "attempt_id": intent.attempt_id,
        "specification_revision_id": intent.specification_revision_id,
        "specification_digest": intent.specification_digest,
        "plan_revision_id": intent.plan_revision_id, "plan_digest": intent.plan_digest,
        "checkpoint_digest": intent.checkpoint_digest,
    }
    if any(context.get(key) != value for key, value in linked_values.items()):
        raise GatewayExecutionError("approval_context_operation_mismatch")
    scope = context.get("scope")
    if (type(scope) is not list or not scope or any(type(item) is not str for item in scope)
            or scope != sorted(set(scope)) or not set(scope) <= set(descriptor.allowed_scopes)
            or not set(descriptor.required_scopes) <= set(scope)):
        raise GatewayExecutionError("operation_scope_not_allowlisted")
    impact = context.get("impact")
    if type(impact) is not dict or set(impact) != _IMPACT_FIELDS or impact.get("kind") != "task_attempt":
        raise GatewayExecutionError("impact_context_incomplete_or_unknown")
    requirements, tasks = impact.get("affected_requirement_ids"), impact.get("affected_task_ids")
    if (type(requirements) is not list or any(type(item) is not str for item in requirements)
            or requirements != sorted(set(requirements)) or type(tasks) is not list
            or any(type(item) is not str for item in tasks) or tasks != sorted(set(tasks))
            or intent.task_id not in tasks):
        raise GatewayExecutionError("impact_scope_invalid")
    impact_body = {key: value for key, value in impact.items() if key != "impact_digest"}
    if digest_json(impact_body) != impact.get("impact_digest"):
        raise GatewayExecutionError("impact_digest_mismatch")
    try:
        provider_payload_json = canonical_json(payload["provider_payload"])
        parameters = parse_canonical_json(intent.parameters_json)
    except ProtocolError as exc:
        raise GatewayExecutionError("operation_payload_invalid") from exc
    if type(parameters) is not dict or set(parameters) - set(descriptor.allowed_parameter_keys):
        raise GatewayExecutionError("operation_parameters_not_allowlisted")
    if not set(descriptor.required_parameter_keys) <= set(parameters):
        raise GatewayExecutionError("required_operation_parameters_missing")
    budget = _validate_budget(intent.budget_json, descriptor)
    if len(provider_payload_json.encode("utf-8")) > descriptor.max_payload_bytes:
        raise GatewayExecutionError("provider_payload_exceeds_handler_limit")
    return tuple(scope), impact, budget


class TrustedExecutionGateway:
    """Explicit one-operation coordinator; default authority is permanently unavailable."""

    def __init__(self, store: GatewayStore, identity: GatewayIdentity,
                 handlers: GatewayHandlerRegistry | None = None,
                 authority: GatewayAuthority | None = None):
        if store.identity.identity_digest != identity.identity_digest:
            raise GatewayExecutionError("gateway_store_identity_mismatch")
        self.store = store
        self.identity = identity
        self.handlers = handlers or GatewayHandlerRegistry()
        self.authority = authority or UnavailableGatewayAuthority()

    def submit(self, submission: GatewaySubmission, *, now: datetime | None = None) -> str:
        instant = _utc(now)
        state, is_new = self.store.receive(submission)
        if not is_new:
            return state
        try:
            handler = self.handlers.select(submission.intent)
            scope, impact, budget = _validate_submission(submission, handler.descriptor, now=instant)
        except (GatewayExecutionError, GatewayContractError, ProtocolError, ValueError):
            self.store.record_failure(submission.operation_id, "operation_validation_failed")
            return "failed"
        self.store.mark_validated(submission.operation_id)
        self.store.await_authorization(submission.operation_id)
        try:
            status = self.authority.status()
        except Exception:
            return "awaiting_authorization"
        if not isinstance(status, GatewayAuthorityStatus) or not status.available:
            return "awaiting_authorization"
        binding = ExecutionBinding.from_submission(submission, self.identity)
        suffix = binding.binding_digest.removeprefix("sha256:")[:32]
        query = build_message(
            AuthorizationQuery, protocol_version=1, request_id="query." + suffix,
            binding=binding, expires_at=submission.intent.expires_at,
        )
        self.store.record_protocol_message(submission.operation_id, "authorization_query", query)
        try:
            query_result = self.authority.query_authorization(query)
        except Exception:
            return "awaiting_authorization"
        if not isinstance(query_result, AuthorizationQueryResult):
            self.store.mark_unknown(submission.operation_id, "authorization_query_untyped")
            return "unknown"
        self.store.record_protocol_message(submission.operation_id, "authorization_query_result", query_result)
        if (query_result.request_id != query.request_id
                or query_result.binding_digest != binding.binding_digest
                or _timestamp(query_result.expires_at, "query_result_expiry") <= _utc(None)):
            self.store.mark_unknown(submission.operation_id, "authorization_query_binding_mismatch")
            return "unknown"
        if query_result.status != "approved":
            if query_result.status == "unknown":
                self.store.mark_unknown(submission.operation_id, "authorization_query_unknown")
                return "unknown"
            self.store.record_failure(submission.operation_id, "authorization_not_approved")
            return "failed"

        reservation_expiry = min(_timestamp(query_result.expires_at, "query_result_expiry"),
                                 _timestamp(submission.intent.expires_at, "intent_expiry"))
        reservation_request = build_message(
            ConsumptionReservationRequest, protocol_version=1,
            request_id="reserve." + suffix, binding=binding,
            query_result_digest=query_result.result_digest, expires_at=_stamp(reservation_expiry),
        )
        self.store.record_protocol_message(submission.operation_id, "reservation_request", reservation_request)
        try:
            reservation = self.authority.reserve_consumption(reservation_request)
        except Exception:
            self.store.mark_unknown(submission.operation_id, "reservation_response_unknown")
            return "unknown"
        if not isinstance(reservation, ConsumptionReservation):
            self.store.mark_unknown(submission.operation_id, "reservation_response_untyped")
            return "unknown"
        self.store.record_protocol_message(submission.operation_id, "reservation", reservation)
        if reservation.status != "reserved":
            if reservation.status == "unknown":
                self.store.mark_unknown(submission.operation_id, "reservation_state_unknown")
                return "unknown"
            self.store.record_failure(submission.operation_id, "consumption_reservation_rejected")
            return "failed"
        try:
            self.store.record_reservation(submission.operation_id, reservation)
        except Exception:
            self.store.mark_unknown(submission.operation_id, "reservation_persistence_unknown")
            return "unknown"

        preparation = self._prepare(submission, handler, scope, impact, budget)
        self.store.record_preparation(preparation)
        confirm_key = digest_json({"operation_id": binding.operation_id,
                                   "gateway_id": binding.gateway_id,
                                   "reservation_digest": reservation.reservation_digest,
                                   "binding_digest": binding.binding_digest})
        confirmation_request = build_message(
            ConsumptionConfirmationRequest, protocol_version=1,
            request_id="confirm." + suffix, reservation=reservation, binding=binding,
            idempotency_key=confirm_key,
        )
        self.store.record_protocol_message(submission.operation_id, "confirmation_request", confirmation_request)
        self.store.mark_authority_call_started(submission.operation_id)
        try:
            confirmation = self.authority.confirm_consumption(confirmation_request)
        except Exception:
            self.store.mark_unknown(submission.operation_id, "consumption_confirmation_unknown")
            return "unknown"
        if not isinstance(confirmation, ConsumptionConfirmation):
            self.store.mark_unknown(submission.operation_id, "consumption_confirmation_untyped")
            return "unknown"
        self.store.record_protocol_message(submission.operation_id, "confirmation", confirmation)
        if confirmation.status != "confirmed":
            if confirmation.status in {"unknown", "already_consumed"}:
                self.store.mark_unknown(submission.operation_id, "consumption_confirmation_unknown")
                return "unknown"
            self.store.record_failure(submission.operation_id, "consumption_confirmation_rejected")
            return "failed"
        if (confirmation.binding_digest != binding.binding_digest
                or confirmation.reservation_digest != reservation.reservation_digest
                or confirmation.request_id != confirmation_request.request_id
                or _timestamp(confirmation.expires_at, "confirmation_expiry") <= _utc(None)):
            self.store.mark_unknown(submission.operation_id, "consumption_confirmation_binding_mismatch")
            return "unknown"
        self.store.record_authorization(submission.operation_id, confirmation)
        self.store.mark_executing(submission.operation_id)
        execution_started = _utc(None)
        try:
            result = handler.execute(submission, preparation)
            if not isinstance(result, GatewayEffectResult) or result.operation_id != submission.operation_id:
                raise GatewayExecutionError("handler_result_mismatch")
        except Exception:
            result = GatewayEffectResult(1, submission.operation_id, "unknown",
                                         "handler_outcome_unknown", None, None)
        receipt = build_message(
            GatewayExecutionReceipt, protocol_version=1,
            receipt_id="receipt." + submission.submission_digest.removeprefix("sha256:")[:32],
            binding=binding, reservation_digest=reservation.reservation_digest,
            confirmation_digest=confirmation.confirmation_digest, status=result.status,
            started_at=_stamp(execution_started), completed_at=_stamp(_utc(None)),
            effect_idempotency=handler.descriptor.provider_idempotency,
            effect_key=result.effect_key, result_digest=result.result_digest,
        )
        self.store.record_protocol_message(submission.operation_id, "execution_receipt", receipt)
        self.store.record_receipt(receipt)
        return self.store.state(submission.operation_id)

    def reconcile(self, operation_id: str) -> str:
        """Read and journal authority state for an unknown operation; never resumes effects."""
        local_operation_state = self.store.state(operation_id)
        if local_operation_state not in {"unknown", "awaiting_authorization", "execution_prepared", "authorized"}:
            raise GatewayExecutionError("operation_state_cannot_be_reconciled")
        submission = self.store.get_submission(operation_id)
        binding = ExecutionBinding.from_submission(submission, self.identity)
        reservation = self.store.get_reservation(operation_id)
        confirmation = self.store.get_confirmation(operation_id)
        suffix = digest_json({"operation_id": operation_id, "binding": binding.binding_digest,
                              "reservation": reservation.reservation_digest if reservation else None})[7:39]
        query = build_message(
            AuthorizationStateQuery, protocol_version=1, request_id="state." + suffix,
            binding=binding, reservation_digest=reservation.reservation_digest if reservation else None,
        )
        self.store.record_protocol_message(operation_id, "authorization_state_query", query)
        try:
            state = self.authority.query_state(query)
        except Exception:
            return "unknown"
        if not isinstance(state, AuthorizationState):
            return "unknown"
        self.store.record_protocol_message(operation_id, "authorization_state", state)
        if (state.request_id != query.request_id or state.binding_digest != binding.binding_digest
                or (reservation and state.reservation_digest != reservation.reservation_digest)):
            return "unknown"
        local_state = "effect_possible" if any(
            event["event_type"] == "gateway.execution.started" for event in self.store.events(operation_id)
        ) else ("confirming" if self.store.db.execute(
            "SELECT authorization_started FROM gateway_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()[0] else "prepared" if local_operation_state == "execution_prepared"
                else "reserved" if reservation else "awaiting_authorization")
        reconcile_request = build_message(
            ReconciliationRequest, protocol_version=1, request_id="reconcile." + suffix,
            binding=binding, local_state=local_state,
            reservation_digest=reservation.reservation_digest if reservation else None,
            confirmation_digest=confirmation.confirmation_digest if confirmation else None,
            receipt_digest=(self.store.db.execute("SELECT receipt_digest FROM gateway_receipts WHERE operation_id=?",
                                                  (operation_id,)).fetchone() or [None])[0],
        )
        self.store.record_protocol_message(operation_id, "reconciliation_request", reconcile_request)
        try:
            result = self.authority.reconcile(reconcile_request)
        except Exception:
            return "unknown"
        if not isinstance(result, ReconciliationResult):
            return "unknown"
        self.store.record_protocol_message(operation_id, "reconciliation_result", result)
        if result.request_id != reconcile_request.request_id or result.binding_digest != binding.binding_digest:
            return "unknown"
        return result.status

    def _prepare(self, submission: GatewaySubmission, handler: GatewayHandler,
                 scope: tuple[str, ...], impact: dict, budget: dict) -> GatewayPreparation:
        intent = submission.intent
        descriptor = handler.descriptor
        fields = {
            "protocol_version": 1, "operation_id": intent.operation_id,
            "intent_digest": intent.intent_digest, "handler_id": descriptor.handler_id,
            "handler_version": descriptor.handler_version,
            "descriptor_digest": descriptor.descriptor_digest, "target_id": intent.target_id,
            "payload_digest": intent.payload_digest,
            "parameters_digest": digest_json(parse_canonical_json(intent.parameters_json)),
            "budget_digest": digest_json(budget), "scope_digest": digest_json(list(scope)),
            "expected_effect_code": _EXPECTED_EFFECT[intent.action_code],
        }
        return GatewayPreparation(**fields, preparation_digest=digest_json(fields))
