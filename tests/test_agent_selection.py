import json
import sqlite3

import pytest

from agentflow.agents import (AgentResult, ExecutionPolicy, ExecutionRequest,
                              ModelSelectionObservation, ModelSelectionRequest,
                              ProcessSpec)
from agentflow.agents import parse_model_routing_policy_toml
from agentflow.planning import digest, load_plan
from agentflow.state import AgentExecutionStore, ConflictError, IntegrityError
import agentflow.state.sqlite as sqlite_module
from test_planning import files


def setup_store(tmp_path, store_type=AgentExecutionStore):
    path = tmp_path / "state.sqlite3"
    plan = load_plan(files(tmp_path / "project"))
    store = store_type(path)
    store.import_plan(plan)
    digest = store.db.execute(
        "SELECT definition_digest FROM task_definitions WHERE project_id='sample' AND task_id='first'").fetchone()[0]
    request = ExecutionRequest(
        "sample", "first", 1, digest, "selection-op", tmp_path, "Implement first",
        ExecutionPolicy(), model_selection=ModelSelectionRequest(
            "antigravity", "google-antigravity", "gemini-3.8-flash-medium", "user:alice"))
    spec = ProcessSpec(tmp_path / "agy", ("--model", "gemini-3.8-flash-medium"), tmp_path,
                       "", (), "antigravity", "1.2.0", "unknown", "request-review",
                       ("headless", "stream-json"), "1", "google-antigravity",
                       "gemini-3.8-flash-medium")
    return path, store, request, spec


def test_v5_migration_and_selection_snapshot_are_atomic_and_immutable(tmp_path):
    path, store, request, spec = setup_store(tmp_path)
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 5
    attempt = store.prepare(request, spec, "owner")
    snapshot = store.model_selection_snapshot(attempt.attempt_id)
    assert snapshot["attempt_id"] == attempt.attempt_id
    assert snapshot["model_id"] == "gemini-3.8-flash-medium"
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.db.execute("UPDATE agent_model_selections SET snapshot_json='{}' WHERE attempt_id=?",
                         (attempt.attempt_id,))
    store.record_model_selection_observation(ModelSelectionObservation(
        attempt.attempt_id, "google-antigravity", "gemini-3.8-flash-medium",
        "confirmed", "agy-stream-init", "2026-11-05T12:00:00+00:00"))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        store.db.execute("DELETE FROM agent_model_observations WHERE attempt_id=?", (attempt.attempt_id,))
    store.close()
    with AgentExecutionStore(path) as reopened:
        assert reopened.model_selection_snapshot(attempt.attempt_id) == snapshot
        assert reopened.db.execute("SELECT status FROM agent_model_observations WHERE attempt_id=?",
                                   (attempt.attempt_id,)).fetchone() == ("confirmed",)


def test_operation_id_rejects_different_manual_model(tmp_path):
    _, store, request, spec = setup_store(tmp_path)
    first = store.prepare(request, spec, "owner")
    changed = ExecutionRequest(
        request.project_id, request.task_id, request.definition_revision,
        request.definition_digest, request.operation_id, request.workspace,
        request.instruction, request.policy,
        model_selection=ModelSelectionRequest("antigravity", "google-antigravity",
                                              "other-model", "user:alice"))
    with pytest.raises(ConflictError, match="parâmetros diferentes"):
        store.prepare(changed, spec, "owner")
    assert store.db.execute("SELECT count(*) FROM execution_attempts").fetchone() == (1,)
    assert store.model_selection_snapshot(first.attempt_id)["model_id"] == "gemini-3.8-flash-medium"
    store.close()


def test_policy_snapshot_is_bound_to_selection_and_operation_digest(tmp_path):
    _, store, request, spec = setup_store(tmp_path)
    policy = parse_model_routing_policy_toml('''schema_version = 1
[policy]
id = "future-routing"
enabled = false
required_capabilities = []
excluded_failure_classes = ["environment", "authentication", "quota", "infrastructure"]
escalation_evidence_ids = []
max_attempts = 3
authorization_required = true
''')
    selected = ModelSelectionRequest(
        "antigravity", "google-antigravity", "gemini-3.8-flash-medium", "user:alice",
        routing_policy_snapshot=policy.snapshot(), routing_policy_digest=policy.policy_digest)
    bound = ExecutionRequest(request.project_id, request.task_id, request.definition_revision,
                             request.definition_digest, request.operation_id, request.workspace,
                             request.instruction, request.policy, model_selection=selected)
    attempt = store.prepare(bound, spec, "owner")
    snapshot = store.model_selection_snapshot(attempt.attempt_id)
    assert snapshot["routing_policy_digest"] == policy.policy_digest
    assert snapshot["routing_policy_snapshot"] == policy.snapshot()
    changed_policy = policy.snapshot().replace('"enabled":false', '"enabled":true')
    changed = ModelSelectionRequest(
        "antigravity", "google-antigravity", "gemini-3.8-flash-medium", "user:alice",
        routing_policy_snapshot=changed_policy,
        routing_policy_digest=digest(json.loads(changed_policy)))
    divergent = ExecutionRequest(request.project_id, request.task_id, request.definition_revision,
                                 request.definition_digest, request.operation_id, request.workspace,
                                 request.instruction, request.policy, model_selection=changed)
    with pytest.raises(ConflictError, match="parâmetros diferentes"):
        store.prepare(divergent, spec, "owner")
    store.close()


def test_v5_migration_failure_rolls_back_to_v4(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    plan = load_plan(files(tmp_path / "project"))
    from agentflow.state.validation import ValidationStateStore
    with ValidationStateStore(path) as store:
        store.import_plan(plan)
    monkeypatch.setattr(sqlite_module, "AGENT_SCHEMA",
                        sqlite_module.AGENT_SCHEMA[:1] + ("INVALID SQL",))
    with pytest.raises(IntegrityError, match="migração v5"):
        AgentExecutionStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (4,)
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,), (2,), (3,), (4,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_model_selections'").fetchone() is None
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_hermes_unreported_provider_is_persisted_as_unknown(tmp_path):
    _, store, original, _ = setup_store(tmp_path)
    selection = ModelSelectionRequest(
        "hermes", "openrouter", "anthropic/claude-sonnet-4", "user:alice")
    request = ExecutionRequest(
        original.project_id, original.task_id, original.definition_revision,
        original.definition_digest, "hermes-selection-op", original.workspace,
        original.instruction, original.policy, model_selection=selection)
    spec = ProcessSpec(tmp_path / "hermes", (), tmp_path, "", (), "hermes",
                       "0.30.1", "unknown", "unknown", (), "1",
                       "openrouter", "anthropic/claude-sonnet-4")
    attempt = store.prepare(request, spec, "owner")
    store.record_model_selection_observation(ModelSelectionObservation(
        attempt.attempt_id, "unknown", "anthropic/claude-sonnet-4", "unknown",
        "hermes-system-init", "2026-11-05T12:00:00+00:00"))
    row = store.db.execute(
        "SELECT provider_id,model_id,status FROM agent_model_observations WHERE attempt_id=?",
        (attempt.attempt_id,)).fetchone()
    assert row == ("unknown", "anthropic/claude-sonnet-4", "unknown")
    store.close()
