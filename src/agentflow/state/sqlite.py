"""SQLite operational state; no external execution."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterator
from uuid import uuid4

from agentflow.planning import Plan, canonical, digest

from .contracts import (ConflictError, EvidenceInput, IntegrityError, TaskState,
                        Transition, TransitionError)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = (
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    "CREATE TABLE projects(project_id TEXT PRIMARY KEY, spec_digest TEXT NOT NULL, tasks_digest TEXT NOT NULL, plan_revision INTEGER NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE task_definitions(project_id TEXT NOT NULL, task_id TEXT NOT NULL, definition_revision INTEGER NOT NULL, definition_digest TEXT NOT NULL, spec_digest TEXT NOT NULL, definition_snapshot TEXT NOT NULL, PRIMARY KEY(project_id,task_id,definition_revision), FOREIGN KEY(project_id) REFERENCES projects(project_id))",
    "CREATE TABLE tasks(project_id TEXT NOT NULL, task_id TEXT NOT NULL, definition_revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','ready','reserved','completed','failed','blocked')), attempt_count INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, revalidation_required INTEGER NOT NULL DEFAULT 0, owner TEXT NOT NULL DEFAULT '', reservation_operation TEXT NOT NULL DEFAULT '', PRIMARY KEY(project_id,task_id), FOREIGN KEY(project_id,task_id,definition_revision) REFERENCES task_definitions(project_id,task_id,definition_revision))",
    "CREATE TABLE task_dependencies(project_id TEXT NOT NULL, task_id TEXT NOT NULL, depends_on_id TEXT NOT NULL, PRIMARY KEY(project_id,task_id,depends_on_id), CHECK(task_id != depends_on_id), FOREIGN KEY(project_id,task_id) REFERENCES tasks(project_id,task_id), FOREIGN KEY(project_id,depends_on_id) REFERENCES tasks(project_id,task_id))",
    "CREATE TABLE task_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, task_id TEXT NOT NULL, definition_revision INTEGER NOT NULL, from_status TEXT, to_status TEXT NOT NULL, reason TEXT NOT NULL, operation_id TEXT NOT NULL, occurred_at TEXT NOT NULL, FOREIGN KEY(project_id,task_id,definition_revision) REFERENCES task_definitions(project_id,task_id,definition_revision))",
    "CREATE TABLE operations(operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, result_type TEXT NOT NULL, result_id INTEGER, result_snapshot TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE evidence(evidence_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, task_id TEXT NOT NULL, definition_revision INTEGER NOT NULL, criterion_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('declared','verified')), payload TEXT NOT NULL, source TEXT NOT NULL, declaration_id INTEGER, method TEXT NOT NULL, author TEXT NOT NULL, passed INTEGER NOT NULL, recorded_at TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE, FOREIGN KEY(project_id,task_id,definition_revision) REFERENCES task_definitions(project_id,task_id,definition_revision), FOREIGN KEY(declaration_id) REFERENCES evidence(evidence_id))",
    "CREATE TABLE checkpoints(checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, event_id INTEGER NOT NULL, plan_revision INTEGER NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(project_id), FOREIGN KEY(event_id) REFERENCES task_events(event_id))",
    "CREATE INDEX tasks_by_status ON tasks(project_id,status)",
    "CREATE INDEX dependencies_by_prerequisite ON task_dependencies(project_id,depends_on_id)",
    "CREATE INDEX events_by_task ON task_events(project_id,task_id,event_id)",
)


class SQLiteStore:
    def __init__(self, path: Path):
        self.path = path
        self.existed = path.exists()
        if not path.parent.is_dir():
            raise IntegrityError(f"diretório inexistente: {path.parent}")
        try:
            self.db = sqlite3.connect(path, timeout=5, isolation_level=None)
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise IntegrityError(f"versão SQLite futura: {version}")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.check_integrity()
        except (sqlite3.DatabaseError, OSError, IntegrityError) as exc:
            if hasattr(self, "db"):
                self.db.close()
            if isinstance(exc, IntegrityError):
                raise
            raise IntegrityError(f"não foi possível abrir estado: {exc}") from exc

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield self.db
            self.db.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise ConflictError(f"SQLite ocupado ou indisponível: {exc}") from exc
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _backup(self) -> None:
        handle, name = tempfile.mkstemp(prefix="state-backup-", suffix=".tmp", dir=self.path.parent)
        os.close(handle)
        temporary = Path(name)
        try:
            target = sqlite3.connect(temporary)
            try:
                self.db.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise IntegrityError("backup inválido")
            finally:
                target.close()
            os.replace(temporary, self.path.with_name(f"{self.path.name}.{uuid4().hex}.bak"))
        finally:
            temporary.unlink(missing_ok=True)

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise IntegrityError(f"versão SQLite futura: {version}")
        if version == 1:
            recorded = self.db.execute("SELECT version FROM schema_migrations").fetchall()
            if recorded != [(1,)]:
                raise IntegrityError("histórico de migração divergente")
            return
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        if tables:
            raise IntegrityError("banco sem versão contém tabelas desconhecidas")
        if self.existed and self.path.stat().st_size:
            self._backup()
        try:
            with self.transaction() as db:
                for statement in SCHEMA:
                    db.execute(statement)
                db.execute("INSERT INTO schema_migrations VALUES(1,?)", (now(),))
                db.execute("PRAGMA user_version=1")
        except ConflictError as exc:
            raise IntegrityError(f"migração falhou: {exc}") from exc

    def check_integrity(self) -> None:
        if self.db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise IntegrityError("integrity_check falhou")
        if self.db.execute("PRAGMA foreign_key_check").fetchall():
            raise IntegrityError("foreign_key_check falhou")

    def _project_id(self) -> str:
        rows = self.db.execute("SELECT project_id FROM projects").fetchall()
        if len(rows) != 1:
            raise IntegrityError("banco sem projeto único")
        return rows[0][0]

    def import_plan(self, plan: Plan) -> None:
        project = plan.specification.project_id
        spec_hash = plan.spec_digest
        task_hash = plan.tasks_digest
        with self.transaction() as db:
            existing = db.execute("SELECT project_id,spec_digest,tasks_digest,plan_revision FROM projects").fetchall()
            if existing and existing[0][0] != project:
                raise ConflictError("banco pertence a outro projeto")
            if existing and (existing[0][1], existing[0][2]) == (spec_hash, task_hash):
                return
            if not existing:
                db.execute("INSERT INTO projects VALUES(?,?,?,?,?)", (project, spec_hash, task_hash, 1, now()))
                plan_revision = 1
            else:
                plan_revision = existing[0][3] + 1
                old_ids = {row[0] for row in db.execute("SELECT task_id FROM tasks")}
                new_ids = {task.id for task in plan.tasks}
                if old_ids - new_ids:
                    raise ConflictError(f"IDs com histórico removidos: {', '.join(sorted(old_ids - new_ids))}")
                db.execute("UPDATE projects SET spec_digest=?,tasks_digest=?,plan_revision=? WHERE project_id=?",
                           (spec_hash, task_hash, plan_revision, project))

            for task in plan.tasks:
                snapshot = canonical({"task": asdict(task), "spec_digest": spec_hash})
                definition_hash = digest({"task": asdict(task), "spec_digest": spec_hash})
                row = db.execute("SELECT t.definition_revision,t.status,t.attempt_count,d.definition_digest FROM tasks t JOIN task_definitions d ON (t.project_id,t.task_id,t.definition_revision)=(d.project_id,d.task_id,d.definition_revision) WHERE t.project_id=? AND t.task_id=?", (project, task.id)).fetchone()
                if row and row[3] == definition_hash:
                    continue
                revision = row[0] + 1 if row else 1
                db.execute("INSERT INTO task_definitions VALUES(?,?,?,?,?,?)",
                           (project, task.id, revision, definition_hash, spec_hash, snapshot))
                if row:
                    has_history = db.execute("SELECT 1 FROM task_events WHERE project_id=? AND task_id=? AND reason NOT LIKE 'plan import%' LIMIT 1", (project, task.id)).fetchone()
                    if row[1] not in ("pending", "ready") or row[2] or has_history:
                        db.execute("UPDATE tasks SET revalidation_required=1 WHERE project_id=? AND task_id=?", (project, task.id))
                        continue
                    db.execute("DELETE FROM task_dependencies WHERE project_id=? AND task_id=?", (project, task.id))
                    db.execute("UPDATE tasks SET definition_revision=?,status=?,revision=revision+1,updated_at=? WHERE project_id=? AND task_id=?",
                               (revision, "pending" if task.depends_on else "ready", now(), project, task.id))
                    self._event(db, project, task.id, revision, row[1], "pending" if task.depends_on else "ready",
                                "plan import revision", f"plan:{plan_revision}:{task.id}", "plan_import")
                else:
                    db.execute("INSERT INTO tasks(project_id,task_id,definition_revision,status,updated_at) VALUES(?,?,?,?,?)",
                               (project, task.id, revision, "pending" if task.depends_on else "ready", now()))
                    self._event(db, project, task.id, revision, None, "pending" if task.depends_on else "ready",
                                "plan import creation", f"plan:{plan_revision}:{task.id}", "plan_import")

            for task in plan.tasks:
                row = db.execute("SELECT definition_revision,revalidation_required,status FROM tasks WHERE project_id=? AND task_id=?", (project, task.id)).fetchone()
                if row[1]:
                    continue
                for dep in task.depends_on:
                    db.execute("INSERT OR IGNORE INTO task_dependencies VALUES(?,?,?)", (project, task.id, dep))
                dependencies_done = self._dependencies_done(db, project, task.id)
                if row[2] == "pending" and dependencies_done:
                    db.execute("UPDATE tasks SET status='ready',revision=revision+1,updated_at=? WHERE project_id=? AND task_id=?", (now(), project, task.id))
                    self._event(db, project, task.id, row[0], "pending", "ready",
                                "plan import dependencies complete", f"plan:{plan_revision}:{task.id}", "plan_import")
                elif row[2] == "ready" and not dependencies_done:
                    db.execute("UPDATE tasks SET status='pending',revision=revision+1,updated_at=? WHERE project_id=? AND task_id=?", (now(), project, task.id))
                    self._event(db, project, task.id, row[0], "ready", "pending",
                                "plan import dependency changed", f"plan:{plan_revision}:{task.id}", "plan_import")

    def list_tasks(self) -> tuple[TaskState, ...]:
        return tuple(TaskState(*row) for row in self.db.execute(
            "SELECT task_id,status,definition_revision,revision,attempt_count,revalidation_required FROM tasks ORDER BY task_id"))

    def _operation(self, db: sqlite3.Connection, operation_id: str, request: object) -> tuple[int, str] | None:
        if not operation_id.strip():
            raise ConflictError("operation_id vazio")
        request_hash = digest(request)
        row = db.execute("SELECT request_digest,result_id,result_snapshot FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row:
            if row[0] != request_hash:
                raise ConflictError("operation_id reutilizado com parâmetros diferentes")
            return row[1], row[2]
        return None

    @staticmethod
    def _external_operation_id(operation_id: str) -> None:
        if operation_id.startswith("recovery:"):
            raise ConflictError("operation_id reservado para recuperação")

    def _record_operation(self, db: sqlite3.Connection, operation_id: str, request: object,
                          result_type: str, result_id: int, result_snapshot: str = "") -> None:
        db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?)",
                   (operation_id, digest(request), result_type, result_id, result_snapshot, now()))

    def _event(self, db: sqlite3.Connection, project: str, task_id: str, definition_revision: int,
               before: str | None, after: str, reason: str, operation_id: str, kind: str) -> int:
        event_id = db.execute("INSERT INTO task_events(project_id,task_id,definition_revision,from_status,to_status,reason,operation_id,occurred_at) VALUES(?,?,?,?,?,?,?,?)",
                              (project, task_id, definition_revision, before, after, reason, operation_id, now())).lastrowid
        plan_revision = db.execute("SELECT plan_revision FROM projects WHERE project_id=?", (project,)).fetchone()[0]
        db.execute("INSERT INTO checkpoints(project_id,event_id,plan_revision,created_at,kind) VALUES(?,?,?,?,?)",
                   (project, event_id, plan_revision, now(), kind))
        return event_id

    def _task_row(self, db: sqlite3.Connection, project: str, task_id: str) -> tuple:
        row = db.execute("SELECT status,definition_revision,revision,attempt_count,revalidation_required,owner,reservation_operation FROM tasks WHERE project_id=? AND task_id=?", (project, task_id)).fetchone()
        if row is None:
            raise TransitionError(f"tarefa desconhecida: {task_id}")
        return row

    def _state(self, db: sqlite3.Connection, project: str, task_id: str) -> TaskState:
        row = self._task_row(db, project, task_id)
        return TaskState(task_id, row[0], row[1], row[2], row[3], bool(row[4]))

    def _dependencies_done(self, db: sqlite3.Connection, project: str, task_id: str) -> bool:
        return not db.execute("SELECT 1 FROM task_dependencies d JOIN tasks t ON (d.project_id,d.depends_on_id)=(t.project_id,t.task_id) WHERE d.project_id=? AND d.task_id=? AND (t.status!='completed' OR t.revalidation_required=1) LIMIT 1", (project, task_id)).fetchone()

    def revalidate(self, task_id: str, operation_id: str, author: str, reason: str) -> TaskState:
        self._external_operation_id(operation_id)
        if not author.strip() or not reason.strip():
            raise TransitionError("revalidação exige autor e justificativa")
        with self.transaction() as db:
            project = self._project_id()
            request = {"verb": "revalidate", "project": project, "task_id": task_id,
                       "author": author, "reason": reason}
            if prior := self._operation(db, operation_id, request):
                return TaskState(**json.loads(prior[1]))
            old = self._task_row(db, project, task_id)
            if not old[4]:
                raise TransitionError("tarefa não requer revalidação")
            newest = db.execute("SELECT max(definition_revision) FROM task_definitions WHERE project_id=? AND task_id=?", (project, task_id)).fetchone()[0]
            status = "blocked"
            snapshot = db.execute("SELECT definition_snapshot FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=?", (project, task_id, newest)).fetchone()[0]
            dependencies = json.loads(snapshot)["task"]["depends_on"]
            db.execute("DELETE FROM task_dependencies WHERE project_id=? AND task_id=?", (project, task_id))
            for dependency in dependencies:
                db.execute("INSERT INTO task_dependencies VALUES(?,?,?)", (project, task_id, dependency))
            db.execute("UPDATE tasks SET definition_revision=?,status=?,revision=revision+1,revalidation_required=0,owner='',reservation_operation='',updated_at=? WHERE project_id=? AND task_id=?",
                       (newest, status, now(), project, task_id))
            event = self._event(db, project, task_id, newest, old[0], status,
                                f"revalidated by {author}: {reason}", operation_id, "revalidation")
            result = self._state(db, project, task_id)
            self._record_operation(db, operation_id, request, "event", event, canonical(asdict(result)))
            return result

    def add_evidence(self, item: EvidenceInput) -> int:
        self._external_operation_id(item.operation_id)
        with self.transaction() as db:
            project = self._project_id()
            request = {"verb": "evidence", "project": project, **asdict(item)}
            prior = self._operation(db, item.operation_id, request)
            if prior is not None:
                return prior[0]
            row = self._task_row(db, project, item.task_id)
            if row[4]:
                raise ConflictError("definição requer revalidação")
            if row[0] == "completed":
                raise TransitionError("tarefa concluída é imutável")
            snapshot = db.execute("SELECT definition_snapshot FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=?", (project, item.task_id, row[1])).fetchone()[0]
            criteria = {criterion["id"] for criterion in json.loads(snapshot)["task"]["acceptance_criteria"]}
            if item.criterion_id not in criteria:
                raise TransitionError("critério desconhecido na revisão")
            if item.kind not in ("declared", "verified") or not item.source.strip() or not item.payload.strip():
                raise TransitionError("evidência inválida")
            if item.kind == "verified":
                if not item.method.strip() or not item.author.strip() or item.declaration_id is None:
                    raise TransitionError("verificação exige declaração, método e autor")
                declaration = db.execute("SELECT project_id,task_id,definition_revision,criterion_id,kind FROM evidence WHERE evidence_id=?", (item.declaration_id,)).fetchone()
                if declaration != (project, item.task_id, row[1], item.criterion_id, "declared"):
                    raise TransitionError("declaração incompatível")
            elif item.declaration_id is not None or item.passed:
                raise TransitionError("declaração não pode conter resultado de verificação")
            evidence_id = db.execute("INSERT INTO evidence(project_id,task_id,definition_revision,criterion_id,kind,payload,source,declaration_id,method,author,passed,recorded_at,operation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (project, item.task_id, row[1], item.criterion_id, item.kind, item.payload, item.source,
                                      item.declaration_id, item.method, item.author, int(item.passed), now(), item.operation_id)).lastrowid
            self._event(db, project, item.task_id, row[1], row[0], row[0],
                        f"evidence:{evidence_id}:{item.kind}", item.operation_id, "evidence")
            self._record_operation(db, item.operation_id, request, "evidence", evidence_id)
            return evidence_id

    def transition(self, command: Transition) -> TaskState:
        self._external_operation_id(command.operation_id)
        allowed = {"pending": {"ready", "blocked"}, "ready": {"reserved", "blocked"},
                   "reserved": {"completed", "failed", "ready"}, "failed": {"ready", "blocked"},
                   "blocked": {"pending", "ready"}, "completed": set()}
        with self.transaction() as db:
            project = self._project_id()
            request = {"verb": "transition", "project": project, **asdict(command)}
            if prior := self._operation(db, command.operation_id, request):
                return TaskState(**json.loads(prior[1]))
            old = self._task_row(db, project, command.task_id)
            before, definition_revision, revision = old[:3]
            if old[4]:
                raise ConflictError("definição requer revalidação")
            if revision != command.expected_revision:
                raise ConflictError("revisão de estado divergente")
            if command.destination not in allowed[before]:
                raise TransitionError(f"transição inválida: {before} -> {command.destination}")
            if command.destination in ("ready", "reserved") and not self._dependencies_done(db, project, command.task_id):
                raise TransitionError("dependências incompletas")
            if before == "blocked" and command.destination != ("ready" if self._dependencies_done(db, project, command.task_id) else "pending"):
                raise TransitionError("destino incompatível com dependências")
            if command.destination in ("blocked", "failed") or (before == "reserved" and command.destination == "ready"):
                if not command.reason.strip():
                    raise TransitionError("motivo obrigatório")
            if command.destination == "reserved" and not command.owner.strip():
                raise TransitionError("reserva exige proprietário")
            if command.destination == "completed":
                if bool(command.exception_author.strip()) != bool(command.exception_reason.strip()):
                    raise TransitionError("exceção exige autor e justificativa")
                snapshot = db.execute("SELECT definition_snapshot FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=?", (project, command.task_id, definition_revision)).fetchone()[0]
                criteria = {item["id"] for item in json.loads(snapshot)["task"]["acceptance_criteria"]}
                verified = {row[0] for row in db.execute("SELECT criterion_id FROM evidence WHERE project_id=? AND task_id=? AND definition_revision=? AND kind='verified' AND passed=1", (project, command.task_id, definition_revision))}
                if not criteria <= verified and not (command.exception_author.strip() and command.exception_reason.strip()):
                    raise TransitionError("critérios sem evidência verificada")
            db.execute("UPDATE tasks SET status=?,revision=revision+1,attempt_count=attempt_count+?,owner=?,reservation_operation=?,updated_at=? WHERE project_id=? AND task_id=? AND revision=?",
                       (command.destination, int(command.destination == "reserved"), command.owner if command.destination == "reserved" else "",
                        command.operation_id if command.destination == "reserved" else "", now(), project, command.task_id, revision))
            reason = command.reason
            if command.destination == "completed" and command.exception_author:
                reason = f"exception by {command.exception_author}: {command.exception_reason}"
            event = self._event(db, project, command.task_id, definition_revision, before, command.destination,
                                reason, command.operation_id, "transition")
            if command.destination == "completed":
                for dependent in db.execute("SELECT task_id FROM task_dependencies WHERE project_id=? AND depends_on_id=? ORDER BY task_id", (project, command.task_id)).fetchall():
                    task_id = dependent[0]
                    target = self._task_row(db, project, task_id)
                    if target[0] == "pending" and not target[4] and self._dependencies_done(db, project, task_id):
                        db.execute("UPDATE tasks SET status='ready',revision=revision+1,updated_at=? WHERE project_id=? AND task_id=?", (now(), project, task_id))
                        self._event(db, project, task_id, target[1], "pending", "ready", "dependencies completed", command.operation_id, "promotion")
            result = self._state(db, project, command.task_id)
            self._record_operation(db, command.operation_id, request, "event", event, canonical(asdict(result)))
            return result

    def reopen(self, plan: Plan) -> tuple[str, ...]:
        """Verify the supplied definitions before deterministic recovery."""
        self.check_integrity()
        project = self._project_id()
        row = self.db.execute("SELECT spec_digest,tasks_digest FROM projects WHERE project_id=?", (project,)).fetchone()
        if project != plan.specification.project_id or row != (plan.spec_digest, plan.tasks_digest):
            raise ConflictError("definições alteradas; importação e revalidação explícitas necessárias")
        return self.recover()

    def recover(self) -> tuple[str, ...]:
        self.check_integrity()
        project = self._project_id()
        recovered: list[str] = []
        with self.transaction() as db:
            reservations = db.execute("SELECT task_id,definition_revision,status,reservation_operation FROM tasks WHERE project_id=? AND status='reserved' ORDER BY task_id", (project,)).fetchall()
            for task_id, definition_revision, before, reservation_id in reservations:
                if not reservation_id:
                    raise IntegrityError(f"reserva sem operation_id: {task_id}")
                operation_id = f"recovery:{reservation_id}"
                request = {"verb": "recover", "project": project, "task_id": task_id,
                           "reservation_operation": reservation_id}
                if self._operation(db, operation_id, request) is not None:
                    continue
                db.execute("UPDATE tasks SET status='blocked',revision=revision+1,owner='',reservation_operation='',updated_at=? WHERE project_id=? AND task_id=? AND status='reserved'",
                           (now(), project, task_id))
                event = self._event(db, project, task_id, definition_revision, before, "blocked",
                                    "recovery_required", operation_id, "recovery")
                self._record_operation(db, operation_id, request, "event", event)
                recovered.append(task_id)
        return tuple(recovered)
