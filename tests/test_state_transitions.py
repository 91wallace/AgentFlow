import pytest
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from threading import Barrier

from agentflow.planning import load_plan
from agentflow.state import ConflictError, EvidenceInput, Transition, TransitionError
from agentflow.state.sqlite import SQLiteStore
from test_planning import TASKS, files


def _reserve_in_process(path, number, start, results):
    try:
        with SQLiteStore(path) as store:
            start.wait(10)
            store.transition(Transition("first", "reserved", f"process-{number}", 0, owner=f"worker-{number}"))
        results.put("reserved")
    except ConflictError:
        results.put("conflict")
    except BaseException as exc:
        results.put(f"unexpected:{exc}")


def test_transitions_evidence_idempotence_and_dependencies(tmp_path):
    directory = files(tmp_path / "plan")
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        store.import_plan(load_plan(directory))
        with pytest.raises(TransitionError, match="inválida"):
            store.transition(Transition("first", "completed", "bad", 0))
        reserved = store.transition(Transition("first", "reserved", "reserve-first", 0, owner="worker"))
        assert reserved.revision == 1 and reserved.attempt_count == 1
        assert store.transition(Transition("first", "reserved", "reserve-first", 0, owner="worker")) == reserved
        with pytest.raises(ConflictError, match="parâmetros diferentes"):
            store.transition(Transition("first", "reserved", "reserve-first", 0, owner="other"))
        with pytest.raises(TransitionError, match="evidência verificada"):
            store.transition(Transition("first", "completed", "complete-first", 1))
        declaration = store.add_evidence(EvidenceInput("first", "ok", "declared", "done", "person", "declare-first"))
        assert store.add_evidence(EvidenceInput("first", "ok", "declared", "done", "person", "declare-first")) == declaration
        with pytest.raises(TransitionError, match="evidência verificada"):
            store.transition(Transition("first", "completed", "complete-first", 1))
        store.add_evidence(EvidenceInput("first", "ok", "verified", "checked", "person", "verify-first", declaration, "review", "reviewer", True))
        assert store.transition(Transition("first", "completed", "complete-first", 1)).status == "completed"
        assert store.transition(Transition("first", "reserved", "reserve-first", 0, owner="worker")) == reserved
        assert store.list_tasks()[1].status == "ready"
        events = store.db.execute("SELECT count(*) FROM task_events").fetchone()[0]
        assert events == store.db.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
    with SQLiteStore(path) as store:
        assert store.transition(Transition("first", "completed", "complete-first", 1)).status == "completed"
        assert store.db.execute("SELECT count(*) FROM task_events").fetchone()[0] == events


def test_revalidation_preserves_definition_history(tmp_path):
    directory = files(tmp_path / "plan")
    with SQLiteStore(tmp_path / "state.sqlite3") as store:
        store.import_plan(load_plan(directory))
        store.transition(Transition("first", "reserved", "reserve", 0, owner="worker"))
        (directory / "tasks.toml").write_text(TASKS.replace("First works", "First works better"), encoding="utf-8")
        store.import_plan(load_plan(directory))
        with pytest.raises(ConflictError, match="revalidação"):
            store.transition(Transition("first", "failed", "fail", 1, reason="failure"))
        result = store.revalidate("first", "revalidate", "reviewer", "accepted changed task")
        assert result.definition_revision == 2 and result.status == "blocked"
        assert not result.revalidation_required
        assert store.db.execute("SELECT count(*) FROM task_definitions WHERE task_id='first'").fetchone()[0] == 2
        assert store.revalidate("first", "revalidate", "reviewer", "accepted changed task") == result


def test_two_connections_cannot_reserve_same_task(tmp_path):
    plan = load_plan(files(tmp_path / "plan"))
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        store.import_plan(plan)
    barrier = Barrier(2)

    def reserve(number):
        with SQLiteStore(path) as store:
            barrier.wait(timeout=5)
            try:
                return store.transition(Transition("first", "reserved", f"reserve-{number}", 0, owner=f"worker-{number}"))
            except ConflictError:
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (1, 2)))
    assert sum(result == "conflict" for result in results) == 1
    assert sum(result != "conflict" for result in results) == 1
    with SQLiteStore(path) as store:
        assert store.list_tasks()[0].attempt_count == 1


def test_explicit_completion_exception_is_audited(tmp_path):
    plan = load_plan(files(tmp_path / "plan"))
    with SQLiteStore(tmp_path / "state.sqlite3") as store:
        store.import_plan(plan)
        store.transition(Transition("first", "reserved", "reserve", 0, owner="worker"))
        completed = store.transition(Transition("first", "completed", "exception", 1,
                                                exception_author="reviewer", exception_reason="manual waiver"))
        assert completed.status == "completed"
        reason = store.db.execute("SELECT reason FROM task_events WHERE operation_id='exception'").fetchone()[0]
        assert "reviewer" in reason and "manual waiver" in reason


def test_two_processes_cannot_reserve_same_task(tmp_path):
    plan = load_plan(files(tmp_path / "plan"))
    path = tmp_path / "state.sqlite3"
    with SQLiteStore(path) as store:
        store.import_plan(plan)
    context = get_context("spawn")
    start = context.Event()
    results = context.Queue()
    workers = [context.Process(target=_reserve_in_process, args=(path, number, start, results)) for number in (1, 2)]
    for worker in workers:
        worker.start()
    start.set()
    outcomes = [results.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert sorted(outcomes) == ["conflict", "reserved"]
