"""Read-only state inspection for the CLI."""

from pathlib import Path
from contextlib import closing
import sqlite3

from agentflow.planning import Plan

from .contracts import ConflictError, IntegrityError, TaskState


def inspect_state(path: Path, plan: Plan) -> tuple[TaskState, ...]:
    if not path.is_file():
        raise IntegrityError(f"estado inexistente: {path}")
    if path.with_name(path.name + "-wal").exists():
        raise IntegrityError("WAL pendente; inspeção somente leitura indisponível até reconciliação")
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise IntegrityError("versão SQLite incompatível")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise IntegrityError("banco corrompido")
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise IntegrityError("chaves estrangeiras inválidas")
            project = db.execute("SELECT project_id,spec_digest,tasks_digest FROM projects").fetchone()
            if project != (plan.specification.project_id, plan.spec_digest, plan.tasks_digest):
                raise ConflictError("definições alteradas; revalidação necessária")
            return tuple(TaskState(*row) for row in db.execute(
                "SELECT task_id,status,definition_revision,revision,attempt_count,revalidation_required FROM tasks ORDER BY task_id"))
    except sqlite3.DatabaseError as exc:
        raise IntegrityError(f"não foi possível ler estado: {exc}") from exc
