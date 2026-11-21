"""Compile the active milestone of a Markdown-backed task queue."""

from dataclasses import dataclass
from pathlib import Path

from .loader import PlanError
from .task_source import task_source_objective
from .master_plan import inspect_master_plan, MasterPlan, DEFAULT_CONTEXT_BYTES


@dataclass(frozen=True)
class ActiveMilestone:
    source: dict
    master_plan: str
    reference: MasterPlan | None = None


def _acceptance(node):
    values = node.get("acceptance")
    if not isinstance(values, list) or not values:
        raise PlanError(f"{node['id']}: acceptance deve ser uma lista não vazia")
    for value in values:
        text = value.get("description") if isinstance(value, dict) else value
        if not isinstance(text, str) or not text.strip():
            raise PlanError(f"{node['id']}: acceptance deve conter critérios verificáveis")


def prepare_active_milestone(root: Path, data: dict, completed_run=None) -> ActiveMilestone:
    """Never execute a milestone or infer completion from a JSON status field.

    completed_run(run_id, exact_objective) consults persisted operational results.
    No Markdown or queue file is rewritten and future work is not decomposed.
    """
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise PlanError("fila tipada: schema_version deve ser 1")
    nodes = data["tasks"]
    by_id = {node["id"]: node for node in nodes}
    milestones = {key: node for key, node in by_id.items() if node.get("type") == "milestone"}
    if not milestones:
        raise PlanError("fila tipada requer ao menos um milestone")
    for node in nodes:
        kind = node.get("type")
        if type(kind) is not str or kind not in {"milestone", "work"}:
            raise PlanError(f"{node['id']}: type deve ser milestone ou work")
        if not isinstance(node.get("goal"), str) or not node["goal"].strip():
            raise PlanError(f"{node['id']}: goal obrigatório")
        _acceptance(node)
        if kind == "milestone":
            selectors = node.get("plan_sections", [])
            if not isinstance(selectors, list) or any(type(item) is not str or not item.strip() for item in selectors):
                raise PlanError(f"{node['id']}: plan_sections deve conter títulos, hierarquias ou IDs de seção")
            if any(dep not in milestones for dep in node.get("depends_on", [])):
                raise PlanError(f"{node['id']}: milestone só depende de milestones")
        else:
            if type(node.get("parent_id")) is not str or node["parent_id"] not in milestones:
                raise PlanError(f"{node['id']}: parent_id deve identificar um milestone")
            for dep in node.get("depends_on", []):
                predecessor = by_id[dep]
                if (predecessor.get("type") != "work"
                        or predecessor.get("parent_id") != node["parent_id"]):
                    raise PlanError(f"{node['id']}: dependências work devem pertencer ao mesmo marco; "
                                    "ordem entre marcos pertence ao milestone")
    active_id = data.get("active_milestone")
    if active_id is None:
        detailed = {node["parent_id"] for node in nodes if node.get("type") == "work"}
        if len(detailed) != 1:
            raise PlanError("indique active_milestone quando houver zero ou vários marcos detalhados")
        active_id = detailed.pop()
    if type(active_id) is not str or active_id not in milestones:
        raise PlanError("active_milestone inexistente")
    master_path = data.get("master_plan", "IMPLEMENTATION_PLAN.md")
    if not isinstance(master_path, str) or not master_path.strip():
        raise PlanError("master_plan deve indicar um arquivo Markdown")
    master_path = root / master_path
    if master_path.suffix.casefold() != ".md":
        raise PlanError("master_plan deve ser Markdown (.md)")
    reference = inspect_master_plan(root, master_path,
        context_bytes=data.get("plan_context_bytes", DEFAULT_CONTEXT_BYTES))
    canonical, master_digest = reference.path, reference.sha256

    def source_for(milestone_id):
        milestone = milestones[milestone_id]
        work = [node for node in nodes if node.get("type") == "work"
                and node["parent_id"] == milestone_id]
        if not 1 <= len(work) <= 12:
            raise PlanError(f"{milestone_id}: detalhe de 1 a 12 tarefas work antes de executar")
        source = {
            "master_plan": str(canonical.relative_to(root.resolve()).as_posix()),
            "master_plan_digest": master_digest,
            "milestone": {key: milestone.get(key, [] if key == "depends_on" else "")
                          for key in ("id", "goal", "acceptance", "depends_on")},
            "tasks": [{key: value for key, value in node.items()
                       if key not in {"status", "completed_run_id"}} for node in work],
        }
        if "plan_sections" in milestone:
            source["milestone"]["plan_sections"] = milestone["plan_sections"]
        return source

    checked = set()

    def require_completed(milestone_id):
        if milestone_id in checked:
            return
        milestone = milestones[milestone_id]
        for predecessor in milestone.get("depends_on", []):
            require_completed(predecessor)
        run_id = milestone.get("completed_run_id")
        expected = task_source_objective(source_for(milestone_id))
        if (not isinstance(run_id, str) or completed_run is None
                or not completed_run(run_id, expected)):
            raise PlanError(f"{milestone_id}: saída do marco ainda não demonstrada; "
                            "completed_run_id deve referenciar execução concluída deste plano")
        checked.add(milestone_id)

    for predecessor in milestones[active_id].get("depends_on", []):
        require_completed(predecessor)
    context = reference.initial_context(milestones[active_id].get("plan_sections", ()))
    return ActiveMilestone(source_for(active_id), context, reference)
