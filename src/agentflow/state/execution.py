"""Transactional execution attempts layered on the phase 2 state store."""

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agentflow.agents import (ExecutionRequest, ModelSelectionObservation, ProcessIdentity,
                              ProcessResult, ProcessSpec)
from agentflow.planning import canonical, digest

from .contracts import ConflictError, TransitionError
from .sqlite import SQLiteStore, now


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    task_id: str
    definition_revision: int
    status: str
    validation_status: str
    operation_id: str
    created: bool = field(default=False, compare=False)


class ExecutionStore(SQLiteStore):
    def __init__(self, path: Path, *, agent_models: bool = False):
        super().__init__(path, execution=True, git=True, validation=agent_models,
                         agent_models=agent_models)


    def attempt(self, attempt_id: str) -> Attempt:
        row = self.db.execute("SELECT attempt_id,task_id,definition_revision,status,validation_status,operation_id FROM execution_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise TransitionError("tentativa desconhecida")
        return Attempt(*row)

    def process_identity(self, attempt_id: str) -> ProcessIdentity | None:
        row = self.db.execute("SELECT pid,process_started_at,group_id,parent_pid,parent_started_at,process_token FROM execution_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row[0] is None or row[1] is None or row[2] is None:
            return None
        return ProcessIdentity(row[0], row[1], row[2], row[3] or 0, attempt_id, row[5] or "", row[4] or "")

    def prepare(self, request: ExecutionRequest, spec: ProcessSpec, owner: str, *, retry_reason: str = "") -> Attempt:
        if not owner.strip() or not request.operation_id.strip():
            raise ConflictError("proprietário e operation_id obrigatórios")
        if not request.instruction.strip():
            raise TransitionError("instrução vazia")
        fingerprint = digest({"request": {"project_id": request.project_id, "task_id": request.task_id,
                             "definition_revision": request.definition_revision,
                             "definition_digest": request.definition_digest,
                             "workspace": str(request.workspace.resolve()), "instruction": request.instruction,
                             "workspace_id": request.workspace_id,
                             "acceptance_criteria": request.acceptance_criteria,
                             "policy": request.policy.__dict__, "retry_reason": retry_reason,
                             "model_selection": request.model_selection.request_data()
                             if request.model_selection else None},
                              "adapter": spec.adapter_name, "version": spec.adapter_version,
                              "adapter_api_version": spec.adapter_api_version,
                              "provider_id": spec.provider_id, "model_id": spec.model_id,
                              "sandbox": spec.effective_sandbox, "approval": spec.effective_approval})
        with self.transaction() as db:
            prior = db.execute("SELECT attempt_id,request_digest FROM execution_attempts WHERE operation_id=?", (request.operation_id,)).fetchone()
            if prior:
                if prior[1] != fingerprint:
                    raise ConflictError("operation_id reutilizado com parâmetros diferentes")
                return self.attempt(prior[0])
            operation = db.execute("SELECT request_digest,result_type FROM operations WHERE operation_id=?", (request.operation_id,)).fetchone()
            if operation is not None:
                raise ConflictError("operation_id já utilizado por outra operação")
            project = db.execute("SELECT project_id FROM projects").fetchone()
            if project != (request.project_id,):
                raise ConflictError("projeto divergente")
            row = db.execute("SELECT t.status,t.definition_revision,t.revalidation_required,t.attempt_count,d.definition_digest FROM tasks t JOIN task_definitions d ON (t.project_id,t.task_id,t.definition_revision)=(d.project_id,d.task_id,d.definition_revision) WHERE t.project_id=? AND t.task_id=?", (request.project_id, request.task_id)).fetchone()
            if row and row[0] == "blocked" and retry_reason.strip():
                last = db.execute("SELECT attempt_id,status FROM execution_attempts WHERE project_id=? AND task_id=? ORDER BY prepared_at DESC LIMIT 1", (request.project_id, request.task_id)).fetchone()
                if last is None or last[1] not in ("process_succeeded", "process_failed", "timed_out", "cancelled", "interrupted"):
                    raise TransitionError("tentativa anterior ainda não reconciliada")
                if row[3] >= 3 or not self._dependencies_done(db, request.project_id, request.task_id):
                    raise TransitionError("nova tentativa não permitida")
                retry_operation = f"retry:{request.operation_id}"
                if db.execute("SELECT 1 FROM operations WHERE operation_id=?", (retry_operation,)).fetchone():
                    raise ConflictError("operation_id de revalidação já utilizado")
                db.execute("UPDATE tasks SET status='ready',revision=revision+1,updated_at=? WHERE project_id=? AND task_id=? AND status='blocked'", (now(), request.project_id, request.task_id))
                self._event(db, request.project_id, request.task_id, row[1], "blocked", "ready",
                            f"retry authorized: {retry_reason[:512]}", retry_operation, "retry_authorization")
                db.execute("INSERT INTO operations(operation_id,request_digest,result_type,result_id,result_snapshot,created_at) VALUES(?,?,?,?,?,?)",
                           (retry_operation, digest({"task_id": request.task_id, "reason": retry_reason}), "retry_authorization", None, last[0], now()))
                row = ("ready", row[1], row[2], row[3], row[4])
            elif retry_reason.strip():
                raise TransitionError("justificativa de repetição fornecida para tarefa que não está bloqueada")
            if row is None or row[0] != "ready" or row[2] or row[1] != request.definition_revision or row[4] != request.definition_digest:
                raise TransitionError("tarefa não está pronta na revisão solicitada")
            if row[3] >= 3:
                raise TransitionError("limite de três tentativas atingido")
            if not self._dependencies_done(db, request.project_id, request.task_id):
                raise TransitionError("dependências incompletas")
            definition_snapshot = db.execute("SELECT definition_snapshot FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=?",
                                             (request.project_id, request.task_id, request.definition_revision)).fetchone()[0]
            authorized_paths = tuple(json.loads(definition_snapshot)["task"].get("versioned_paths", ()))
            if authorized_paths:
                if not request.workspace_id:
                    raise TransitionError("tarefa com escrita versionada exige workspace Git")
                workspace = db.execute("SELECT worktree_path,status,project_id,task_id,definition_revision,authorized_paths_json FROM workspaces WHERE workspace_id=?",
                                       (request.workspace_id,)).fetchone()
                if (workspace is None or workspace[1] != "active"
                        or workspace[2:5] != (request.project_id, request.task_id, request.definition_revision)
                        or Path(workspace[0]).resolve() != request.workspace.resolve()
                        or tuple(json.loads(workspace[5])) != authorized_paths):
                    raise TransitionError("workspace não corresponde ao escopo/revisão solicitados")
            attempt_id = uuid4().hex
            db.execute("UPDATE tasks SET status='reserved',revision=revision+1,attempt_count=attempt_count+1,owner=?,reservation_operation=?,updated_at=? WHERE project_id=? AND task_id=?",
                       (owner, request.operation_id, now(), request.project_id, request.task_id))
            self._event(db, request.project_id, request.task_id, request.definition_revision,
                        "ready", "reserved", "execution prepared", request.operation_id, "execution")
            db.execute("INSERT INTO execution_attempts(attempt_id,project_id,task_id,definition_revision,operation_id,request_digest,status,owner,prepared_at,adapter_name,adapter_version,capabilities,effective_sandbox,effective_approval,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (attempt_id, request.project_id, request.task_id, request.definition_revision,
                        request.operation_id, fingerprint, "prepared", owner, now(), spec.adapter_name,
                        spec.adapter_version, canonical(spec.capabilities), spec.effective_sandbox, spec.effective_approval,
                        request.workspace_id or None))
            db.execute("INSERT INTO operations(operation_id,request_digest,result_type,result_id,result_snapshot,created_at) VALUES(?,?,?,?,?,?)",
                       (request.operation_id, fingerprint, "execution_attempt", None, attempt_id, now()))
            if self.agent_models:
                if request.model_selection is None:
                    raise TransitionError("seleção explícita requerida no fluxo de adapters F6")
                selected = request.model_selection
                if (selected.agent_id != spec.adapter_name
                        or selected.provider_id != spec.provider_id
                        or selected.model_id != spec.model_id):
                    raise TransitionError("seleção solicitada diverge do adapter/modelo preparado")
                snapshot = selected.as_snapshot(
                    attempt_id=attempt_id, adapter_id=spec.adapter_name,
                    adapter_api_version=spec.adapter_api_version,
                    adapter_version=spec.adapter_version,
                    prepared_provider_id=spec.provider_id, prepared_model_id=spec.model_id,
                    capabilities=spec.capabilities, created_at=now())
                encoded = canonical(snapshot)
                db.execute("INSERT INTO agent_model_selections(attempt_id,snapshot_json,snapshot_digest,created_at) VALUES(?,?,?,?)",
                           (attempt_id, encoded, digest(snapshot), snapshot["created_at"]))
            self._execution_event(db, attempt_id, "prepared", {"owner": owner})
            current = self.attempt(attempt_id)
            return Attempt(current.attempt_id, current.task_id, current.definition_revision,
                           current.status, current.validation_status, current.operation_id, True)

    def model_selection_snapshot(self, attempt_id: str) -> dict | None:
        row = self.db.execute("SELECT snapshot_json,snapshot_digest FROM agent_model_selections WHERE attempt_id=?",
                              (attempt_id,)).fetchone()
        if row is None:
            return None
        snapshot = json.loads(row[0])
        if digest(snapshot) != row[1]:
            raise TransitionError("snapshot de seleção corrompido")
        return snapshot

    def record_model_selection_observation(self, observation: ModelSelectionObservation) -> None:
        with self.transaction() as db:
            snapshot = self.model_selection_snapshot(observation.attempt_id)
            if snapshot is None:
                raise TransitionError("snapshot de seleção ausente")
            expected = (snapshot["provider_id"], snapshot["model_id"])
            actual = (observation.effective_provider_id, observation.effective_model_id)
            expected_status = "confirmed" if expected == actual else ("unknown" if "unknown" in actual else "mismatch")
            if observation.status != expected_status:
                raise TransitionError("status da observação diverge da identidade efetiva")
            prior = db.execute("SELECT provider_id,model_id,status,source,observed_at FROM agent_model_observations WHERE attempt_id=?",
                               (observation.attempt_id,)).fetchone()
            value = (observation.effective_provider_id, observation.effective_model_id,
                     observation.status, observation.source, observation.observed_at)
            if prior is not None:
                if prior != value:
                    raise ConflictError("observação de seleção divergente")
                return
            db.execute("INSERT INTO agent_model_observations(attempt_id,provider_id,model_id,status,source,observed_at) VALUES(?,?,?,?,?,?)",
                       (observation.attempt_id, *value))

    @staticmethod
    def _execution_event(db: sqlite3.Connection, attempt_id: str, kind: str, payload: dict) -> None:
        encoded = canonical(payload)
        if len(encoded.encode("utf-8")) > 4096:
            raise ConflictError("evento de execução excede 4 KiB")
        db.execute("INSERT INTO execution_events(attempt_id,kind,payload,occurred_at) VALUES(?,?,?,?)",
                   (attempt_id, kind, encoded, now()))

    def started(self, attempt_id: str, identity: ProcessIdentity) -> None:
        with self.transaction() as db:
            current = self.attempt(attempt_id)
            if current.status != "prepared" or identity.attempt_id != attempt_id:
                raise TransitionError("identidade ou estado da tentativa inválido")
            db.execute("UPDATE execution_attempts SET status='running',started_at=?,pid=?,process_started_at=?,group_id=?,parent_pid=?,parent_started_at=?,process_token=? WHERE attempt_id=?",
                       (now(), identity.pid, identity.started_at, identity.group_id, identity.parent_pid,
                        identity.parent_started_at, identity.token, attempt_id))
            self._execution_event(db, attempt_id, "running", {"pid": identity.pid, "started_at": identity.started_at})

    def finish(self, attempt_id: str, result: ProcessResult) -> Attempt:
        if result.status not in ("process_succeeded", "process_failed", "timed_out", "cancelled", "interrupted"):
            raise TransitionError("resultado de processo inválido")
        with self.transaction() as db:
            current = self.attempt(attempt_id)
            if current.status not in ("prepared", "running"):
                raise TransitionError("tentativa já encerrada")
            db.execute("UPDATE execution_attempts SET status=?,finished_at=?,exit_code=?,reason=?,stdout_path=?,stderr_path=?,stdout_digest=?,stderr_digest=?,stdout_bytes=?,stderr_bytes=? WHERE attempt_id=?",
                       (result.status, now(), result.exit_code, result.reason[:1024], str(result.stdout_path), str(result.stderr_path), result.stdout_digest, result.stderr_digest, result.stdout_bytes, result.stderr_bytes, attempt_id))
            self._execution_event(db, attempt_id, result.status, {"exit_code": result.exit_code, "reason": result.reason[:512]})
            db.execute("UPDATE tasks SET status='blocked',owner='',reservation_operation='',revision=revision+1,updated_at=? WHERE project_id=(SELECT project_id FROM execution_attempts WHERE attempt_id=?) AND task_id=? AND status='reserved'",
                       (now(), attempt_id, current.task_id))
            self._event(db, db.execute("SELECT project_id FROM execution_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0],
                        current.task_id, current.definition_revision, "reserved", "blocked",
                        f"execution {result.status}", f"attempt:{attempt_id}:{result.status}", "execution")
            return self.attempt(attempt_id)

    def set_validation(self, attempt_id: str, passed: bool, evidence_ids: tuple[int, ...], reason: str) -> Attempt:
        if type(passed) is not bool or not reason.strip():
            raise TransitionError("resultado de validação inválido")
        with self.transaction() as db:
            attempt = self.attempt(attempt_id)
            if attempt.status != "process_succeeded" or attempt.validation_status != "pending":
                raise TransitionError("somente processo bem-sucedido pode ser validado uma vez")
            row = db.execute("SELECT project_id FROM execution_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            project = row[0]
            verified = set()
            verified_criteria = set()
            for evidence_id in evidence_ids:
                item = db.execute("SELECT task_id,definition_revision,kind,passed,criterion_id FROM evidence WHERE evidence_id=? AND project_id=?", (evidence_id, project)).fetchone()
                if item and item[0] == attempt.task_id and item[1] == attempt.definition_revision and item[2] == "verified" and item[3]:
                    verified.add(evidence_id)
                    verified_criteria.add(item[4])
            if passed and not evidence_ids:
                raise TransitionError("validação requer evidências verificadas")
            # The task acceptance criteria remain enforced by SQLiteStore.transition(completed).
            db.execute("UPDATE execution_attempts SET validation_status=? WHERE attempt_id=?",
                       ("validation_passed" if passed else "validation_failed", attempt_id))
            self._execution_event(db, attempt_id, "validation_passed" if passed else "validation_failed",
                                  {"evidence_ids": sorted(verified), "reason": reason[:512]})
            if passed:
                task = self._task_row(db, project, attempt.task_id)
                snapshot = db.execute("SELECT definition_snapshot FROM task_definitions WHERE project_id=? AND task_id=? AND definition_revision=?", (project, attempt.task_id, attempt.definition_revision)).fetchone()[0]
                task_definition = json.loads(snapshot)["task"]
                criteria = {item["id"] for item in task_definition["acceptance_criteria"]}
                passed_criteria = {dbrow[0] for dbrow in db.execute("SELECT criterion_id FROM evidence WHERE project_id=? AND task_id=? AND definition_revision=? AND kind='verified' AND passed=1", (project, attempt.task_id, attempt.definition_revision))}
                if not criteria <= passed_criteria or not criteria <= verified_criteria:
                    raise TransitionError("critérios sem evidência verificada")
                # Versioned writes need a fresh Git verification in the Git coordinator.
                # Do not complete from validation alone; F4 owns that final guarded transition.
                if task_definition.get("versioned_paths"):
                    return self.attempt(attempt_id)
                operation_id = f"complete:{attempt_id}"
                self._external_operation_id(operation_id)
                db.execute("UPDATE tasks SET status='completed',revision=revision+1,updated_at=? WHERE project_id=? AND task_id=? AND status='blocked'",
                           (now(), project, attempt.task_id))
                self._event(db, project, attempt.task_id, attempt.definition_revision, "blocked", "completed",
                            "execution validated", operation_id, "execution")
                for dependent, dep_revision, state, needs_review in db.execute(
                        "SELECT t.task_id,t.definition_revision,t.status,t.revalidation_required FROM task_dependencies d JOIN tasks t ON (d.project_id,d.task_id)=(t.project_id,t.task_id) WHERE d.project_id=? AND d.depends_on_id=? ORDER BY t.task_id",
                        (project, attempt.task_id)).fetchall():
                    if state == "pending" and not needs_review and self._dependencies_done(db, project, dependent):
                        db.execute("UPDATE tasks SET status='ready',revision=revision+1,updated_at=? WHERE project_id=? AND task_id=?", (now(), project, dependent))
                        self._event(db, project, dependent, dep_revision, "pending", "ready", "dependencies completed", operation_id, "promotion")
            return self.attempt(attempt_id)

    def reconcile_interrupted(self) -> tuple[str, ...]:
        """Conservatively block all ambiguous attempts; never restarts a process."""
        recovered = []
        with self.transaction() as db:
            rows = db.execute("SELECT attempt_id,project_id,task_id,definition_revision,status FROM execution_attempts WHERE status IN ('prepared','running') ORDER BY prepared_at").fetchall()
            for attempt_id, project, task_id, revision, status in rows:
                if status == "running":
                    identity = self.process_identity(attempt_id)
                    if identity is not None:
                        from agentflow.runtime.local import LocalRuntime
                        if LocalRuntime.identity_matches(identity):
                            seen = db.execute("SELECT 1 FROM execution_events WHERE attempt_id=? AND kind='recovery_process_alive'", (attempt_id,)).fetchone()
                            if not seen:
                                self._execution_event(db, attempt_id, "recovery_process_alive", {"pid": identity.pid, "started_at": identity.started_at})
                            continue
                db.execute("UPDATE execution_attempts SET status='interrupted',finished_at=?,reason='recovery_required' WHERE attempt_id=? AND status IN ('prepared','running')", (now(), attempt_id))
                db.execute("UPDATE tasks SET status='blocked',owner='',reservation_operation='',revision=revision+1,updated_at=? WHERE project_id=? AND task_id=? AND status='reserved'", (now(), project, task_id))
                self._execution_event(db, attempt_id, "interrupted", {"previous_status": status, "outcome": "unknown"})
                self._event(db, project, task_id, revision, "reserved", "blocked", "recovery_required",
                            f"recovery:{attempt_id}", "recovery")
                recovered.append(attempt_id)
        return tuple(recovered)

    def request_cancel(self, attempt_id: str, source: str = "cli") -> None:
        with self.transaction() as db:
            attempt = self.attempt(attempt_id)
            if attempt.status != "running":
                return
            exists = db.execute("SELECT 1 FROM execution_events WHERE attempt_id=? AND kind='cancel_requested' LIMIT 1", (attempt_id,)).fetchone()
            if not exists:
                self._execution_event(db, attempt_id, "cancel_requested", {"source": source})

    def cleanup_logs(self, log_root: Path, older_than_days: int = 30, *, now_utc: datetime | None = None) -> tuple[str, ...]:
        if older_than_days < 1:
            raise ValueError("retenção deve ser positiva")
        cutoff = (now_utc or datetime.now(timezone.utc)) - timedelta(days=older_than_days)
        removed = []
        rows = self.db.execute("SELECT attempt_id,finished_at FROM execution_attempts WHERE status NOT IN ('prepared','running') AND finished_at IS NOT NULL").fetchall()
        for attempt_id, finished_at in rows:
            try:
                timestamp = datetime.fromisoformat(finished_at)
            except ValueError:
                continue
            if timestamp < cutoff:
                path = (log_root / attempt_id).resolve()
                if path.parent == log_root.resolve() and path.is_dir():
                    shutil.rmtree(path)
                    removed.append(attempt_id)
        return tuple(removed)

    def storage_metrics(self) -> dict[str, int]:
        return {"database_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "wal_bytes": Path(str(self.path) + "-wal").stat().st_size if Path(str(self.path) + "-wal").exists() else 0,
                "attempts": self.db.execute("SELECT count(*) FROM execution_attempts").fetchone()[0],
                "events": self.db.execute("SELECT count(*) FROM execution_events").fetchone()[0]}


class AgentExecutionStore(ExecutionStore):
    """Execution state with the optional F6 immutable model-selection schema."""

    def __init__(self, path: Path):
        super().__init__(path, agent_models=True)
