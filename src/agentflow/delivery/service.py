"""Local artifact verification and explicitly authorized delivery boundary."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from agentflow.planning import canonical, digest
from agentflow.state.contracts import ConflictError
from agentflow.state.sqlite import now
from agentflow.validation.contracts import BuildStatus

from .contracts import (ArtifactReference, ArtifactStatus, ArtifactBuilder,
                        BuildRequest, BuildResult, DeliveryAuthorization,
                        DeliveryProvider, DeliveryRequest, DeliveryResult,
                        DeliveryStatus)


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ARTIFACT_LIMIT = 256 * 1024**2


def _has_reparse(path: Path) -> bool:
    candidate = Path(path).absolute()
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return True
            if current.exists() and os.name == "nt":
                import stat
                if current.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return True
        except OSError:
            return True
    return False


class ArtifactDeliveryService:
    """Build/verify immutable single-file artifacts and gate all delivery effects."""

    def __init__(self, store, artifact_root: Path, *, delivery_enabled: bool = False,
                 allowed_destinations: tuple[str, ...] = (),
                 snapshot_verifier=None):
        self.store = store
        self.artifact_root = Path(artifact_root).absolute()
        if _has_reparse(self.artifact_root):
            raise ConflictError("artifact root contém symlink/reparse point")
        self.delivery_enabled = delivery_enabled
        self.allowed_destinations = frozenset(allowed_destinations)
        self.snapshot_verifier = snapshot_verifier or (lambda _run, _path, _digest: False)

    def build(self, request: BuildRequest, builder: ArtifactBuilder,
              snapshot_path: Path) -> BuildResult:
        if not SAFE_ID.fullmatch(request.operation_id) or not SAFE_ID.fullmatch(
                request.output_directory_id):
            raise ConflictError("ID de build/output inválido")
        output = self.artifact_root / request.output_directory_id
        if _has_reparse(snapshot_path):
            raise ConflictError("snapshot de build contém symlink/reparse point")
        snapshot = snapshot_path.resolve(strict=True)
        if not snapshot.is_dir():
            raise ConflictError("snapshot de build inválido")
        request = replace(request, source_run_id=request.source_run_id or "",
                          snapshot_path=snapshot, output_path=output)
        request_value = {key: str(value) if isinstance(value, Path) else value
                         for key, value in asdict(request).items()}
        request_digest = digest(request_value)
        with self.store.transaction() as db:
            existing = db.execute(
                "SELECT request_digest,status,result_snapshot FROM validation_operations "
                "WHERE operation_id=?", (request.operation_id,)).fetchone()
            if existing:
                if existing[0] != request_digest:
                    raise ConflictError("operation_id de build reutilizado divergentemente")
                if existing[1] in {"passed", "failed", "inconclusive"} and existing[2]:
                    return self._build_result(json.loads(existing[2]))
                raise ConflictError("build anterior não reconciliado; sem repetição automática")
            self._check_build_source(db, request)
            db.execute("INSERT INTO validation_operations(operation_id,request_digest,operation_kind,status,owner_token,request_snapshot,result_snapshot,created_at,updated_at) VALUES(?,?,'build','prepared','',?,'',?,?)",
                       (request.operation_id, request_digest, canonical(request_value), now(), now()))
        self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _has_reparse(self.artifact_root):
            self._finish_build(request.operation_id, request_digest,
                               BuildResult(request.operation_id, BuildStatus.INCONCLUSIVE,
                                           None, request.builder_id, request.builder_version,
                                           request.builder_digest, request.configuration_digest,
                                           request.dependency_digest,
                                           "artifact root changed to symlink/reparse point"),
                               "inconclusive")
            raise ConflictError("artifact root changed to symlink/reparse point")
        if output.exists() or output.is_symlink():
            self._finish_build(request.operation_id, request_digest,
                               BuildResult(request.operation_id, BuildStatus.INCONCLUSIVE,
                                           None, request.builder_id, request.builder_version,
                                           request.builder_digest, request.configuration_digest,
                                           request.dependency_digest,
                                           "diretório já existe; preservado"), "inconclusive")
            raise ConflictError("diretório de artefatos já existe; preservado")
        result: BuildResult
        try:
            output.mkdir(mode=0o700, parents=True, exist_ok=False)
            if not self.snapshot_verifier(request.source_run_id, snapshot,
                                          request.snapshot_digest):
                raise ConflictError("snapshot de origem não foi verificado antes do build")
            result = builder.build(request)
            if (result.operation_id != request.operation_id
                    or (result.builder_id, result.builder_version, result.builder_digest,
                        result.configuration_digest, result.dependency_digest)
                    != (request.builder_id, request.builder_version, request.builder_digest,
                        request.configuration_digest, request.dependency_digest)):
                raise ConflictError("proveniência do builder diverge do pedido")
            if result.status is BuildStatus.PASSED:
                artifact = self._verify_artifact(request, result.artifact, output)
                if not self.snapshot_verifier(request.source_run_id, snapshot,
                                              request.snapshot_digest):
                    raise ConflictError("snapshot de origem mudou durante o build")
                result = replace(result, artifact=artifact)
                self._persist_artifact(request, result, request_digest, output)
            else:
                self._finish_build(request.operation_id, request_digest, result,
                                   "failed" if result.status is BuildStatus.FAILED
                                   else "inconclusive")
            return result
        except BaseException as exc:
            unknown = BuildResult(request.operation_id, BuildStatus.INCONCLUSIVE, None,
                                  request.builder_id, request.builder_version,
                                  request.builder_digest, request.configuration_digest,
                                  request.dependency_digest,
                                  f"build não reconciliado: {type(exc).__name__}")
            self._finish_build(request.operation_id, request_digest, unknown, "inconclusive")
            raise

    def _check_build_source(self, db, request: BuildRequest) -> None:
        if not request.source_run_id:
            raise ConflictError("build exige validation_run_id de origem")
        row = db.execute(
            "SELECT project_id,task_id,definition_revision,checkpoint_operation_id,"
            "commit_oid,tree_oid,status FROM validation_runs WHERE validation_run_id=?",
            (request.source_run_id,)).fetchone()
        snapshot = db.execute(
            "SELECT snapshot_digest,state FROM validation_snapshots WHERE validation_run_id=?",
            (request.source_run_id,)).fetchone()
        task = db.execute("SELECT definition_revision,revalidation_required FROM tasks "
                          "WHERE project_id=? AND task_id=?", (request.project_id,
                                                               request.task_id)).fetchone()
        checkpoint = db.execute("SELECT checkpoint_id,verified,commit_oid,tree_oid FROM git_checkpoints WHERE operation_id=?",
                                (row[3],)).fetchone() if row else None
        if (row is None or snapshot is None or task != (request.definition_revision, 0)
                or (row[0], row[1], row[2], row[4], row[5], row[6]) !=
                (request.project_id, request.task_id, request.definition_revision,
                 request.source_commit_oid, request.source_tree_oid, "passed")
                or snapshot != (request.snapshot_digest, "verified")
                or checkpoint != (request.checkpoint_id, 1,
                                  request.source_commit_oid, request.source_tree_oid)):
            raise ConflictError("build não corresponde a validação/checkpoint vigente")

    def _verify_artifact(self, request: BuildRequest,
                         artifact: ArtifactReference | None,
                         output: Path) -> ArtifactReference:
        if artifact is None or not SAFE_ID.fullmatch(artifact.artifact_id):
            raise ConflictError("builder não devolveu artefato identificável")
        target = output / artifact.artifact_id
        if (_has_reparse(target) or not target.is_file()
                or target.resolve().parent != output.resolve()):
            raise ConflictError("artefato fora do diretório autorizado ou não regular")
        observed = []
        for child in output.rglob("*"):
            if child.is_symlink():
                raise ConflictError("build produziu symlink no diretório de artefatos")
            if child.is_file():
                observed.append(child.resolve())
            elif not child.is_dir():
                raise ConflictError("build produziu arquivo especial")
        if observed != [target.resolve()]:
            raise ConflictError("build deve produzir exatamente um arquivo de artefato")
        size = target.stat().st_size
        if not 0 <= size <= ARTIFACT_LIMIT:
            raise ConflictError("artefato excede o limite de 256 MiB")
        hasher = hashlib.sha256()
        with target.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        actual = "sha256:" + hasher.hexdigest()
        if (artifact.sha256 != actual or artifact.size_bytes != size
                or (artifact.source_commit_oid, artifact.source_tree_oid,
                    artifact.snapshot_digest)
                != (request.source_commit_oid, request.source_tree_oid,
                    request.snapshot_digest)):
            raise ConflictError("digest, tamanho ou proveniência do artefato diverge")
        return artifact

    def _persist_artifact(self, request: BuildRequest, result: BuildResult,
                          request_digest: str, output: Path) -> None:
        artifact = result.artifact
        assert artifact is not None
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        with self.store.transaction() as db:
            run = db.execute("SELECT checkpoint_operation_id FROM validation_runs WHERE validation_run_id=?",
                             (request.source_run_id,)).fetchone()
            project_artifacts = db.execute(
                "SELECT COALESCE(sum(size_bytes),0) FROM validation_artifacts "
                "WHERE project_id=? AND status='verified'", (request.project_id,)).fetchone()[0]
            run_artifacts = db.execute(
                "SELECT COALESCE(sum(size_bytes),0) FROM validation_artifacts "
                "WHERE source_run_id=? AND status='verified'", (request.source_run_id,)).fetchone()[0]
            project_logs = db.execute(
                "SELECT COALESCE(sum(g.stdout_bytes+g.stderr_bytes),0) FROM validation_gate_results g "
                "JOIN validation_runs r USING(validation_run_id) WHERE r.project_id=? AND g.logs_retained=1",
                (request.project_id,)).fetchone()[0]
            project_snapshots = db.execute(
                "SELECT COALESCE(sum(s.size_bytes),0) FROM validation_snapshots s "
                "JOIN validation_runs r USING(validation_run_id) WHERE r.project_id=? "
                "AND s.state IN ('verified','quarantined')", (request.project_id,)).fetchone()[0]
            if (project_artifacts + artifact.size_bytes > 10 * 1024**3
                    or run_artifacts + artifact.size_bytes > 1024**3
                    or project_logs + project_snapshots + project_artifacts + artifact.size_bytes
                    > 20 * 1024**3):
                raise ConflictError("quota de artifacts/run/projeto/storage total excedida")
            db.execute("INSERT INTO validation_artifacts(artifact_id,operation_id,source_run_id,project_id,task_id,definition_revision,checkpoint_operation_id,source_commit_oid,source_tree_oid,snapshot_digest,builder_id,builder_version,builder_digest,configuration_digest,dependency_digest,artifact_digest,size_bytes,storage_path,status,pinned,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'verified',0,?,?)",
                       (artifact.artifact_id, request.operation_id, request.source_run_id,
                        request.project_id, request.task_id, request.definition_revision,
                        run[0], request.source_commit_oid, request.source_tree_oid,
                        request.snapshot_digest, request.builder_id, request.builder_version,
                        request.builder_digest, request.configuration_digest,
                        request.dependency_digest, artifact.sha256, artifact.size_bytes,
                        str(output / artifact.artifact_id), now(), expires))
            snapshot = canonical(asdict(result))
            db.execute("UPDATE validation_operations SET status='passed',result_snapshot=?,updated_at=? WHERE operation_id=? AND request_digest=?",
                       (snapshot, now(), request.operation_id, request_digest))
            db.execute("INSERT INTO validation_events(operation_id,validation_run_id,kind,payload,occurred_at) VALUES(?,?,'artifact_verified',?,?)",
                       (request.operation_id, request.source_run_id,
                        canonical({"artifact_id": artifact.artifact_id,
                                   "sha256": artifact.sha256,
                                   "size_bytes": artifact.size_bytes}), now()))

    def _finish_build(self, operation_id: str, request_digest: str,
                      result: BuildResult, status: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE validation_operations SET status=?,result_snapshot=?,updated_at=? WHERE operation_id=? AND request_digest=?",
                       (status, canonical(asdict(result)), now(), operation_id,
                        request_digest))

    def authorize_delivery(self, operation_id: str, project_id: str,
                           artifact_id: str, destination_id: str,
                           *, authorized_by: str,
                           credential_reference_ids: tuple[str, ...] = ()) -> DeliveryAuthorization:
        if not self.delivery_enabled:
            raise ConflictError("delivery está desabilitada por configuração")
        if (not SAFE_ID.fullmatch(operation_id) or not SAFE_ID.fullmatch(artifact_id)
                or destination_id not in self.allowed_destinations or not authorized_by
                or len(set(credential_reference_ids)) != len(credential_reference_ids)):
            raise ConflictError("destino não autorizado ou autor ausente")
        row = self.store.db.execute(
            "SELECT artifact_digest,status,size_bytes,source_commit_oid,source_tree_oid,snapshot_digest,expires_at,storage_path "
            "FROM validation_artifacts WHERE artifact_id=? "
            "AND project_id=?", (artifact_id, project_id)).fetchone()
        if (row is None or row[1] != ArtifactStatus.VERIFIED.value
                or row[6] <= now()):
            raise ConflictError("artefato não está verificado para entrega")
        target = Path(row[7])
        if (_has_reparse(target) or not target.is_file()
                or target.stat().st_size != row[2]
                or self._file_digest(target) != row[0]):
            raise ConflictError("artefato falhou verificação antes da autorização")
        auth_id = "auth-" + hashlib.sha256(
            (operation_id + "\0" + destination_id + "\0" + row[0]
             + "\0" + authorized_by).encode()).hexdigest()[:32]
        auth_id = "auth-" + auth_id
        authorization = DeliveryAuthorization(auth_id, operation_id, destination_id,
                                              row[0], authorized_by, now())
        artifact = ArtifactReference(artifact_id, row[0], row[2], row[3], row[4], row[5])
        request_digest = self._delivery_digest(
            operation_id, project_id, artifact, destination_id, auth_id,
            credential_reference_ids)
        with self.store.transaction() as db:
            existing = db.execute("SELECT authorization_id,project_id,artifact_id,authorized_by,authorized_at FROM validation_delivery_authorizations WHERE operation_id=? AND destination_id=? AND artifact_digest=?",
                                  (operation_id, destination_id, row[0])).fetchone()
            if existing:
                if (existing[1], existing[2], existing[3]) != (project_id, artifact_id,
                                                                authorized_by):
                    raise ConflictError("autorização existente diverge dos parâmetros")
                operation = db.execute(
                    "SELECT request_digest,operation_kind FROM validation_operations WHERE operation_id=?",
                    (operation_id,)).fetchone()
                if operation != (request_digest, "deliver"):
                    raise ConflictError("operation_id de autorização reutilizado divergentemente")
                return DeliveryAuthorization(existing[0], operation_id, destination_id,
                                             row[0], existing[3], existing[4])
            operation = db.execute(
                "SELECT request_digest,operation_kind FROM validation_operations WHERE operation_id=?",
                (operation_id,)).fetchone()
            if operation:
                raise ConflictError("operation_id já reservado por outra operação")
            db.execute("INSERT INTO validation_operations(operation_id,request_digest,operation_kind,status,owner_token,request_snapshot,result_snapshot,created_at,updated_at) VALUES(?,?,'deliver','prepared','',?,'',?,?)",
                       (operation_id, request_digest, canonical({
                           "destination_id": destination_id, "artifact_id": artifact_id,
                           "artifact_digest": row[0], "authorization_id": auth_id,
                           "credential_reference_ids": list(credential_reference_ids)}),
                        now(), now()))
            db.execute("INSERT INTO validation_delivery_authorizations(authorization_id,operation_id,project_id,artifact_id,destination_id,artifact_digest,authorized_by,authorized_at) VALUES(?,?,?,?,?,?,?,?)",
                       (auth_id, operation_id, project_id, artifact_id, destination_id,
                        row[0], authorized_by, authorization.authorized_at))
            db.execute("UPDATE validation_artifacts SET pinned=1 WHERE artifact_id=?",
                       (artifact_id,))
        return authorization

    def deliver(self, request: DeliveryRequest,
                provider: DeliveryProvider) -> DeliveryResult:
        if not self.delivery_enabled:
            raise ConflictError("delivery está desabilitada por configuração")
        auth = request.authorization
        auth_row = self.store.db.execute(
            "SELECT authorization_id,artifact_id,project_id FROM validation_delivery_authorizations "
            "WHERE operation_id=? AND destination_id=? AND artifact_digest=?",
            (request.operation_id, request.destination_id,
             request.artifact.sha256)).fetchone()
        if auth_row is None or (auth_row[0], auth_row[1], auth_row[2]) != (
                auth.authorization_id, request.artifact.artifact_id, request.project_id):
            raise ConflictError("autorização não persistida para operação/destino/artefato exatos")
        artifact_row = self.store.db.execute(
            "SELECT artifact_digest,size_bytes,storage_path,status,expires_at FROM validation_artifacts "
            "WHERE artifact_id=? AND project_id=?", (request.artifact.artifact_id,
                                                       request.project_id)).fetchone()
        if (artifact_row is None or artifact_row[3] != "verified"
                or artifact_row[4] <= now()):
            raise ConflictError("artefato ausente, expirado ou corrompido")
        target = Path(artifact_row[2])
        if (_has_reparse(target) or not target.is_file()
                or target.stat().st_size != artifact_row[1]
                or self._file_digest(target) != artifact_row[0]
                or (artifact_row[0], artifact_row[1]) !=
                (request.artifact.sha256, request.artifact.size_bytes)):
            with self.store.transaction() as db:
                db.execute("UPDATE validation_artifacts SET status='corrupt' WHERE artifact_id=?",
                           (request.artifact.artifact_id,))
            raise ConflictError("verificação de artefato falhou; entrega bloqueada")
        request_digest = self._delivery_digest(
            request.operation_id, request.project_id, request.artifact,
            request.destination_id, auth.authorization_id,
            request.credential_reference_ids)
        with self.store.transaction() as db:
            existing = db.execute("SELECT request_digest,status,result_snapshot,operation_kind FROM validation_operations WHERE operation_id=?",
                                  (request.operation_id,)).fetchone()
            if existing is None or existing[0] != request_digest or existing[3] != "deliver":
                raise ConflictError("delivery exige autorização/reserva persistida exata")
            if existing[1] in {"succeeded", "failed", "unknown"} and existing[2]:
                return self._delivery_result(json.loads(existing[2]))
            if existing[1] != "prepared":
                raise ConflictError("delivery anterior não reconciliado; sem repetição automática")
            if db.execute("SELECT 1 FROM validation_deliveries WHERE operation_id=?",
                          (request.operation_id,)).fetchone():
                raise ConflictError("delivery já iniciada; exige reconciliação explícita")
            db.execute("UPDATE validation_operations SET status='running',updated_at=? WHERE operation_id=? AND status='prepared'",
                       (now(), request.operation_id))
            db.execute("INSERT INTO validation_deliveries(operation_id,request_digest,artifact_id,destination_id,authorization_id,authorized_by,authorized_at,status,credential_reference_ids_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'running',?,?,?)",
                       (request.operation_id, request_digest, request.artifact.artifact_id,
                        request.destination_id, auth.authorization_id, auth.authorized_by,
                        auth.authorized_at, json.dumps(request.credential_reference_ids),
                        now(), now()))
        try:
            result = provider.deliver(request)
            if (result.operation_id != request.operation_id
                    or result.destination_id != request.destination_id
                    or result.artifact_digest != request.artifact.sha256
                    or result.provider_id != provider.provider_id
                    or result.provider_version != provider.version):
                raise ConflictError("resultado do provider diverge da operação autorizada")
        except BaseException as exc:
            result = DeliveryResult(request.operation_id, DeliveryStatus.UNKNOWN,
                                    request.destination_id, request.artifact.sha256,
                                    provider.provider_id, provider.version,
                                    reason=f"resultado não reconciliado: {type(exc).__name__}")
        status = result.status.value
        with self.store.transaction() as db:
            db.execute("UPDATE validation_deliveries SET status=?,provider_id=?,provider_version=?,receipt_digest=?,reason=?,updated_at=? WHERE operation_id=?",
                       (status, result.provider_id, result.provider_version,
                        result.receipt_digest, result.reason[:512], now(),
                        request.operation_id))
            db.execute("UPDATE validation_operations SET status=?,result_snapshot=?,updated_at=? WHERE operation_id=?",
                       (status, canonical(asdict(result)), now(), request.operation_id))
            if result.status in {DeliveryStatus.SUCCEEDED, DeliveryStatus.FAILED}:
                db.execute("UPDATE validation_artifacts SET pinned=0 WHERE artifact_id=?",
                           (request.artifact.artifact_id,))
            db.execute("INSERT INTO validation_events(operation_id,kind,payload,occurred_at) VALUES(?,?,?,?)",
                       (request.operation_id, "delivery_" + status,
                        canonical({"destination_id": request.destination_id,
                        "artifact_digest": request.artifact.sha256,
                        "authorization_id": auth.authorization_id}), now()))
        return result

    @staticmethod
    def _delivery_digest(operation_id, project_id, artifact, destination_id,
                         authorization_id, credential_reference_ids):
        return digest({"operation_id": operation_id, "project_id": project_id,
                       "artifact": asdict(artifact), "destination_id": destination_id,
                       "authorization_id": authorization_id,
                       "credential_reference_ids": list(credential_reference_ids)})

    @staticmethod
    def _file_digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        return "sha256:" + hasher.hexdigest()


    @staticmethod
    def _build_result(value: dict) -> BuildResult:
        artifact = value.get("artifact")
        return BuildResult(value["operation_id"], BuildStatus(value["status"]),
                           ArtifactReference(**artifact) if artifact else None,
                           value["builder_id"], value["builder_version"],
                           value["builder_digest"], value["configuration_digest"],
                           value["dependency_digest"], value.get("reason", ""))

    @staticmethod
    def _delivery_result(value: dict) -> DeliveryResult:
        return DeliveryResult(value["operation_id"], DeliveryStatus(value["status"]),
                              value["destination_id"], value["artifact_digest"],
                              value["provider_id"], value["provider_version"],
                              value.get("receipt_digest", ""), value.get("reason", ""))
