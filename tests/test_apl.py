from threading import Event, Thread
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.apl import (APLController, APLLimits, APLState, Evaluation,
                           ExecutionOutcome, JobProposal, SQLiteCycleStore,
                           SupervisedCycleService, DeterministicPlannerAdapter,
                           EvidenceReference, PlanProposal, ReplanContext,
                           RemainingLimits, TaskChange, validate_proposal,
                           proposal_from_mapping, UnavailableSemanticPlanner,
                           PlannerUnavailable)


class Planner:
    def plan(self, objective, scope, history):
        done = {job for job, _ in history}
        return tuple(JobProposal(name, name, (prev,) if prev else (), scope, ("done",))
                     for name, prev in (("one", ""), ("two", "one"), ("three", "two")) if name not in done)


class Executor:
    def __init__(self): self.calls = []
    def execute(self, cycle_id, job, scope):
        self.calls.append(job.job_id)
        return ExecutionOutcome("succeeded", "ok", ({"proof": job.job_id},), progress_key=job.job_id)


class Evaluator:
    def evaluate(self, objective, outcomes): return Evaluation(len(outcomes) == 3, True, bool(outcomes))


class Policy:
    def __init__(self, decision="authorized"): self.decision = decision
    def authorize(self, job, scope): return self.decision if set(job.required_scope).issubset(scope) else "denied"


def setup(tmp_path, executor=None, planner=None, evaluator=None, policy=None, limits=None, filename="agentflow.sqlite3"):
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / filename
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    executor = executor or Executor()
    controller = APLController(planner or Planner(), executor, evaluator or Evaluator(), policy or Policy(), store, limits or APLLimits())
    return controller, store, executor


def test_three_job_automatic_loop_and_recovery_after_completion(tmp_path):
    controller, store, executor = setup(tmp_path)
    result = controller.start("goal", ("src",), "cycle")
    assert result.state is APLState.COMPLETED
    assert executor.calls == ["one", "two", "three"]
    assert len(result.outcomes) == 3
    assert store.load("cycle") == result


def test_out_of_scope_proposal_blocked(tmp_path):
    class Outside:
        def plan(self, *args): return (JobProposal("bad", "bad", required_scope=("secret",)),)
    controller, _, _ = setup(tmp_path, planner=Outside())
    assert controller.start("goal", ("src",)).state is APLState.BLOCKED


def test_approval_pending(tmp_path):
    controller, _, _ = setup(tmp_path, policy=Policy("approval_required"))
    assert controller.start("goal", ("src",)).state is APLState.WAITING_APPROVAL


def test_unknown_never_retried_on_recovery(tmp_path):
    class Unknown:
        def __init__(self): self.calls = 0
        def execute(self, *args): self.calls += 1; return ExecutionOutcome("unknown", "ambiguous")
    executor = Unknown()
    controller, _, _ = setup(tmp_path, executor=executor)
    result = controller.start("goal", ("src",), "unknown")
    assert result.state is APLState.BLOCKED
    assert controller.recover("unknown").state is APLState.BLOCKED
    assert executor.calls == 1


def test_failed_job_and_cancel(tmp_path):
    class Failed:
        def execute(self, *args): return ExecutionOutcome("failed", "no")
    controller, _, _ = setup(tmp_path, executor=Failed())
    assert controller.start("goal", ("src",)).state is APLState.FAILED
    controller, _, _ = setup(tmp_path)
    assert controller.start("goal", ("src",), cancel=lambda: True).state is APLState.CANCELLED


def test_job_iteration_and_time_limits(tmp_path):
    controller, _, _ = setup(tmp_path, limits=APLLimits(max_jobs=2))
    assert controller.start("goal", ("src",)).state is APLState.BLOCKED
    controller, _, _ = setup(tmp_path, limits=APLLimits(max_seconds=0.1), executor=Executor())
    ticks = iter((0, 0, 1))
    controller.clock = lambda: next(ticks)
    assert controller.start("goal", ("src",)).state is APLState.BLOCKED


def test_no_progress_and_budget_limits(tmp_path):
    class StuckPlanner:
        def plan(self, *args): return (JobProposal("one", "one", required_scope=("src",)),)
    class StuckEval:
        def evaluate(self, *args): return Evaluation(False, False, False)
    class Repeated:
        def execute(self, *args): return ExecutionOutcome("succeeded", "ok", cost=1, progress_key="same")
    controller, _, _ = setup(tmp_path, executor=Repeated(), planner=StuckPlanner(), evaluator=StuckEval(), limits=APLLimits(max_iterations=5, no_progress_limit=1))
    assert controller.start("goal", ("src",)).state is APLState.BLOCKED
    controller, _, _ = setup(tmp_path, executor=Repeated(), planner=StuckPlanner(), evaluator=StuckEval(), limits=APLLimits(max_budget=.5))
    assert controller.start("goal", ("src",)).state is APLState.BLOCKED


def test_duplicate_concurrent_save_rejected(tmp_path):
    controller, store, _ = setup(tmp_path)
    controller.start("goal", ("src",), "cycle")
    snap = store.load("cycle")
    outcomes = []
    def save():
        try: store.save(snap, snap.iterations)
        except Exception as exc: outcomes.append(exc)
    threads = [Thread(target=save) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert len(outcomes) == 2


def test_read_only_cli_status_history_and_evidence(tmp_path, capsys, monkeypatch):
    from agentflow.cli import main
    import agentflow.cli as cli
    root = tmp_path / "repo"
    (root / ".agentflow").mkdir(parents=True)
    monkeypatch.setattr(cli, "repository_root", lambda path: root)
    controller, _, _ = setup(root / ".agentflow", filename="state.sqlite3")
    controller.start("goal", ("src",), "visible-cycle")
    assert main(["apl", "status", "--path", str(root)]) == 0
    assert "visible-cycle" in capsys.readouterr().out
    assert main(["apl", "history", "--path", str(root), "--cycle-id", "visible-cycle"]) == 0
    assert "COMPLETED" in capsys.readouterr().out
    assert main(["apl", "inspect", "--path", str(root), "--cycle-id", "visible-cycle"]) == 0
    output = capsys.readouterr().out
    assert "Escopo imutável" in output and "evidência:" in output


def supervised_plan():
    tasks = tuple(SimpleNamespace(id=name, title=name.title(), description="work " + name,
                                  depends_on=(previous,) if previous else (),
                                  versioned_paths=(),
                                  acceptance_criteria=(SimpleNamespace(id="ok", description="works"),))
                  for name, previous in (("first", ""), ("second", "first"), ("third", "second")))
    return SimpleNamespace(tasks=tasks, order=("first", "second", "third"),
                           spec_digest="spec-test", tasks_digest="tasks-test")


def test_supervised_start_next_pause_resume_cancel_and_scope(tmp_path):
    from agentflow.state.execution import ExecutionStore
    with ExecutionStore(tmp_path / "state.sqlite3"):
        pass
    store = SQLiteCycleStore(tmp_path / "state.sqlite3")
    service = SupervisedCycleService(store, tmp_path / "state.sqlite3")
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "supervised")
    assert cycle.state is APLState.READY and len(cycle.jobs) == 3
    assert service.next_job(cycle.cycle_id).job_id == "first"
    paused = service.pause(cycle.cycle_id)
    assert paused.state is APLState.PAUSED
    resumed = service.resume(cycle.cycle_id)
    assert resumed.state is APLState.READY
    assert service.next_job(cycle.cycle_id).job_id == "first"
    assert service.cancel(cycle.cycle_id).state is APLState.CANCELLED
    with pytest.raises(ValueError, match="outside"):
        service.start(supervised_plan(), "goal", ("task:first",), "outside")


def test_supervised_pause_concurrency_rejects_stale_transition(tmp_path):
    from agentflow.state.execution import ExecutionStore
    with ExecutionStore(tmp_path / "state.sqlite3"):
        pass
    store = SQLiteCycleStore(tmp_path / "state.sqlite3")
    service = SupervisedCycleService(store, tmp_path / "state.sqlite3")
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "race")
    outcomes = []
    def pause():
        try: outcomes.append(service.pause(cycle.cycle_id))
        except Exception as exc: outcomes.append(exc)
    threads = [Thread(target=pause) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, Exception) for item in outcomes) == 1
    assert len(store.history(cycle.cycle_id)) == 2


def _persisted_attempt(state_path, tmp_path, *, interrupted=False, validated=True):
    from test_planning import SPEC, TASKS, files
    from agentflow.planning import load_plan, digest
    from agentflow.planning.loader import task_data
    from agentflow.agents import ExecutionPolicy, ExecutionRequest, ProcessSpec, ProcessIdentity, ProcessResult
    from agentflow.state.execution import ExecutionStore
    from agentflow.state.contracts import EvidenceInput
    control = files(tmp_path / "project")
    plan = load_plan(control)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = next(item for item in plan.tasks if item.id == "first")
    definition = digest({"task": task_data(task), "spec_digest": plan.spec_digest})
    with ExecutionStore(state_path) as store:
        store.import_plan(plan)
        current = next(item for item in store.list_tasks() if item.task_id == "first")
        request = ExecutionRequest("sample", "first", current.definition_revision, definition, "synthetic-op",
                                   workspace, "synthetic", ExecutionPolicy(sandbox="read-only", approval="never"),
                                   (("ok", "works"),))
        spec = ProcessSpec(Path("synthetic"), (), workspace, "synthetic", (), "fake", "1", "read-only", "never")
        attempt = store.prepare(request, spec, "test")
        if interrupted:
            store.reconcile_interrupted()
            return plan, attempt.attempt_id
        store.started(attempt.attempt_id, ProcessIdentity(11, "start", "11", 1, attempt.attempt_id, "token"))
        empty = tmp_path / "empty.log"
        empty.write_text("", encoding="utf-8")
        result = ProcessResult("process_succeeded", 0, empty, empty, "0" * 64, "0" * 64, 0, 0, "ok")
        store.finish(attempt.attempt_id, result)
        if validated:
            declared = store.add_evidence(EvidenceInput("first", "ok", "declared", "declaration", "test", "decl-op"))
            verified = store.add_evidence(EvidenceInput("first", "ok", "verified", "verified", "test", "verified-op",
                                                        declaration_id=declared, method="test-review", author="test", passed=True))
            store.set_validation(attempt.attempt_id, True, (verified,), "verified fixture")
        return plan, attempt.attempt_id


def test_submit_result_requires_persisted_verified_attempt_and_deduplicates(tmp_path):
    from agentflow.state.execution import ExecutionStore
    state_path = tmp_path / "state.sqlite3"
    from agentflow.state.execution import ExecutionStore
    with ExecutionStore(state_path):
        pass
    apl_store = SQLiteCycleStore(state_path)
    plan, attempt_id = _persisted_attempt(state_path, tmp_path, interrupted=False)
    service = SupervisedCycleService(apl_store, state_path)
    cycle = service.start(plan, "goal", ("task:first", "task:second", "task:third"), "result-cycle")
    submitted = service.submit_result(cycle.cycle_id, attempt_id)
    assert submitted.outcomes[0][1].evidence[0]["criterion_id"] == "ok"
    assert submitted.state is APLState.READY
    with pytest.raises(ValueError, match="already associated"):
        service.submit_result(cycle.cycle_id, attempt_id)
    second = service.start(plan, "goal", ("task:first", "task:second", "task:third"), "second-cycle")
    with pytest.raises(ValueError, match="another cycle"):
        service.submit_result(second.cycle_id, attempt_id)


def test_execution_store_exposes_only_evidence_bound_to_validation_event(tmp_path):
    from agentflow.state.execution import ExecutionStore
    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    _, attempt_id = _persisted_attempt(state_path, tmp_path)
    with ExecutionStore(state_path) as store:
        evidence = store.validated_evidence(attempt_id)
        assert len(evidence) == 1
        assert evidence[0].criterion_id == "ok"
        assert evidence[0].passed is True
        assert evidence[0].evidence_id > 0


def test_execution_store_rejects_validation_event_with_unbound_evidence(tmp_path):
    from agentflow.state.execution import ExecutionStore
    from agentflow.state.contracts import TransitionError
    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    _, attempt_id = _persisted_attempt(state_path, tmp_path)
    with ExecutionStore(state_path) as store:
        store.db.execute(
            "UPDATE execution_events SET payload=? WHERE attempt_id=? AND kind='validation_passed'",
            ('{"evidence_ids":[999999],"reason":"test tamper"}', attempt_id),
        )
        with pytest.raises(TransitionError, match="does not match validation event"):
            store.validated_evidence(attempt_id)


def test_unverified_persisted_attempt_is_rejected(tmp_path):
    from agentflow.state.execution import ExecutionStore
    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    store = SQLiteCycleStore(state_path)
    plan, attempt_id = _persisted_attempt(state_path, tmp_path, validated=False)
    service = SupervisedCycleService(store, state_path)
    cycle = service.start(plan, "goal", ("task:first", "task:second", "task:third"), "unverified-cycle")
    with pytest.raises(ValueError, match="validation_passed"):
        service.submit_result(cycle.cycle_id, attempt_id)
    assert store.load(cycle.cycle_id).state is APLState.READY


def test_unknown_persisted_attempt_blocks_cycle(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    from agentflow.state.execution import ExecutionStore
    with ExecutionStore(state_path):
        pass
    store = SQLiteCycleStore(state_path)
    plan, attempt_id = _persisted_attempt(state_path, tmp_path, interrupted=True)
    service = SupervisedCycleService(store, state_path)
    cycle = service.start(plan, "goal", ("task:first", "task:second", "task:third"), "unknown-cycle")
    with pytest.raises(ValueError, match="unknown execution outcome"):
        service.submit_result(cycle.cycle_id, attempt_id)
    assert store.load(cycle.cycle_id).state is APLState.BLOCKED


def test_cli_supervised_lifecycle_does_not_execute(tmp_path, monkeypatch, capsys):
    import agentflow.cli as cli
    from agentflow.cli import main
    from test_planning import SPEC, TASKS, files
    from agentflow.planning import load_plan
    from agentflow.state.execution import ExecutionStore
    root = tmp_path / "repo"
    root.mkdir()
    control = files(root / ".agentflow", TASKS, SPEC)
    state_path = control / "state.sqlite3"
    with ExecutionStore(state_path) as state:
        state.import_plan(load_plan(control))
    monkeypatch.setattr(cli, "repository_root", lambda path: root)
    base = ["--path", str(root)]
    assert main(["apl", "start", *base, "--profile", "developer", "--objective", "goal", "--scope", "task:first", "--scope", "task:second"]) == 0
    cycle_id = capsys.readouterr().out.split(": ", 1)[1].split()[0]
    assert main(["apl", "replan", *base, "--cycle-id", cycle_id,
                 "--planner", "codex"]) == 2
    assert "--enable-experimental-codex" in capsys.readouterr().err
    assert main(["apl", "next", *base, "--cycle-id", cycle_id]) == 0
    proposed = capsys.readouterr().out
    assert "first" in proposed and "Nenhuma execução" in proposed
    assert main(["apl", "pause", *base, "--cycle-id", cycle_id]) == 0
    assert "PAUSED" in capsys.readouterr().out
    assert main(["apl", "resume", *base, "--cycle-id", cycle_id]) == 0
    assert "READY" in capsys.readouterr().out
    assert main(["apl", "cancel", *base, "--cycle-id", cycle_id]) == 0
    assert "CANCELLED" in capsys.readouterr().out


def test_cli_replan_proposals_accept_and_reject(tmp_path, monkeypatch, capsys):
    import agentflow.cli as cli
    from agentflow.cli import main
    from test_planning import SPEC, TASKS, files
    from agentflow.planning import load_plan
    from agentflow.state.execution import ExecutionStore
    root = tmp_path / "repo"
    root.mkdir()
    control = files(root / ".agentflow", TASKS, SPEC)
    with ExecutionStore(control / "state.sqlite3") as state:
        state.import_plan(load_plan(control))
    monkeypatch.setattr(cli, "repository_root", lambda path: root)
    base = ["--path", str(root)]
    assert main(["apl", "start", *base, "--profile", "developer", "--objective", "goal",
                 "--scope", "task:first", "--scope", "task:second"]) == 0
    cycle_id = capsys.readouterr().out.split(": ", 1)[1].split()[0]
    assert main(["apl", "replan", *base, "--cycle-id", cycle_id, "--planner", "deterministic", "--request-id", "rp1"]) == 0
    output = capsys.readouterr().out
    proposal_id = output.split()[1].rstrip(":")
    assert "nenhuma inferência semântica" in output
    assert main(["apl", "proposals", *base, "--cycle-id", cycle_id]) == 0
    assert proposal_id in capsys.readouterr().out
    assert main(["apl", "reject-plan", *base, "--cycle-id", cycle_id,
                 "--proposal-id", proposal_id]) == 0
    assert "rejeitada" in capsys.readouterr().out
    assert main(["apl", "replan", *base, "--cycle-id", cycle_id, "--planner", "deterministic", "--request-id", "rp2"]) == 0
    proposal_two = capsys.readouterr().out.split()[1].rstrip(":")
    assert main(["apl", "accept-plan", *base, "--cycle-id", cycle_id,
                 "--proposal-id", proposal_two, "--confirm"]) == 0
    assert "Plano aceito" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(["apl", "replan", *base, "--cycle-id", cycle_id, "--planner", "semantic"])
    assert error.value.code == 2


def test_semantic_replan_proposal_acceptance_is_immutable(tmp_path):
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    service = SupervisedCycleService(store, path)
    plan = supervised_plan()
    scope = ("task:first", "task:second", "task:third", "task:fourth")
    cycle = service.start(plan, "goal", scope, "replan")
    new_job = JobProposal("fourth", "fourth task", ("third",), ("task:fourth",), ("done",))
    proposal, status, created = service.replan(cycle.cycle_id,
        DeterministicPlannerAdapter((TaskChange("add", new_job),), "fixture adds fourth"), "request-add")
    assert status == "proposed" and created
    assert len(store.load(cycle.cycle_id).jobs) == 3
    before = store.plan_revisions(cycle.cycle_id)
    with pytest.raises(ValueError, match="confirmation"):
        service.accept_plan(cycle.cycle_id, proposal.proposal_id)
    accepted = service.accept_plan(cycle.cycle_id, proposal.proposal_id, confirmed=True)
    assert len(accepted.jobs) == 4
    assert accepted.plan_revision_id != cycle.plan_revision_id
    assert len(store.plan_revisions(cycle.cycle_id)) == len(before) + 1
    assert store.proposals(cycle.cycle_id)[0][2] == "accepted"
    with pytest.raises(ValueError, match="accepted"):
        service.accept_plan(cycle.cycle_id, proposal.proposal_id, confirmed=True)


def test_replan_is_idempotent_and_reject_is_audited(tmp_path):
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    service = SupervisedCycleService(store, path)
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "idempotent")
    adapter = DeterministicPlannerAdapter(rationale="no semantic inference")
    first, _, created = service.replan(cycle.cycle_id, adapter, "same-request")
    second, _, created_again = service.replan(cycle.cycle_id, adapter, "same-request")
    assert first.proposal_id == second.proposal_id and created and not created_again
    assert service.reject_plan(cycle.cycle_id, first.proposal_id)
    assert not service.reject_plan(cycle.cycle_id, first.proposal_id)
    assert store.proposals(cycle.cycle_id)[0][2] == "rejected"


@pytest.mark.parametrize("change, message", [
    (lambda done, scope: TaskChange("add", JobProposal("outside", "x", (), ("other",), ("ok",))), "scope"),
    (lambda done, scope: TaskChange("add", JobProposal("fourth", "x", ("missing",), ("task:fourth",), ("ok",))), "unknown dependencies"),
    (lambda done, scope: TaskChange("add", JobProposal("fourth", "x", ("fifth",), ("task:fourth",), ("ok",))) , "unknown dependencies"),
    (lambda done, scope: TaskChange("modify", done[0],), "previously executed task"),
])
def test_replan_rejects_unsafe_changes(change, message):
    jobs = (JobProposal("first", "one", (), ("task:first",), ("ok",)),
            JobProposal("second", "two", ("first",), ("task:second",), ("ok",)))
    completed_job = jobs[0]
    context = ReplanContext("goal", ("task:first", "task:second", "task:fourth", "task:fifth"),
                            jobs, "revision-1", ("first",), (), (), (),
                            RemainingLimits(5, 5, 10, 0), 2)
    task_change = change((completed_job,), context.authorized_scope)
    proposal = PlanProposal("proposal", "revision-1", 2, "reason", (task_change,), "done")
    with pytest.raises(ValueError, match=message):
        validate_proposal(context, proposal, max_jobs=10)


def test_replan_rejects_cycle_and_untrusted_evidence_reference():
    one = JobProposal("one", "one", (), ("one",), ("ok",))
    two = JobProposal("two", "two", ("one",), ("two",), ("ok",))
    context = ReplanContext("goal", ("one", "two", "three"), (one, two), "rev", (), (),
                            (EvidenceReference(7, "one", "ok"),), (), RemainingLimits(3, 3, 3, 0))
    cycle = JobProposal("one", "changed", ("two",), ("one",), ("ok",))
    proposal = PlanProposal("p1", "rev", 0, "cycle", (TaskChange("modify", cycle),), "done")
    with pytest.raises(ValueError, match="cycle"):
        validate_proposal(context, proposal, max_jobs=10)
    evidence_proposal = PlanProposal("p2", "rev", 0, "bad evidence", (), "done", (999,))
    with pytest.raises(ValueError, match="evidence"):
        validate_proposal(context, evidence_proposal, max_jobs=10)


def test_blocker_removal_requires_verified_evidence_and_unknown_stays_blocked(tmp_path):
    from dataclasses import replace
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    service = SupervisedCycleService(store, path)
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "unknown-replan")
    blocked = store.save(replace(cycle, state=APLState.BLOCKED, reason="unknown attempt outcome"), cycle.iterations,
                         expected_revision=cycle.revision)
    proposal = PlanProposal("x", blocked.plan_revision_id, blocked.revision, "clear blocker", (), "done",
                            (), (blocked.reason,))
    class Adapter:
        def propose(self, context): return proposal
    with pytest.raises(ValueError, match="without verified evidence"):
        service.replan(blocked.cycle_id, Adapter())
    assert store.load(blocked.cycle_id).state is APLState.BLOCKED


def test_concurrent_acceptance_has_one_winner_and_recovery_persists(tmp_path):
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    service = SupervisedCycleService(store, path)
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "accept-race")
    proposal, _, _ = service.replan(cycle.cycle_id, DeterministicPlannerAdapter(), "race")
    outcomes = []
    def accept():
        try: outcomes.append(service.accept_plan(cycle.cycle_id, proposal.proposal_id, confirmed=True))
        except Exception as exc: outcomes.append(exc)
    threads = [Thread(target=accept) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, Exception) for item in outcomes) == 1
    reopened = SQLiteCycleStore(path, initialize=False)
    assert len(reopened.plan_revisions(cycle.cycle_id)) == 2
    assert reopened.proposals(cycle.cycle_id)[0][2] == "accepted"


def test_untrusted_planner_response_rejects_command_shaped_fields():
    payload = {"proposal_id": "p", "source_revision_id": "r", "source_cycle_revision": 1,
               "rationale": "reason", "changes": [], "completion_condition": "done",
               "evidence_used": [], "resolves_blockers": [], "schema_version": 1}
    assert proposal_from_mapping(payload).proposal_id == "p"
    malicious = dict(payload, command="powershell -Command Invoke-WebRequest")
    with pytest.raises(ValueError, match="unsupported fields"):
        proposal_from_mapping(malicious)
    malicious_task = dict(payload, changes=[{"action": "add", "task": {
        "job_id": "new", "instruction": "implement", "depends_on": [],
        "required_scope": ["task:new"], "acceptance_criteria": ["ok"],
        "shell_command": "whoami"}}])
    with pytest.raises(ValueError, match="command, tool"):
        proposal_from_mapping(malicious_task)


def test_semantic_planner_unavailable_and_adapter_failure_persist_nothing(tmp_path):
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    service = SupervisedCycleService(store, path)
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "planner-fail")
    with pytest.raises(PlannerUnavailable, match="no authorized local semantic planner"):
        service.replan(cycle.cycle_id, UnavailableSemanticPlanner(), "unavailable")
    class Broken:
        def propose(self, context): raise RuntimeError("simulated planner failure")
    with pytest.raises(RuntimeError, match="simulated planner failure"):
        service.replan(cycle.cycle_id, Broken(), "broken")
    assert store.proposals(cycle.cycle_id) == ()
    assert [event["status"] for event in store.planner_events(cycle.cycle_id)] == [
        "started", "failed", "started", "failed"]


def test_interrupted_planner_request_is_not_replayed_and_success_is_idempotent(tmp_path):
    from hashlib import sha256
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    service = SupervisedCycleService(store, path)
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "planner-idempotent")
    class Counting:
        calls = 0
        def propose(self, context):
            self.calls += 1
            return PlanProposal("local", context.plan_revision_id, context.cycle_revision,
                                "no changes", (), "current criteria remain valid")
    adapter = Counting()
    first, _, created = service.replan(cycle.cycle_id, adapter, "stable-request")
    second, _, created_again = service.replan(cycle.cycle_id, adapter, "stable-request")
    assert first.proposal_id == second.proposal_id and created and not created_again
    assert adapter.calls == 1
    incomplete = sha256("new request".encode()).hexdigest()
    store.claim_planner_request(cycle.cycle_id, cycle.revision, incomplete, "test")
    reopened = SQLiteCycleStore(path, initialize=False)
    with pytest.raises(RuntimeError, match="already started"):
        reopened.claim_planner_request(cycle.cycle_id, cycle.revision, incomplete, "test")


@pytest.mark.parametrize("error", [TimeoutError("simulated planner timeout"), InterruptedError("simulated cancellation")])
def test_planner_timeout_and_cancel_leave_no_proposal(tmp_path, error):
    from agentflow.state.execution import ExecutionStore
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    store = SQLiteCycleStore(path)
    service = SupervisedCycleService(store, path)
    cycle = service.start(supervised_plan(), "goal", ("task:first", "task:second", "task:third"), "planner-abort")
    class Aborted:
        def propose(self, context): raise error
    with pytest.raises(type(error)):
        service.replan(cycle.cycle_id, Aborted())
    assert store.proposals(cycle.cycle_id) == ()
