"""Transactional v3 persistence for workspaces and local Git operations."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from agentflow.git.contracts import Checkpoint, CheckpointRequest, Workspace, WorkspaceRequest
from agentflow.git.digests import operation_digest, pathset_digest

from .contracts import ConflictError, TransitionError
from .execution import ExecutionStore
from .sqlite import now


class GitStateStore(ExecutionStore):
    def operation(self, operation_id: str) -> tuple[str, str, str] | None:
        row = self.db.execute("SELECT request_digest,status,workspace_id FROM git_operations WHERE operation_id=?",
                              (operation_id,)).fetchone()
        return tuple(row) if row else None

    def workspace(self, workspace_id: str) -> Workspace:
        row = self.db.execute("SELECT workspace_id,project_id,task_id,definition_revision,worktree_path,common_dir,branch,base_oid,status,current_head_oid,repository_root FROM workspaces WHERE workspace_id=?",
                              (workspace_id,)).fetchone()
        if row is None:
            raise TransitionError("workspace desconhecido")
        return Workspace(row[0], row[1], row[2], row[3], Path(row[4]), Path(row[5]),
                         row[6], row[7], row[8], head_oid=row[9], repository_root=Path(row[10]))

    def workspace_for_task(self, project_id: str, task_id: str, revision: int) -> Workspace | None:
        row = self.db.execute("SELECT workspace_id FROM workspaces WHERE project_id=? AND task_id=? AND definition_revision=? AND status!='closed'",
                              (project_id, task_id, revision)).fetchone()
        return self.workspace(row[0]) if row else None

    def begin_workspace(self, request: WorkspaceRequest, workspace_id: str, worktree: Path,
                        common_dir: Path, branch: str, owner_token: str,
                        request_hash: str, pathset_hash: str) -> Workspace:
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM git_operations WHERE operation_id=?", (request.operation_id,)).fetchone():
                raise ConflictError("operation_id Git já reservado")
            project = db.execute("SELECT project_id FROM projects").fetchone()
            if project != (request.project_id,):
                raise ConflictError("projeto divergente")
            row = db.execute("SELECT t.definition_revision,t.revalidation_required,d.definition_digest,d.definition_snapshot FROM tasks t JOIN task_definitions d ON (t.project_id,t.task_id,t.definition_revision)=(d.project_id,d.task_id,d.definition_revision) WHERE t.project_id=? AND t.task_id=?",
                             (request.project_id, request.task_id)).fetchone()
            if row is None or row[0] != request.definition_revision or row[1] or row[2] != request.definition_digest:
                raise ConflictError("escopo/revisão da tarefa divergente ou requer revalidação")
            snapshot = json.loads(row[3])["task"]
            if tuple(snapshot.get("versioned_paths", ())) != request.authorized_paths:
                raise ConflictError("pathset não corresponde ao escopo autorizado da revisão")
            existing = db.execute("SELECT workspace_id FROM workspaces WHERE project_id=? AND task_id=? AND definition_revision=? AND status!='closed'",
                                  (request.project_id, request.task_id, request.definition_revision)).fetchone()
            if existing:
                raise ConflictError("já existe workspace não encerrado para a tarefa/revisão")
            db.execute("INSERT INTO workspaces(workspace_id,project_id,task_id,definition_revision,repository_root,common_dir,worktree_path,branch,base_oid,current_head_oid,scope_digest,authorized_paths_json,status,owner_token,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (workspace_id, request.project_id, request.task_id, request.definition_revision,
                        str(request.repository.resolve()), str(common_dir.resolve()), str(worktree.resolve()), branch,
                        request.base_oid, request.base_oid, pathset_hash, json.dumps(request.authorized_paths, ensure_ascii=False),
                        "creating", owner_token, now(), now()))
            db.execute("INSERT INTO git_operations(operation_id,request_digest,workspace_id,project_id,task_id,definition_revision,kind,status,owner_token,request_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (request.operation_id, request_hash, workspace_id, request.project_id, request.task_id,
                        request.definition_revision, "workspace_create", "prepared", owner_token,
                        json.dumps({**asdict(request), "repository": str(request.repository),
                                    "authorized_paths": list(request.authorized_paths)}, ensure_ascii=False,
                                   sort_keys=True, default=str), "", now(), now()))
            self._git_event(db, request.operation_id, "intent", {"workspace_id": workspace_id, "branch": branch})
            return self.workspace(workspace_id)

    def mark_git_running(self, operation_id: str) -> None:
        with self.transaction() as db:
            cursor = db.execute("UPDATE git_operations SET status='running',updated_at=? WHERE operation_id=? AND status='prepared'",
                                (now(), operation_id))
            if cursor.rowcount != 1:
                raise ConflictError("intenção Git não está preparada")
            self._git_event(db, operation_id, "running", {})

    def mark_abandoned_operation(self, operation_id: str, owner: dict[str, object]) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT workspace_id,status FROM git_operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row is None or row[1] not in ("prepared", "running", "unknown"):
                return
            db.execute("UPDATE git_operations SET status='unknown',updated_at=? WHERE operation_id=?",
                       (now(), operation_id))
            db.execute("UPDATE workspaces SET status='blocked',updated_at=? WHERE workspace_id=? AND status!='closed'",
                       (now(), row[0]))
            self._git_event(db, operation_id, "abandoned_lock", {"owner": owner, "outcome": "unknown"})

    def complete_workspace(self, operation_id: str, result: Workspace) -> Workspace:
        with self.transaction() as db:
            db.execute("UPDATE workspaces SET status='active',current_head_oid=?,updated_at=? WHERE workspace_id=? AND status='creating'",
                       (result.head_oid or result.base_oid, now(), result.workspace_id))
            db.execute("UPDATE git_operations SET status='verified',result_json=?,updated_at=? WHERE operation_id=? AND status='running'",
                       (json.dumps({"path": str(result.path), "head": result.head_oid or result.base_oid, "branch": result.branch}, sort_keys=True), now(), operation_id))
            self._git_event(db, operation_id, "workspace_verified", {"workspace_id": result.workspace_id, "head": result.head_oid or result.base_oid})
            return self.workspace(result.workspace_id)

    def block_git_operation(self, operation_id: str, reason: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT workspace_id FROM git_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE git_operations SET status='unknown',updated_at=? WHERE operation_id=? AND status IN ('prepared','running','unknown')",
                       (now(), operation_id))
            db.execute("UPDATE workspaces SET status='blocked',updated_at=? WHERE workspace_id=? AND status!='closed'",
                       (now(), row[0]))
            self._git_event(db, operation_id, "unknown", {"reason": reason[:512]})

    def authorized_paths(self, workspace_id: str) -> tuple[str, ...]:
        row = self.db.execute("SELECT authorized_paths_json FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        if row is None:
            raise TransitionError("workspace desconhecido")
        return tuple(json.loads(row[0]))

    def checkpoint_by_operation(self, operation_id: str) -> Checkpoint | None:
        row = self.db.execute("SELECT checkpoint_id,workspace_id,operation_id,commit_oid,parent_oid,tree_oid,branch,paths_json,pathset_digest,verified,actor_name,actor_email FROM git_checkpoints WHERE operation_id=?",
                              (operation_id,)).fetchone()
        return Checkpoint(row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                          tuple(json.loads(row[7])), row[8], bool(row[9]), row[10], row[11]) if row else None

    def latest_checkpoint(self, workspace_id: str) -> Checkpoint | None:
        row = self.db.execute("SELECT operation_id FROM git_checkpoints WHERE workspace_id=? AND verified=1 ORDER BY created_at DESC LIMIT 1",
                              (workspace_id,)).fetchone()
        return self.checkpoint_by_operation(row[0]) if row else None

    def latest_attempt_for_task(self, project_id: str, task_id: str, revision: int) -> tuple | None:
        return self.db.execute("SELECT attempt_id,status,validation_status,workspace_id FROM execution_attempts WHERE project_id=? AND task_id=? AND definition_revision=? ORDER BY prepared_at DESC LIMIT 1",
                               (project_id, task_id, revision)).fetchone()

    def pending_git_operations(self) -> tuple[tuple, ...]:
        return tuple(self.db.execute("SELECT operation_id,kind,status,workspace_id,request_json FROM git_operations WHERE status IN ('prepared','running','unknown') ORDER BY created_at,operation_id"))

    def reconcile_git_operation(self, operation_id: str, result: Workspace | Checkpoint | None) -> str:
        with self.transaction() as db:
            row = db.execute("SELECT kind,workspace_id,status FROM git_operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row is None:
                raise TransitionError("operação Git desconhecida")
            kind, workspace_id, status = row
            if status == "verified":
                return status
            if result is None:
                db.execute("UPDATE git_operations SET status='unknown',updated_at=? WHERE operation_id=?",
                           (now(), operation_id))
                db.execute("UPDATE workspaces SET status='blocked',updated_at=? WHERE workspace_id=? AND status!='closed'",
                           (now(), workspace_id))
                self._git_event(db, operation_id, "reconciliation_ambiguous", {"kind": kind})
                return "unknown"
            if kind == "workspace_create" and isinstance(result, Workspace):
                if result.workspace_id != workspace_id or result.status != "active":
                    raise ConflictError("workspace reconciliado com identidade/status divergente")
                db.execute("UPDATE workspaces SET status='active',current_head_oid=?,updated_at=? WHERE workspace_id=? AND status IN ('creating','blocked')",
                           (result.head_oid or result.base_oid, now(), workspace_id))
                db.execute("UPDATE git_operations SET status='verified',result_json=?,updated_at=? WHERE operation_id=?",
                           (json.dumps({"path": str(result.path), "head": result.head_oid or result.base_oid,
                                        "branch": result.branch}, sort_keys=True), now(), operation_id))
                self._git_event(db, operation_id, "workspace_reconciled", {"head": result.head_oid or result.base_oid})
                return "verified"
            if kind == "checkpoint" and isinstance(result, Checkpoint):
                workspace = self.workspace(workspace_id)
                request_row = db.execute("SELECT request_json FROM git_operations WHERE operation_id=?",
                                         (operation_id,)).fetchone()
                request = json.loads(request_row[0])
                if (result.workspace_id != workspace_id or result.operation_id != operation_id
                        or result.parent_oid != workspace.head_oid or result.branch != workspace.branch
                        or not result.verified or not set(result.paths) <= set(request["paths"])):
                    raise ConflictError("checkpoint reconciliado não corresponde à intenção")
                if pathset_digest(result.paths) != result.pathset_digest:
                    raise ConflictError("digest de pathset reconciliado divergente")
                paths = json.dumps(result.paths, ensure_ascii=False, separators=(",", ":"))
                db.execute("INSERT INTO git_checkpoints(checkpoint_id,operation_id,workspace_id,project_id,task_id,definition_revision,base_oid,commit_oid,parent_oid,tree_oid,branch,paths_json,pathset_digest,actor_name,actor_email,verified,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                           (result.checkpoint_id, operation_id, workspace_id, workspace.project_id,
                            workspace.task_id, workspace.definition_revision, result.parent_oid,
                            result.commit_oid, result.parent_oid, result.tree_oid, result.branch, paths,
                            result.pathset_digest, result.actor_name, result.actor_email, now()))
                db.execute("UPDATE workspaces SET status='active',current_head_oid=?,updated_at=? WHERE workspace_id=?",
                           (result.commit_oid, now(), workspace_id))
                db.execute("UPDATE git_operations SET status='verified',result_json=?,updated_at=? WHERE operation_id=?",
                           (json.dumps({"checkpoint_id": result.checkpoint_id, "commit_oid": result.commit_oid,
                                        "tree_oid": result.tree_oid, "paths": result.paths},
                                       ensure_ascii=False, sort_keys=True), now(), operation_id))
                self._git_event(db, operation_id, "checkpoint_reconciled", {"commit_oid": result.commit_oid})
                return "verified"
            if kind == "reconcile" and isinstance(result, Workspace):
                request_row = db.execute("SELECT request_json FROM git_operations WHERE operation_id=?",
                                         (operation_id,)).fetchone()
                request = json.loads(request_row[0])
                workspace = self.workspace(workspace_id)
                if (request.get("action") != "verify_no_changes" or result.workspace_id != workspace_id
                        or result.status != "active" or result.head_oid != workspace.head_oid):
                    raise ConflictError("reconciliação sem alterações incompatível")
                encoded = json.dumps({"no_changes_verified": True, "head_oid": result.head_oid,
                                      "workspace_id": workspace_id}, sort_keys=True)
                db.execute("UPDATE git_operations SET status='verified',result_json=?,updated_at=? WHERE operation_id=?",
                           (encoded, now(), operation_id))
                db.execute("UPDATE workspaces SET status='active',updated_at=? WHERE workspace_id=?",
                           (now(), workspace_id))
                self._git_event(db, operation_id, "no_changes_reconciled", json.loads(encoded))
                return "verified"
            raise ConflictError("resultado de reconciliação incompatível com a operação")

    def begin_checkpoint(self, workspace: Workspace, request: CheckpointRequest,
                         owner_token: str, request_hash: str) -> None:
        with self.transaction() as db:
            existing = db.execute("SELECT request_digest,status FROM git_operations WHERE operation_id=?",
                                  (request.operation_id,)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ConflictError("operation_id reutilizado com parâmetros divergentes")
                raise ConflictError(f"operação checkpoint já existe em estado {existing[1]}")
            row = db.execute("SELECT project_id,task_id,definition_revision,status,branch,current_head_oid,scope_digest,authorized_paths_json FROM workspaces WHERE workspace_id=?",
                             (workspace.workspace_id,)).fetchone()
            if row is None or row[3] != "active":
                raise TransitionError("workspace não está ativo")
            authorized = tuple(json.loads(row[7]))
            if not request.paths or len(request.paths) > 128 or len(set(request.paths)) != len(request.paths):
                raise TransitionError("checkpoint exige paths explícitos, únicos e até 128")
            if not set(request.paths) <= set(authorized):
                raise TransitionError("pathset excede escopo autorizado da revisão")
            if pathset_digest(authorized) != row[6]:
                raise ConflictError("digest do escopo persistido diverge")
            if row[:3] != (workspace.project_id, workspace.task_id, workspace.definition_revision):
                raise ConflictError("identidade da tarefa/revisão do workspace diverge")
            if row[4] != workspace.branch or row[5] != (workspace.head_oid or workspace.base_oid):
                raise ConflictError("HEAD/branch do workspace diverge do estado persistido")
            active = db.execute("SELECT operation_id FROM git_operations WHERE workspace_id=? AND status IN ('prepared','running','unknown') LIMIT 1",
                                (workspace.workspace_id,)).fetchone()
            if active:
                raise ConflictError("workspace tem operação Git ativa ou ambígua")
            encoded = json.dumps({"workspace_id": request.workspace_id, "operation_id": request.operation_id,
                                  "paths": list(request.paths), "actor_name": request.actor_name,
                                  "actor_email": request.actor_email, "message": request.message},
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            db.execute("INSERT INTO git_operations(operation_id,request_digest,workspace_id,project_id,task_id,definition_revision,kind,status,owner_token,request_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (request.operation_id, request_hash, workspace.workspace_id, workspace.project_id,
                        workspace.task_id, workspace.definition_revision, "checkpoint", "prepared", owner_token,
                        encoded, "", now(), now()))
            self._git_event(db, request.operation_id, "intent", {"workspace_id": request.workspace_id,
                                                                   "paths_digest": pathset_digest(request.paths)})

    def complete_checkpoint(self, request: CheckpointRequest, checkpoint: Checkpoint) -> Checkpoint:
        with self.transaction() as db:
            row = db.execute("SELECT repository_root,common_dir,branch,base_oid,current_head_oid,status FROM workspaces WHERE workspace_id=?",
                             (checkpoint.workspace_id,)).fetchone()
            if row is None or row[5] != "active":
                raise TransitionError("workspace não está ativo ao confirmar checkpoint")
            op = db.execute("SELECT status,workspace_id FROM git_operations WHERE operation_id=?",
                            (request.operation_id,)).fetchone()
            if op != ("running", checkpoint.workspace_id):
                raise ConflictError("operação checkpoint não está em execução ou workspace diverge")
            if row[2] != checkpoint.branch or row[4] != checkpoint.parent_oid:
                raise ConflictError("parent/ref checkpoint mudou desde a intenção")
            paths = json.dumps(checkpoint.paths, ensure_ascii=False, separators=(",", ":"))
            if pathset_digest(checkpoint.paths) != checkpoint.pathset_digest:
                raise ConflictError("digest do pathset checkpoint inválido")
            db.execute("INSERT INTO git_checkpoints(checkpoint_id,operation_id,workspace_id,project_id,task_id,definition_revision,base_oid,commit_oid,parent_oid,tree_oid,branch,paths_json,pathset_digest,actor_name,actor_email,verified,created_at) SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,? FROM workspaces WHERE workspace_id=?",
                       (checkpoint.checkpoint_id, request.operation_id, checkpoint.workspace_id,
                        self.workspace(checkpoint.workspace_id).project_id,
                        self.workspace(checkpoint.workspace_id).task_id,
                        self.workspace(checkpoint.workspace_id).definition_revision,
                        checkpoint.parent_oid, checkpoint.commit_oid, checkpoint.parent_oid,
                        checkpoint.tree_oid, checkpoint.branch, paths, checkpoint.pathset_digest,
                        checkpoint.actor_name, checkpoint.actor_email, now(), checkpoint.workspace_id))
            db.execute("UPDATE workspaces SET current_head_oid=?,updated_at=? WHERE workspace_id=? AND current_head_oid=?",
                       (checkpoint.commit_oid, now(), checkpoint.workspace_id, checkpoint.parent_oid))
            db.execute("UPDATE git_operations SET status='verified',result_json=?,updated_at=? WHERE operation_id=? AND status='running'",
                       (json.dumps({"checkpoint_id": checkpoint.checkpoint_id, "commit_oid": checkpoint.commit_oid,
                                    "tree_oid": checkpoint.tree_oid, "paths": checkpoint.paths},
                                   ensure_ascii=False, sort_keys=True), now(), request.operation_id))
            self._git_event(db, request.operation_id, "checkpoint_verified", {"checkpoint_id": checkpoint.checkpoint_id,
                                                                               "commit_oid": checkpoint.commit_oid})
            return checkpoint

    def begin_no_changes(self, workspace: Workspace, operation_id: str, request_digest: str,
                         owner_token: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT status,current_head_oid,branch FROM workspaces WHERE workspace_id=?",
                             (workspace.workspace_id,)).fetchone()
            if row != ("active", workspace.head_oid or workspace.base_oid, workspace.branch):
                raise ConflictError("workspace diverge antes da verificação sem alterações")
            active = db.execute("SELECT operation_id FROM git_operations WHERE workspace_id=? AND status IN ('prepared','running','unknown') LIMIT 1",
                                (workspace.workspace_id,)).fetchone()
            if active:
                raise ConflictError("workspace tem operação Git ativa ou ambígua")
            request = {"action": "verify_no_changes", "workspace_id": workspace.workspace_id,
                       "operation_id": operation_id, "head_oid": row[1]}
            db.execute("INSERT INTO git_operations(operation_id,request_digest,workspace_id,project_id,task_id,definition_revision,kind,status,owner_token,request_json,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (operation_id, request_digest, workspace.workspace_id, workspace.project_id,
                        workspace.task_id, workspace.definition_revision, "reconcile", "prepared",
                        owner_token, json.dumps(request, sort_keys=True), "", now(), now()))
            self._git_event(db, operation_id, "intent", request)

    def complete_no_changes(self, operation_id: str, workspace: Workspace) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT status,workspace_id FROM git_operations WHERE operation_id=?",
                             (operation_id,)).fetchone()
            if row != ("running", workspace.workspace_id):
                raise ConflictError("operação de verificação sem alterações não está ativa")
            result = {"no_changes_verified": True, "head_oid": workspace.head_oid or workspace.base_oid,
                      "workspace_id": workspace.workspace_id}
            db.execute("UPDATE git_operations SET status='verified',result_json=?,updated_at=? WHERE operation_id=?",
                       (json.dumps(result, sort_keys=True), now(), operation_id))
            self._git_event(db, operation_id, "no_changes_verified", result)

    def _git_event(self, db: sqlite3.Connection, operation_id: str, kind: str, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4096:
            raise ConflictError("evento Git excede 4 KiB")
        db.execute("INSERT OR IGNORE INTO git_events(operation_id,kind,payload,occurred_at) VALUES(?,?,?,?)",
                   (operation_id, kind, encoded, now()))
