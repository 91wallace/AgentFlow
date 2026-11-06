"""Deterministic model routing preview. It never starts or switches an agent."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re

from .contracts import ExecutionError
from .models import ModelCatalogEntry, ModelDescriptor
from .routing_policy import ModelRoutingPolicy


@dataclass(frozen=True)
class TaskClassification:
    complexity: str
    risk: str
    required_capabilities: tuple[str, ...]
    source: str
    reasons: tuple[str, ...] = ()

    def __post_init__(self):
        if self.complexity not in {"simple", "standard", "complex"} or self.risk not in {"low", "medium", "high"}:
            raise ExecutionError("classification values invalid")
        if self.source not in {"declared", "deterministic_rule"}:
            raise ExecutionError("classification source must be explicit")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ExecutionError("duplicate required capability")


@dataclass(frozen=True)
class RoutingDecision:
    status: str
    provider_id: str
    model_id: str
    reason: str
    alternatives: tuple[tuple[str, str], ...]
    authorization_required: bool = True
    dispatch_allowed: bool = False
    budget_status: str = "unavailable"
    verified_capabilities: tuple[str, ...] = ()
    rejected_alternatives: tuple[tuple[str, str, str], ...] = ()
    constraints: tuple[str, ...] = ()
    budget_prediction: tuple[tuple[str, str], ...] = ()

    def as_snapshot(self) -> dict:
        return {"status": self.status, "provider_id": self.provider_id, "model_id": self.model_id,
                "reason": self.reason, "alternatives": [list(item) for item in self.alternatives],
                "authorization_required": self.authorization_required, "dispatch_allowed": False,
                "budget_status": self.budget_status,
                "verified_capabilities": list(self.verified_capabilities),
                "rejected_alternatives": [list(item) for item in self.rejected_alternatives],
                "constraints": list(self.constraints), "budget_prediction": dict(self.budget_prediction)}


def evaluate_routing(classification: TaskClassification, policy: ModelRoutingPolicy,
                     catalog: tuple[ModelCatalogEntry, ...], *, current: tuple[str, str] | None = None) -> RoutingDecision:
    """Preview the cheapest sufficient known candidate; no fallback or execution occurs."""
    if not policy.enabled:
        return RoutingDecision("blocked", "", "", "policy_disabled", ())
    if policy.max_latency_seconds is not None:
        return RoutingDecision("blocked", "", "", "latency_budget_unverifiable_from_catalog", (),
                               budget_status="unavailable", constraints=("max_latency_seconds",),
                               budget_prediction=(("latency_status", "unavailable"),))
    if policy.max_cost is not None and policy.max_tokens is None:
        return RoutingDecision("blocked", "", "", "cost_budget_cannot_be_estimated_without_token_budget", (),
                               budget_status="unavailable", constraints=("max_cost", "max_tokens_missing"),
                               budget_prediction=(("cost_status", "unavailable"),))
    required = set(classification.required_capabilities) | set(policy.required_capabilities)
    eligible = []
    rejected = []
    for entry in catalog:
        model = entry.descriptor
        if model.availability != "available" or model.availability_is_stale(3600):
            rejected.append((model.provider_id, model.model_id, "model_unavailable_or_stale"))
            continue
        if policy.max_tokens is not None and (model.context_tokens is None or
                                               model.context_tokens < policy.max_tokens):
            rejected.append((model.provider_id, model.model_id, "context_capacity_unknown_or_insufficient"))
            continue
        # Descriptor claims are unknown until evidence is attached to the catalog entry.
        capabilities = set(entry.verified_capabilities)
        if not required <= capabilities:
            rejected.append((model.provider_id, model.model_id, "required_capability_unverified"))
            continue
        if classification.risk == "high" and "quality:advanced" not in capabilities:
            rejected.append((model.provider_id, model.model_id, "advanced_quality_not_verified"))
            continue
        cost = model.input_cost_per_million
        output_cost = model.output_cost_per_million
        if policy.max_cost is not None and (cost is None or output_cost is None or model.cost_currency != policy.currency):
            rejected.append((model.provider_id, model.model_id, "cost_metadata_unavailable"))
            continue
        if cost is not None and policy.max_cost is not None:
            try:
                estimated_cost = max(Decimal(cost), Decimal(output_cost or cost)) * Decimal(policy.max_tokens) / Decimal(1_000_000)
                if estimated_cost > Decimal(policy.max_cost):
                    rejected.append((model.provider_id, model.model_id, "estimated_cost_exceeds_budget"))
                    continue
            except InvalidOperation as exc:
                raise ExecutionError("catalog cost is invalid") from exc
        if cost is None or output_cost is None:
            # Unknown price cannot beat a priced candidate; keep it only if no hard cost cap exists.
            if policy.max_cost is not None:
                continue
            cost_sort = Decimal("Infinity")
        else:
            cost_sort = Decimal(cost) + Decimal(output_cost)
        eligible.append((cost_sort, model.provider_id, model.model_id, model, entry))
    eligible.sort(key=lambda item: (item[0], item[1], item[2]))
    if not eligible:
        return RoutingDecision("blocked", "", "", "no_verified_sufficient_model", (),
                               budget_status="estimated" if policy.max_cost is not None else "unavailable",
                               rejected_alternatives=tuple(sorted(rejected)),
                               constraints=tuple(sorted(required)),
                               budget_prediction=(("cost_status", "estimated" if policy.max_cost else "unavailable"),
                                                  ("token_limit", str(policy.max_tokens or ""))))
    selected = eligible[0][3]
    selected_entry = eligible[0][4]
    alternatives = tuple((item[3].provider_id, item[3].model_id) for item in eligible)
    rejected.extend((item[3].provider_id, item[3].model_id, "eligible_but_ranked_lower") for item in eligible[1:])
    status = "same_selection" if current == (selected.provider_id, selected.model_id) else "proposal"
    return RoutingDecision(status, selected.provider_id, selected.model_id,
                           "lowest_declared_cost_within_known_capabilities", alternatives,
                           budget_status="estimated" if policy.max_cost is not None else "unavailable",
                           verified_capabilities=tuple(sorted(selected_entry.verified_capabilities)),
                           rejected_alternatives=tuple(sorted(rejected)),
                           constraints=tuple(sorted(required)),
                           budget_prediction=(("cost_status", "estimated" if policy.max_cost else "unavailable"),
                                              ("max_cost", policy.max_cost or ""),
                                              ("currency", policy.currency or ""),
                                              ("token_limit", str(policy.max_tokens or ""))))


def classify_task(task) -> TaskClassification:
    """Stable conservative rule set over immutable task definition fields."""
    criteria_count = len(task.acceptance_criteria)
    path_count = len(task.versioned_paths)
    description_size = len(task.description.encode("utf-8"))
    task_text = " ".join((task.title, task.description,
                           *(criterion.description for criterion in task.acceptance_criteria)))
    if (criteria_count >= 6 or path_count >= 12 or description_size >= 3000
            or re.search(r"\b(architecture|architectural|planning|critical review|recovery)\b", task_text, re.I)):
        complexity = "complex"
        reasons = ["large_acceptance_or_scope"]
    elif criteria_count >= 3 or path_count >= 4 or description_size >= 1200 or task.depends_on:
        complexity = "standard"
        reasons = ["multi_criterion_scope_or_dependency"]
    else:
        complexity = "simple"
        reasons = ["small_declared_scope"]
    sensitive_path = re.compile(r"(^|/)(auth|security|credential|secret|permission|migration)(/|\.|$)", re.IGNORECASE)
    sensitive_task = re.compile(r"\b(security|authentication|authorization|credential|secret|permission|migration|privacy|encryption|data loss|payment|production)\b", re.IGNORECASE)
    if any(sensitive_path.search(path) for path in task.versioned_paths) or sensitive_task.search(task_text):
        risk = "high"
        reasons.append("sensitive_path")
    elif task.versioned_paths:
        risk = "medium"
        reasons.append("versioned_write_scope")
    else:
        risk = "low"
        reasons.append("no_versioned_write_scope")
    required = ("quality:advanced",) if risk == "high" else ()
    return TaskClassification(complexity, risk, required, "deterministic_rule", tuple(reasons))
