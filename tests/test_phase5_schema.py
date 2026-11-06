import sqlite3

import pytest

from agentflow.planning import load_plan
from agentflow.state import ConflictError, IntegrityError
from agentflow.state.execution import ExecutionStore
from agentflow.state.sqlite import SQLiteStore
from agentflow.state.validation import ValidationStateStore
import agentflow.state.sqlite as sqlite_module
from test_planning import files


SPEC = '''schema_version = 2
project_id = "sample"
title = "Sample"
objective = "Validate sample"
[[acceptance_criteria]]
id = "spec-ok"
description = "Specification is reviewed"
[validation]
commands = [{ id = "pytest", platform = "linux", executable = "/usr/bin/pytest", sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", arguments = ["--version"], working_directory = "snapshot", version = "8" }]
'''

TASKS = '''schema_version = 3
[[tasks]]
id = "first"
title = "First"
description = "Run checks"
versioned_paths = []
[[tasks.acceptance_criteria]]
id = "ok"
description = "Checks pass"
[[tasks.gates]]
id = "unit-tests"
version = 1
criterion_ids = ["ok"]
command_id = "pytest"
'''


def phase5_plan(tmp_path, tasks=TASKS):
    return load_plan(files(tmp_path, tasks=tasks, spec=SPEC))


def test_v4_migration_preserves_v3_data_and_checks_integrity(tmp_path):
    path = tmp_path / "state.sqlite3"
    plan = phase5_plan(tmp_path / "plan")
    with ExecutionStore(path) as store:
        store.import_plan(plan)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
    with ValidationStateStore(path) as store:
        store.import_plan(plan)
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert store.db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,), (2,), (3,), (4,)]
        assert [task.task_id for task in store.list_tasks()] == ["first"]
        tables = {row[0] for row in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"validation_runs", "validation_gate_results", "validation_snapshots",
                "validation_artifacts", "validation_deliveries", "validation_events",
                "validation_id_registry"} <= tables
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert store.db.execute("SELECT stable_id,active FROM validation_id_registry ORDER BY object_type").fetchall() == [
            ("pytest", 1), ("unit-tests", 1)]
    assert list(tmp_path.glob("state.sqlite3.*.bak")), "v3 backup must exist before migration"


def test_v4_migration_failure_rolls_back_entire_schema(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    monkeypatch.setattr(sqlite_module, "VALIDATION_SCHEMA",
                        sqlite_module.VALIDATION_SCHEMA[:2] + ("INVALID SQL",))
    with pytest.raises(IntegrityError, match="migração v4"):
        ValidationStateStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall() == [
            (1,), (2,), (3,)]
        assert db.execute("SELECT name FROM sqlite_master WHERE name='validation_operations'").fetchone() is None
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert list(tmp_path.glob("state.sqlite3.*.bak"))


def test_v4_reader_rejects_future_database_version_without_mutation(tmp_path):
    path = tmp_path / "state.sqlite3"
    with ExecutionStore(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=8")
    with pytest.raises(IntegrityError, match="futura"):
        ValidationStateStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 8
        assert db.execute("SELECT name FROM sqlite_master WHERE name='validation_operations'").fetchone() is None


def test_registry_rejects_reuse_after_id_was_removed(tmp_path):
    path = tmp_path / "state.sqlite3"
    directory = tmp_path / "plan"
    initial = phase5_plan(directory)
    with ValidationStateStore(path) as store:
        store.import_plan(initial)
        removed_tasks = TASKS.replace('[[tasks.gates]]\nid = "unit-tests"\nversion = 1\ncriterion_ids = ["ok"]\ncommand_id = "pytest"', "")
        removed_tasks = removed_tasks.replace('[[tasks.acceptance_criteria]]', 'gates = []\n[[tasks.acceptance_criteria]]')
        store.import_plan(load_plan(files(tmp_path / "removed", tasks=removed_tasks, spec=SPEC)))
        assert store.db.execute("SELECT active FROM validation_id_registry WHERE object_type='gate'").fetchone() == (0,)
        with pytest.raises(ConflictError, match="não pode ser reutilizado"):
            store.import_plan(load_plan(files(tmp_path / "reintroduced", tasks=TASKS, spec=SPEC)))
        assert store.db.execute("SELECT active FROM validation_id_registry WHERE object_type='gate'").fetchone() == (0,)


def test_same_operation_id_cannot_cross_into_validation_domain(tmp_path):
    with ValidationStateStore(tmp_path / "state.sqlite3") as store:
        store.db.execute("INSERT INTO validation_operations(operation_id,request_digest,operation_kind,status,owner_token,request_snapshot,result_snapshot,created_at,updated_at) VALUES('shared','d','validate','prepared','','{}','','now','now')")
        with pytest.raises(sqlite3.IntegrityError, match="already used by validation"):
            store.db.execute("INSERT INTO operations(operation_id,request_digest,result_type,result_id,result_snapshot,created_at) VALUES('shared','d','task',NULL,'{}','now')")
        assert store.db.execute("SELECT count(*) FROM operations WHERE operation_id='shared'").fetchone() == (0,)


def test_v4_is_not_opened_by_older_store_profile(tmp_path):
    path = tmp_path / "state.sqlite3"
    with ValidationStateStore(path):
        pass
    with pytest.raises(IntegrityError, match="futura"):
        SQLiteStore(path)
