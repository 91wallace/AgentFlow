"""Read-only, secret-minimizing integrity export for isolated Broker and TEG stores.

This module diagnoses local journal consistency only. It is not an authorization,
execution, validation, or externally anchored audit service.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from agentflow.approval.protocol import OperationIntent
from agentflow.teg.contracts import GatewaySubmission


MAX_EXPORT_BYTES = 4 * 1024 * 1024


class RecoveryAuditError(RuntimeError):
    """A recovery report cannot be safely produced."""


def _id_ref(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_recovery_export(broker_store, gateway_store, operation_id: str) -> bytes:
    """Return a deterministic redacted JSON evidence bundle for one operation.

    Both stores are verified in place and queried only. Raw snapshots, payloads,
    protocol message bodies, credentials, assertions, and event payloads are omitted.
    """
    if type(operation_id) is not str or not operation_id:
        raise RecoveryAuditError("operation_id_required")
    try:
        broker_store.verify_integrity()
        gateway_store.verify_integrity()
        broker = broker_store.db
        gateway = gateway_store.db
        intent_row = broker.execute(
            "SELECT intent_json,intent_digest FROM operation_intents WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        gateway_row = gateway.execute(
            "SELECT submission_json,submission_digest,state FROM gateway_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if intent_row is None and gateway_row is None:
            raise RecoveryAuditError("operation_not_found_in_either_store")

        refs: dict[str, dict] = {}
        mismatches: list[str] = []
        intent = None
        submission = None
        if intent_row is not None:
            intent = OperationIntent.from_json(intent_row[0])
            refs["operation_intent"] = {"status": "verified", "digest": intent_row[1]}
            if intent.operation_id != operation_id or intent.intent_digest != intent_row[1]:
                mismatches.append("broker_intent_projection")
                refs["operation_intent"]["status"] = "divergent"
        else:
            refs["operation_intent"] = {"status": "missing"}
        if gateway_row is not None:
            submission = GatewaySubmission.from_json(gateway_row[0])
            refs["gateway_submission"] = {
                "status": "verified", "digest": gateway_row[1], "state": gateway_row[2]
            }
            if submission.operation_id != operation_id or submission.submission_digest != gateway_row[1]:
                mismatches.append("gateway_submission_projection")
                refs["gateway_submission"]["status"] = "divergent"
        else:
            refs["gateway_submission"] = {"status": "missing"}

        if intent is not None and submission is not None:
            if (intent.intent_digest != submission.intent_digest
                    or intent.to_json() != submission.intent.to_json()):
                mismatches.append("intent_digest_or_content")
                refs["operation_intent"]["status"] = "divergent"
            snapshots = {item.kind: item for item in submission.snapshots}
            snapshot_report = {}
            for kind, snapshot_id, expected_digest in (
                ("specification", intent.specification_snapshot_id, intent.specification_digest),
                ("plan", intent.plan_snapshot_id, intent.plan_digest),
                ("checkpoint", intent.checkpoint_snapshot_id, intent.checkpoint_digest),
                ("payload", intent.payload_snapshot_id, intent.payload_digest),
            ):
                submitted = snapshots.get(kind)
                stored = broker.execute(
                    "SELECT b.canonical_digest,s.status FROM broker_snapshots b "
                    "JOIN snapshot_state s USING(snapshot_id) WHERE b.snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()
                valid = bool(submitted and stored and stored[1] == "accepted"
                             and submitted.snapshot_id == snapshot_id
                             and submitted.canonical_digest == expected_digest == stored[0])
                snapshot_report[kind] = {
                    "snapshot_ref": _id_ref(snapshot_id),
                    "digest": expected_digest,
                    "status": "verified" if valid else "missing_or_divergent",
                }
                if not valid:
                    mismatches.append("snapshot_" + kind)
            refs["accepted_snapshots"] = snapshot_report
        else:
            refs["accepted_snapshots"] = {"status": "unverified_without_both_records"}

        request_row = broker.execute(
            "SELECT r.request_id,r.request_digest,s.status FROM approval_requests r "
            "JOIN approval_request_state s USING(request_id) WHERE r.operation_id=?",
            (operation_id,),
        ).fetchone()
        decision_row = broker.execute(
            "SELECT decision_id,decision_digest FROM approval_decisions WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        authorization_row = broker.execute(
            "SELECT a.authorization_id,a.authorization_digest,s.status FROM execution_authorizations a "
            "JOIN authorization_state s USING(authorization_id) WHERE a.operation_id=?",
            (operation_id,),
        ).fetchone()
        broker_receipt = broker.execute(
            "SELECT r.receipt_id,r.receipt_digest,s.status FROM execution_receipts r "
            "JOIN execution_receipt_state s USING(operation_id) WHERE r.operation_id=?",
            (operation_id,),
        ).fetchone()
        refs["approval_request"] = ({"request_ref": _id_ref(request_row[0]), "digest": request_row[1],
                                      "state": request_row[2]} if request_row else {"status": "absent"})
        if decision_row:
            decision_contract = json.loads(broker.execute(
                "SELECT decision_json FROM approval_decisions WHERE decision_id=?", (decision_row[0],)
            ).fetchone()[0])
            refs["approval_decision"] = {
                "decision_ref": _id_ref(decision_row[0]), "digest": decision_row[1],
                "decision": decision_contract.get("decision"),
            }
        else:
            refs["approval_decision"] = {"status": "absent"}
        refs["execution_authorization"] = ({"authorization_ref": _id_ref(authorization_row[0]),
                                            "digest": authorization_row[1], "state": authorization_row[2]}
                                           if authorization_row else {"status": "absent"})
        refs["broker_execution_receipt"] = ({"receipt_ref": _id_ref(broker_receipt[0]),
                                             "digest": broker_receipt[1], "status": broker_receipt[2]}
                                            if broker_receipt else {"status": "absent"})
        if request_row is not None and intent_row is not None:
            request_contract = json.loads(broker.execute(
                "SELECT request_json FROM approval_requests WHERE request_id=?", (request_row[0],)
            ).fetchone()[0])
            if request_contract.get("intent_digest") != intent_row[1]:
                mismatches.append("approval_request_intent")
        if decision_row is not None and request_row is not None:
            decision_contract = json.loads(broker.execute(
                "SELECT decision_json FROM approval_decisions WHERE decision_id=?", (decision_row[0],)
            ).fetchone()[0])
            if (decision_contract.get("request_id") != request_row[0]
                    or decision_contract.get("request_digest") != request_row[1]):
                mismatches.append("approval_decision_request")
        if authorization_row is not None and request_row is not None:
            auth_contract = json.loads(broker.execute(
                "SELECT authorization_json FROM execution_authorizations WHERE authorization_id=?",
                (authorization_row[0],),
            ).fetchone()[0])
            if (auth_contract.get("request_id") != request_row[0]
                    or auth_contract.get("intent_digest") != (intent_row[1] if intent_row else None)):
                mismatches.append("authorization_binding")

        reservation = gateway.execute(
            "SELECT reservation_id,reservation_digest,binding_digest FROM gateway_reservations "
            "WHERE operation_id=?", (operation_id,),
        ).fetchone()
        confirmation = gateway.execute(
            "SELECT consumption_id,consumption_digest FROM gateway_authorizations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        preparation = gateway.execute(
            "SELECT preparation_digest FROM gateway_preparations WHERE operation_id=?", (operation_id,),
        ).fetchone()
        gateway_receipt = gateway.execute(
            "SELECT receipt_digest,receipt_json FROM gateway_receipts WHERE operation_id=?", (operation_id,),
        ).fetchone()
        refs["consumption_reservation"] = ({"reservation_ref": _id_ref(reservation[0]), "digest": reservation[1],
                                           "binding_digest": reservation[2], "status": "present"}
                                          if reservation else {"status": "absent"})
        refs["consumption_confirmation"] = ({"confirmation_ref": _id_ref(confirmation[0]),
                                              "digest": confirmation[1], "status": "present"}
                                             if confirmation else {"status": "absent"})
        refs["execution_preparation"] = ({"digest": preparation[0], "status": "present"}
                                         if preparation else {"status": "absent"})
        if gateway_receipt:
            receipt_body = json.loads(gateway_receipt[1])
            refs["gateway_execution_receipt"] = {
                "digest": gateway_receipt[0], "effect_outcome": receipt_body.get("status")
            }
        else:
            refs["gateway_execution_receipt"] = {"status": "absent"}

        related_ids = {operation_id}
        if intent:
            related_ids.update((intent.specification_snapshot_id, intent.plan_snapshot_id,
                                intent.checkpoint_snapshot_id, intent.payload_snapshot_id))
        for related in (request_row, decision_row, authorization_row, broker_receipt):
            if related:
                related_ids.add(related[0])
        placeholders = ",".join("?" for _ in related_ids)
        broker_events = broker.execute(
            "SELECT sequence,event_id,aggregate_type,aggregate_id,event_type,occurred_at,"
            "previous_digest,event_digest FROM broker_events WHERE aggregate_id IN (" + placeholders + ") "
            "ORDER BY sequence", tuple(sorted(related_ids)),
        ).fetchall()
        gateway_events = gateway.execute(
            "SELECT sequence,event_id,event_type,occurred_at,previous_digest,event_digest "
            "FROM gateway_events WHERE operation_id=? ORDER BY sequence", (operation_id,),
        ).fetchall()
        broker_head = broker.execute(
            "SELECT sequence,event_digest FROM broker_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        gateway_head = gateway.execute(
            "SELECT sequence,event_digest FROM gateway_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        complete = bool(intent_row and gateway_row and request_row and decision_row
                        and authorization_row and reservation and confirmation and preparation
                        and gateway_receipt)
        status = "divergent" if mismatches else "consistent" if complete else "incomplete"
        report = {
            "schema_version": 1,
            "operation_ref": _id_ref(operation_id),
            "classification": status,
            "mismatches": sorted(set(mismatches)),
            "stores": {
                "broker": {"schema_version": broker.execute("PRAGMA user_version").fetchone()[0],
                           "journal_events": [
                               {"sequence": row[0], "event_id": row[1], "aggregate_type": row[2],
                                "aggregate_ref": _id_ref(row[3]), "event_type": row[4], "occurred_at": row[5],
                                "previous_digest": row[6], "digest": row[7]} for row in broker_events
                           ],
                           "chain_head": {"sequence": broker_head[0], "digest": broker_head[1]}
                           if broker_head else None},
                "gateway": {"schema_version": gateway.execute("PRAGMA user_version").fetchone()[0],
                            "journal_events": [
                                {"sequence": row[0], "event_id": row[1], "event_type": row[2],
                                 "occurred_at": row[3], "previous_digest": row[4], "digest": row[5]}
                                for row in gateway_events
                            ],
                            "chain_head": {"sequence": gateway_head[0], "digest": gateway_head[1]}
                            if gateway_head else None},
            },
            "references": refs,
            "execution_effect_validation": "not_proven_by_this_report",
            "external_audit_anchor": "unavailable",
        }
        data = json.dumps(report, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > MAX_EXPORT_BYTES:
            raise RecoveryAuditError("audit_export_size_limit_exceeded")
        return data
    except RecoveryAuditError:
        raise
    except Exception as exc:
        raise RecoveryAuditError("store_integrity_or_linkage_check_failed") from exc


def write_recovery_export(broker_store, gateway_store, operation_id: str,
                          destination: str | Path) -> Path:
    """Write a redacted export atomically to a new path, never replacing user data."""
    target = Path(destination).expanduser().resolve()
    if (any(part.casefold() == ".agentflow" for part in target.parts)
            or target in {Path(broker_store.path).resolve(), Path(gateway_store.path).resolve()}):
        raise RecoveryAuditError("unsafe_export_destination")
    data = build_recovery_export(broker_store, gateway_store, operation_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp-" + uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target)
        temporary.unlink()
    except FileExistsError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RecoveryAuditError("export_destination_exists") from exc
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target
