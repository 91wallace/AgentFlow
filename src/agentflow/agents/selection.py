"""Immutable user selection and per-attempt selection snapshots."""

from dataclasses import asdict, dataclass
import json
import re

from agentflow.planning import canonical, digest


_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")
_MODES = {"manual", "provider-default", "legacy-default"}


@dataclass(frozen=True)
class ModelSelectionRequest:
    agent_id: str
    provider_id: str
    model_id: str
    requested_by: str
    mode: str = "manual"
    provider_descriptor_digest: str = ""
    model_descriptor_digest: str = ""
    routing_policy_snapshot: str = ""
    routing_policy_digest: str = ""

    def __post_init__(self) -> None:
        for name, value in (("agent_id", self.agent_id), ("provider_id", self.provider_id),
                            ("model_id", self.model_id)):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} inválido")
        if self.mode not in _MODES or not self.requested_by.strip() or len(self.requested_by) > 128:
            raise ValueError("modo/autor da seleção inválido")
        for value in (self.provider_descriptor_digest, self.model_descriptor_digest):
            if value and not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise ValueError("digest de descriptor inválido")
        if bool(self.routing_policy_snapshot) != bool(self.routing_policy_digest):
            raise ValueError("snapshot e digest da policy devem estar juntos")
        if self.routing_policy_snapshot:
            try:
                policy_data = json.loads(self.routing_policy_snapshot)
            except (TypeError, ValueError) as exc:
                raise ValueError("snapshot de policy inválido") from exc
            if canonical(policy_data) != self.routing_policy_snapshot or digest(policy_data) != self.routing_policy_digest:
                raise ValueError("digest/snapshot da policy divergente")

    def as_snapshot(self, *, attempt_id: str, adapter_id: str, adapter_api_version: str,
                    adapter_version: str, prepared_provider_id: str, prepared_model_id: str,
                    capabilities: tuple[str, ...], created_at: str) -> dict:
        return {
            "snapshot_version": 1,
            "attempt_id": attempt_id,
            "agent_id": self.agent_id,
            "adapter_id": adapter_id,
            "adapter_api_version": adapter_api_version,
            "adapter_version": adapter_version,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "provider_descriptor_digest": self.provider_descriptor_digest,
            "model_descriptor_digest": self.model_descriptor_digest,
            "routing_policy_snapshot": self.routing_policy_snapshot,
            "routing_policy_digest": self.routing_policy_digest,
            "mode": self.mode,
            "requested_by": self.requested_by,
            "prepared_provider_id": prepared_provider_id,
            "prepared_model_id": prepared_model_id,
            "capabilities": list(sorted(set(capabilities))),
            "created_at": created_at,
        }

    def request_data(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ModelSelectionObservation:
    attempt_id: str
    effective_provider_id: str
    effective_model_id: str
    status: str
    source: str
    observed_at: str

    def __post_init__(self) -> None:
        if self.status not in {"confirmed", "unknown", "mismatch"}:
            raise ValueError("status de observação inválido")
        if not self.source.strip() or not self.observed_at:
            raise ValueError("fonte/instante da observação obrigatórios")
