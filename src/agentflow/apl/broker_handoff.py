"""Supervised handoff of validated APL proposals to the non-authoritative Broker."""

from __future__ import annotations

from dataclasses import asdict, replace
from hashlib import sha256
import json
import sqlite3
from contextlib import contextmanager

from agentflow.approval.protocol import (
    PROTOCOL_VERSION, PendingProposalCancellation, PendingProposalSnapshot, ProposalLifecycleSnapshot,
    SnapshotProposal, canonical_json, digest_json, utc_now,
)
from agentflow.approval.transport import BrokerEnvelope, BrokerTransportError
from .contracts import APLState


class BrokerHandoffError(RuntimeError):
    """Handoff failed or returned a response that cannot be safely reconciled."""


def _stable_id(label: str, *parts: str) -> str:
    return sha256((label + ":" + ":".join(parts)).encode("utf-8")).hexdigest()[:32]


class BrokerHandoffService:
    """Persistently bind one APL proposal to one immutable Broker proposal.

    This service only submits and queries pending snapshots. It has no acceptance,
    grant, or execution operation.
    """

    def __init__(self, cycle_store, state_path, broker_client, *, project_id: str,
                 principal_id: str):
        self.store = cycle_store
        self.state_path = str(state_path)
        self.client = broker_client
        self.project_id = project_id
        self.principal_id = principal_id
        self._initialize()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.state_path, timeout=10, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    def _initialize(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS apl_broker_handoffs (
                cycle_id TEXT NOT NULL, apl_proposal_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL UNIQUE, operation_id TEXT NOT NULL UNIQUE,
                proposal_json TEXT NOT NULL, digest TEXT NOT NULL,
                PRIMARY KEY(cycle_id, apl_proposal_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS apl_broker_handoff_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id TEXT NOT NULL, apl_proposal_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN
                    ('prepared','submitted','awaiting_authorization','unknown',
                     'expired','cancelled','obsolete','accepted','rejected')),
                detail TEXT NOT NULL, occurred_at TEXT NOT NULL)""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS apl_broker_handoffs_no_update
                BEFORE UPDATE ON apl_broker_handoffs BEGIN
                SELECT RAISE(ABORT,'APL Broker handoff bindings are immutable'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS apl_broker_handoffs_no_delete
                BEFORE DELETE ON apl_broker_handoffs BEGIN
                SELECT RAISE(ABORT,'APL Broker handoff bindings are immutable'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS apl_broker_handoff_events_no_update
                BEFORE UPDATE ON apl_broker_handoff_events BEGIN
                SELECT RAISE(ABORT,'APL Broker handoff events are append-only'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS apl_broker_handoff_events_no_delete
                BEFORE DELETE ON apl_broker_handoff_events BEGIN
                SELECT RAISE(ABORT,'APL Broker handoff events are append-only'); END""")

    def _event(self, cycle_id, proposal_id, state, detail):
        with self._connect() as db:
            db.execute("INSERT INTO apl_broker_handoff_events"
                       "(cycle_id,apl_proposal_id,state,detail,occurred_at) VALUES(?,?,?,?,?)",
                       (cycle_id, proposal_id, state, detail, utc_now()))

    def _binding(self, cycle_id, proposal_id):
        with self._connect() as db:
            return db.execute("SELECT snapshot_id,operation_id,proposal_json,digest "
                              "FROM apl_broker_handoffs WHERE cycle_id=? AND apl_proposal_id=?",
                              (cycle_id, proposal_id)).fetchone()

    def _prepare(self, cycle_id, proposal_id):
        current = self.store.load(cycle_id)
        rows = {item[0].proposal_id: item for item in self.store.proposals(cycle_id)}
        row = rows.get(proposal_id)
        if row is None:
            raise KeyError(proposal_id)
        proposal, apl_digest, status = row
        if status != "proposed":
            raise BrokerHandoffError(f"APL proposal is {status}; only proposed revisions can be handed off")
        existing_binding = self._binding(cycle_id, proposal_id)
        if existing_binding is None and (
                proposal.source_revision_id != current.plan_revision_id
                or proposal.source_cycle_revision != current.revision):
            raise BrokerHandoffError("APL proposal is obsolete against the current cycle revision")
        content = {
            "authorized_scope": list(current.authorized_scope),
            "changes": [{"action": change.action, "task": asdict(change.task)}
                        for change in proposal.changes],
            "completion_condition": proposal.completion_condition,
            "evidence_used": list(proposal.evidence_used),
            "objective": current.objective,
            "rationale": proposal.rationale,
            "source_cycle_revision": proposal.source_cycle_revision,
            "source_plan_revision_id": proposal.source_revision_id,
        }
        content_json = canonical_json(content)
        operation_id = _stable_id("apl-broker-submit-v1", cycle_id, proposal_id, apl_digest)
        snapshot_id = _stable_id("apl-broker-snapshot-v1", cycle_id, proposal_id, apl_digest)
        broker_proposal = SnapshotProposal(
            protocol_version=PROTOCOL_VERSION, snapshot_id=snapshot_id, kind="plan",
            project_id=self.project_id, revision_id=proposal.proposal_id,
            schema_version=proposal.schema_version, content_json=content_json,
            canonical_digest=digest_json(json.loads(content_json)),
            provenance_json=canonical_json({"source": "agentflow.apl", "cycle_id": cycle_id,
                                            "apl_proposal_id": proposal_id,
                                            "apl_proposal_digest": apl_digest}),
            proposed_by=self.principal_id, operation_id=operation_id,
            proposed_at=utc_now(), parent_digest=None)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                prior = db.execute("SELECT snapshot_id,operation_id,proposal_json,digest "
                                   "FROM apl_broker_handoffs WHERE cycle_id=? AND apl_proposal_id=?",
                                   (cycle_id, proposal_id)).fetchone()
                if prior is None:
                    cycle_row = db.execute(
                        "SELECT revision,snapshot_json FROM apl_cycles WHERE cycle_id=?",
                        (cycle_id,)).fetchone()
                    if cycle_row is None:
                        raise KeyError(cycle_id)
                    persisted_cycle = self.store._decode(cycle_row[1])
                    if (cycle_row[0] != proposal.source_cycle_revision
                            or persisted_cycle.revision != proposal.source_cycle_revision
                            or persisted_cycle.plan_revision_id != proposal.source_revision_id
                            or persisted_cycle.state.value not in {"READY", "PAUSED", "BLOCKED"}):
                        raise BrokerHandoffError("APL proposal became obsolete before handoff reservation")
                    db.execute("INSERT INTO apl_broker_handoffs VALUES(?,?,?,?,?,?)",
                               (cycle_id, proposal_id, snapshot_id, operation_id,
                                broker_proposal.to_json(), broker_proposal.canonical_digest))
                    waiting = replace(persisted_cycle, state=APLState.WAITING_APPROVAL,
                        revision=persisted_cycle.revision + 1,
                        reason="Broker handoff pending authorization or reconciliation")
                    encoded = self.store._encode(waiting)
                    changed = db.execute(
                        "UPDATE apl_cycles SET revision=?,snapshot_json=? WHERE cycle_id=? AND revision=?",
                        (waiting.revision, encoded, cycle_id, persisted_cycle.revision)).rowcount
                    if changed != 1:
                        raise BrokerHandoffError("concurrent APL transition prevented handoff reservation")
                    db.execute("INSERT INTO apl_events(cycle_id,revision,state,snapshot_digest) VALUES(?,?,?,?)",
                               (cycle_id, waiting.revision, waiting.state.value,
                                sha256(encoded.encode("utf-8")).hexdigest()))
                    db.execute("INSERT INTO apl_broker_handoff_events"
                               "(cycle_id,apl_proposal_id,state,detail,occurred_at) VALUES(?,?,?,?,?)",
                               (cycle_id, proposal_id, "prepared", "immutable broker binding persisted; cycle awaiting authorization", utc_now()))
                else:
                    if prior[0] != snapshot_id or prior[1] != operation_id or prior[3] != broker_proposal.canonical_digest:
                        raise BrokerHandoffError("existing handoff binding conflicts with current APL proposal")
                db.commit()
            except Exception:
                db.rollback()
                raise
        return self._binding(cycle_id, proposal_id)

    @staticmethod
    def _response(response, expected_action, operation_id):
        try:
            body = json.loads(response.body_json)
            if (body.get("operation_id") != operation_id
                    or body.get("protocol_version") != PROTOCOL_VERSION):
                raise ValueError
            if "error_code" in body:
                raise BrokerHandoffError(f"Broker rejected handoff: {body['error_code']}")
            if body.get("action") != expected_action or set(body) != {
                    "action", "operation_id", "protocol_version", "snapshot"}:
                raise ValueError
            return body["snapshot"]
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BrokerHandoffError("Broker response is malformed or incompatible") from exc

    def _query(self, cycle_id, proposal_id, binding):
        snapshot_id, _submit_operation, proposal_json, digest = binding
        query_operation = _stable_id("apl-broker-query-v1", cycle_id, proposal_id)
        request = BrokerEnvelope.new_request(operation_id=query_operation, body={
            "action": "snapshot_get", "operation_id": query_operation,
            "protocol_version": PROTOCOL_VERSION, "snapshot_id": snapshot_id,
        })
        response = self.client.request(request)
        try:
            body = json.loads(response.body_json)
            action = body.get("action")
            raw = self._response(response, action, query_operation)
            if action == "snapshot_pending":
                status = PendingProposalSnapshot.from_dict(raw)
            elif action == "snapshot_lifecycle":
                status = ProposalLifecycleSnapshot.from_dict(raw)
            else:
                raise BrokerHandoffError("Broker returned an unsupported proposal state")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BrokerHandoffError("Broker lifecycle response is invalid") from exc
        broker_proposal = SnapshotProposal.from_json(proposal_json)
        if (status.snapshot_id != snapshot_id or status.revision_id != broker_proposal.revision_id
                or status.digest != digest or status.principal_id != self.principal_id
                or status.authority_status != "unavailable" or status.dispatch_allowed is not False):
            raise BrokerHandoffError("Broker snapshot does not match the immutable APL handoff")
        state = {"pending": "awaiting_authorization", "expired": "expired",
                 "cancelled": "cancelled", "obsolete": "obsolete",
                 "accepted": "accepted", "rejected": "rejected"}[status.status]
        self._event(cycle_id, proposal_id, state, f"broker journal sequence {status.journal_sequence}")
        if status.status != "pending":
            cycle = self.store.load(cycle_id)
            if cycle.state == APLState.WAITING_APPROVAL:
                blocked = replace(cycle, state=APLState.BLOCKED,
                                  reason=f"Broker proposal is terminal: {status.status}")
                self.store.save(blocked, cycle.iterations, expected_revision=cycle.revision)
        return {"cycle_id": cycle_id, "apl_proposal_id": proposal_id,
                "snapshot_id": snapshot_id, "revision_id": status.revision_id,
                "digest": status.digest, "status": state,
                "journal_sequence": status.journal_sequence,
                "authority_status": status.authority_status,
                "dispatch_allowed": status.dispatch_allowed}

    def submit(self, cycle_id, proposal_id):
        binding = self._prepare(cycle_id, proposal_id)
        snapshot_id, operation_id, proposal_json, _digest = binding
        request = BrokerEnvelope.new_request(operation_id=operation_id, body={
            "action": "snapshot_submit", "operation_id": operation_id,
            "protocol_version": PROTOCOL_VERSION, "proposal_json": proposal_json,
        })
        try:
            response = self.client.request(request)
            self._response(response, "snapshot_pending", operation_id)
            self._event(cycle_id, proposal_id, "submitted", "Broker acknowledged pending snapshot")
        except BrokerTransportError:
            # Outcome may be unknown. The fixed immutable operation can be safely reconciled.
            self._event(cycle_id, proposal_id, "unknown", "submission outcome requires Broker query/replay")
            raise
        return self._query(cycle_id, proposal_id, binding)

    def resume(self, cycle_id, proposal_id):
        binding = self._binding(cycle_id, proposal_id)
        if binding is None:
            return self.submit(cycle_id, proposal_id)
        try:
            return self._query(cycle_id, proposal_id, binding)
        except BrokerHandoffError as exc:
            # A not-yet-seen snapshot can arise after interruption between reservation
            # and network dispatch. Replaying the exact immutable proposal is idempotent.
            if "snapshot_unavailable" not in str(exc):
                raise
            return self._submit_binding(cycle_id, proposal_id, binding)

    def _submit_binding(self, cycle_id, proposal_id, binding):
        _snapshot_id, operation_id, proposal_json, _digest = binding
        request = BrokerEnvelope.new_request(operation_id=operation_id, body={
            "action": "snapshot_submit", "operation_id": operation_id,
            "protocol_version": PROTOCOL_VERSION, "proposal_json": proposal_json,
        })
        response = self.client.request(request)
        self._response(response, "snapshot_pending", operation_id)
        self._event(cycle_id, proposal_id, "submitted", "idempotent Broker replay acknowledged")
        return self._query(cycle_id, proposal_id, binding)

    def status(self, cycle_id, proposal_id):
        binding = self._binding(cycle_id, proposal_id)
        if binding is None:
            raise KeyError(proposal_id)
        return self._query(cycle_id, proposal_id, binding)

    def cancel(self, cycle_id, proposal_id):
        """Cancel the owner's pending proposal through the existing authenticated Broker client."""
        binding = self._binding(cycle_id, proposal_id)
        if binding is None:
            raise KeyError(proposal_id)
        snapshot_id, _submit_operation, proposal_json, digest = binding
        proposal = SnapshotProposal.from_json(proposal_json)
        operation_id = _stable_id("apl-broker-cancel-v1", cycle_id, proposal_id, digest)
        request = BrokerEnvelope.new_request(operation_id=operation_id, body={
            "action": "snapshot_cancel", "operation_id": operation_id,
            "protocol_version": PROTOCOL_VERSION, "snapshot_id": snapshot_id,
        })
        # Validate locally constructed envelope against the versioned owner-cancel contract.
        PendingProposalCancellation(PROTOCOL_VERSION, operation_id, snapshot_id)
        response = self.client.request(request)
        raw = self._response(response, "snapshot_lifecycle", operation_id)
        try:
            status = ProposalLifecycleSnapshot.from_dict(raw)
        except Exception as exc:
            raise BrokerHandoffError("Broker cancellation lifecycle is invalid") from exc
        if (status.snapshot_id != snapshot_id or status.revision_id != proposal.revision_id
                or status.digest != digest or status.principal_id != self.principal_id
                or status.status != "cancelled" or status.authority_status != "unavailable"
                or status.dispatch_allowed is not False):
            raise BrokerHandoffError("Broker cancellation did not reconcile to the bound proposal")
        prior = self.history(cycle_id, proposal_id)
        if not prior or prior[-1]["state"] != "cancelled":
            self._event(cycle_id, proposal_id, "cancelled",
                        f"owner cancellation persisted at Broker journal sequence {status.journal_sequence}")
        current = self.store.load(cycle_id)
        if current.state == APLState.WAITING_APPROVAL:
            blocked = replace(current, state=APLState.BLOCKED,
                              reason="Broker proposal cancelled by its authenticated owner")
            self.store.save(blocked, current.iterations, expected_revision=current.revision)
        return {"cycle_id": cycle_id, "apl_proposal_id": proposal_id,
                "snapshot_id": snapshot_id, "revision_id": status.revision_id,
                "digest": status.digest, "status": "cancelled",
                "journal_sequence": status.journal_sequence,
                "authority_status": status.authority_status,
                "dispatch_allowed": status.dispatch_allowed}

    def history(self, cycle_id, proposal_id):
        with self._connect() as db:
            return tuple(dict(zip(("event_id", "state", "detail", "occurred_at"), row))
                         for row in db.execute(
                             "SELECT event_id,state,detail,occurred_at FROM apl_broker_handoff_events "
                             "WHERE cycle_id=? AND apl_proposal_id=? ORDER BY event_id",
                             (cycle_id, proposal_id)))
