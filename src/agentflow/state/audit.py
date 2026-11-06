"""Read-only, secret-minimizing inspection of the Phase 7 state database."""

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from agentflow.planning import digest

from .contracts import IntegrityError


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MONEY = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
_ROUTING_REASONS = {"policy_disabled", "latency_budget_unverifiable_from_catalog",
                    "cost_budget_cannot_be_estimated_without_token_budget",
                    "no_verified_sufficient_model", "lowest_declared_cost_within_known_capabilities"}
_REJECTION_REASONS = {"model_unavailable_or_stale", "context_capacity_unknown_or_insufficient",
                      "required_capability_unverified", "advanced_quality_not_verified",
                      "cost_metadata_unavailable", "estimated_cost_exceeds_budget",
                      "eligible_but_ranked_lower"}


def _safe_id(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else "<invalid-id>"


def _safe_digest(value):
    return value if isinstance(value, str) and _DIGEST.fullmatch(value) else "<invalid-digest>"


def _safe_oid(value):
    return value if isinstance(value, str) and _GIT_OID.fullmatch(value) else "<invalid-object-id>"


def _safe_budget(value):
    if not isinstance(value, dict):
        return {"status": "unavailable"}
    status = value.get("status") if value.get("status") in {"applied_verified", "estimated", "unavailable"} else "unavailable"
    cost = value.get("cost_estimate")
    currency = value.get("currency")
    constraints = value.get("constraints", [])
    return {"status": status,
            "cost_estimate": cost if isinstance(cost, str) and _MONEY.fullmatch(cost) else None,
            "currency": currency if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) else None,
            "token_limit": value.get("token_limit") if type(value.get("token_limit")) is int and value.get("token_limit") >= 0 else None,
            "time_limit_seconds": value.get("time_limit_seconds") if type(value.get("time_limit_seconds")) is int and value.get("time_limit_seconds") >= 0 else None,
            "constraints": [_safe_id(item) for item in constraints if isinstance(item, str)]
            if isinstance(constraints, list) else []}


def _tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _records(db, tables, table, query):
    if table not in tables:
        return []
    return [dict(row) for row in db.execute(query)]


def inspect_phase7_database(path: Path) -> dict:
    """Inspect supported state using SQLite read-only + immutable mode; never migrate/write.

    WAL-backed databases are refused because immutable mode would omit committed WAL
    content and ordinary read-only WAL access may create shared-memory sidecars.
    """
    path = Path(path)
    if not path.is_file():
        raise IntegrityError("banco operacional não existe")
    if path.with_name(path.name + "-wal").exists():
        raise IntegrityError("WAL presente; feche os escritores e faça checkpoint pelo fluxo operacional antes da inspeção")
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(1, 8):
                raise IntegrityError("versão SQLite incompatível")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise IntegrityError("integridade SQLite inválida")
            tables = _tables(connection)
            if "schema_migrations" in tables:
                migrations = [item[0] for item in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version")]
                if migrations != list(range(1, version + 1)):
                    raise IntegrityError("histórico de migrações divergente")

            report = {
                "schema_version": version,
                "integrity": {"integrity_check": integrity,
                              "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall())},
                "specification_revisions": [], "plan_revisions": [], "requirement_coverage": [],
                "change_impacts": [], "catalog": [], "routing_decisions": [],
                "budget_accounts": [], "budget_reservations": [], "budget_ledger": [],
                "escalation_proposals": [], "task_states": [], "evidence_links": [], "audit_events": [],
                "unknowns_and_pending": [], "unavailable_sections": [],
            }

            def unavailable(table):
                if table not in tables:
                    report["unavailable_sections"].append(table)

            for table in ("specification_revisions", "plan_revisions", "change_impacts",
                          "model_catalog_snapshots", "routing_decisions", "budget_accounts",
                          "budget_reservations", "budget_ledger", "escalation_proposals",
                          "sdd_events", "evidence", "validation_runs", "validation_gate_results", "tasks"):
                unavailable(table)

            for row in _records(connection, tables, "specification_revisions",
                    "SELECT revision_id,project_id,sequence,schema_version,snapshot_json,spec_digest,status,created_at "
                    "FROM specification_revisions ORDER BY project_id,sequence"):
                try:
                    snapshot = json.loads(row["snapshot_json"])
                    valid = digest(snapshot) == row["spec_digest"]
                except (ValueError, TypeError):
                    valid = False
                item = {"revision_id": _safe_id(row["revision_id"]), "project_id": _safe_id(row["project_id"]),
                        "sequence": row["sequence"], "schema_version": row["schema_version"],
                        "digest": _safe_digest(row["spec_digest"]), "status": row["status"],
                        "created_at": row["created_at"], "digest_valid": valid}
                report["specification_revisions"].append(item)
                if not valid:
                    report["unknowns_and_pending"].append({"kind": "invalid_specification_digest",
                                                            "id": item["revision_id"]})
                if row["status"] == "proposed":
                    report["unknowns_and_pending"].append({"kind": "specification_proposed",
                                                            "id": item["revision_id"]})

            for row in _records(connection, tables, "plan_revisions",
                    "SELECT plan_revision_id,project_id,sequence,specification_revision_id,spec_digest,tasks_digest,snapshot_json,trace_json,status,created_at "
                    "FROM plan_revisions ORDER BY project_id,sequence"):
                item = {"plan_revision_id": _safe_id(row["plan_revision_id"]),
                        "project_id": _safe_id(row["project_id"]), "sequence": row["sequence"],
                        "specification_revision_id": _safe_id(row["specification_revision_id"]),
                        "spec_digest": _safe_digest(row["spec_digest"]),
                        "tasks_digest": _safe_digest(row["tasks_digest"]), "status": row["status"],
                        "created_at": row["created_at"]}
                try:
                    trace = json.loads(row["trace_json"])
                    snapshot = json.loads(row["snapshot_json"])
                    plan_data = snapshot["plan"]
                    tasks = plan_data["tasks"]
                    task_hash = digest(tasks) if row["status"] == "legacy_unmapped" else digest({
                        "tasks": tasks, "deferred_requirements": plan_data.get("deferred_requirements", [])})
                    spec_valid = digest(snapshot["spec"]) == row["spec_digest"]
                    plan_valid = task_hash == row["tasks_digest"]
                    item["snapshot_digests_valid"] = spec_valid and plan_valid
                    if not spec_valid or not plan_valid:
                        report["unknowns_and_pending"].append({"kind": "invalid_plan_snapshot_digest",
                                                                "id": item["plan_revision_id"]})
                    for requirement_id, links in trace.items():
                        report["requirement_coverage"].append({
                            "plan_revision_id": item["plan_revision_id"],
                            "requirement_id": _safe_id(requirement_id), "status": "covered",
                            "links": [{"task_id": _safe_id(link.get("task_id")),
                                       "criterion_id": _safe_id(link.get("criterion_id"))}
                                      for link in links if isinstance(link, dict)]})
                    deferred = plan_data.get("deferred_requirements", [])
                    accounted = set(trace) | {value.get("id") for value in deferred}
                    for requirement in deferred:
                        report["requirement_coverage"].append({
                            "plan_revision_id": item["plan_revision_id"],
                            "requirement_id": _safe_id(requirement.get("id")), "status": "deferred",
                            "rationale_digest": _safe_digest(digest(requirement.get("rationale", "")))})
                    for requirement in snapshot["spec"].get("acceptance_criteria", []):
                        if requirement.get("id") not in accounted:
                            report["requirement_coverage"].append({
                                "plan_revision_id": item["plan_revision_id"],
                                "requirement_id": _safe_id(requirement.get("id")),
                                "status": "unmapped", "links": []})
                            report["unknowns_and_pending"].append({
                                "kind": "requirement_unmapped",
                                "id": _safe_id(requirement.get("id"))})
                    if row["status"] in {"proposed", "legacy_unmapped"}:
                        report["unknowns_and_pending"].append({"kind": "plan_" + row["status"],
                                                                "id": item["plan_revision_id"]})
                except (ValueError, TypeError, AttributeError):
                    item["snapshot_digests_valid"] = False
                    report["unknowns_and_pending"].append({"kind": "invalid_requirement_trace",
                                                            "id": item["plan_revision_id"]})
                report["plan_revisions"].append(item)

            for row in _records(connection, tables, "change_impacts",
                    "SELECT impact_id,project_id,source_plan_revision_id,target_plan_revision_id,impact_json,impact_digest,created_at FROM change_impacts ORDER BY created_at,impact_id"):
                try:
                    impact = json.loads(row["impact_json"])
                    if digest({"inventory": json.loads(connection.execute(
                            "SELECT inventory_json FROM change_impacts WHERE impact_id=?", (row["impact_id"],)).fetchone()[0]),
                            "impact": impact,
                            "target_spec_digest": connection.execute(
                                "SELECT spec_digest FROM plan_revisions WHERE plan_revision_id=?",
                                (row["target_plan_revision_id"],)).fetchone()[0],
                            "target_tasks_digest": connection.execute(
                                "SELECT tasks_digest FROM plan_revisions WHERE plan_revision_id=?",
                                (row["target_plan_revision_id"],)).fetchone()[0]}) != row["impact_digest"]:
                        raise ValueError("impact digest mismatch")
                    valid = True
                except (ValueError, TypeError, sqlite3.DatabaseError):
                    impact, valid = {}, False
                report["change_impacts"].append({"impact_id": _safe_id(row["impact_id"]),
                    "project_id": _safe_id(row["project_id"]),
                    "source_plan_revision_id": _safe_id(row["source_plan_revision_id"]) if row["source_plan_revision_id"] else None,
                    "target_plan_revision_id": _safe_id(row["target_plan_revision_id"]),
                    "changed_requirements": [_safe_id(value) for value in impact.get("changed_requirements", [])],
                    "added_tasks": [_safe_id(value) for value in impact.get("added_tasks", [])],
                    "changed_tasks": [_safe_id(value) for value in impact.get("changed_tasks", [])],
                    "removed_tasks": [_safe_id(value) for value in impact.get("removed_tasks", [])],
                    "affected_tasks": [_safe_id(value) for value in impact.get("affected_tasks", [])],
                    "disposition": impact.get("disposition", "unknown"),
                    "digest": _safe_digest(row["impact_digest"]), "digest_valid": valid,
                    "created_at": row["created_at"]})
                if not valid:
                    report["unknowns_and_pending"].append({"kind": "invalid_change_impact_digest",
                                                            "id": _safe_id(row["impact_id"])})

            for row in _records(connection, tables, "model_catalog_snapshots",
                    "SELECT catalog_digest,snapshot_json,source,created_at FROM model_catalog_snapshots ORDER BY created_at,catalog_digest"):
                try:
                    catalog = json.loads(row["snapshot_json"])
                    valid = digest(catalog) == row["catalog_digest"]
                    models = [{"provider_id": _safe_id(entry["descriptor"].get("provider_id")),
                               "model_id": _safe_id(entry["descriptor"].get("model_id")),
                               "revision": _safe_id(entry["descriptor"].get("revision")),
                               "availability": entry["descriptor"].get("availability")
                                   if entry["descriptor"].get("availability") in {"available", "unavailable", "unknown"}
                                   else "unknown",
                               "verified_capabilities": [_safe_id(value) for value in entry.get("verified_capabilities", [])],
                               "unknown_capabilities": [_safe_id(value) for value in entry.get("unknown_capabilities", [])]}
                              for entry in catalog.get("models", [])]
                except (ValueError, TypeError, KeyError):
                    valid, models = False, []
                report["catalog"].append({"catalog_digest": _safe_digest(row["catalog_digest"]),
                    "models": models, "digest_valid": valid, "created_at": row["created_at"]})
                if not valid:
                    report["unknowns_and_pending"].append({"kind": "invalid_catalog_digest",
                                                            "id": _safe_digest(row["catalog_digest"])})

            for row in _records(connection, tables, "routing_decisions",
                    "SELECT decision_id,project_id,plan_revision_id,task_id,classification_json,catalog_digest,policy_digest,policy_snapshot_json,decision_json,decision_digest,specification_digest,specification_snapshot_json,budget_snapshot_json,created_at FROM routing_decisions ORDER BY created_at,decision_id"):
                try:
                    decision = json.loads(row["decision_json"])
                    classification = json.loads(row["classification_json"])
                    policy = json.loads(row["policy_snapshot_json"])
                    specification = json.loads(row["specification_snapshot_json"])
                    budget = json.loads(row["budget_snapshot_json"])
                    payload = {"specification_digest": row["specification_digest"],
                               "specification_snapshot": specification, "classification": classification,
                               "catalog_digest": row["catalog_digest"], "policy_digest": row["policy_digest"],
                               "policy_snapshot": policy, "budget_snapshot": budget,
                               "decision": decision}
                    valid = (digest(specification) == row["specification_digest"]
                             and digest(policy) == row["policy_digest"]
                             and digest(payload) == row["decision_digest"])
                except (ValueError, TypeError):
                    decision, valid = {}, False
                report["routing_decisions"].append({"decision_id": _safe_id(row["decision_id"]),
                    "project_id": _safe_id(row["project_id"]),
                    "plan_revision_id": _safe_id(row["plan_revision_id"]), "task_id": _safe_id(row["task_id"]),
                    "selection": {"provider_id": _safe_id(decision["provider_id"]) if decision.get("provider_id") else None,
                                  "model_id": _safe_id(decision["model_id"]) if decision.get("model_id") else None},
                    "reason": decision.get("reason") if decision.get("reason") in _ROUTING_REASONS else "unknown",
                    "verified_capabilities": [_safe_id(value) for value in decision.get("verified_capabilities", [])],
                    "rejected_alternatives": [{"provider_id": _safe_id(item[0]), "model_id": _safe_id(item[1]),
                                                "reason": item[2] if item[2] in _REJECTION_REASONS else "unknown"}
                                               for item in decision.get("rejected_alternatives", [])
                                               if isinstance(item, list) and len(item) == 3],
                    "catalog_digest": _safe_digest(row["catalog_digest"]),
                    "policy_digest": _safe_digest(row["policy_digest"]),
                    "specification_digest": _safe_digest(row["specification_digest"]),
                    "budget_snapshot": _safe_budget(budget), "decision_digest": _safe_digest(row["decision_digest"]),
                    "digest_valid": valid, "dispatch_allowed": False, "created_at": row["created_at"]})
                if not valid:
                    report["unknowns_and_pending"].append({"kind": "invalid_routing_decision_digest",
                                                            "id": _safe_id(row["decision_id"])})

            if "budget_accounts" in tables:
                for row in connection.execute("SELECT account_id,project_id,scope_type,scope_id,currency,cost_limit,token_limit,time_limit_seconds,cost_status,token_status,time_status,snapshot_json,snapshot_digest FROM budget_accounts ORDER BY created_at,account_id"):
                    try:
                        valid = digest(json.loads(row[11])) == row[12]
                    except (ValueError, TypeError):
                        valid = False
                    report["budget_accounts"].append({"account_id": _safe_id(row[0]),
                        "project_id": _safe_id(row[1]), "scope_type": row[2], "scope_id": _safe_id(row[3]),
                        "currency": row[4], "limits": {"cost": row[5], "tokens": row[6], "seconds": row[7]},
                        "enforcement": {"cost": row[8], "tokens": row[9], "time": row[10]},
                        "snapshot_digest": _safe_digest(row[12]), "digest_valid": valid,
                        "provider_invoice_enforcement": "unavailable"})
                for row in connection.execute("SELECT reservation_id,project_id,task_id,attempt_id,execution_id,currency,status,cost_state,tokens_state,time_state,created_at FROM budget_reservations ORDER BY created_at,reservation_id"):
                    item = {"reservation_id": _safe_id(row[0]), "project_id": _safe_id(row[1]),
                        "task_id": _safe_id(row[2]), "attempt_id": _safe_id(row[3]) if row[3] else None,
                        "execution_id": _safe_id(row[4]), "currency": row[5], "status": row[6],
                        "measurement": {"cost": row[7], "tokens": row[8], "time": row[9]},
                        "created_at": row[10]}
                    report["budget_reservations"].append(item)
                    if row[6] in {"unknown", "reserved"}:
                        report["unknowns_and_pending"].append({"kind": "budget_" + row[6],
                                                                "id": item["reservation_id"]})
                for row in connection.execute("SELECT event_id,operation_id,reservation_id,account_id,kind,cost_delta,tokens_delta,seconds_delta,request_digest,occurred_at FROM budget_ledger ORDER BY event_id"):
                    report["budget_ledger"].append({"event_id": row[0], "operation_id": _safe_id(row[1]),
                        "reservation_id": _safe_id(row[2]), "account_id": _safe_id(row[3]), "kind": row[4],
                        "usage_delta": {"cost": row[5], "tokens": row[6], "seconds": row[7]},
                        "request_digest": _safe_digest(row[8]), "occurred_at": row[9]})

            if "escalation_proposals" in tables:
                for row in connection.execute("SELECT proposal_id,operation_id,project_id,plan_revision_id,task_id,source_attempt_id,source_provider_id,source_model_id,target_provider_id,target_model_id,evidence_ids_json,evidence_digest,reason_code,currency,source_cost_estimate,target_cost_estimate,cost_delta_estimate,budget_status,authorization_required,expires_at,status FROM escalation_proposals ORDER BY created_at,proposal_id"):
                    try:
                        evidence_ids = [_safe_id(value) for value in json.loads(row[10])]
                    except (ValueError, TypeError):
                        evidence_ids = []
                    report["escalation_proposals"].append({"proposal_id": _safe_id(row[0]),
                        "operation_id": _safe_id(row[1]), "project_id": _safe_id(row[2]),
                        "plan_revision_id": _safe_id(row[3]), "task_id": _safe_id(row[4]),
                        "source_attempt_id": _safe_id(row[5]),
                        "source_model": [_safe_id(row[6]), _safe_id(row[7])],
                        "target_model": [_safe_id(row[8]), _safe_id(row[9])],
                        "evidence_ids": evidence_ids, "evidence_digest": _safe_digest(row[11]),
                        "reason": row[12], "currency": row[13],
                        "cost_estimates": {"source": row[14], "target": row[15], "delta": row[16]},
                        "budget_status": row[17], "authorization_required": bool(row[18]),
                        "expires_at": row[19], "status": row[20]})
                    if row[20] in {"proposed", "blocked"}:
                        report["unknowns_and_pending"].append({"kind": "escalation_" + row[20],
                                                                "id": _safe_id(row[0])})

            if "evidence" in tables:
                for row in connection.execute("SELECT evidence_id,project_id,task_id,definition_revision,criterion_id,kind,passed,recorded_at FROM evidence ORDER BY evidence_id"):
                    report["evidence_links"].append({"evidence_id": row[0], "project_id": _safe_id(row[1]),
                        "task_id": _safe_id(row[2]), "definition_revision": row[3],
                        "criterion_id": _safe_id(row[4]), "kind": row[5],
                        "passed": bool(row[6]), "recorded_at": row[7]})
            if "tasks" in tables:
                for row in connection.execute("SELECT task_id,status,definition_revision,revision,attempt_count,revalidation_required FROM tasks ORDER BY task_id"):
                    item = {"task_id": _safe_id(row[0]), "status": row[1],
                        "definition_revision": row[2], "revision": row[3],
                        "attempt_count": row[4], "revalidation_required": bool(row[5])}
                    report["task_states"].append(item)
                    if row[1] in {"blocked", "ready"} or row[5]:
                        report["unknowns_and_pending"].append({"kind": "task_" + row[1] +
                            ("_revalidation_required" if row[5] else ""), "id": item["task_id"]})
            if "validation_gate_results" in tables:
                for row in connection.execute("SELECT g.gate_result_id,r.validation_run_id,r.task_id,r.definition_revision,r.checkpoint_operation_id,r.commit_oid,r.tree_oid,g.gate_id,g.criterion_ids_json,g.snapshot_digest,g.validator_digest,g.status FROM validation_gate_results g JOIN validation_runs r ON r.validation_run_id=g.validation_run_id ORDER BY r.created_at,g.gate_id"):
                    try:
                        criteria = [_safe_id(value) for value in json.loads(row[8])]
                    except (ValueError, TypeError):
                        criteria = []
                    report["evidence_links"].append({"gate_result_id": _safe_id(row[0]),
                        "validation_run_id": _safe_id(row[1]), "task_id": _safe_id(row[2]),
                        "definition_revision": row[3], "checkpoint_operation_id": _safe_id(row[4]),
                        "checkpoint": {"commit_oid": _safe_oid(row[5]), "tree_oid": _safe_oid(row[6])},
                        "gate_id": _safe_id(row[7]), "criterion_ids": criteria,
                        "snapshot_digest": _safe_digest(row[9]), "validator_digest": _safe_digest(row[10]),
                        "status": row[11]})
            if "sdd_events" in tables:
                report["audit_events"] = [{"event_id": row[0], "project_id": _safe_id(row[1]),
                    "kind": _safe_id(row[2]), "subject_id": _safe_id(row[3]),
                    "operation_id": _safe_id(row[4]), "occurred_at": row[5]}
                    for row in connection.execute("SELECT event_id,project_id,kind,subject_id,operation_id,occurred_at FROM sdd_events ORDER BY event_id")]

            for table, query, kind_index, id_index in (
                    ("execution_attempts", "SELECT attempt_id,status FROM execution_attempts WHERE status IN ('prepared','running','interrupted') ORDER BY prepared_at", 1, 0),
                    ("git_operations", "SELECT operation_id,status FROM git_operations WHERE status IN ('prepared','running','unknown') ORDER BY created_at", 1, 0),
                    ("validation_runs", "SELECT validation_run_id,status FROM validation_runs WHERE status IN ('requested','preflight','running','inconclusive','cancelled') ORDER BY created_at", 1, 0)):
                if table in tables:
                    report["unknowns_and_pending"].extend({"kind": kind + "_" + row[kind_index],
                        "id": _safe_id(row[id_index])} for row in connection.execute(query))
            if "agent_model_observations" in tables:
                report["unknowns_and_pending"].extend({"kind": "model_identity_" + row[1],
                    "id": _safe_id(row[0])} for row in connection.execute(
                        "SELECT attempt_id,status FROM agent_model_observations WHERE status!='confirmed' ORDER BY observed_at"))
            report["unavailable_sections"].sort()
            report["unknowns_and_pending"].sort(key=lambda item: (item["kind"], item["id"]))
            return report
    except sqlite3.DatabaseError as exc:
        raise IntegrityError(f"não foi possível inspecionar o banco sem escrita: {exc}") from exc
