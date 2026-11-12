"""Synchronous, deterministic APL controller over existing AgentFlow boundaries."""

from dataclasses import replace
from time import monotonic
from uuid import uuid4

from .contracts import (APLState, APLLimits, CycleSnapshot, Evaluation,
                        ExecutionOutcome, JobProposal)


class APLController:
    def __init__(self, planner, executor, evaluator, policy, store, limits=APLLimits(), clock=monotonic):
        self.planner, self.executor, self.evaluator = planner, executor, evaluator
        self.policy, self.store, self.limits, self.clock = policy, store, limits, clock

    def start(self, objective, authorized_scope, cycle_id=None, cancel=None):
        if not objective.strip():
            raise ValueError("objective cannot be empty")
        scope = tuple(sorted(set(authorized_scope)))
        if not scope:
            raise ValueError("authorized scope must be explicit")
        snap = CycleSnapshot(cycle_id or uuid4().hex, objective, scope, APLState.CREATED, (), (), 0, 0)
        self.store.create(snap)
        return self.run(snap.cycle_id, cancel)

    def recover(self, cycle_id, cancel=None):
        snap = self.store.load(cycle_id)
        if snap.state in {APLState.EXECUTING, APLState.EVALUATING}:
            # Outcome may be ambiguous. Never reissue an external effect.
            snap = replace(snap, state=APLState.RECOVERING, reason="execution outcome requires reconciliation")
            snap = replace(snap, state=APLState.BLOCKED)
            self.store.save(snap, self.store.load(cycle_id).iterations, expected_revision=self.store.revision(cycle_id))
            return snap
        return self.run(cycle_id, cancel)

    def _save(self, old, **changes):
        new = replace(old, **changes)
        return self.store.save(new, old.iterations)

    def run(self, cycle_id, cancel=None):
        start = self.clock()
        snap = self.store.load(cycle_id)
        if snap.state in {APLState.COMPLETED, APLState.BLOCKED, APLState.FAILED, APLState.CANCELLED, APLState.WAITING_APPROVAL, APLState.PAUSED}:
            return snap
        cancel = cancel or (lambda: False)
        seen = {outcome.progress_key for _, outcome in snap.outcomes if outcome.progress_key}
        no_progress = 0
        if snap.state == APLState.CREATED:
            snap = self._save(snap, state=APLState.PLANNING)
        while snap.iterations < self.limits.max_iterations:
            if cancel():
                return self._save(snap, state=APLState.CANCELLED, reason="cancelled")
            if self.clock() - start >= self.limits.max_seconds:
                return self._save(snap, state=APLState.BLOCKED, reason="time limit reached")
            if snap.state in (APLState.PLANNING, APLState.REPLANNING):
                proposed = tuple(self.planner.plan(snap.objective, snap.authorized_scope, snap.outcomes))
                known = {j.job_id: j for j in snap.jobs}
                for job in proposed:
                    if not job.job_id or not set(job.required_scope).issubset(snap.authorized_scope):
                        return self._save(snap, state=APLState.BLOCKED, reason="planner proposal exceeds immutable authorized scope")
                    if job.job_id not in known:
                        known[job.job_id] = job
                if len(known) > self.limits.max_jobs:
                    return self._save(snap, state=APLState.BLOCKED, reason="job limit reached")
                snap = self._save(snap, state=APLState.READY, jobs=tuple(known.values()))
            if snap.state == APLState.READY:
                done = {job for job, _ in snap.outcomes}
                eligible = [j for j in snap.jobs if j.job_id not in done and set(j.depends_on).issubset(done)]
                if not eligible:
                    evaluation = self.evaluator.evaluate(snap.objective, snap.outcomes)
                    if evaluation.complete and evaluation.passed:
                        return self._save(snap, state=APLState.COMPLETED)
                    if not evaluation.progress:
                        no_progress += 1
                        if no_progress >= self.limits.no_progress_limit:
                            return self._save(snap, state=APLState.BLOCKED, reason="no progress detected")
                    if snap.iterations + 1 >= self.limits.max_iterations:
                        return self._save(snap, state=APLState.BLOCKED, reason="iteration limit reached")
                    snap = self._save(snap, state=APLState.REPLANNING, iterations=snap.iterations + 1)
                    continue
                job = eligible[0]
                decision = self.policy.authorize(job, snap.authorized_scope)
                if decision == "approval_required":
                    return self._save(snap, state=APLState.WAITING_APPROVAL, reason=job.job_id)
                if decision != "authorized":
                    return self._save(snap, state=APLState.BLOCKED, reason=f"job policy denied: {job.job_id}")
                if snap.consumed_budget >= self.limits.max_budget and self.limits.max_budget > 0:
                    return self._save(snap, state=APLState.BLOCKED, reason="budget limit reached")
                snap = self._save(snap, state=APLState.EXECUTING)
                try:
                    outcome = self.executor.execute(snap.cycle_id, job, snap.authorized_scope)
                except Exception:
                    return self._save(snap, state=APLState.BLOCKED, reason="execution outcome unknown; reconciliation required")
                outcomes = snap.outcomes + ((job.job_id, outcome),)
                if outcome.status == "unknown":
                    return self._save(snap, state=APLState.BLOCKED, outcomes=outcomes, iterations=snap.iterations + 1, reason="execution outcome unknown; reconciliation required")
                if outcome.status == "approval_required":
                    return self._save(snap, state=APLState.WAITING_APPROVAL, outcomes=outcomes, iterations=snap.iterations + 1, reason=job.job_id)
                budget = snap.consumed_budget + outcome.cost
                if budget > self.limits.max_budget and self.limits.max_budget > 0:
                    return self._save(snap, state=APLState.BLOCKED, outcomes=outcomes, consumed_budget=budget, reason="budget limit exceeded")
                snap = self._save(snap, state=APLState.EVALUATING, outcomes=outcomes, consumed_budget=budget)
                evaluation = self.evaluator.evaluate(snap.objective, outcomes)
                if evaluation.complete and evaluation.passed:
                    return self._save(snap, state=APLState.COMPLETED)
                if outcome.progress_key and outcome.progress_key in seen:
                    no_progress += 1
                elif evaluation.progress:
                    no_progress = 0
                else:
                    no_progress += 1
                if no_progress >= self.limits.no_progress_limit:
                    return self._save(snap, state=APLState.BLOCKED, reason="no progress detected")
                if outcome.status != "succeeded":
                    return self._save(snap, state=APLState.FAILED, reason=outcome.summary or "job failed")
                seen.add(outcome.progress_key) if outcome.progress_key else None
                if snap.iterations + 1 >= self.limits.max_iterations:
                    return self._save(snap, state=APLState.BLOCKED, reason="iteration limit reached")
                snap = self._save(snap, state=APLState.REPLANNING, iterations=snap.iterations + 1)
        return self._save(snap, state=APLState.BLOCKED, reason="iteration limit reached")
