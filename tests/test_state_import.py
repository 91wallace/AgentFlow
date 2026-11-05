import pytest

from agentflow.planning import load_plan
from agentflow.state import ConflictError, Transition
from agentflow.state.sqlite import SQLiteStore
from test_planning import SPEC, TASKS, files


def test_import_is_idempotent_and_reopens(tmp_path):
    plan = load_plan(files(tmp_path / "plan"))
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        store.import_plan(plan)
        original = store.list_tasks()
        store.import_plan(plan)
        assert store.list_tasks() == original
        assert [(item.task_id, item.status) for item in original] == [("first", "ready"), ("second", "pending")]
        assert store.db.execute("SELECT count(*) FROM task_events").fetchone()[0] == 2
        assert store.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0] == 2
    with SQLiteStore(path) as store:
        assert store.list_tasks() == original
        assert store.db.execute("SELECT count(*) FROM task_definitions").fetchone()[0] == 2


def test_changed_started_task_keeps_old_revision(tmp_path):
    directory = files(tmp_path / "plan")
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        store.import_plan(load_plan(directory))
        store.db.execute("UPDATE tasks SET status='failed',attempt_count=1 WHERE task_id='first'")
        (directory / "tasks.toml").write_text(TASKS.replace("First works", "First works better"), encoding="utf-8")
        store.import_plan(load_plan(directory))
        first = store.list_tasks()[0]
        assert first.definition_revision == 1
        assert first.revalidation_required
        assert store.db.execute("SELECT count(*) FROM task_definitions WHERE task_id='first'").fetchone()[0] == 2


def test_removed_task_rolls_back(tmp_path):
    directory = files(tmp_path / "plan")
    with SQLiteStore(tmp_path / "state.sqlite3") as store:
        store.import_plan(load_plan(directory))
        before = store.list_tasks()
        (directory / "tasks.toml").write_text(TASKS.split('[[tasks]]\nid = "first"')[0].replace('depends_on = ["first"]', ''), encoding="utf-8")
        with pytest.raises(ConflictError, match="removidos"):
            store.import_plan(load_plan(directory))
        assert store.list_tasks() == before


def test_unstarted_revision_change_is_audited_without_forced_revalidation(tmp_path):
    directory = files(tmp_path / "plan")
    with SQLiteStore(tmp_path / "state.sqlite3") as store:
        store.import_plan(load_plan(directory))
        (directory / "tasks.toml").write_text(TASKS.replace("First works", "First works better"), encoding="utf-8")
        store.import_plan(load_plan(directory))
        first = store.list_tasks()[0]
        assert first.definition_revision == 2
        assert not first.revalidation_required
        assert store.db.execute("SELECT count(*) FROM task_events WHERE task_id='first'").fetchone()[0] == 2


def test_dependent_is_demoted_when_completed_prerequisite_changes(tmp_path):
    directory = files(tmp_path / "plan")
    with SQLiteStore(tmp_path / "state.sqlite3") as store:
        store.import_plan(load_plan(directory))
        store.transition(Transition("first", "reserved", "reserve", 0, owner="worker"))
        store.transition(Transition("first", "completed", "complete", 1,
                                    exception_author="reviewer", exception_reason="manual waiver"))
        assert store.list_tasks()[1].status == "ready"
        (directory / "tasks.toml").write_text(TASKS.replace("First works", "First works better"), encoding="utf-8")
        store.import_plan(load_plan(directory))
        first, second = store.list_tasks()
        assert first.revalidation_required
        assert second.status == "pending"
        assert store.db.execute("SELECT count(*) FROM task_events WHERE task_id='second' AND from_status='ready' AND to_status='pending'").fetchone()[0] == 1
