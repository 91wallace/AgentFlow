import pytest
import json
from datetime import datetime, timedelta, timezone
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

from agentflow.approval import (ApprovalBroker, ApprovalError, ApprovalRequest,
                                GrantReference, UnavailableApprovalAuthority, verify_and_consume)
from agentflow.agents import (ModelCatalogEntry, ModelDescriptor, ModelRoutingPolicy, TaskClassification,
                              classify_task, evaluate_routing)
from agentflow.planning import Criterion, PlanError, Task, canonical, digest, load_plan
from agentflow.state import AgentExecutionStore, ConflictError, IntegrityError, SDDStore
from test_planning import SPEC, TASKS, files


TASKS_V4 = '''schema_version = 4
deferred_requirements = [{ id = "spec-ok", rationale = "Deferred by approved plan proposal" }]
[[tasks]]
id = "first"
title = "First"
description = "Work"
versioned_paths = []
gates = []
[[tasks.acceptance_criteria]]
id = "done"
description = "Done"
'''


def test_v4_requirement_trace_and_explicit_deferral(tmp_path):
    directory = files(tmp_path / "plan", TASKS_V4)
    plan = load_plan(directory)
    assert plan.deferred_requirements[0].id == "spec-ok"
    (directory / "tasks.toml").write_text(TASKS_V4.replace(
        'deferred_requirements = [{ id = "spec-ok", rationale = "Deferred by approved plan proposal" }]',
        "").replace('description = "Done"', 'description = "Done"\nsatisfies = ["spec-ok"]'), encoding="utf-8")
    plan = load_plan(directory)
    assert plan.tasks[0].acceptance_criteria[0].satisfies == ("spec-ok",)
    assert not plan.deferred_requirements


def test_v4_rejects_requirement_that_is_unmapped_and_not_deferred(tmp_path):
    with pytest.raises(PlanError, match="sem rastreabilidade"):
        load_plan(files(tmp_path / "plan", TASKS_V4.replace(
            'deferred_requirements = [{ id = "spec-ok", rationale = "Deferred by approved plan proposal" }]', "")))


def test_v6_migration_sdd_proposals_are_append_only_and_idempotent(tmp_path):
    plan = load_plan(files(tmp_path / "plan", TASKS))
    path = tmp_path / "state.sqlite3"
    with SDDStore(path) as store:
        store.import_plan(plan)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 7
        first = store.propose(plan, "proposal-1")
        assert store.propose(plan, "proposal-1") == first
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("UPDATE plan_revisions SET status='accepted'")
        with pytest.raises(ConflictError, match="operation_id reutilizado"):
            store.propose(load_plan(files(tmp_path / "other", TASKS.replace("First works", "changed"))), "proposal-1")
        assert store.db.execute("SELECT count(*) FROM sdd_events").fetchone()[0] == 1
    with SDDStore(path) as reopened:
        assert reopened.db.execute("SELECT count(*) FROM plan_revisions").fetchone()[0] == 1


def test_concurrent_identical_sdd_proposals_are_idempotent(tmp_path):
    plan = load_plan(files(tmp_path / "plan", TASKS))
    path = tmp_path / "state.sqlite3"
    with SDDStore(path) as store:
        store.import_plan(plan)

    def propose():
        with SDDStore(path) as store:
            return store.propose(plan, "concurrent-proposal")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: propose(), range(4)))
    assert len(set(results)) == 1
    with SDDStore(path) as store:
        assert store.db.execute("SELECT count(*) FROM plan_revisions").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM sdd_events").fetchone()[0] == 1


def test_deferred_rationale_changes_plan_digest(tmp_path):
    directory = files(tmp_path / "plan", TASKS_V4)
    first = load_plan(directory)
    (directory / "tasks.toml").write_text(TASKS_V4.replace("approved plan proposal", "later"), encoding="utf-8")
    second = load_plan(directory)
    assert first.tasks_digest != second.tasks_digest


def test_catalog_and_routing_audit_are_immutable_preview_records(tmp_path):
    from agentflow.agents import ModelCatalogEntry, ModelDescriptor, ProviderDescriptor
    directory = files(tmp_path / "plan", TASKS_V4)
    plan = load_plan(directory)
    with SDDStore(tmp_path / "state.sqlite3") as store:
        store.import_plan(plan)
        proposal = store.propose(plan, "initial")
        provider = ProviderDescriptor("provider", "1", "1", source="test-fixture")
        model = ModelCatalogEntry(ModelDescriptor("provider", "model", "1", ("quality:advanced",), availability="available",
            source="test-fixture"), ("quality:advanced",), "test-fixture")
        catalog_digest = store.record_catalog((provider,), (model,), "fixture-v1")
        assert store.record_catalog((provider,), (model,), "fixture-v1") == catalog_digest
        classification = {"complexity": "simple", "risk": "low", "source": "deterministic_rule", "reasons": []}
        decision = {"status": "proposal", "provider_id": "provider", "model_id": "model",
                    "reason": "lowest_declared_cost_within_known_capabilities", "alternatives": [["provider", "model"]],
                    "verified_capabilities": ["quality:advanced"], "rejected_alternatives": [],
                    "constraints": ["code"], "budget_prediction": {"cost_status": "estimated"}}
        policy_snapshot = {"policy": "v1"}
        policy_digest = digest(policy_snapshot)
        budget_snapshot = {"status": "estimated", "currency": "USD", "cost_estimate": "0.01",
                           "token_limit": 100, "time_limit_seconds": None, "constraints": ["max_cost"]}
        result_digest = store.record_routing_decision("decision-1", "sample", proposal.plan_revision_id,
            "first", classification, catalog_digest, policy_digest, decision, policy_snapshot, budget_snapshot)
        assert store.record_routing_decision("decision-1", "sample", proposal.plan_revision_id,
            "first", classification, catalog_digest, policy_digest, decision, policy_snapshot, budget_snapshot) == result_digest
        with pytest.raises(ValueError, match="snapshot and digest diverge"):
            store.record_routing_decision("decision-2", "sample", proposal.plan_revision_id,
                "first", classification, catalog_digest, policy_digest, decision, {"policy": "changed"}, budget_snapshot)
        stored = store.db.execute("SELECT decision_json FROM routing_decisions WHERE decision_id='decision-1'").fetchone()[0]
        assert '"dispatch_allowed":false' in stored
        assert store.db.execute("SELECT policy_snapshot_json FROM routing_decisions WHERE decision_id='decision-1'").fetchone()[0] == '{"policy":"v1"}'
        spec_snapshot, spec_digest = store.db.execute("SELECT specification_snapshot_json,specification_digest FROM routing_decisions WHERE decision_id='decision-1'").fetchone()
        assert digest(json.loads(spec_snapshot)) == spec_digest
        assert store.db.execute("SELECT budget_snapshot_json FROM routing_decisions WHERE decision_id='decision-1'").fetchone()[0] == canonical(budget_snapshot)
        assert store.inspect_audit()["routing_decisions"] == (("decision-1", result_digest),)


def test_proposal_does_not_accept_without_independent_authority(tmp_path):
    authority = UnavailableApprovalAuthority()
    assert authority.status().available is False
    request = ApprovalRequest("op", "p", "accept", "sha256:" + "a"*64, "s1",
                              "sha256:" + "b"*64, "p1", "sha256:" + "c"*64,
                              "sha256:" + "d"*64)
    assert not authority.verify(request, GrantReference("g", "sha256:" + "e"*64))
    with pytest.raises(ApprovalError, match="approval_unavailable"):
        authority.consume(request, GrantReference("g", "sha256:" + "e"*64))


def test_broker_fails_closed_on_unverified_boundary_and_has_no_issue_api(monkeypatch):
    broker = ApprovalBroker()
    assert not broker.status().available
    assert not hasattr(broker, "issue")
    monkeypatch.setenv("AGENTFLOW_APPROVAL_ENABLED", "1")
    monkeypatch.setenv("AGENTFLOW_BROKER_URL", "https://localhost")
    assert not ApprovalBroker().status().available
    with pytest.raises(TypeError):
        ApprovalBroker(object())


def test_verify_and_consume_fails_closed_before_touching_grant():
    class Authority(UnavailableApprovalAuthority):
        def verify(self, request, reference):
            raise AssertionError("unavailable authority must not inspect grant")

    with pytest.raises(ApprovalError, match="approval_unavailable"):
        verify_and_consume(Authority(), ApprovalRequest("op", "p", "accept", "d", "s", "d", "p", "d", "d"),
                           GrantReference("g", "d"))


def test_simulated_authority_rejects_replay_expiry_and_changed_request_concurrently():
    from agentflow.approval import ApprovalStatus
    from agentflow.planning import digest

    request = ApprovalRequest("op", "p", "accept", "scope", "spec-1", "spec-digest",
                              "plan-1", "plan-digest", "impact-digest")
    ref = GrantReference("grant-1", digest(request.__dict__))

    class SimulatedAuthority:
        def __init__(self, expires):
            self.expires = expires
            self.used = set()
            self.lock = Lock()

        def status(self):
            return ApprovalStatus(True, "SIMULATOR ONLY")

        def verify(self, req, grant):
            return (grant.request_digest == digest(req.__dict__) and
                    datetime.now(timezone.utc) < self.expires and grant.grant_id not in self.used)

        def consume(self, req, grant):
            with self.lock:
                if not self.verify(req, grant):
                    raise ApprovalError("replay/expiry/digest mismatch")
                self.used.add(grant.grant_id)
                return "simulated-receipt"

    live = SimulatedAuthority(datetime.now(timezone.utc) + timedelta(seconds=10))
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: _consume_simulated(live, request, ref), range(8)))
    assert sum(outcome == "simulated-receipt" for outcome in outcomes) == 1
    assert sum(outcome == "rejected" for outcome in outcomes) == 7

    expired = SimulatedAuthority(datetime.now(timezone.utc) - timedelta(seconds=1))
    assert _consume_simulated(expired, request, ref) == "rejected"
    changed = ApprovalRequest("op", "p", "accept", "changed-scope", "spec-1", "spec-digest",
                              "plan-1", "plan-digest", "impact-digest")
    assert _consume_simulated(SimulatedAuthority(datetime.now(timezone.utc) + timedelta(seconds=10)),
                              changed, ref) == "rejected"


def _consume_simulated(authority, request, reference):
    try:
        return verify_and_consume(authority, request, reference)
    except ApprovalError:
        return "rejected"


def test_model_routing_is_deterministic_preview_and_rejects_unknown_capabilities():
    from datetime import datetime, timezone
    observed = datetime.now(timezone.utc).isoformat()
    policy = ModelRoutingPolicy("policy", 1, True, ("code",), (), (), 1,
                                1000, None, "2.00", "USD", True)
    classification = TaskClassification("standard", "low", ("code",), "deterministic_rule")
    models = (
        ModelCatalogEntry(ModelDescriptor("provider-z", "model-z", "1", ("code",), context_tokens=2000, availability="available",
                        observed_at=observed, source="local-catalog", input_cost_per_million="0.20",
                        output_cost_per_million="0.30", cost_currency="USD", cost_source="fixture"),
                        ("code",), "simulated-probe"),
        ModelCatalogEntry(ModelDescriptor("provider-a", "model-a", "1", ("code",), context_tokens=2000, availability="unknown",
                        observed_at=observed, source="local-catalog", input_cost_per_million="0.01",
                        output_cost_per_million="0.01", cost_currency="USD", cost_source="fixture")),
    )
    result = evaluate_routing(classification, policy, models)
    assert (result.provider_id, result.model_id) == ("provider-z", "model-z")
    assert result.status == "proposal"
    assert result.authorization_required and not result.dispatch_allowed
    assert result.budget_status == "estimated"


def test_declared_but_unverified_model_capability_is_unknown():
    from datetime import datetime, timezone
    model = ModelDescriptor("provider", "model", "1", ("code",), availability="available",
                            observed_at=datetime.now(timezone.utc).isoformat(), source="declaration")
    entry = ModelCatalogEntry(model)
    assert entry.unknown_capabilities == ("code",)
    policy = ModelRoutingPolicy("policy", 1, True, ("code",), (), (), 1, None, None, None, None, True)
    result = evaluate_routing(TaskClassification("simple", "low", (), "declared"), policy, (entry,))
    assert result.status == "blocked"


def test_classifier_is_deterministic_and_conservative():
    task = Task("secure", "Secure", "implement", (Criterion("done", "done"),),
                versioned_paths=("src/security/auth.py",))
    first = classify_task(task)
    assert first == classify_task(task)
    assert first.source == "deterministic_rule"
    assert first.risk == "high"
    assert "quality:advanced" in first.required_capabilities


def test_unverifiable_routing_budgets_block_preview():
    policy = ModelRoutingPolicy("policy", 1, True, (), (), (), 1,
                                None, 10, "1.00", "USD", True)
    classification = TaskClassification("simple", "low", (), "declared")
    assert evaluate_routing(classification, policy, ()).reason == "latency_budget_unverifiable_from_catalog"


def test_sdd_v6_migration_failure_rolls_back(monkeypatch, tmp_path):
    import agentflow.state.sqlite as sqlite_module
    path = tmp_path / "state.sqlite3"
    with AgentExecutionStore(path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 5
    monkeypatch.setattr(sqlite_module, "SDD_SCHEMA", ("CREATE TABLE should_rollback(id INTEGER)", "invalid sql"))
    with pytest.raises(IntegrityError, match="migração v6 falhou"):
        SDDStore(path)
    import sqlite3
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert db.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
