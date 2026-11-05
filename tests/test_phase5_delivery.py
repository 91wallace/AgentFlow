import hashlib
from pathlib import Path

import pytest

from agentflow.delivery import (ArtifactDeliveryService, ArtifactReference, BuildRequest,
                                BuildResult, DeliveryRequest, DeliveryResult,
                                DeliveryStatus)
from agentflow.state import ConflictError
from agentflow.validation import BuildStatus, SnapshotManager, ValidationScheduler
from test_phase5_scheduler import FakeRuntime, FakeValidator, make_validation_project, request_for


class FakeBuilder:
    builder_id = "builder"
    version = "1.0"
    digest = "sha256:" + "4" * 64

    def build(self, request):
        body = b"verified artifact\n"
        artifact_id = "artifact-one"
        (request.output_path / artifact_id).write_bytes(body)
        reference = ArtifactReference(
            artifact_id, "sha256:" + hashlib.sha256(body).hexdigest(), len(body),
            request.source_commit_oid, request.source_tree_oid, request.snapshot_digest,
        )
        return BuildResult(request.operation_id, BuildStatus.PASSED,
            reference, self.builder_id, self.version, self.digest,
            request.configuration_digest, request.dependency_digest)


class UnknownProvider:
    provider_id = "fake-destination"
    version = "1"

    def __init__(self):
        self.calls = 0

    def deliver(self, request):
        self.calls += 1
        return DeliveryResult(request.operation_id, DeliveryStatus.UNKNOWN,
                              request.destination_id, request.artifact.sha256,
                              self.provider_id, self.version, reason="ack lost")


def test_artifact_provenance_and_unknown_delivery_are_idempotent(tmp_path):
    _, plan, store, provider, workspace, checkpoint = make_validation_project(tmp_path)
    validation_request = request_for(plan, store, workspace, checkpoint)
    validation = ValidationScheduler(
        store, provider, FakeRuntime(), FakeValidator(), SnapshotManager(provider),
        tmp_path / "snapshots", tmp_path / "outputs",
    ).run(validation_request, workspace, checkpoint, plan)
    snapshot_path = Path(store.db.execute(
        "SELECT storage_path FROM validation_snapshots WHERE validation_run_id=?",
        (validation.validation_run_id,)).fetchone()[0])
    service = ArtifactDeliveryService(
        store, tmp_path / "artifacts", delivery_enabled=True,
        allowed_destinations=("staging",),
        snapshot_verifier=lambda run_id, path, digest: (
            run_id == validation.validation_run_id and path == snapshot_path
            and digest == validation.snapshot_digest),
    )
    build_request = BuildRequest(
        "build-op-1", plan.specification.project_id, "task-one", 1,
        checkpoint.checkpoint_id, checkpoint.commit_oid, checkpoint.tree_oid,
        validation.snapshot_digest, "builder", "1.0", "sha256:" + "4" * 64,
        "sha256:" + "5" * 64, "sha256:" + "6" * 64, "output-one",
        validation.validation_run_id,
    )
    built = service.build(build_request, FakeBuilder(), snapshot_path)
    assert built.artifact and built.artifact.size_bytes == len(b"verified artifact\n")
    assert service.build(build_request, FakeBuilder(), snapshot_path) == built
    authorization = service.authorize_delivery(
        "delivery-op-1", plan.specification.project_id, built.artifact.artifact_id,
        "staging", authorized_by="operator")
    request = DeliveryRequest("delivery-op-1", plan.specification.project_id,
                              built.artifact, "staging", authorization)
    provider_stub = UnknownProvider()
    result = service.deliver(request, provider_stub)
    assert result.status is DeliveryStatus.UNKNOWN
    assert service.deliver(request, provider_stub) == result
    assert provider_stub.calls == 1
    target = Path(store.db.execute(
        "SELECT storage_path FROM validation_artifacts WHERE artifact_id=?",
        (built.artifact.artifact_id,)).fetchone()[0])
    target.write_bytes(b"tampered")
    with pytest.raises(ConflictError, match="verificação de artefato"):
        service.deliver(request, provider_stub)
    assert provider_stub.calls == 1
    with pytest.raises(ConflictError, match="desabilitada"):
        ArtifactDeliveryService(store, tmp_path / "artifacts").authorize_delivery(
            "delivery-op-2", plan.specification.project_id,
            built.artifact.artifact_id, "staging", authorized_by="operator")
