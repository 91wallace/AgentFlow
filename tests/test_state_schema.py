import sqlite3

import pytest

from agentflow.state import IntegrityError
from agentflow.state.sqlite import SQLiteStore
import agentflow.state.sqlite as sqlite_module


def test_schema_reopens_with_integrity(tmp_path):
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    with SQLiteStore(path) as store:
        store.check_integrity()
        assert store.db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_future_version_is_rejected_without_change(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
    before = path.read_bytes()
    with pytest.raises(IntegrityError, match="futura"):
        SQLiteStore(path)
    assert path.read_bytes() == before


def test_backup_failure_prevents_migration(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA application_id=1234")
    before = path.read_bytes()
    monkeypatch.setattr(SQLiteStore, "_backup", lambda self: (_ for _ in ()).throw(IntegrityError("backup falhou")))
    with pytest.raises(IntegrityError, match="backup falhou"):
        SQLiteStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert path.read_bytes() == before


def test_migration_failure_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    monkeypatch.setattr(sqlite_module, "SCHEMA", sqlite_module.SCHEMA[:2] + ("INVALID SQL",))
    with pytest.raises(IntegrityError):
        SQLiteStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone() is None


def test_foreign_keys_reject_orphan_rows(tmp_path):
    with SQLiteStore(tmp_path / "state.sqlite3") as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.db.execute("INSERT INTO task_dependencies VALUES('missing','a','b')")
        store.check_integrity()


def test_migration_backup_is_consistent_and_preserves_existing_backup(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA application_id=1234")
    existing = tmp_path / "state.sqlite3.bak"
    existing.write_bytes(b"preserve")
    with SQLiteStore(path):
        pass
    assert existing.read_bytes() == b"preserve"
    backups = [item for item in tmp_path.glob("state.sqlite3.*.bak")]
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
