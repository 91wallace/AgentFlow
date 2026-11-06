"""Proposal-only SDD revisions, deterministic impact, catalogue and routing audit."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import json
import re
import sqlite3

from agentflow.agents.models import ModelCatalogEntry, ModelDescriptor, ProviderDescriptor
from agentflow.planning import Plan, canonical, digest
from agentflow.planning.loader import specification_data, task_data

from .sqlite import SQLiteStore, now
from .contracts import ConflictError, IntegrityError


@dataclass(frozen=True)
class RevisionProposal:
    specification_revision_id: str
    plan_revision_id: str
    change_impact_id: str
    spec_digest: str
    tasks_digest: str
    impact_digest: str
    status: str


@dataclass(frozen=True)
class ChangeImpact:
    changed_requirements: tuple[str, ...]
    added_tasks: tuple[str, ...]
    changed_tasks: tuple[str, ...]
    removed_tasks: tuple[str, ...]
    affected_tasks: tuple[str, ...]
    disposition: str


@dataclass(frozen=True)
class EscalationProposal:
    proposal_id: str
    operation_id: str
    source_provider_id: str
    source_model_id: str
    target_provider_id: str
    target_model_id: str
    evidence_digest: str
    currency: str
    source_cost_estimate: str
    target_cost_estimate: str
    cost_delta_estimate: str
    authorization_required: bool
    expires_at: str
    status: str
    project_id: str = ""
    task_id: str = ""
    plan_revision_id: str = ""
    source_attempt_id: str = ""
    evidence_ids: tuple[str, ...] = ()
    reason_code: str = "verified_validation_gate_failure"
    budget_status: str = "estimated"


class SDDStore(SQLiteStore):
    """SQLite v6 facade. Proposals are append-only; there is no local approval path."""

    def __init__(self, path):
        super().__init__(path, execution=True, git=True, validation=True, agent_models=True, sdd=True)

    def propose(self, plan: Plan, operation_id: str) -> RevisionProposal:
        if not operation_id or len(operation_id) > 200:
            raise ConflictError("operation_id inválido")
        project_id = plan.specification.project_id
        spec_snapshot = canonical(specification_data(plan.specification))
        task_snapshot = canonical({"tasks": [task_data(task) for task in plan.tasks],
                                   "deferred_requirements": [{"id": item.id, "rationale": item.description}
                                                              for item in plan.deferred_requirements]})
        request_hash = digest({"project_id": project_id, "spec_digest": plan.spec_digest,
                               "tasks_digest": plan.tasks_digest})
        with self.transaction() as db:
            prior = db.execute("SELECT subject_id,payload_json FROM sdd_events WHERE operation_id=?", (operation_id,)).fetchone()
            if prior:
                event = json.loads(prior[1])
                if event["request_digest"] != request_hash:
                    raise ConflictError("operation_id reutilizado com parâmetros diferentes")
                return RevisionProposal(**event["result"])
            if db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() is None:
                raise IntegrityError("projeto deve ser importado antes de propor uma revisão SDD")
            previous = db.execute("SELECT plan_revision_id,snapshot_json,trace_json,sequence FROM plan_revisions WHERE project_id=? ORDER BY sequence DESC LIMIT 1", (project_id,)).fetchone()
            seq = (previous[3] + 1) if previous else 1
            spec_seq = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM specification_revisions WHERE project_id=?", (project_id,)).fetchone()[0]
            spec_id, plan_id, impact_id = uuid4().hex, uuid4().hex, uuid4().hex
            trace = {}
            for task in plan.tasks:
                for criterion in task.acceptance_criteria:
                    for requirement in criterion.satisfies:
                        trace.setdefault(requirement, []).append({"task_id": task.id, "criterion_id": criterion.id})
            trace = {key: sorted(value, key=lambda item: (item["task_id"], item["criterion_id"])) for key, value in sorted(trace.items())}
            is_legacy = plan.tasks_schema_version < 4
            plan_status = "legacy_unmapped" if is_legacy else "proposed"
            db.execute("INSERT INTO specification_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (spec_id, project_id, spec_seq, plan.spec_schema_version, spec_snapshot, plan.spec_digest,
                        None, "proposed", operation_id + ":spec", now()))
            db.execute("INSERT INTO plan_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (plan_id, project_id, seq, spec_id, plan.spec_digest, plan.tasks_digest,
                        canonical({"spec": json.loads(spec_snapshot), "plan": json.loads(task_snapshot)}),
                        canonical(trace), plan_status, operation_id + ":plan", now()))
            impact = self._impact(previous, plan, trace)
            inventory = {"source_plan_revision_id": previous[0] if previous else None,
                         "source_plan_snapshot_digest": digest(json.loads(previous[1])) if previous else None}
            impact_payload = asdict(impact)
            impact_hash = digest({"inventory": inventory, "impact": impact_payload,
                                  "target_spec_digest": plan.spec_digest, "target_tasks_digest": plan.tasks_digest})
            db.execute("INSERT INTO change_impacts VALUES(?,?,?,?,?,?,?,?,?)",
                       (impact_id, project_id, previous[0] if previous else None, plan_id,
                        canonical(inventory), canonical(impact_payload), impact_hash, operation_id + ":impact", now()))
            result = RevisionProposal(spec_id, plan_id, impact_id, plan.spec_digest,
                                      plan.tasks_digest, impact_hash, plan_status)
            event = {"request_digest": request_hash, "result": asdict(result)}
            db.execute("INSERT INTO sdd_events(project_id,kind,subject_id,payload_json,occurred_at,operation_id) VALUES(?,?,?,?,?,?)",
                       (project_id, "revision_proposed", plan_id, canonical(event), now(), operation_id))
            return result

    @staticmethod
    def _impact(previous, plan: Plan, trace: dict) -> ChangeImpact:
        current_tasks = {task.id: task_data(task) for task in plan.tasks}
        current_requirements = {item.id for item in plan.specification.acceptance_criteria}
        if previous is None:
            return ChangeImpact(tuple(sorted(current_requirements)), tuple(sorted(current_tasks)), (), (),
                                tuple(sorted(current_tasks)), "initial")
        old = json.loads(previous[1])
        old_plan = old.get("plan", {"tasks": old.get("tasks", []), "deferred_requirements": []})
        old_tasks = {item["id"]: item for item in old_plan["tasks"]}
        old_spec_criteria = {item["id"]: item["description"]
                             for item in old["spec"].get("acceptance_criteria", ())}
        new_spec_criteria = {item.id: item.description for item in plan.specification.acceptance_criteria}
        old_deferred = {item["id"]: item["rationale"] for item in old_plan.get("deferred_requirements", [])}
        new_deferred = {item.id: item.description for item in plan.deferred_requirements}
        changed_reqs = tuple(sorted(key for key in (set(old_spec_criteria) | set(new_spec_criteria)
                                                       | set(old_deferred) | set(new_deferred))
                                    if (old_spec_criteria.get(key), old_deferred.get(key)) !=
                                       (new_spec_criteria.get(key), new_deferred.get(key))))
        added = set(current_tasks) - set(old_tasks)
        removed = set(old_tasks) - set(current_tasks)
        changed = {key for key in current_tasks.keys() & old_tasks.keys() if current_tasks[key] != old_tasks[key]}
        affected = added | removed | changed
        if changed_reqs:
            affected.update(current_tasks)
        # Dependents are included transitively, even if their payload is unchanged.
        old_tasks_by_id = {item["id"]: item for item in old_plan["tasks"]}
        changed_flag = True
        while changed_flag:
            changed_flag = False
            for task in plan.tasks:
                if task.id not in affected and set(task.depends_on) & affected:
                    affected.add(task.id)
                    changed_flag = True
            for task_id, old_task in old_tasks_by_id.items():
                if task_id not in affected and set(old_task.get("depends_on", ())) & affected:
                    affected.add(task_id)
                    changed_flag = True
        return ChangeImpact(changed_reqs, tuple(sorted(added)), tuple(sorted(changed)),
                            tuple(sorted(removed)), tuple(sorted(affected)),
                            "unknown" if any(not item.satisfies for task in plan.tasks for item in task.acceptance_criteria) else "revalidate")

    def record_catalog(self, providers: tuple[ProviderDescriptor, ...], models: tuple[ModelCatalogEntry, ...], source: str) -> str:
        safe_label = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
        if not safe_label.fullmatch(source):
            raise ValueError("catalog source must be a bounded non-secret identifier")
        for provider in providers:
            if not safe_label.fullmatch(provider.source):
                raise ValueError("provider source must be a bounded non-secret identifier")
        for entry in models:
            descriptor = entry.descriptor
            if not safe_label.fullmatch(descriptor.source) or (descriptor.cost_source and not safe_label.fullmatch(descriptor.cost_source)):
                raise ValueError("model provenance must use bounded non-secret identifiers")
            if entry.verification_source and not safe_label.fullmatch(entry.verification_source):
                raise ValueError("capability provenance must be a bounded non-secret identifier")
        payload = {"schema_version": 1, "source": source,
                   "providers": [asdict(item) for item in sorted(providers, key=lambda x: x.provider_id)],
                   "models": [{"descriptor": asdict(item.descriptor),
                               "verified_capabilities": list(sorted(item.verified_capabilities)),
                               "unknown_capabilities": list(item.unknown_capabilities),
                               "verification_source": item.verification_source}
                              for item in sorted(models, key=lambda x: (x.descriptor.provider_id,
                                                                       x.descriptor.model_id))]}
        catalog_digest = digest(payload)
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO model_catalog_snapshots VALUES(?,?,?,?,?)",
                       (catalog_digest, 1, canonical(payload), source, now()))
        return catalog_digest

    def record_routing_decision(self, decision_id: str, project_id: str, plan_revision_id: str,
                                task_id: str, classification: dict, catalog_digest: str,
                                policy_digest: str, decision: dict, policy_snapshot: dict,
                                budget_snapshot: dict) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", decision_id):
            raise ValueError("decision_id must be a bounded stable identifier")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", catalog_digest) or not re.fullmatch(r"sha256:[0-9a-f]{64}", policy_digest):
            raise ValueError("catalog/policy digest is invalid")
        if not isinstance(policy_snapshot, dict) or digest(policy_snapshot) != policy_digest:
            raise ValueError("policy snapshot and digest diverge")
        if (not isinstance(budget_snapshot, dict)
                or budget_snapshot.get("status") not in {"applied_verified", "estimated", "unavailable"}
                or set(budget_snapshot) - {"status", "currency", "cost_estimate", "token_limit",
                                           "time_limit_seconds", "constraints"}):
            raise ValueError("budget prediction snapshot invalid")
        if set(classification) - {"complexity", "risk", "source", "reasons", "required_capabilities"}:
            raise ValueError("classification contains unapproved fields")
        if set(decision) - {"status", "provider_id", "model_id", "reason", "alternatives", "budget_status",
                            "verified_capabilities", "rejected_alternatives", "constraints",
                            "budget_prediction", "authorization_required", "dispatch_allowed"}:
            raise ValueError("routing decision contains unapproved fields")
        if classification.get("complexity") not in {"simple", "standard", "complex"} or classification.get("risk") not in {"low", "medium", "high"}:
            raise ValueError("classification values are invalid")
        if classification.get("source") not in {"declared", "deterministic_rule"}:
            raise ValueError("classification source is invalid")
        if any(not isinstance(classification.get(name, []), (tuple, list))
               for name in ("reasons", "required_capabilities")):
            raise ValueError("classification lists are invalid")
        safe_value = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
        for label in (*classification.get("reasons", ()), *classification.get("required_capabilities", ())):
            if not isinstance(label, str) or not safe_value.fullmatch(label):
                raise ValueError("classification provenance must be bounded identifiers")
        if decision.get("status") not in {"blocked", "proposal", "same_selection"}:
            raise ValueError("routing decision status is invalid")
        if decision.get("reason") not in {"policy_disabled", "latency_budget_unverifiable_from_catalog",
                "cost_budget_cannot_be_estimated_without_token_budget", "no_verified_sufficient_model",
                "lowest_declared_cost_within_known_capabilities"}:
            raise ValueError("routing decision reason is not a controlled code")
        if decision.get("budget_status", "unavailable") not in {"applied_verified", "estimated", "unavailable"}:
            raise ValueError("budget status is invalid")
        for key in ("provider_id", "model_id"):
            value = decision.get(key, "")
            if value and not safe_value.fullmatch(value):
                raise ValueError("selected model identity is invalid")
        alternatives = decision.get("alternatives", ())
        if not isinstance(alternatives, (list, tuple)) or len(alternatives) > 100:
            raise ValueError("routing alternatives are invalid")
        for pair in alternatives:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                    or any(not isinstance(value, str) or not safe_value.fullmatch(value) for value in pair)):
                raise ValueError("routing alternative identity is invalid")
        rejected = decision.get("rejected_alternatives", ())
        valid_reasons = {"model_unavailable_or_stale", "context_capacity_unknown_or_insufficient",
                         "required_capability_unverified", "advanced_quality_not_verified",
                         "cost_metadata_unavailable", "estimated_cost_exceeds_budget",
                         "eligible_but_ranked_lower"}
        if not isinstance(rejected, (list, tuple)) or len(rejected) > 100:
            raise ValueError("rejected alternatives invalid")
        for item in rejected:
            if (not isinstance(item, (list, tuple)) or len(item) != 3
                    or not safe_value.fullmatch(item[0]) or not safe_value.fullmatch(item[1])
                    or item[2] not in valid_reasons):
                raise ValueError("rejected alternative lacks a controlled reason")
        verified_caps = decision.get("verified_capabilities", ())
        if (not isinstance(verified_caps, (list, tuple))
                or any(not isinstance(item, str) or not safe_value.fullmatch(item) for item in verified_caps)):
            raise ValueError("verified capability list invalid")
        if len(canonical(classification).encode("utf-8")) > 4096 or len(canonical(decision).encode("utf-8")) > 12 * 1024:
            raise ValueError("routing audit payload exceeds limit")
        with self.transaction() as db:
            plan_row = db.execute("SELECT status,trace_json,snapshot_json,spec_digest FROM plan_revisions WHERE plan_revision_id=? AND project_id=?",
                                  (plan_revision_id, project_id)).fetchone()
            if plan_row is None or plan_row[0] == "legacy_unmapped":
                raise ConflictError("roteamento preview exige revisão com rastreabilidade explícita")
            plan_snapshot = json.loads(plan_row[2])
            if task_id not in {item["id"] for item in plan_snapshot["plan"]["tasks"]}:
                raise ConflictError("tarefa não pertence à revisão do plano")
            catalog_row = db.execute("SELECT snapshot_json FROM model_catalog_snapshots WHERE catalog_digest=?",
                                     (catalog_digest,)).fetchone()
            if catalog_row is None:
                raise ConflictError("catalog snapshot does not exist")
            catalog_snapshot = json.loads(catalog_row[0])
            if digest(catalog_snapshot) != catalog_digest:
                raise IntegrityError("catalog snapshot digest mismatch")
            catalog_models = {(item["descriptor"]["provider_id"], item["descriptor"]["model_id"]): item
                              for item in catalog_snapshot["models"]}
            selection = (decision.get("provider_id"), decision.get("model_id"))
            if selection != ("", ""):
                candidate = catalog_models.get(selection)
                if candidate is None or not set(verified_caps) <= set(candidate["verified_capabilities"]):
                    raise ConflictError("selected model or verified capabilities diverge from catalog")
                if decision["status"] in {"proposal", "same_selection"} and selection not in {
                        tuple(item) for item in decision.get("alternatives", ())}:
                    raise ConflictError("selected model is absent from ranked alternatives")
            listed_models = [tuple(item) for item in decision.get("alternatives", ())]
            listed_models.extend((item[0], item[1]) for item in decision.get("rejected_alternatives", ()))
            if any(model_id not in catalog_models for model_id in listed_models):
                raise ConflictError("routing alternative is absent from catalog snapshot")
            spec_snapshot = plan_snapshot["spec"]
            snapshot = {"specification_digest": plan_row[3], "specification_snapshot": spec_snapshot,
                        "classification": classification, "catalog_digest": catalog_digest,
                        "policy_digest": policy_digest, "policy_snapshot": policy_snapshot,
                        "budget_snapshot": budget_snapshot,
                        "decision": {**decision, "authorization_required": True, "dispatch_allowed": False}}
            decision_digest = digest(snapshot)
            previous = db.execute("SELECT decision_digest FROM routing_decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if previous:
                if previous[0] != decision_digest:
                    raise ConflictError("decision_id reutilizado com parâmetros diferentes")
                return decision_digest
            db.execute("INSERT INTO routing_decisions(decision_id,project_id,plan_revision_id,task_id,classification_json,catalog_digest,policy_digest,policy_snapshot_json,decision_json,decision_digest,created_at,specification_digest,specification_snapshot_json,budget_snapshot_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (decision_id, project_id, plan_revision_id, task_id, canonical(classification),
                        catalog_digest, policy_digest, canonical(policy_snapshot),
                        canonical(snapshot["decision"]), decision_digest, now(),
                        plan_row[3], canonical(spec_snapshot), canonical(budget_snapshot)))
        return decision_digest

    def inspect_audit(self) -> dict:
        """Read-only audit summary; contains no grant bearer, credentials, or execution side effect."""
        return {
            "schema_version": self.db.execute("PRAGMA user_version").fetchone()[0],
            "specification_revisions": tuple(self.db.execute(
                "SELECT revision_id,sequence,spec_digest,status FROM specification_revisions ORDER BY sequence")),
            "plan_revisions": tuple(self.db.execute(
                "SELECT plan_revision_id,sequence,specification_revision_id,tasks_digest,status FROM plan_revisions ORDER BY sequence")),
            "change_impacts": tuple(self.db.execute(
                "SELECT impact_id,impact_digest FROM change_impacts ORDER BY created_at,impact_id")),
            "catalog_digests": tuple(row[0] for row in self.db.execute(
                "SELECT catalog_digest FROM model_catalog_snapshots ORDER BY created_at,catalog_digest")),
            "routing_decisions": tuple(self.db.execute(
                "SELECT decision_id,decision_digest FROM routing_decisions ORDER BY created_at,decision_id")),
        }

    @staticmethod
    def _catalog_model(db, catalog_digest: str, provider_id: str, model_id: str):
        row = db.execute("SELECT snapshot_json FROM model_catalog_snapshots WHERE catalog_digest=?",
                         (catalog_digest,)).fetchone()
        if row is None:
            raise ConflictError("catalog snapshot missing")
        snapshot = json.loads(row[0])
        if digest(snapshot) != catalog_digest:
            raise IntegrityError("catalog snapshot digest mismatch")
        for entry in snapshot.get("models", []):
            descriptor_data = entry["descriptor"]
            if (descriptor_data.get("provider_id"), descriptor_data.get("model_id")) == (provider_id, model_id):
                descriptor_data = dict(descriptor_data)
                for key in ("capabilities", "modalities"):
                    descriptor_data[key] = tuple(descriptor_data.get(key, ()))
                descriptor = ModelDescriptor(**descriptor_data)
                return ModelCatalogEntry(descriptor, tuple(entry.get("verified_capabilities", ())),
                                         entry.get("verification_source", ""))
        raise ConflictError("model identity absent from immutable catalog")

    @staticmethod
    def _model_cost(entry: ModelCatalogEntry, tokens: int, currency: str) -> Decimal:
        model = entry.descriptor
        if (model.cost_currency != currency or model.input_cost_per_million is None
                or model.output_cost_per_million is None):
            raise ConflictError("model cost cannot be estimated from catalog")
        return max(Decimal(model.input_cost_per_million), Decimal(model.output_cost_per_million)) * Decimal(tokens) / Decimal(1_000_000)

    def propose_escalation(self, *, operation_id: str, project_id: str, task_id: str,
                           plan_revision_id: str, source_attempt_id: str,
                           evidence_ids: tuple[str, ...], source_catalog_digest: str,
                           target_catalog_digest: str, target_provider_id: str,
                           target_model_id: str, token_budget: int, currency: str) -> EscalationProposal:
        """Persist an authorization-required escalation proposal; never dispatches it."""
        safe = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
        if (not safe.fullmatch(operation_id) or not safe.fullmatch(project_id) or not safe.fullmatch(task_id)
                or not safe.fullmatch(source_attempt_id) or not safe.fullmatch(target_provider_id)
                or not safe.fullmatch(target_model_id)):
            raise ValueError("escalation identity invalid")
        if type(token_budget) is not int or not 1 <= token_budget <= 10_000_000:
            raise ValueError("escalation token budget invalid")
        if len(currency) != 3 or currency.upper() != currency:
            raise ValueError("escalation currency invalid")
        if not evidence_ids or len(evidence_ids) > 32 or len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("verified gate evidence IDs required")
        evidence_ids = tuple(sorted(evidence_ids))
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", source_catalog_digest) or not re.fullmatch(r"sha256:[0-9a-f]{64}", target_catalog_digest):
            raise ValueError("catalog digest invalid")
        with self.transaction() as db:
            plan = db.execute("SELECT status,snapshot_json,trace_json,spec_digest FROM plan_revisions WHERE plan_revision_id=? AND project_id=?",
                              (plan_revision_id, project_id)).fetchone()
            if plan is None or plan[0] == "legacy_unmapped":
                raise ConflictError("escalation requires an explicit requirement-traced plan revision")
            plan_snapshot = json.loads(plan[1])
            if digest(plan_snapshot["spec"]) != plan[3]:
                raise IntegrityError("plan revision specification snapshot digest mismatch")
            task_data = next((item for item in plan_snapshot["plan"]["tasks"] if item["id"] == task_id), None)
            if task_data is None:
                raise ConflictError("task is outside plan revision")
            trace = json.loads(plan[2])
            traced_criteria = {(item["task_id"], item["criterion_id"])
                               for pairs in trace.values() for item in pairs}
            if not traced_criteria:
                raise ConflictError("plan revision has no explicit requirement trace")

            run = db.execute(
                "SELECT r.validation_run_id,r.definition_revision,r.definition_digest,r.checkpoint_operation_id,r.workspace_id,r.commit_oid,r.tree_oid,r.status,r.unresolved,r.attempt_id,"
                "s.snapshot_digest,s.state,s.source_commit_oid,s.source_tree_oid,"
                "c.verified,c.commit_oid,c.tree_oid,g.status,w.status,w.current_head_oid,"
                "t.definition_revision,t.revalidation_required,d.definition_digest,"
                "a.status,a.project_id,a.task_id,a.definition_revision,d.spec_digest,"
                "ms.snapshot_json,ms.snapshot_digest,mo.provider_id,mo.model_id,mo.status "
                "FROM validation_runs r JOIN validation_snapshots s ON s.validation_run_id=r.validation_run_id "
                "JOIN git_checkpoints c ON c.operation_id=r.checkpoint_operation_id "
                "JOIN git_operations g ON g.operation_id=c.operation_id "
                "JOIN workspaces w ON w.workspace_id=r.workspace_id "
                "JOIN tasks t ON t.project_id=r.project_id AND t.task_id=r.task_id "
                "JOIN task_definitions d ON d.project_id=r.project_id AND d.task_id=r.task_id AND d.definition_revision=r.definition_revision "
                "JOIN execution_attempts a ON a.attempt_id=r.attempt_id "
                "JOIN agent_model_selections ms ON ms.attempt_id=a.attempt_id "
                "JOIN agent_model_observations mo ON mo.attempt_id=a.attempt_id "
                "WHERE r.attempt_id=? AND r.project_id=? AND r.task_id=? ORDER BY r.created_at DESC LIMIT 1",
                (source_attempt_id, project_id, task_id)).fetchone()
            if run is None:
                raise ConflictError("source attempt has no complete F5 verification chain")
            if (run[1] != run[20] or run[2] != run[22] or run[21] != 0 or run[7] != "failed"
                    or run[8] != 0 or run[9] != source_attempt_id or run[11] != "verified"
                    or (run[5], run[6]) != (run[12], run[13]) or run[14] != 1
                    or (run[15], run[16]) != (run[5], run[6]) or run[17] != "verified"
                    or (run[18], run[19]) != ("active", run[5])
                    or (run[23], run[24], run[25], run[26]) != ("process_succeeded", project_id, task_id, run[1])
                    or run[27] != plan[3] or run[32] != "confirmed"):
                raise ConflictError("validation/checkpoint/evidence is stale, inconclusive, or inconsistent")
            selection = json.loads(run[28])
            if digest(selection) != run[29]:
                raise IntegrityError("source model selection snapshot digest mismatch")
            if (run[30], run[31]) != (selection.get("provider_id"), selection.get("model_id")):
                raise IntegrityError("confirmed model identity diverges from selection")
            evidence_rows = db.execute(
                "SELECT gate_result_id,gate_definition_digest,criterion_ids_json,validator_id,validator_version,validator_digest,snapshot_digest,exit_code,reason "
                "FROM validation_gate_results WHERE validation_run_id=? AND status='failed' AND required=1 ORDER BY gate_id",
                (run[0],)).fetchall()
            by_evidence = {row[0]: row for row in evidence_rows}
            if any(item not in by_evidence for item in evidence_ids):
                raise ConflictError("escalation evidence must reference required failed gates from the same run")
            selected_evidence = [by_evidence[item] for item in evidence_ids]
            for item in selected_evidence:
                criteria = set(json.loads(item[2]))
                if (not any((task_id, criterion) in traced_criteria for criterion in criteria)
                        or item[6] != run[10] or not item[3] or not item[4] or not item[5]
                        or item[7] in (None, 0) or not item[8]):
                    raise ConflictError("failed gate lacks current checkpoint, validator, or requirement evidence")
            evidence_digest = digest({"run_id": run[0], "checkpoint": [run[5], run[6]],
                                      "snapshot_digest": run[10], "evidence": selected_evidence})
            source_provider, source_model = run[30], run[31]
            target = self._catalog_model(db, target_catalog_digest, target_provider_id, target_model_id)
            if (target.descriptor.availability != "available" or target.descriptor.availability_is_stale(3600)
                    or "quality:advanced" not in target.verified_capabilities):
                raise ConflictError("target model lacks current verified advanced-quality capability")
            source = self._catalog_model(db, source_catalog_digest, source_provider, source_model)
            if source.descriptor.availability_is_stale(3600):
                raise ConflictError("source model pricing provenance is stale")
            if source_provider == target_provider_id and source_model == target_model_id:
                raise ConflictError("escalation target must differ from source model")
            source_cost = self._model_cost(source, token_budget, currency)
            target_cost = self._model_cost(target, token_budget, currency)
            request_data = {"project_id": project_id, "task_id": task_id, "plan_revision_id": plan_revision_id,
                            "source_attempt_id": source_attempt_id, "evidence_digest": evidence_digest,
                            "source_catalog_digest": source_catalog_digest,
                            "target_catalog_digest": target_catalog_digest,
                            "source_model": [source_provider, source_model],
                            "target_model": [target_provider_id, target_model_id],
                            "tokens": token_budget, "currency": currency,
                            "source_cost": str(source_cost), "target_cost": str(target_cost)}
            request_digest = digest(request_data)
            prior = db.execute("SELECT request_digest,proposal_id,source_provider_id,source_model_id,target_provider_id,target_model_id,evidence_digest,currency,source_cost_estimate,target_cost_estimate,cost_delta_estimate,authorization_required,expires_at,status,project_id,task_id,plan_revision_id,source_attempt_id,evidence_ids_json,reason_code,budget_status FROM escalation_proposals WHERE operation_id=?",
                               (operation_id,)).fetchone()
            if prior:
                if prior[0] != request_digest:
                    raise ConflictError("escalation operation_id reused with divergent parameters")
                return EscalationProposal(prior[1], operation_id, *prior[2:14], prior[14], prior[15],
                                          prior[16], prior[17], tuple(json.loads(prior[18])),
                                          prior[19], prior[20])
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
            proposal_id = uuid4().hex
            delta = target_cost - source_cost
            db.execute("INSERT INTO escalation_proposals(proposal_id,operation_id,request_digest,project_id,plan_revision_id,task_id,source_attempt_id,source_provider_id,source_model_id,target_provider_id,target_model_id,evidence_ids_json,evidence_digest,reason_code,currency,source_cost_estimate,target_cost_estimate,cost_delta_estimate,budget_status,authorization_required,expires_at,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (proposal_id, operation_id, request_digest, project_id, plan_revision_id, task_id,
                        source_attempt_id, source_provider, source_model, target_provider_id, target_model_id,
                        canonical(evidence_ids), evidence_digest, "verified_validation_gate_failure", currency,
                        str(source_cost), str(target_cost), str(delta), "estimated", 1, expires_at,
                        "proposed", now()))
            return EscalationProposal(proposal_id, operation_id, source_provider, source_model,
                target_provider_id, target_model_id, evidence_digest, currency, str(source_cost),
                str(target_cost), str(delta), True, expires_at, "proposed", project_id, task_id,
                plan_revision_id, source_attempt_id, evidence_ids,
                "verified_validation_gate_failure", "estimated")

    def inspect_escalation(self, proposal_id: str) -> EscalationProposal:
        row = self.db.execute("SELECT proposal_id,operation_id,source_provider_id,source_model_id,target_provider_id,target_model_id,evidence_digest,currency,source_cost_estimate,target_cost_estimate,cost_delta_estimate,authorization_required,expires_at,status,project_id,task_id,plan_revision_id,source_attempt_id,evidence_ids_json,reason_code,budget_status FROM escalation_proposals WHERE proposal_id=?",
                              (proposal_id,)).fetchone()
        if row is None:
            raise IntegrityError("escalation proposal missing")
        status = "expired" if datetime.fromisoformat(row[12]) <= datetime.now(timezone.utc) else row[13]
        return EscalationProposal(*row[:12], row[12], status, row[14], row[15], row[16], row[17],
                                  tuple(json.loads(row[18])), row[19], row[20])
