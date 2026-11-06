import hashlib
import json
import sqlite3

import pytest

from agentflow.cli import main
from agentflow.state import BudgetAccount, BudgetStore, BudgetVector, IntegrityError
from agentflow.state.audit import inspect_phase7_database
from test_phase7_sdd import TASKS_V4
from test_planning import SPEC, files
from agentflow.planning import load_plan


def _seed(root):
    data = files(root / "project", TASKS_V4,
                 SPEC.replace("Deliver sample", "credential-secret-sentinel"))
    plan = load_plan(data)
    state_path = root / ".agentflow" / "state.sqlite3"
    state_path.parent.mkdir()
    with BudgetStore(state_path) as store:
        store.import_plan(plan)
        proposal = store.propose(plan, "audit-proposal")
        store.create_budget_account(BudgetAccount(
            "audit-account", "sample", "task", "first", "USD", "1.00", 100, 30,
            "unavailable", "estimated", "unavailable"), "audit-account-op")
        store.reserve_budget("audit-reservation", project_id="sample", task_id="first",
            execution_id="audit-execution", attempt_id=None, account_ids=("audit-account",),
            estimate=BudgetVector("0.25", 20, 10, "USD", "estimated", "estimated", "estimated"))
    return state_path, proposal


def test_audit_cli_json_is_read_only_and_secret_minimizing(monkeypatch, tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    state_path, proposal = _seed(root)
    before = hashlib.sha256(state_path.read_bytes()).hexdigest()
    monkeypatch.setattr("agentflow.cli.repository_root", lambda _: root)

    assert main(["audit", "--path", str(root), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    after = hashlib.sha256(state_path.read_bytes()).hexdigest()
    assert before == after
    assert report["schema_version"] == 7
    assert report["integrity"] == {"integrity_check": "ok", "foreign_key_violations": 0}
    assert report["plan_revisions"][0]["plan_revision_id"] == proposal.plan_revision_id
    assert report["requirement_coverage"][0]["requirement_id"] == "spec-ok"
    assert report["budget_reservations"][0]["status"] == "reserved"
    assert report["unknowns_and_pending"]
    assert "credential-secret-sentinel" not in json.dumps(report)
    assert "payload_json" not in json.dumps(report)


def test_audit_human_output_and_wal_refusal_are_controlled(monkeypatch, tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    state_path, _ = _seed(root)
    monkeypatch.setattr("agentflow.cli.repository_root", lambda _: root)
    assert main(["audit", "--path", str(root)]) == 0
    assert "Routing decisions (preview only)" in capsys.readouterr().out
    wal = state_path.with_name(state_path.name + "-wal")
    wal.write_bytes(b"uncheckpointed-data")
    with pytest.raises(IntegrityError, match="WAL presente"):
        inspect_phase7_database(state_path)
    assert wal.read_bytes() == b"uncheckpointed-data"


def test_audit_opens_database_read_only_without_migration(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_migrations(version INTEGER)")
        db.execute("INSERT INTO schema_migrations VALUES(1)")
        db.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    report = inspect_phase7_database(path)
    assert report["schema_version"] == 1
    assert "routing_decisions" in report["unavailable_sections"]
    assert path.read_bytes() == before
