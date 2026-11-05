import sqlite3

import pytest

from agentflow.state.sqlite import SQLiteStore
from agentflow.state import EvidenceInput, IntegrityError
import agentflow.state.sqlite as module
from agentflow.state.execution import ExecutionStore
from agentflow.planning import load_plan
from test_planning import files


def test_v2_migration_preserves_v1(tmp_path):
    path = tmp_path / "state.sqlite3"
    plan = load_plan(files(tmp_path / "plan"))
    with SQLiteStore(path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
        store.import_plan(plan)
        store.add_evidence(EvidenceInput("first", "ok", "declared", "done", "user", "declare"))
    with SQLiteStore(path, execution=True) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2
        store.check_integrity()
        assert [item.task_id for item in store.list_tasks()] == ["first", "second"]
        assert store.db.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1
    with SQLiteStore(path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2


def test_v2_migration_rollback(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path):
        pass
    monkeypatch.setattr(module, "EXECUTION_SCHEMA", module.EXECUTION_SCHEMA[:1] + ("INVALID SQL",))
    with pytest.raises(IntegrityError):
        SQLiteStore(path, execution=True)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='execution_attempts'").fetchone() is None


def test_v3_git_migration_preserves_v2_and_adds_integrity_constraints(tmp_path):
    path = tmp_path / "state.sqlite3"
    plan = load_plan(files(tmp_path / "plan"))
    with SQLiteStore(path, execution=True) as store:
        store.import_plan(plan)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    with ExecutionStore(path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert store.db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,), (3,)]
        assert [item.task_id for item in store.list_tasks()] == ["first", "second"]
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"workspaces", "git_operations", "git_checkpoints", "git_events"} <= tables
        assert "workspace_id" in {row[1] for row in store.db.execute("PRAGMA table_info(execution_attempts)")}


def test_v3_migration_failure_rolls_back_to_v2(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path, execution=True):
        pass
    monkeypatch.setattr(module, "GIT_SCHEMA", module.GIT_SCHEMA[:1] + ("INVALID SQL",))
    with pytest.raises(IntegrityError, match="migração v3"):
        ExecutionStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [(1,), (2,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='workspaces'").fetchone() is None
