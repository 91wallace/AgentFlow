"""SQLite v4 state boundary for validation, build and delivery records."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from agentflow.git.contracts import Checkpoint, Workspace
from agentflow.planning import Plan, canonical, digest
from agentflow.planning.loader import command_data, gate_data, task_data
from agentflow.validation.contracts import (GateDefinition, GateResult, GateStatus,
                                            RunStatus, RuntimeCapability,
                                            CapabilityState, ValidationRequest,
                                            ValidationResult, ValidationSnapshot)

from .contracts import ConflictError, TransitionError
from .git import GitStateStore
from .sqlite import SQLiteStore, now


@dataclass(frozen=True)
class ValidationReservation:
    validation_run_id: str
    operation_id: str
    status: str
    request_digest: str
    created: bool = False


class ValidationStateStore(GitStateStore):
    """F4 Git state plus the transactional F5 schema.

    All validation mutations use short SQLite transactions; no transaction is
    held across snapshot I/O or runtime execution.
    """

    def __init__(self, path: Path):
        SQLiteStore.__init__(self, path, execution=True, git=True, validation=True)

    def begin_validation(self, request: ValidationRequest, workspace: Workspace,
                         checkpoint: Checkpoint, plan: Plan) -> ValidationReservation:
        request_digest = digest({
            "operation_id": request.operation_id,
            "validation_run_id": request.validation_run_id,
            "project_id": request.project_id,
            "task_id": request.task_id,
            "definition_revision": request.definition_revision,
            "definition_digest": request.definition_digest,
            "checkpoint_id": request.checkpoint_id,
            "commit_oid": request.commit_oid,
            "tree_oid": request.tree_oid,
            "workspace_id": request.workspace_id,
            "gate_snapshot": request.gate_snapshot,
            "gate_snapshot_digest": request.gate_snapshot_digest,
            "policy_snapshot": request.policy_snapshot,
            "policy_digest": request.policy_digest,
            "gate_ids": list(request.gate_ids),
            "runtime_profile": request.runtime_profile.value,
            "allow_unconfined_network": request.allow_unconfined_network,
            "attempt_id": request.attempt_id,
            "requested_by": request.requested_by,
        })
        try:
            snapshot_data = json.loads(request.gate_snapshot)
        except (ValueError, TypeError) as exc:
            raise ConflictError("snapshot de configuração não é JSON canônico") from exc
        if canonical(snapshot_data) != request.gate_snapshot or digest(snapshot_data) != request.gate_snapshot_digest:
            raise ConflictError("digest/snapshot de configuração divergente")
        try:
            policy_data = json.loads(request.policy_snapshot)
        except (ValueError, TypeError) as exc:
            raise ConflictError("snapshot da política não é JSON canônico") from exc
        if canonical(policy_data) != request.policy_snapshot or digest(policy_data) != request.policy_digest:
            raise ConflictError("digest/snapshot de política divergente")
        network_policy = policy_data.get("network")
        if (policy_data.get("version") != 1
                or policy_data.get("runtime_profile") != request.runtime_profile.value
                or not isinstance(network_policy, dict)
                or network_policy.get("allow_unconfined") is not request.allow_unconfined_network):
            raise ConflictError("política não corresponde ao runtime/rede autorizados")
        with self.transaction() as db:
            existing = db.execute("SELECT request_digest,status FROM validation_operations WHERE operation_id=?",
                                  (request.operation_id,)).fetchone()
            if existing:
                if existing[0] != request_digest:
                    raise ConflictError("operation_id de validação reutilizado com parâmetros divergentes")
                run = db.execute("SELECT validation_run_id,status FROM validation_runs WHERE operation_id=?",
                                 (request.operation_id,)).fetchone()
                if run is None:
                    raise ConflictError("operação de validação sem run persistido")
                return ValidationReservation(run[0], request.operation_id, run[1], request_digest)

            task = db.execute("SELECT t.definition_revision,t.revalidation_required,d.definition_digest,d.definition_snapshot,d.spec_digest FROM tasks t JOIN task_definitions d ON (t.project_id,t.task_id,t.definition_revision)=(d.project_id,d.task_id,d.definition_revision) WHERE t.project_id=? AND t.task_id=?",
                              (request.project_id, request.task_id)).fetchone()
            if (task is None or task[0] != request.definition_revision or task[1]
                    or task[2] != request.definition_digest or task[4] != plan.spec_digest
                    or plan.specification.project_id != request.project_id):
                raise ConflictError("revisão da tarefa está ausente, alterada ou requer revalidação")
            if workspace.workspace_id != request.workspace_id or (
                    workspace.project_id, workspace.task_id, workspace.definition_revision,
                    workspace.status) != (request.project_id, request.task_id,
                                          request.definition_revision, "active"):
                raise ConflictError("workspace não corresponde à revisão solicitada")
            task_snapshot = json.loads(task[3])["task"]
            planned_gates = {item["id"]: item for item in task_snapshot.get("gates", [])}
            plan_task = next((item for item in plan.tasks if item.id == request.task_id), None)
            if (plan_task is None or digest({"task": task_data(plan_task),
                                             "spec_digest": plan.spec_digest}) != task[2]):
                raise ConflictError("plano carregado diverge da definição persistida")
            selected = set(request.gate_ids)
            if not selected or not selected <= planned_gates.keys():
                raise ConflictError("seleção contém gate desconhecido ou vazia")
            missing_deps = {dep for gate_id in selected
                            for dep in planned_gates[gate_id].get("depends_on", [])} - selected
            if missing_deps:
                raise ConflictError("seleção explícita deve incluir dependências: " + ", ".join(sorted(missing_deps)))
            plan_gates = {gate.gate_id: gate for gate in plan_task.gates}
            command_ids = {plan_gates[gate_id].command_id for gate_id in selected}
            allowed_commands = [command for command in plan.specification.validation_commands
                                if command.command_id in command_ids]
            if command_ids - {command.command_id for command in allowed_commands}:
                raise ConflictError("plano seleciona comando ausente da allowlist")
            expected_snapshot = {
                "version": 1,
                "gates": [gate_data(plan_gates[gate_id]) for gate_id in
                          sorted(selected, key=lambda gate_id: plan.gate_order.index(gate_id))],
                "commands": [command_data(command) for command in sorted(
                    allowed_commands, key=lambda item: (item.command_id, item.platform))],
                "criteria": [{"id": item.id, "description": item.description}
                             for item in sorted(plan_task.acceptance_criteria,
                                                key=lambda criterion: criterion.id)
                             if any(item.id in plan_gates[gate_id].criterion_ids
                                    for gate_id in selected)],
            }
            if snapshot_data != expected_snapshot:
                raise ConflictError("snapshot de configuração não corresponde ao plano/allowlist")
            checkpoint_row = db.execute("SELECT checkpoint_id,workspace_id,project_id,task_id,definition_revision,commit_oid,tree_oid,verified FROM git_checkpoints WHERE operation_id=?",
                                        (checkpoint.operation_id,)).fetchone()
            if (checkpoint_row is None or checkpoint_row[0] != request.checkpoint_id
                    or checkpoint_row[1] != request.workspace_id
                    or checkpoint_row[2] != request.project_id
                    or checkpoint_row[3] != request.task_id
                    or checkpoint_row[4] != request.definition_revision
                    or checkpoint_row[5] != request.commit_oid
                    or checkpoint_row[6] != request.tree_oid or checkpoint_row[7] != 1
                    or (checkpoint.checkpoint_id, checkpoint.commit_oid, checkpoint.tree_oid,
                        checkpoint.verified) != (request.checkpoint_id, request.commit_oid,
                                                 request.tree_oid, True)):
                raise ConflictError("checkpoint não está verificado ou diverge da solicitação")
            persisted_workspace = db.execute("SELECT current_head_oid,status FROM workspaces WHERE workspace_id=?",
                                             (request.workspace_id,)).fetchone()
            if persisted_workspace != (request.commit_oid, "active"):
                raise ConflictError("workspace não está na revisão do checkpoint")
            if request.attempt_id:
                attempt = db.execute("SELECT project_id,task_id,definition_revision,status,validation_status FROM execution_attempts WHERE attempt_id=?",
                                     (request.attempt_id,)).fetchone()
                if (attempt is None or attempt[:3] != (request.project_id, request.task_id,
                                                       request.definition_revision)
                        or attempt[3] not in ("process_succeeded", "process_failed", "timed_out", "cancelled", "interrupted")):
                    raise ConflictError("tentativa de origem não corresponde ou não foi resolvida")
            active = db.execute("SELECT validation_run_id FROM validation_runs WHERE project_id=? AND (status IN ('requested','preflight','running') OR unresolved=1) LIMIT 1",
                                 (request.project_id,)).fetchone()
            if active:
                raise ConflictError("já existe validação ativa ou não reconciliada para o projeto")
            duplicate_run = db.execute("SELECT operation_id FROM validation_runs WHERE validation_run_id=?",
                                       (request.validation_run_id,)).fetchone()
            if duplicate_run:
                raise ConflictError("validation_run_id já utilizado")
            run_id = request.validation_run_id
            db.execute("INSERT INTO validation_operations(operation_id,request_digest,operation_kind,status,owner_token,request_snapshot,result_snapshot,created_at,updated_at) VALUES(?,?,?,'prepared',?,?,?, ?,?)",
                       (request.operation_id, request_digest, "validate", "", canonical(snapshot_data), "",
                        now(), now()))
            db.execute("INSERT INTO validation_runs(validation_run_id,operation_id,request_digest,project_id,task_id,definition_revision,definition_digest,attempt_id,checkpoint_operation_id,workspace_id,commit_oid,tree_oid,configuration_snapshot,configuration_digest,policy_snapshot,policy_digest,runtime_profile,status,unresolved,requested_by,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'requested',0,?,'',?)",
                       (run_id, request.operation_id, request_digest, request.project_id,
                        request.task_id, request.definition_revision, request.definition_digest,
                        request.attempt_id or None, checkpoint.operation_id, request.workspace_id,
                        request.commit_oid, request.tree_oid, request.gate_snapshot,
                        request.gate_snapshot_digest, request.policy_snapshot, request.policy_digest,
                        request.runtime_profile.value, request.requested_by, now()))
            self._validation_event(db, request.operation_id, run_id, "intent", {
                "task_id": request.task_id, "revision": request.definition_revision,
                "checkpoint_id": request.checkpoint_id, "gate_ids": sorted(selected),
            })
            return ValidationReservation(run_id, request.operation_id, "requested",
                                         request_digest, True)

    def record_snapshot(self, run_id: str, snapshot: ValidationSnapshot) -> None:
        with self.transaction() as db:
            run = db.execute("SELECT operation_id,commit_oid,tree_oid,status FROM validation_runs WHERE validation_run_id=?",
                             (run_id,)).fetchone()
            if run is None or run[1:3] != (snapshot.commit_oid, snapshot.tree_oid):
                raise ConflictError("snapshot diverge do run/checkpoint reservado")
            existing = db.execute("SELECT snapshot_id,snapshot_digest,storage_path,size_bytes,state FROM validation_snapshots WHERE validation_run_id=?",
                                  (run_id,)).fetchone()
            if existing:
                if (existing[0], existing[1], existing[2], existing[3]) != (
                        snapshot.snapshot_id, snapshot.snapshot_digest, str(snapshot.path),
                        snapshot.size_bytes):
                    raise ConflictError("snapshot diferente já registrado para este run")
                return
            if run[3] not in ("requested", "preflight"):
                raise TransitionError("snapshot só pode ser registrado antes da execução")
            db.execute("INSERT INTO validation_snapshots(snapshot_id,validation_run_id,source_commit_oid,source_tree_oid,snapshot_digest,manifest_json,storage_path,size_bytes,state,created_at) VALUES(?,?,?,?,?,?,?,?, 'verified',?)",
                       (snapshot.snapshot_id, run_id, snapshot.commit_oid, snapshot.tree_oid,
                        snapshot.snapshot_digest, snapshot.manifest_json, str(snapshot.path),
                        snapshot.size_bytes, now()))
            self._validation_event(db, run[0], run_id, "snapshot_verified", {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_digest": snapshot.snapshot_digest,
                "size_bytes": snapshot.size_bytes,
            })

    def prepare_gate_results(self, request: ValidationRequest, gates: tuple[GateDefinition, ...],
                             validators, validator_version: str = "",
                             validator_digest: str = "", snapshot_digest: str = "") -> None:
        with self.transaction() as db:
            run = db.execute("SELECT operation_id,project_id,task_id,definition_revision,commit_oid,tree_oid,status FROM validation_runs WHERE validation_run_id=?",
                             (request.validation_run_id,)).fetchone()
            if run is None or run[:6] != (request.operation_id, request.project_id,
                                          request.task_id, request.definition_revision,
                                          request.commit_oid, request.tree_oid):
                raise ConflictError("run difere da preparação de gates")
            snapshot = db.execute("SELECT snapshot_digest,state FROM validation_snapshots WHERE validation_run_id=?",
                                  (request.validation_run_id,)).fetchone()
            if snapshot != (snapshot_digest, "verified"):
                raise ConflictError("snapshot não está materializado e verificado")
            existing = db.execute("SELECT gate_id,gate_definition_digest,validator_id,validator_version,validator_digest,snapshot_digest FROM validation_gate_results WHERE validation_run_id=? ORDER BY gate_id",
                                  (request.validation_run_id,)).fetchall()
            if existing:
                by_gate = (validators if isinstance(validators, dict) else
                           {gate.gate_id: validators for gate in gates})
                expected = sorted((gate.gate_id, digest({"id": gate.gate_id, "version": gate.version,
                                                        "criterion_ids": list(gate.criterion_ids),
                                                        "command_id": gate.command_id,
                                                        "depends_on": list(gate.depends_on),
                                                        "required": gate.required,
                                                        "timeout_seconds": gate.timeout_seconds,
                                                        "output_limit_bytes": gate.output_limit_bytes}),
                                   by_gate[gate.gate_id].validator_id,
                                   by_gate[gate.gate_id].version,
                                   by_gate[gate.gate_id].digest,
                                   snapshot_digest) for gate in gates)
                if existing != expected:
                    raise ConflictError("gate results já preparados com proveniência divergente")
                return
            if set(gate.gate_id for gate in gates) != set(request.gate_ids):
                raise ConflictError("definições de gates não correspondem à seleção explícita")
            for gate in gates:
                validator = (validators[gate.gate_id] if isinstance(validators, dict)
                             else validators)
                gate_hash = digest({"id": gate.gate_id, "version": gate.version,
                                    "criterion_ids": list(gate.criterion_ids),
                                    "command_id": gate.command_id,
                                    "depends_on": list(gate.depends_on),
                                    "required": gate.required,
                                    "timeout_seconds": gate.timeout_seconds,
                                    "output_limit_bytes": gate.output_limit_bytes})
                db.execute("INSERT INTO validation_gate_results(gate_result_id,validation_run_id,gate_id,gate_version,gate_definition_digest,criterion_ids_json,required,validator_id,validator_version,validator_digest,snapshot_digest,status,capabilities_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued','[]')",
                           ("gr-" + uuid4().hex, request.validation_run_id, gate.gate_id,
                            gate.version, gate_hash, json.dumps(gate.criterion_ids),
                            int(gate.required), validator.validator_id,
                            validator.version, validator.digest, snapshot_digest))
            self._validation_event(db, request.operation_id, request.validation_run_id,
                                   "gates_queued", {"gate_ids": list(request.gate_ids)})

    def transition_gate(self, run_id: str, gate_id: str, status: GateStatus, *,
                        expected: GateStatus, exit_code: int | None = None,
                        reason: str = "", capabilities_json: str = "[]",
                        stdout_digest: str = "", stderr_digest: str = "",
                        stdout_bytes: int = 0, stderr_bytes: int = 0,
                        truncated: bool = False, process_identity: str = "") -> None:
        with self.transaction() as db:
            row = db.execute("SELECT r.operation_id,g.status FROM validation_runs r JOIN validation_gate_results g USING(validation_run_id) WHERE r.validation_run_id=? AND g.gate_id=?",
                             (run_id, gate_id)).fetchone()
            if row is None or row[1] != expected.value:
                raise TransitionError("estado atual do gate diverge da transição solicitada")
            timestamp = now()
            started = timestamp if status is GateStatus.RUNNING else None
            finished = timestamp if status in {GateStatus.PASSED, GateStatus.FAILED,
                                                GateStatus.INCONCLUSIVE, GateStatus.CANCELLED,
                                                GateStatus.NOT_RUN} else None
            cursor = db.execute("UPDATE validation_gate_results SET status=?,exit_code=?,reason=?,capabilities_json=?,process_identity=COALESCE(NULLIF(?,''),process_identity),stdout_digest=?,stderr_digest=?,stdout_bytes=?,stderr_bytes=?,truncated=?,started_at=COALESCE(started_at,?),finished_at=COALESCE(?,finished_at) WHERE validation_run_id=? AND gate_id=? AND status=?",
                                (status.value, exit_code, reason[:512], capabilities_json,
                                 process_identity, stdout_digest, stderr_digest, stdout_bytes, stderr_bytes,
                                 int(truncated), started, finished, run_id, gate_id,
                                 expected.value))
            if cursor.rowcount != 1:
                raise TransitionError("concorrência durante transição de gate")
            self._validation_event(db, row[0], run_id, "gate_" + status.value,
                                  {"gate_id": gate_id, "from": expected.value,
                                   "reason": reason[:256]})

    def record_gate_spawn_intent(self, run_id: str, gate_id: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT r.operation_id,g.status FROM validation_runs r JOIN validation_gate_results g USING(validation_run_id) WHERE r.validation_run_id=? AND g.gate_id=?",
                             (run_id, gate_id)).fetchone()
            if row is None or row[1] != GateStatus.PREFLIGHT.value:
                raise TransitionError("gate precisa estar em preflight antes da intenção de spawn")
            self._validation_event(db, row[0], run_id, "gate_spawn_intent",
                                   {"gate_id": gate_id})

    def record_gate_probe_identity(self, run_id: str, gate_id: str,
                                   process_identity: str) -> None:
        if not process_identity or len(process_identity.encode("utf-8")) > 1024:
            raise ConflictError("identidade de version probe inválida")
        with self.transaction() as db:
            row = db.execute("SELECT r.operation_id,g.status FROM validation_runs r JOIN validation_gate_results g USING(validation_run_id) WHERE r.validation_run_id=? AND g.gate_id=?",
                             (run_id, gate_id)).fetchone()
            if row is None or row[1] != GateStatus.PREFLIGHT.value:
                raise TransitionError("version probe deve ocorrer durante preflight")
            self._validation_event(db, row[0], run_id, "validator_version_probe",
                                   {"gate_id": gate_id, "process_identity": process_identity})

    def transition_run(self, run_id: str, status: RunStatus, *, expected: RunStatus,
                       reason: str = "", unresolved: bool = False) -> None:
        allowed = {
            RunStatus.REQUESTED: {RunStatus.PREFLIGHT, RunStatus.FAILED,
                                  RunStatus.INCONCLUSIVE, RunStatus.CANCELLED},
            RunStatus.PREFLIGHT: {RunStatus.RUNNING, RunStatus.FAILED,
                                  RunStatus.INCONCLUSIVE, RunStatus.CANCELLED},
            RunStatus.RUNNING: {RunStatus.PASSED, RunStatus.FAILED,
                                RunStatus.INCONCLUSIVE, RunStatus.CANCELLED},
        }
        if status not in allowed.get(expected, set()):
            raise TransitionError(f"transição inválida de run: {expected.value} -> {status.value}")
        with self.transaction() as db:
            row = db.execute("SELECT operation_id,status FROM validation_runs WHERE validation_run_id=?",
                             (run_id,)).fetchone()
            if row is None or row[1] != expected.value:
                raise TransitionError("estado atual do run diverge da transição solicitada")
            if status is RunStatus.PASSED:
                failed = db.execute("SELECT 1 FROM validation_gate_results WHERE validation_run_id=? AND required=1 AND status!='passed' LIMIT 1",
                                    (run_id,)).fetchone()
                total_gates = db.execute("SELECT count(*) FROM validation_gate_results WHERE validation_run_id=?",
                                         (run_id,)).fetchone()[0]
                snapshot = db.execute("SELECT state,snapshot_digest,source_commit_oid,source_tree_oid FROM validation_snapshots WHERE validation_run_id=?",
                                      (run_id,)).fetchone()
                provenance = db.execute("SELECT commit_oid,tree_oid,checkpoint_operation_id FROM validation_runs WHERE validation_run_id=?",
                                        (run_id,)).fetchone()
                checkpoint = db.execute("SELECT commit_oid,tree_oid,verified FROM git_checkpoints WHERE operation_id=?",
                                        (provenance[2],)).fetchone() if provenance else None
                divergent_gate = db.execute("SELECT 1 FROM validation_gate_results WHERE validation_run_id=? AND snapshot_digest!=? LIMIT 1",
                                            (run_id, snapshot[1] if snapshot else "")).fetchone()
                if (failed or total_gates == 0 or snapshot is None or snapshot[0] != "verified"
                        or provenance is None or snapshot[2:] != provenance[:2]
                        or checkpoint != (provenance[0], provenance[1], 1) or divergent_gate):
                    raise TransitionError("run não pode passar sem todos os gates obrigatórios e snapshot verificado")
            timestamp = now()
            cursor = db.execute("UPDATE validation_runs SET status=?,unresolved=?,reason=?,started_at=COALESCE(started_at,?),finished_at=CASE WHEN ? IN ('passed','failed','inconclusive','cancelled') THEN ? ELSE finished_at END WHERE validation_run_id=? AND status=?",
                                (status.value, int(unresolved), reason[:512],
                                 timestamp if status is RunStatus.RUNNING else None,
                                 status.value, timestamp, run_id, expected.value))
            if cursor.rowcount != 1:
                raise TransitionError("concorrência durante transição de run")
            operation_status = ("prepared" if status in {RunStatus.REQUESTED, RunStatus.PREFLIGHT}
                                else "running" if status is RunStatus.RUNNING else status.value)
            db.execute("UPDATE validation_operations SET status=?,updated_at=?,result_snapshot=? WHERE operation_id=?",
                       (operation_status, timestamp, canonical({"run_id": run_id,
                                                                 "status": status.value,
                                                                 "reason": reason[:512]}), row[0]))
            self._validation_event(db, row[0], run_id, "run_" + status.value,
                                  {"from": expected.value, "reason": reason[:256],
                                   "unresolved": unresolved})

    def gate_rows(self, run_id: str) -> tuple[tuple, ...]:
        return tuple(self.db.execute("SELECT gate_id,status,gate_definition_digest,criterion_ids_json,required,validator_id,validator_version,validator_digest,snapshot_digest,exit_code,reason,stdout_digest,stderr_digest,stdout_bytes,stderr_bytes,truncated,started_at,finished_at,process_identity FROM validation_gate_results WHERE validation_run_id=? ORDER BY gate_id",
                                     (run_id,)))

    def validation_result(self, run_id: str) -> ValidationResult:
        run = self.db.execute("SELECT operation_id,status,request_digest,reason,definition_revision,checkpoint_operation_id,commit_oid,tree_oid FROM validation_runs WHERE validation_run_id=?",
                              (run_id,)).fetchone()
        if run is None:
            raise TransitionError("run de validação desconhecido")
        checkpoint = self.db.execute("SELECT checkpoint_id FROM git_checkpoints WHERE operation_id=?",
                                     (run[5],)).fetchone()
        snapshot = self.db.execute("SELECT snapshot_digest FROM validation_snapshots WHERE validation_run_id=?",
                                  (run_id,)).fetchone()
        results = []
        for row in self.db.execute("SELECT gate_id,status,gate_definition_digest,criterion_ids_json,validator_id,validator_version,validator_digest,snapshot_digest,exit_code,reason,stdout_digest,stderr_digest,stdout_bytes,stderr_bytes,truncated,capabilities_json,started_at,finished_at FROM validation_gate_results WHERE validation_run_id=? ORDER BY gate_id",
                                   (run_id,)):
            capability_items = json.loads(row[15])
            capabilities = tuple(RuntimeCapability(item["name"], CapabilityState(item["state"]),
                                                   item.get("detail", ""),
                                                   item.get("effective_value", ""))
                                 for item in capability_items)
            results.append(GateResult(run_id, row[0], GateStatus(row[1]), run[4],
                                      checkpoint[0] if checkpoint else "", run[6], run[7],
                                      row[2], row[4], row[5], row[6], row[7],
                                      row[16] or "", row[17] or "", row[8], row[9],
                                      row[10], row[11], row[12], row[13], bool(row[14]),
                                      capabilities))
        return ValidationResult(run_id, run[0], RunStatus(run[1]), tuple(results),
                                run[2], snapshot[0] if snapshot else "", run[3])

    def record_verified_gate_evidence(self, run_id: str, gate_id: str,
                                     criterion_id: str, operation_id: str,
                                     *, author: str) -> int:
        """Append criterion evidence only from a passed gate at the current checkpoint."""
        if not all((run_id, gate_id, criterion_id, operation_id, author)):
            raise ConflictError("identidade da evidência incompleta")
        with self.transaction() as db:
            existing = db.execute(
                "SELECT evidence_id,payload FROM evidence WHERE operation_id=?",
                (operation_id,)).fetchone()
            run = db.execute(
                "SELECT project_id,task_id,definition_revision,definition_digest,"
                "checkpoint_operation_id,commit_oid,tree_oid,status FROM validation_runs "
                "WHERE validation_run_id=?", (run_id,)).fetchone()
            gate = db.execute(
                "SELECT status,criterion_ids_json,gate_definition_digest,validator_id,"
                "validator_version,validator_digest,snapshot_digest FROM validation_gate_results "
                "WHERE validation_run_id=? AND gate_id=?", (run_id, gate_id)).fetchone()
            snapshot = db.execute(
                "SELECT source_commit_oid,source_tree_oid,snapshot_digest,state "
                "FROM validation_snapshots WHERE validation_run_id=?", (run_id,)).fetchone()
            if run is None or gate is None or snapshot is None:
                raise ConflictError("run/gate/snapshot não encontrado")
            criteria = json.loads(gate[1])
            provenance = {
                "kind": "verified", "validation_run_id": run_id, "gate_id": gate_id,
                "criterion_id": criterion_id, "definition_revision": run[2],
                "definition_digest": run[3], "checkpoint_id": db.execute(
                    "SELECT checkpoint_id FROM git_checkpoints WHERE operation_id=?",
                    (run[4],)).fetchone()[0], "commit_oid": run[5], "tree_oid": run[6],
                "gate_definition_digest": gate[2], "validator_id": gate[3],
                "validator_version": gate[4], "validator_digest": gate[5],
                "snapshot_digest": gate[6], "author": author,
            }
            encoded = canonical(provenance)
            if existing:
                if existing[1] != encoded:
                    raise ConflictError("operation_id de evidência reutilizado divergentemente")
                return int(existing[0])
            task = db.execute(
                "SELECT definition_revision,revalidation_required FROM tasks "
                "WHERE project_id=? AND task_id=?", run[:2]).fetchone()
            checkpoint = db.execute(
                "SELECT verified,commit_oid,tree_oid FROM git_checkpoints WHERE operation_id=?",
                (run[4],)).fetchone()
            if (run[7] != "passed" or gate[0] != "passed" or snapshot[3] != "verified"
                    or snapshot[:3] != (run[5], run[6], gate[6])
                    or task != (run[2], 0) or checkpoint != (1, run[5], run[6])
                    or criterion_id not in criteria):
                raise ConflictError("evidência não é vigente/verificada para este critério")
            cursor = db.execute(
                "INSERT INTO evidence(project_id,task_id,definition_revision,criterion_id,kind,"
                "payload,source,declaration_id,method,author,passed,recorded_at,operation_id) "
                "VALUES(?,?,?,?, 'verified', ?, ?, NULL, ?, ?, 1, ?, ?)",
                (*run[:3], criterion_id, encoded, f"validation:{run_id}:{gate_id}",
                 "independent_quality_gate", author, now(), operation_id))
            self._validation_event(db, db.execute(
                "SELECT operation_id FROM validation_runs WHERE validation_run_id=?",
                (run_id,)).fetchone()[0], run_id, "criterion_evidence_verified",
                {"gate_id": gate_id, "criterion_id": criterion_id,
                 "evidence_operation_id": operation_id})
            return int(cursor.lastrowid)

    def recover_interrupted_validation(self, run_id: str, *, process_stopped: bool | None,
                                       reason: str) -> str:
        """Close interrupted states conservatively; never resubmit a gate."""
        with self.transaction() as db:
            run = db.execute("SELECT operation_id,status,unresolved FROM validation_runs WHERE validation_run_id=?",
                             (run_id,)).fetchone()
            if run is None:
                raise TransitionError("run de validação desconhecido")
            if run[1] in {"passed", "failed", "inconclusive", "cancelled"}:
                if run[2] and process_stopped is True:
                    db.execute("UPDATE validation_runs SET unresolved=0,reason=? WHERE validation_run_id=?",
                               (reason[:512], run_id))
                    self._validation_event(db, run[0], run_id, "orphan_reconciled_stopped",
                                           {"reason": reason[:256]})
                elif run[2] and process_stopped is False:
                    self._validation_event(db, run[0], run_id, "orphan_still_running",
                                           {"reason": reason[:256]})
                return run[1]
            unresolved = process_stopped is not True
            rows = db.execute("SELECT gate_id,status FROM validation_gate_results WHERE validation_run_id=?",
                              (run_id,)).fetchall()
            for gate_id, gate_status in rows:
                if gate_status == "running":
                    db.execute("UPDATE validation_gate_results SET status='inconclusive',reason=?,finished_at=? WHERE validation_run_id=? AND gate_id=?",
                               ("interrupção durante execução; resultado desconhecido", now(), run_id, gate_id))
                    self._validation_event(db, run[0], run_id, "gate_recovered_inconclusive",
                                           {"gate_id": gate_id})
                elif gate_status in ("queued", "preflight"):
                    target = "not_run" if gate_status == "queued" else "inconclusive"
                    db.execute("UPDATE validation_gate_results SET status=?,reason=?,finished_at=? WHERE validation_run_id=? AND gate_id=?",
                               (target, "interrupção antes de resultado verificável", now(), run_id, gate_id))
                    self._validation_event(db, run[0], run_id, "gate_recovered_" + target,
                                           {"gate_id": gate_id})
            timestamp = now()
            db.execute("UPDATE validation_runs SET status='inconclusive',unresolved=?,reason=?,finished_at=? WHERE validation_run_id=?",
                       (int(unresolved), "execução interrompida; nenhum resultado presumido", timestamp, run_id))
            db.execute("UPDATE validation_operations SET status='inconclusive',updated_at=?,result_snapshot=? WHERE operation_id=?",
                       (timestamp, canonical({"run_id": run_id, "status": "inconclusive",
                                              "unresolved": unresolved}), run[0]))
            self._validation_event(db, run[0], run_id, "run_recovered_inconclusive",
                                   {"unresolved": unresolved, "reason": reason[:256]})
            return "inconclusive"

    @staticmethod
    def _validation_event(db: sqlite3.Connection, operation_id: str, run_id: str,
                          kind: str, payload: dict) -> None:
        encoded = canonical(payload)
        if len(encoded.encode("utf-8")) > 4096:
            raise ConflictError("evento de validação excede 4 KiB")
        db.execute("INSERT INTO validation_events(operation_id,validation_run_id,kind,payload,occurred_at) VALUES(?,?,?,?,?)",
                   (operation_id, run_id, kind, encoded, now()))
