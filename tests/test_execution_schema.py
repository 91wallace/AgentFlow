import sqlite3

import pytest

from agentflow.state.sqlite import SQLiteStore
from agentflow.state import EvidenceInput, IntegrityError
import agentflow.state.sqlite as module
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
