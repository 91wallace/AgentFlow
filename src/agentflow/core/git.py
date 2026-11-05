"""Application coordination for explicit workspace and checkpoint operations."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import unicodedata
from uuid import uuid4

from agentflow.git.contracts import (Checkpoint, CheckpointRequest, GitConflict, GitError, GitProvider,
                                     LockLease, Workspace, WorkspaceRequest)
from agentflow.git.digests import operation_digest, pathset_digest
from agentflow.state.contracts import Transition, TransitionError
from agentflow.state.git import GitStateStore


class GitCoordinator:
    def __init__(self, store: GitStateStore, provider: GitProvider):
        self.store = store
        self.provider = provider

    def create_workspace(self, request: WorkspaceRequest) -> Workspace:
        repository = request.repository.resolve(strict=True)
        request = WorkspaceRequest(request.project_id, request.task_id, request.definition_revision,
                                   request.definition_digest, request.operation_id, repository,
                                   request.base_oid, tuple(request.authorized_paths))
        request_hash = operation_digest({
            "kind": "workspace_create", "project_id": request.project_id,
            "task_id": request.task_id, "definition_revision": request.definition_revision,
            "definition_digest": request.definition_digest, "operation_id": request.operation_id,
            "repository": str(repository), "base_oid": request.base_oid,
            "paths": list(request.authorized_paths),
        })
        prior = self.store.operation(request.operation_id)
        if prior:
            if prior[0] != request_hash:
                raise GitConflict("operation_id reutilizado com parâmetros divergentes")
            if prior[1] == "verified":
                existing = self.store.workspace(prior[2])
                if not self.provider.verify_workspace(existing):
                    raise GitConflict("workspace verificado diverge da identidade Git persistida")
                return existing
            raise GitConflict(f"operação anterior em estado {prior[1]}; reconciliação explícita necessária")

        info = self.provider.preflight(repository, request.authorized_paths)
        root, common, head = info.get("root"), info.get("common_dir"), info.get("head")
        if not isinstance(root, Path) or not isinstance(common, Path) or not isinstance(head, str):
            raise GitConflict("preflight Git incompleto")
        if root != repository or head != request.base_oid:
            raise GitConflict("raiz ou HEAD diverge do workspace autorizado")
        workspace_id = "ws-" + uuid4().hex
        branch = f"agentflow/ws/{workspace_id[3:]}"
        target = common / "agentflow" / "worktrees" / workspace_id[3:]
        path_digest = pathset_digest(request.authorized_paths)
        repo_lock_id = "repo-" + hashlib.sha256(str(common).encode("utf-8")).hexdigest()[:32]
        with self.provider.lock(root, repo_lock_id, request.operation_id) as repo_lease:
            self._check_old_owner(repo_lease.previous_owner)
            with self.provider.lock(root, workspace_id, request.operation_id) as lease:
                self._check_old_owner(lease.previous_owner)
                return self._create_locked(request, workspace_id, target, common, branch,
                                           path_digest, request_hash, lease)

    def _check_old_owner(self, owner: dict[str, object] | None, *, recovering_operation: str = "") -> None:
        if owner is None:
            return
        previous_id = owner.get("operation_id")
        previous = self.store.operation(str(previous_id)) if previous_id else None
        if previous and previous[1] in ("prepared", "running", "unknown"):
            self.store.mark_abandoned_operation(str(previous_id), owner)
            if str(previous_id) == recovering_operation:
                return
            raise GitConflict("lock abandonado reconciliado como unknown; nenhuma operação repetida")
        if previous is None and not self.store.db.execute("SELECT 1 FROM operations WHERE operation_id=?", (str(previous_id),)).fetchone():
            raise GitConflict("lock contém owner sem operação persistida; reconciliação manual necessária")

    def _create_locked(self, request: WorkspaceRequest, workspace_id: str, target: Path,
                       common: Path, branch: str, path_digest: str, request_hash: str,
                       lease: LockLease) -> Workspace:
        owner = lease.owner or {}
        self.store.begin_workspace(request, workspace_id, target, common, branch,
                                   str(owner.get("token", "")), request_hash, path_digest)
        try:
            self.store.mark_git_running(request.operation_id)
            result = self.provider.create_workspace(request, workspace_id)
            if (result.workspace_id != workspace_id or result.path.resolve(strict=True) != target.resolve(strict=True)
                    or result.branch != branch or result.base_oid != request.base_oid or result.common_dir != common):
                raise GitConflict("identidade observada do workspace diverge da intenção")
            return self.store.complete_workspace(request.operation_id, result)
        except BaseException as exc:
            self.store.block_git_operation(request.operation_id, f"workspace creation ambiguous: {type(exc).__name__}")
            raise

    def create_checkpoint(self, request: CheckpointRequest) -> Checkpoint:
        normalized_paths = tuple(sorted((unicodedata.normalize("NFC", item) for item in request.paths),
                                         key=lambda value: value.encode("utf-8")))
        request = CheckpointRequest(request.workspace_id, request.operation_id, normalized_paths,
                                    request.actor_name, request.actor_email, request.message)
        if (not request.operation_id.isascii() or not 1 <= len(request.operation_id) <= 128
                or any(ord(char) < 33 or ord(char) > 126 for char in request.operation_id)):
            raise GitConflict("operation_id de checkpoint inválido")
        if (not request.actor_name.strip() or not request.actor_email.strip()
                or len(request.actor_name.encode("utf-8")) > 200 or len(request.actor_email.encode("utf-8")) > 254
                or any(ord(char) < 32 or ord(char) == 127 for char in request.actor_name + request.actor_email)):
            raise GitConflict("identidade de autoria inválida")
        if (not request.message.strip() or len(request.message.encode("utf-8")) > 1024
                or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in request.message)):
            raise GitConflict("mensagem de checkpoint inválida")
        request_hash = operation_digest({
            "kind": "checkpoint", "workspace_id": request.workspace_id,
            "operation_id": request.operation_id, "paths": list(request.paths),
            "actor_name": request.actor_name, "actor_email": request.actor_email,
            "message": request.message,
        })
        prior = self.store.operation(request.operation_id)
        if prior:
            if prior[0] != request_hash:
                raise GitConflict("operation_id reutilizado com parâmetros divergentes")
            if prior[1] == "verified":
                checkpoint = self.store.checkpoint_by_operation(request.operation_id)
                workspace = self.store.workspace(request.workspace_id)
                if checkpoint is not None and self.provider.verify_checkpoint(workspace, checkpoint):
                    return checkpoint
                raise GitConflict("checkpoint anterior não está mais presente/verificável no workspace")
            raise GitConflict(f"checkpoint em estado {prior[1]}; reconciliação explícita necessária")
        workspace = self.store.workspace(request.workspace_id)
        authorized = set(self.store.authorized_paths(request.workspace_id))
        if not request.paths or len(request.paths) > 128 or len(set(request.paths)) != len(request.paths):
            raise GitConflict("lista de checkpoint deve ser explícita, única e limitada")
        if not set(request.paths) <= authorized:
            raise GitConflict("pathset excede escopo autorizado da revisão")
        root, common, head = self.provider.inspect(workspace.path)
        if root != workspace.path.resolve(strict=True) or common != workspace.common_dir.resolve(strict=True):
            raise GitConflict("identidade do workspace diverge do registro")
        if head != (workspace.head_oid or workspace.base_oid):
            raise GitConflict("HEAD mudou; checkpoint requer reconciliação")
        with self.provider.lock(workspace.path, workspace.workspace_id, request.operation_id) as lease:
            self._check_old_owner(lease.previous_owner)
            owner = lease.owner or {}
            self.store.begin_checkpoint(workspace, request, str(owner.get("token", "")), request_hash)
            try:
                self.store.mark_git_running(request.operation_id)
                checkpoint = self.provider.create_checkpoint(workspace, request)
                if (not checkpoint.verified or checkpoint.workspace_id != workspace.workspace_id
                        or checkpoint.operation_id != request.operation_id
                        or not self.provider.verify_checkpoint(workspace, checkpoint)):
                    raise GitConflict("checkpoint criado mas identidade/verificação divergente")
                return self.store.complete_checkpoint(request, checkpoint)
            except BaseException as exc:
                self.store.block_git_operation(request.operation_id, f"checkpoint result ambiguous: {type(exc).__name__}")
                raise

    def verify_no_changes(self, workspace_id: str, operation_id: str) -> bool:
        """Persist a verified clean/no-op result without manufacturing an empty commit."""
        if (not operation_id.isascii() or not 1 <= len(operation_id) <= 128
                or any(ord(char) < 33 or ord(char) > 126 for char in operation_id)):
            raise GitConflict("operation_id de verificação inválido")
        workspace = self.store.workspace(workspace_id)
        request_hash = operation_digest({"kind": "verify_no_changes", "workspace_id": workspace_id,
                                         "head_oid": workspace.head_oid or workspace.base_oid})
        prior = self.store.operation(operation_id)
        if prior:
            if prior[0] != request_hash:
                raise GitConflict("operation_id reutilizado com parâmetros divergentes")
            if prior[1] == "verified":
                return self.provider.verify_no_changes(workspace)
            raise GitConflict(f"operação em estado {prior[1]}; reconciliação explícita necessária")
        with self.provider.lock(workspace.repository_root, workspace.workspace_id, operation_id) as lease:
            self._check_old_owner(lease.previous_owner)
            owner = lease.owner or {}
            self.store.begin_no_changes(workspace, operation_id, request_hash, str(owner.get("token", "")))
            try:
                self.store.mark_git_running(operation_id)
                if not self.provider.verify_no_changes(workspace):
                    raise GitConflict("workspace contém alterações; checkpoint ou reconciliação necessária")
                self.store.complete_no_changes(operation_id, workspace)
                return True
            except BaseException as exc:
                self.store.block_git_operation(operation_id, f"no-change verification ambiguous: {type(exc).__name__}")
                raise

    def recover(self) -> tuple[tuple[str, str], ...]:
        results: list[tuple[str, str]] = []
        for operation_id, kind, status, workspace_id, request_json in self.store.pending_git_operations():
            workspace = self.store.workspace(workspace_id)
            try:
                with self.provider.lock(workspace.repository_root, workspace.workspace_id, operation_id) as lease:
                    if lease.previous_owner:
                        self._check_old_owner(lease.previous_owner, recovering_operation=operation_id)
                    else:
                        self.store.mark_abandoned_operation(operation_id, {"reason": "lock livre durante recovery"})
                    observed = self.provider.reconcile_operation(workspace, kind, operation_id, request_json)
                    state = self.store.reconcile_git_operation(operation_id, observed)
                    results.append((operation_id, state))
            except GitConflict as exc:
                if "ocupado" in str(exc):
                    results.append((operation_id, "still_active"))
                    continue
                results.append((operation_id, "unknown"))
            except (GitError, OSError, ValueError):
                self.store.reconcile_git_operation(operation_id, None)
                results.append((operation_id, "unknown"))
        return tuple(results)

    def complete_task(self, command: Transition):
        """Complete only after the persisted checkpoint is independently verified live."""
        if command.destination != "completed":
            raise TransitionError("complete_task aceita somente destino completed")
        project = self.store.db.execute("SELECT project_id FROM projects").fetchone()
        if project is None:
            raise TransitionError("projeto não inicializado")
        task = self.store.db.execute(
            "SELECT t.definition_revision,d.definition_snapshot FROM tasks t JOIN task_definitions d "
            "USING(project_id,task_id,definition_revision) WHERE t.project_id=? AND t.task_id=?",
            (project[0], command.task_id),
        ).fetchone()
        if task is None:
            raise TransitionError("tarefa desconhecida")
        if json.loads(task[1])["task"].get("versioned_paths"):
            attempt = self.store.latest_attempt_for_task(project[0], command.task_id, task[0])
            if attempt is None or attempt[1] != "process_succeeded" or attempt[2] != "validation_passed" or not attempt[3]:
                raise TransitionError("conclusão exige execução e validação aprovadas na revisão vigente")
            workspace = self.store.workspace(attempt[3])
            preliminary_checkpoint = self.store.latest_checkpoint(workspace.workspace_id)
            noop = self.store.db.execute("SELECT operation_id,result_json FROM git_operations WHERE workspace_id=? AND kind='reconcile' AND status='verified' ORDER BY updated_at DESC LIMIT 1",
                                         (workspace.workspace_id,)).fetchone()
            checkpoint_current = (preliminary_checkpoint is not None
                                  and preliminary_checkpoint.commit_oid == workspace.head_oid)
            noop_current = False
            if noop is not None:
                try:
                    result = json.loads(noop[1])
                    noop_current = (result.get("no_changes_verified") is True
                                    and result.get("head_oid") == workspace.head_oid)
                except (TypeError, ValueError):
                    noop_current = False
            if not checkpoint_current and not noop_current:
                raise TransitionError("conclusão exige checkpoint Git atual, ativo e verificável")
            with self.provider.lock(workspace.repository_root, workspace.workspace_id,
                                    preliminary_checkpoint.operation_id if checkpoint_current else noop[0]) as lease:
                self._check_old_owner(lease.previous_owner)
                workspace = self.store.workspace(attempt[3])
                checkpoint = self.store.latest_checkpoint(workspace.workspace_id)
                checkpoint_valid = (checkpoint is not None and checkpoint.commit_oid == workspace.head_oid
                                    and self.provider.verify_checkpoint(workspace, checkpoint))
                if not checkpoint_valid and not self.provider.verify_no_changes(workspace):
                    raise TransitionError("conclusão exige checkpoint Git atual, ativo e verificável")
                if workspace.status != "active":
                    raise TransitionError("workspace bloqueado requer reconciliação antes da conclusão")
                return self.store.transition(command)
        return self.store.transition(command)
