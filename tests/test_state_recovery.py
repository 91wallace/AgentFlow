import pytest

from agentflow.planning import load_plan
from agentflow.state import ConflictError, IntegrityError, Transition
from agentflow.state.sqlite import SQLiteStore
from test_planning import TASKS, files


def test_reopen_blocks_orphan_once(tmp_path):
    directory = files(tmp_path / "plan")
    plan = load_plan(directory)
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        store.import_plan(plan)
        store.transition(Transition("first", "reserved", "reserve", 0, owner="worker"))
    with SQLiteStore(path) as store:
        assert store.reopen(plan) == ("first",)
        assert store.list_tasks()[0].status == "blocked"
        events = store.db.execute("SELECT count(*) FROM task_events").fetchone()[0]
        assert store.reopen(plan) == ()
        assert store.db.execute("SELECT count(*) FROM task_events").fetchone()[0] == events


def test_changed_definitions_do_not_recover_or_mutate(tmp_path):
    directory = files(tmp_path / "plan")
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        store.import_plan(load_plan(directory))
        store.transition(Transition("first", "reserved", "reserve", 0, owner="worker"))
        (directory / "tasks.toml").write_text(TASKS.replace("First works", "First changed"), encoding="utf-8")
        with pytest.raises(ConflictError, match="definições alteradas"):
            store.reopen(load_plan(directory))
        assert store.list_tasks()[0].status == "reserved"


def test_uncommitted_transition_rolls_back(tmp_path):
    path = tmp_path / "state.sqlite3"
    plan = load_plan(files(tmp_path / "plan"))
    with SQLiteStore(path) as store:
        store.import_plan(plan)
        with pytest.raises(RuntimeError):
            with store.transaction() as db:
                db.execute("UPDATE tasks SET status='reserved' WHERE task_id='first'")
                raise RuntimeError("interrupted")
    with SQLiteStore(path) as store:
        assert store.list_tasks()[0].status == "ready"


def test_corrupt_file_is_preserved(tmp_path):
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not a database")
    before = path.read_bytes()
    with pytest.raises(IntegrityError):
        SQLiteStore(path)
    assert path.read_bytes() == before
