"""Crash-left WAL probing must not make an unidentified source writable."""

from pathlib import Path
import sqlite3

import pytest

from agentflow.approval import persistence
from agentflow.approval.persistence import BrokerStore, BrokerStoreError


def force_probe_error(monkeypatch, path, code=sqlite3.SQLITE_IOERR_TRUNCATE):
    connect = sqlite3.connect
    opened = []

    class FailedReadOnlyProbe:
        def execute(self, statement):
            error = sqlite3.OperationalError("synthetic read-only probe failure")
            error.sqlite_errorcode = code
            raise error

        def close(self):
            pass

    def probe(database, *args, **kwargs):
        opened.append(database)
        if database == path.as_uri() + "?mode=ro":
            return FailedReadOnlyProbe()
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(persistence.sqlite3, "connect", probe)
    return opened


def source_bytes(path):
    return {p.name: p.read_bytes() for p in path.parent.glob(path.name + "*") if p.is_file()}


def test_isolated_probe_preserves_broker_source_and_removes_temporary_files(tmp_path, monkeypatch):
    path = tmp_path / "broker.sqlite"
    with BrokerStore(path):
        pass
    before = source_bytes(path)
    opened = force_probe_error(monkeypatch, path)
    probe = object.__new__(BrokerStore)
    probe.path = path
    probe._validate_existing_path_identity()
    copies = [Path(item) for item in opened if isinstance(item, Path)]
    assert len(copies) == 1
    assert copies[0].parent != path.parent
    assert not copies[0].parent.exists()
    assert source_bytes(path) == before


@pytest.mark.parametrize("wal", [False, True])
def test_isolated_probe_rejects_foreign_db_including_schema_only_in_wal(tmp_path, monkeypatch, wal):
    path = tmp_path / "foreign.sqlite"
    writer = sqlite3.connect(path)
    try:
        if wal:
            assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE foreign_records (value TEXT)")
        writer.execute("INSERT INTO foreign_records VALUES ('preserve me')")
        writer.commit()
        if not wal:
            writer.close()
        before = source_bytes(path)
        force_probe_error(monkeypatch, path)
        with pytest.raises(BrokerStoreError, match="non-empty"):
            BrokerStore(path)
        assert source_bytes(path) == before
        assert not path.with_name(path.name + ".lock").exists()
    finally:
        writer.close()


def test_source_drift_aborts_probe_before_opening_a_copy(tmp_path, monkeypatch):
    path = tmp_path / "broker.sqlite"
    with BrokerStore(path):
        pass
    opened = force_probe_error(monkeypatch, path)
    copyfile = persistence.shutil.copyfile

    def changed(source, target):
        result = copyfile(source, target)
        with path.open("ab") as stream:
            stream.write(b"changed during copy")
        return result

    monkeypatch.setattr(persistence.shutil, "copyfile", changed)
    with pytest.raises(BrokerStoreError, match="changed during identity probe"):
        BrokerStore(path)
    assert not any(isinstance(item, Path) for item in opened)


def test_copy_failure_stays_fail_closed_and_preserves_source(tmp_path, monkeypatch):
    path = tmp_path / "broker.sqlite"
    with BrokerStore(path):
        pass
    before = source_bytes(path)
    force_probe_error(monkeypatch, path)

    def denied(*args):
        raise PermissionError("copy denied")

    monkeypatch.setattr(persistence.shutil, "copyfile", denied)
    with pytest.raises(BrokerStoreError, match="unreadable") as error:
        BrokerStore(path)
    assert isinstance(error.value.__cause__, PermissionError)
    assert source_bytes(path) == before


def test_other_sqlite_io_errors_do_not_trigger_copy_fallback(tmp_path, monkeypatch):
    path = tmp_path / "foreign.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE foreign_records (value TEXT)")
    before = source_bytes(path)
    opened = force_probe_error(monkeypatch, path, sqlite3.SQLITE_IOERR_READ)
    with pytest.raises(BrokerStoreError, match="unreadable") as error:
        BrokerStore(path)
    assert error.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_IOERR_READ
    assert not any(isinstance(item, Path) for item in opened)
    assert source_bytes(path) == before
    assert not path.with_name(path.name + ".lock").exists()
