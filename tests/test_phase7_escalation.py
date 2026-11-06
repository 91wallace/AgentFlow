from datetime import datetime, timezone
from pathlib import Path

import pytest

from agentflow.agents import (ExecutionPolicy, ExecutionRequest, ModelCatalogEntry,
                              ModelDescriptor, ModelSelectionObservation, ModelSelectionRequest,
                              ProcessResult, ProcessSpec)
from agentflow.state import AgentExecutionStore, BudgetStore, ConflictError
from agentflow.validation import SnapshotManager, ValidationScheduler
from test_phase5_scheduler import FakeRuntime, FakeValidator, make_validation_project, request_for


def test_escalation_proposal_requires_verifiable_gate_and_is_idempotent(tmp_path):
    repository, plan, validation_store, provider, workspace, checkpoint = make_validation_project(
        tmp_path, gates=[{"id": "gate-a", "dependencies": []}], tasks_schema=4)
    database = validation_store.path
    definition_digest = validation_store.db.execute(
        "SELECT definition_digest FROM task_definitions WHERE task_id='task-one'").fetchone()[0]
    validation_store.close()

    source = ModelSelectionRequest("sim-adapter", "sim-provider", "sim-model", "test-operator")
    execution_request = ExecutionRequest(
        plan.specification.project_id, "task-one", 1, definition_digest, "sim-attempt-operation",
        workspace.path, "simulated task", ExecutionPolicy(), workspace_id=workspace.workspace_id,
        model_selection=source)
    process_spec = ProcessSpec(Path("simulated-runtime"), (), workspace.path, "simulated task", (),
        "sim-adapter", "1", "inherited", "inherited", provider_id="sim-provider", model_id="sim-model")
    with AgentExecutionStore(database) as executions:
        attempt = executions.prepare(execution_request, process_spec, "test-owner")
        executions.finish(attempt.attempt_id, ProcessResult(
            "process_succeeded", 0, tmp_path / "stdout", tmp_path / "stderr",
            "1" * 64, "2" * 64, 0, 0, "simulated process result"))
        executions.record_model_selection_observation(ModelSelectionObservation(
            attempt.attempt_id, "sim-provider", "sim-model", "confirmed", "test-simulator",
            datetime.now(timezone.utc).isoformat()))

    with type(validation_store)(database) as validation:
        request = request_for(plan, validation, workspace, checkpoint, operation="failed-validation-op",
                              run_id="failed-validation-run", attempt_id=attempt.attempt_id)
        result = ValidationScheduler(validation, provider, FakeRuntime({"gate-a": {"exit_code": 9}}),
            FakeValidator(), SnapshotManager(provider), tmp_path / "snapshots", tmp_path / "outputs").run(
                request, workspace, checkpoint, plan)
        assert result.status.value == "failed"
        gate_id = validation.db.execute(
            "SELECT gate_result_id FROM validation_gate_results WHERE validation_run_id=? AND gate_id='gate-a'",
            (result.validation_run_id,)).fetchone()[0]

    observed = datetime.now(timezone.utc).isoformat()
    models = (
        ModelCatalogEntry(ModelDescriptor("sim-provider", "sim-model", "1", ("code",),
            availability="available", observed_at=observed, source="sim-catalog",
            input_cost_per_million="0.10", output_cost_per_million="0.20",
            cost_currency="USD", cost_source="fixture-price"), ("code",), "simulator"),
        ModelCatalogEntry(ModelDescriptor("advanced-provider", "advanced-model", "2",
            ("quality:advanced",), availability="available", observed_at=observed, source="sim-catalog",
            input_cost_per_million="1.00", output_cost_per_million="2.00",
            cost_currency="USD", cost_source="fixture-price"),
            ("quality:advanced",), "simulator"),
    )
    with BudgetStore(database) as store:
        proposal_revision = store.propose(plan, "sdd-proposal")
        catalog = store.record_catalog((), models, "sim-catalog-v1")
        kwargs = dict(operation_id="escalation-op", project_id=plan.specification.project_id,
            task_id="task-one", plan_revision_id=proposal_revision.plan_revision_id,
            source_attempt_id=attempt.attempt_id, evidence_ids=(gate_id,),
            source_catalog_digest=catalog, target_catalog_digest=catalog,
            target_provider_id="advanced-provider", target_model_id="advanced-model",
            token_budget=20_000, currency="USD")
        first = store.propose_escalation(**kwargs)
        assert store.propose_escalation(**kwargs) == first
        assert first.authorization_required and first.status == "proposed"
        assert first.source_model_id == "sim-model" and first.target_model_id == "advanced-model"
        assert first.cost_delta_estimate == "0.036"
        assert first.evidence_ids == (gate_id,)
        assert first.reason_code == "verified_validation_gate_failure"
        assert first.budget_status == "estimated"
        assert store.inspect_escalation(first.proposal_id) == first
        with pytest.raises(ConflictError, match="evidence must reference"):
            store.propose_escalation(**{**kwargs, "operation_id": "fake-evidence-op",
                                         "evidence_ids": ("forged-gate-result",)})
