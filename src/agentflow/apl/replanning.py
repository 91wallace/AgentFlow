"""Supervised semantic replanning contracts and conservative validation."""

from dataclasses import replace
from hashlib import sha256
import re
from uuid import uuid4

from .contracts import (EvidenceReference, JobProposal, PlanProposal, ReplanContext,
                        TaskChange, ValidatedResult)


class PlannerUnavailable(RuntimeError):
    pass


class DeterministicPlannerAdapter:
    """Test/baseline adapter. It emits configured changes; it performs no semantic inference."""

    def __init__(self, changes=(), rationale="deterministic fixture proposal"):
        self.changes = tuple(changes)
        self.rationale = rationale

    def propose(self, context):
        evidence_ids = tuple(sorted({item.evidence_id for item in context.verified_evidence}))
        return PlanProposal(uuid4().hex, context.plan_revision_id, context.cycle_revision, self.rationale,
                            self.changes, "all proposed acceptance criteria have verified evidence",
                            evidence_ids)


class UnavailableSemanticPlanner:
    def propose(self, context):
        raise PlannerUnavailable("no authorized local semantic planner is configured")


def proposal_from_mapping(data):
    """Strictly decode untrusted adapter output; reject tool/command-shaped fields."""
    from .contracts import PlanProposal, TaskChange
    if not isinstance(data, dict) or set(data) != {
            "proposal_id", "source_revision_id", "source_cycle_revision", "rationale",
            "changes", "completion_condition", "evidence_used", "resolves_blockers", "schema_version"}:
        raise ValueError("planner response has missing or unsupported fields")
    if type(data["source_cycle_revision"]) is not int or type(data["schema_version"]) is not int:
        raise ValueError("planner response versions must be integers")
    if not isinstance(data["changes"], list):
        raise ValueError("planner changes must be a list")
    changes = []
    for item in data["changes"]:
        if not isinstance(item, dict) or set(item) != {"action", "task"}:
            raise ValueError("planner task change has unsupported fields")
        task = item["task"]
        if not isinstance(task, dict) or set(task) != {
                "job_id", "instruction", "depends_on", "required_scope", "acceptance_criteria"}:
            raise ValueError("planner task contains command, tool, or unsupported fields")
        if (not isinstance(task["job_id"], str) or not isinstance(task["instruction"], str)
                or any(not isinstance(task[name], list) or any(not isinstance(value, str) for value in task[name])
                       for name in ("depends_on", "required_scope", "acceptance_criteria"))):
            raise ValueError("planner task fields have invalid types")
        changes.append(TaskChange(item["action"], JobProposal(
            task["job_id"], task["instruction"], tuple(task["depends_on"]),
            tuple(task["required_scope"]), tuple(task["acceptance_criteria"]))))
    if (not isinstance(data["evidence_used"], list) or any(type(item) is not int for item in data["evidence_used"])
            or not isinstance(data["resolves_blockers"], list)
            or any(not isinstance(item, str) for item in data["resolves_blockers"])):
        raise ValueError("planner evidence or blocker references have invalid types")
    if any(not isinstance(data[name], str) for name in ("proposal_id", "source_revision_id", "rationale", "completion_condition")):
        raise ValueError("planner proposal text fields have invalid types")
    return PlanProposal(data["proposal_id"], data["source_revision_id"],
                        data["source_cycle_revision"], data["rationale"], tuple(changes),
                        data["completion_condition"], tuple(data["evidence_used"]),
                        tuple(data["resolves_blockers"]), data["schema_version"])


def validate_proposal(context: ReplanContext, proposal: PlanProposal, *, max_jobs: int) -> tuple:
    if proposal.schema_version != 1 or proposal.source_revision_id != context.plan_revision_id:
        raise ValueError("proposal schema or source plan revision is invalid")
    if not proposal.rationale.strip() or not proposal.completion_condition.strip():
        raise ValueError("proposal rationale and completion condition are required")
    known_evidence = {item.evidence_id for item in context.verified_evidence}
    if not set(proposal.evidence_used) <= known_evidence:
        raise ValueError("proposal references evidence outside the verified result set")
    if set(proposal.resolves_blockers) - set(context.blockers):
        raise ValueError("proposal references an unknown blocker")
    if proposal.resolves_blockers and not proposal.evidence_used:
        raise ValueError("a blocker cannot be removed without verified evidence")
    if context.blockers and not set(context.blockers) <= set(proposal.resolves_blockers):
        raise ValueError("proposal must explicitly resolve each blocker with verified evidence")
    if len(set(proposal.resolves_blockers)) != len(proposal.resolves_blockers):
        raise ValueError("duplicate blocker reference")
    if len(set(proposal.evidence_used)) != len(proposal.evidence_used):
        raise ValueError("duplicate evidence reference")
    jobs = {job.job_id: job for job in context.current_plan}
    completed = set(context.completed_jobs)
    changes_seen = set()
    for change in proposal.changes:
        task = change.task
        if (change.action not in {"add", "modify"}
                or not re.fullmatch(r"[a-z][a-z0-9-]*", task.job_id)):
            raise ValueError("unsupported task change")
        if task.job_id in changes_seen:
            raise ValueError("duplicate task change")
        changes_seen.add(task.job_id)
        if (not task.instruction.strip() or not task.acceptance_criteria
                or any(not isinstance(item, str) or not item.strip() for item in task.acceptance_criteria)
                or len(set(task.acceptance_criteria)) != len(task.acceptance_criteria)):
            raise ValueError("task requires acceptance criteria")
        if not task.required_scope or not set(task.required_scope) <= set(context.authorized_scope):
            raise ValueError("task scope is missing or exceeds immutable authorized scope")
        if len(set(task.depends_on)) != len(task.depends_on):
            raise ValueError("duplicate task dependency")
        if change.action == "add":
            if task.job_id in jobs:
                raise ValueError("added task ID already exists; identifiers are stable")
        else:
            if task.job_id not in jobs:
                raise ValueError("modified task does not exist")
            if task.job_id in completed or task.job_id in context.previously_executed_jobs:
                raise ValueError("previously executed task cannot be changed retroactively")
        jobs[task.job_id] = task
    if len(jobs) > max_jobs or len(jobs) > context.remaining_limits.jobs + len(context.current_plan):
        raise ValueError("proposal exceeds remaining job limit")
    for job in jobs.values():
        missing = set(job.depends_on) - jobs.keys()
        if missing:
            raise ValueError(f"unknown dependencies for {job.job_id}: {', '.join(sorted(missing))}")
    visiting, visited = set(), set()
    def visit(job_id):
        if job_id in visiting:
            raise ValueError("dependency cycle in proposed plan")
        if job_id in visited:
            return
        visiting.add(job_id)
        for dep in jobs[job_id].depends_on:
            visit(dep)
        visiting.remove(job_id)
        visited.add(job_id)
    for job_id in sorted(jobs):
        visit(job_id)
    for job_id in completed:
        before = next((job for job in context.current_plan if job.job_id == job_id), None)
        if before is None or jobs[job_id] != before:
            raise ValueError("proposal invalidates a completed task")
    return tuple(jobs.values())


def proposal_digest(proposal):
    from dataclasses import asdict
    import json
    payload = json.dumps(asdict(proposal), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(payload.encode()).hexdigest()
