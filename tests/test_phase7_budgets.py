from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agentflow.state import (BudgetAccount, BudgetStore, BudgetVector, ConflictError, IntegrityError,
                             AgentExecutionStore)
from agentflow.agents import ExecutionPolicy, ExecutionRequest, ModelSelectionRequest, ProcessSpec
from test_planning import TASKS, files
from agentflow.planning import load_plan


def initialized(tmp_path, *, cap="10.00"):
    path = tmp_path / "state.sqlite3"
    plan = load_plan(files(tmp_path / "plan", TASKS))
    store = BudgetStore(path)
    store.import_plan(plan)
    store.create_budget_account(BudgetAccount(
        "execution-account", "sample", "execution", "exec-1", "USD", cap,
        1000, 3600, "applied_verified", "applied_verified", "applied_verified",
        "local-reservation-ledger-v1", "local-reservation-ledger-v1",
        "local-reservation-ledger-v1"), "account-execution")
    store.create_budget_account(BudgetAccount(
        "task-account", "sample", "task", "first", "USD", cap,
        1000, 3600, "applied_verified", "estimated", "unavailable",
        "local-reservation-ledger-v1"), "account-task")
    return store, plan


def estimate(cost="1.00", tokens=100, seconds=30, states=("estimated", "known", "known")):
    return BudgetVector(cost, tokens, seconds, "USD", *states)


def reserve(store, op="reserve-1", e=estimate()):
    return store.reserve_budget(op, project_id="sample", task_id="first", execution_id="exec-1",
                                attempt_id=None, account_ids=("execution-account", "task-account"),
                                estimate=e)


def test_budget_v7_migration_reservation_is_idempotent_and_reconciles_once(tmp_path):
    store, _ = initialized(tmp_path)
    with store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 7
        first = reserve(store)
        assert reserve(store) == first
        with pytest.raises(ConflictError, match="parâmetros diferentes"):
            reserve(store, e=estimate("2.00"))
        usage = BudgetVector("0.40", 70, 20, "USD", "known", "known", "known")
        settled = store.reconcile_budget("settle-1", first.reservation_id, usage, source="provider-receipt")
        assert settled.status == "reconciled"
        assert store.reconcile_budget("settle-1", first.reservation_id, usage, source="provider-receipt") == settled
        assert store.budget_account_usage("execution-account")["cost_committed"] == "0.40"
        assert store.budget_account_usage("execution-account")["tokens_committed"] == 70
        assert store.budget_account_usage("execution-account")["provider_invoice_enforcement"] == "unavailable"
        assert store.db.execute("SELECT count(*) FROM budget_ledger WHERE kind='reserved'").fetchone()[0] == 2


def test_budget_v7_migration_failure_preserves_integral_v6_schema(monkeypatch, tmp_path):
    import sqlite3
    import agentflow.state.sqlite as sqlite_module
    path = tmp_path / "state.sqlite3"
    with AgentExecutionStore(path) as base:
        assert base.db.execute("PRAGMA user_version").fetchone()[0] == 5
    monkeypatch.setattr(sqlite_module, "BUDGET_SCHEMA",
                        ("CREATE TABLE should_rollback(id INTEGER)", "invalid sql"))
    with pytest.raises(IntegrityError, match="migração v7 falhou"):
        BudgetStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
        assert db.execute("SELECT name FROM sqlite_master WHERE name='plan_revisions'").fetchone() is not None
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_reservations_cannot_double_spend_budget(tmp_path):
    store, _ = initialized(tmp_path)
    path = store.path
    store.close()

    def worker(index):
        with BudgetStore(path) as concurrent:
            try:
                return reserve(concurrent, f"parallel-{index}", estimate("7.00", 100, 10))
            except ConflictError:
                return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert sum(result != "rejected" for result in results) == 1
    with BudgetStore(path) as check:
        assert check.budget_account_usage("execution-account")["cost_committed"] == "7.00"


def test_interrupted_reservation_holds_full_unknown_budget_until_reconciled(tmp_path):
    store, _ = initialized(tmp_path, cap="1.00")
    with store:
        unknown = BudgetVector(None, None, None, "USD", "unknown", "unknown", "unknown")
        first = reserve(store, "unknown-reservation", unknown)
        assert store.recover_budget_reservations() == (first.reservation_id,)
        assert store.recover_budget_reservations() == ()
        assert store.budget_account_usage("execution-account")["cost_committed"] == "1.00"
        with pytest.raises(ConflictError, match="limit exceeded"):
            reserve(store, "blocked-by-unknown", estimate("0.01", 1, 1))
        partial = BudgetVector("0.20", 20, None, "USD", "known", "known", "unknown")
        assert store.reconcile_budget("partial-usage", first.reservation_id, partial,
                                      source="usage-probe").status == "unknown"
        final = BudgetVector("0.25", 30, 10, "USD", "known", "known", "known")
        assert store.reconcile_budget("final-usage", first.reservation_id, final,
                                      source="usage-reconciliation").status == "reconciled"
        assert store.budget_account_usage("execution-account")["cost_committed"] == "0.25"
        assert store.budget_account_usage("execution-account")["tokens_committed"] == 30
        assert store.budget_account_usage("execution-account")["seconds_committed"] == 10


def test_budget_account_and_usage_validation_and_limits(tmp_path):
    with pytest.raises(ValueError, match="requires a verification evidence"):
        BudgetAccount("missing-evidence", "sample", "execution", "exec-1", "USD", "2.00",
                      cost_status="applied_verified")
    store, _ = initialized(tmp_path, cap="2.00")
    with store:
        with pytest.raises(ConflictError, match="account_id"):
            store.create_budget_account(BudgetAccount(
                "execution-account", "sample", "execution", "exec-1", "USD", "99.00"), "new-account")
        reserve(store, "limit-1", estimate("1.25"))
        with pytest.raises(ConflictError, match="limit exceeded"):
            reserve(store, "limit-2", estimate("1.00"))
        assert store.db.execute("SELECT count(*) FROM budget_ledger WHERE kind='reserved'").fetchone()[0] == 2


def test_unknown_usage_requires_unknown_state_and_currency_match(tmp_path):
    with pytest.raises(ValueError, match="value and measurement state diverge"):
        BudgetVector("0.10", 1, 1, "USD", "unknown", "known", "known")
    store, _ = initialized(tmp_path)
    with store:
        first = reserve(store)
        with pytest.raises(ConflictError, match="currency"):
            store.reconcile_budget("wrong-currency", first.reservation_id,
                BudgetVector("0.1", 1, 1, "EUR", "known", "known", "known"), source="test")


def test_reservation_can_be_scoped_to_prepared_attempt_without_spawning(tmp_path):
    store, plan = initialized(tmp_path)
    path = store.path
    task = next(item for item in plan.tasks if item.id == "first")
    definition_digest = store.db.execute(
        "SELECT definition_digest FROM task_definitions WHERE project_id='sample' AND task_id='first'").fetchone()[0]
    store.close()
    with AgentExecutionStore(path) as execution_store:
        selection = ModelSelectionRequest("simulated-adapter", "simulated-provider", "simulated-model",
                                          "test-principal")
        request = ExecutionRequest("sample", "first", 1, definition_digest, "budget-attempt-op",
            tmp_path, "simulated instruction", ExecutionPolicy(), model_selection=selection)
        spec = ProcessSpec(Path("simulated-executable"), (), tmp_path, "simulated instruction", (),
                           "simulated-adapter", "1", "inherited", "inherited",
                           provider_id="simulated-provider", model_id="simulated-model")
        attempt = execution_store.prepare(request, spec, "simulated-owner")
        assert attempt.status == "prepared"
        assert execution_store.db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0] == 1
    with BudgetStore(path) as budgets:
        budgets.create_budget_account(BudgetAccount(
            "attempt-account", "sample", "attempt", attempt.attempt_id, "USD", "10.00",
            1000, 3600, "estimated", "estimated", "estimated"), "account-attempt")
        result = budgets.reserve_budget("reserve-attempt", project_id="sample", task_id="first",
            execution_id="exec-1", attempt_id=attempt.attempt_id,
            account_ids=("execution-account", "task-account", "attempt-account"),
            estimate=estimate("0.10", 20, 5))
        assert result.status == "reserved"
