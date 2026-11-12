"""Supervised developer mode: propose work and attach already validated attempts only."""

from dataclasses import replace
import json
from uuid import uuid4
from hashlib import sha256

from .contracts import APLState, CycleSnapshot, ExecutionOutcome, JobProposal
from .contracts import (EvidenceReference, PlanProposal, ReplanContext,
                        RemainingLimits, TaskChange, ValidatedResult)
from .replanning import proposal_digest, validate_proposal


class SupervisedCycleService:
    def __init__(self, store, state_path):
        self.store = store
        self.state_path = state_path

    def _commit(self, current, updated, result_ref=None):
        # Revision CAS makes competing CLI controls/results mutually exclusive.
        return self.store.save(updated, current.iterations, expected_revision=current.revision,
                               result_ref=result_ref)

    def start(self, plan, objective, authorized_scope, cycle_id=None):
        scope = tuple(sorted(set(authorized_scope)))
        if not objective.strip() or not scope:
            raise ValueError("objective and explicit authorized scope are required")
        jobs = tuple(JobProposal(task.id, f"{task.title}\n\n{task.description}", task.depends_on,
                                 task.versioned_paths or (f"task:{task.id}",), tuple(c.id for c in task.acceptance_criteria))
                     for task in (next(item for item in plan.tasks if item.id == task_id) for task_id in plan.order))
        outside = [job.job_id for job in jobs if not set(job.required_scope) <= set(scope)]
        if outside:
            raise ValueError("plan includes jobs outside or without explicit authorized scope: " + ", ".join(outside))
        snap = CycleSnapshot(cycle_id or uuid4().hex, objective, scope, APLState.READY, jobs, (), 0, 0,
                             plan_digest=f"spec:{plan.spec_digest};tasks:{plan.tasks_digest}")
        return self.store.create(snap)

    def _replan_context(self, current):
        completed = tuple(job_id for job_id, outcome in current.outcomes if outcome.status == "succeeded")
        results, evidence = [], []
        for job_id, outcome in current.outcomes:
            refs = tuple(EvidenceReference(item["evidence_id"], job_id, item["criterion_id"])
                         for item in outcome.evidence if isinstance(item, dict)
                         and item.get("passed") is True and isinstance(item.get("evidence_id"), int)
                         and isinstance(item.get("criterion_id"), str))
            evidence.extend(refs)
            results.append(ValidatedResult(job_id, outcome.progress_key, outcome.status, refs))
        executed = set(job_id for job_id, _ in current.outcomes)
        try:
            from pathlib import Path
            import sqlite3
            uri = Path(self.state_path).resolve().as_uri() + "?mode=ro"
            db = sqlite3.connect(uri, uri=True)
            try:
                with db:
                    executed.update(row[0] for row in db.execute(
                        "SELECT DISTINCT task_id FROM execution_attempts"))
            finally:
                db.close()
        except sqlite3.Error:
            executed.update(job.job_id for job in current.jobs)
        return ReplanContext(current.objective, current.authorized_scope, current.jobs,
                             current.plan_revision_id, completed, tuple(results), tuple(evidence),
                             (current.reason,) if current.state == APLState.BLOCKED and current.reason else (),
                             RemainingLimits(max(0, 10 - len(current.jobs)), max(0, 10 - current.iterations),
                                             3600.0, 0.0), current.revision,
                             tuple(sorted(executed)))

    def replan(self, cycle_id, planner_adapter, request_id=None):
        current = self.store.load(cycle_id)
        if current.state not in {APLState.READY, APLState.PAUSED, APLState.BLOCKED}:
            raise ValueError(f"cycle cannot be replanned from {current.state.value}")
        context = self._replan_context(current)
        context_data = {
            "cycle_id": cycle_id, "cycle_revision": current.revision,
            "plan_revision_id": current.plan_revision_id, "objective": context.objective,
            "scope": context.authorized_scope, "jobs": [job.__dict__ for job in context.current_plan],
            "completed": context.completed_jobs,
            "results": [{"job": result.job_id, "attempt": result.attempt_id,
                         "status": result.status,
                         "evidence": [item.__dict__ for item in result.evidence]}
                        for result in context.validated_results],
            "blockers": context.blockers, "limits": context.remaining_limits.__dict__}
        request_payload = json.dumps(context_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        request_hash = sha256(request_payload.encode()).hexdigest()
        request_key = sha256(f"{request_id or ''}:{request_hash}".encode()).hexdigest()
        prior = self.store.proposal_for_request(cycle_id, request_key)
        if prior is not None:
            return prior[0], prior[1], False
        provider = getattr(planner_adapter, "provider_name", type(planner_adapter).__name__)
        self.store.claim_planner_request(cycle_id, current.revision, request_key, provider)
        try:
            proposal = planner_adapter.propose(context)
            if proposal.source_cycle_revision != current.revision:
                raise ValueError("planner proposal is bound to a stale cycle revision")
            proposal = replace(proposal, proposal_id=sha256(f"{cycle_id}:{request_key}".encode()).hexdigest()[:32])
            validate_proposal(context, proposal, max_jobs=10)
            saved = self.store.propose_plan(cycle_id, proposal, request_key,
                                            proposal_digest(proposal), current.revision)
            self.store.finish_planner_request(cycle_id, current.revision, request_key, provider,
                                              "succeeded", proposal_digest(proposal))
            return saved
        except Exception as exc:
            self.store.finish_planner_request(cycle_id, current.revision, request_key, provider,
                                              "failed", type(exc).__name__)
            raise

    def proposals(self, cycle_id):
        self.store.load(cycle_id)
        return self.store.proposals(cycle_id)

    def reject_plan(self, cycle_id, proposal_id):
        return self.store.reject_proposal(cycle_id, proposal_id)

    def accept_plan(self, cycle_id, proposal_id, *, confirmed=False):
        if confirmed is not True:
            raise ValueError("explicit confirmation is required")
        current = self.store.load(cycle_id)
        if current.state not in {APLState.READY, APLState.PAUSED, APLState.BLOCKED}:
            raise ValueError(f"plan cannot be accepted from {current.state.value}")
        row = next((item for item in self.store.proposals(cycle_id) if item[0].proposal_id == proposal_id), None)
        if row is None:
            raise KeyError(proposal_id)
        proposal, _, status = row
        if status != "proposed":
            raise ValueError(f"proposal is {status}")
        context = self._replan_context(current)
        jobs = validate_proposal(context, proposal, max_jobs=10)
        self.store.proposal_status(proposal_id)
        return self.store.accept_proposal(current, proposal, jobs, row[1], current.revision)

    def pause(self, cycle_id):
        current = self.store.load(cycle_id)
        if current.state not in {APLState.READY, APLState.REPLANNING}:
            raise ValueError(f"cycle cannot pause from {current.state.value}")
        return self._commit(current, replace(current, state=APLState.PAUSED, reason="paused by user"))

    def resume(self, cycle_id):
        current = self.store.load(cycle_id)
        if current.state != APLState.PAUSED:
            raise ValueError(f"only paused cycles can resume; current state={current.state.value}")
        return self._commit(current, replace(current, state=APLState.READY, reason=""))

    def cancel(self, cycle_id):
        current = self.store.load(cycle_id)
        if current.state not in {APLState.CREATED, APLState.PLANNING, APLState.READY, APLState.REPLANNING, APLState.PAUSED}:
            raise ValueError(f"terminal cycle cannot cancel from {current.state.value}")
        return self._commit(current, replace(current, state=APLState.CANCELLED, reason="cancelled by user"))

    def next_job(self, cycle_id):
        current = self.store.load(cycle_id)
        if current.state != APLState.READY:
            raise ValueError(f"cycle is not ready for a proposal; state={current.state.value}")
        completed = {job_id for job_id, _ in current.outcomes}
        for job in current.jobs:
            if job.job_id not in completed and set(job.depends_on) <= completed:
                if not set(job.required_scope) <= set(current.authorized_scope):
                    raise ValueError("proposed job exceeds immutable authorized scope")
                return job
        return None

    def submit_result(self, cycle_id, attempt_id):
        from agentflow.state.execution import ExecutionStore
        current = self.store.load(cycle_id)
        if current.state != APLState.READY:
            raise ValueError(f"cycle is not ready for results; state={current.state.value}")
        job = self.next_job(cycle_id)
        if job is None:
            raise ValueError("no eligible proposed job")
        if any(outcome.progress_key == attempt_id for _, outcome in current.outcomes):
            raise ValueError("attempt result already associated with this cycle")
        if self.store.result_used(attempt_id):
            raise ValueError("attempt result already associated with another cycle")
        with ExecutionStore(self.state_path) as execution:
            attempt = execution.attempt(attempt_id)
            if attempt.task_id != job.job_id:
                raise ValueError("persisted attempt does not match next proposed job")
            if attempt.status == "interrupted":
                blocked = replace(current, state=APLState.BLOCKED,
                                  reason=f"unknown execution outcome for attempt {attempt_id}; reconcile manually")
                self._commit(current, blocked)
                raise ValueError(blocked.reason)
            if attempt.status != "process_succeeded" or attempt.validation_status != "validation_passed":
                raise ValueError("attempt must be process_succeeded and validation_passed")
            evidence_rows = execution.validated_evidence(attempt_id)
            criteria = set(job.acceptance_criteria)
            verified = {row.criterion_id for row in evidence_rows if row.passed}
            if not criteria or not criteria <= verified:
                raise ValueError("persisted verified evidence does not cover proposed acceptance criteria")
            evidence = tuple({"evidence_id": row.evidence_id, "criterion_id": row.criterion_id,
                              "passed": row.passed, "attempt_id": attempt_id}
                             for row in evidence_rows if row.criterion_id in criteria)
        outcome = ExecutionOutcome("succeeded", f"validated attempt {attempt_id}", evidence,
                                   progress_key=attempt_id)
        updated = replace(current, outcomes=current.outcomes + ((job.job_id, outcome),),
                          iterations=current.iterations + 1,
                          state=APLState.COMPLETED if len(current.outcomes) + 1 == len(current.jobs) else APLState.READY,
                          reason="")
        return self._commit(current, updated, (attempt_id, job.job_id))
