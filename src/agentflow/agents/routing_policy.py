"""Versioned, inert schema for the future model-routing phase."""

from dataclasses import asdict, dataclass
import re
import tomllib
from typing import Any

from agentflow.planning import canonical, digest

from .contracts import ExecutionError


_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_FAILURE_CLASSES = {"environment", "authentication", "quota", "infrastructure",
                    "validation", "model_insufficiency", "unknown"}


@dataclass(frozen=True)
class ModelRoutingPolicy:
    """Declarative future policy. F6 intentionally exposes no evaluation method."""

    policy_id: str
    schema_version: int
    enabled: bool
    required_capabilities: tuple[str, ...]
    excluded_failure_classes: tuple[str, ...]
    escalation_evidence_ids: tuple[str, ...]
    max_attempts: int
    max_tokens: int | None
    max_latency_seconds: int | None
    max_cost: str | None
    currency: str | None
    authorization_required: bool

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.policy_id) or self.schema_version != 1:
            raise ExecutionError("identidade/versão da ModelRoutingPolicy inválida")
        if type(self.enabled) is not bool or type(self.authorization_required) is not bool:
            raise ExecutionError("flags da policy devem ser booleanas")
        if not self.authorization_required:
            raise ExecutionError("trocas/escalonamentos sempre exigem autorização")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 3:
            raise ExecutionError("max_attempts deve estar entre 1 e 3")
        for value, label in ((self.max_tokens, "max_tokens"),
                             (self.max_latency_seconds, "max_latency_seconds")):
            if value is not None and (type(value) is not int or value < 1):
                raise ExecutionError(f"{label} precisa ser inteiro positivo")
        if self.max_cost is not None:
            if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?", self.max_cost):
                raise ExecutionError("max_cost deve ser string decimal positiva")
            if not self.currency or not re.fullmatch(r"[A-Z]{3}", self.currency):
                raise ExecutionError("limite de custo requer moeda ISO explícita")
        elif self.currency is not None:
            raise ExecutionError("currency sem max_cost")
        if set(self.excluded_failure_classes) - _FAILURE_CLASSES:
            raise ExecutionError("classe de falha excluída desconhecida")
        if len(set(self.required_capabilities)) != len(self.required_capabilities):
            raise ExecutionError("capability duplicada")
        if len(set(self.excluded_failure_classes)) != len(self.excluded_failure_classes):
            raise ExecutionError("classe de falha duplicada")
        if len(set(self.escalation_evidence_ids)) != len(self.escalation_evidence_ids):
            raise ExecutionError("ID de evidência duplicado")

    @property
    def executable(self) -> bool:
        return False

    @property
    def policy_digest(self) -> str:
        return digest(asdict(self))

    def snapshot(self) -> str:
        return canonical(asdict(self))


def parse_model_routing_policy_toml(source: str) -> ModelRoutingPolicy:
    """Parse a policy document for validation/snapshot only; never executes it."""
    try:
        root = tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        raise ExecutionError(f"TOML de ModelRoutingPolicy inválido: {exc}") from exc
    if set(root) != {"schema_version", "policy"}:
        raise ExecutionError("documento requer apenas schema_version e [policy]")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ExecutionError("schema_version de policy não suportado")
    raw = root["policy"]
    allowed = {"id", "enabled", "required_capabilities", "excluded_failure_classes",
               "escalation_evidence_ids", "max_attempts", "max_tokens",
               "max_latency_seconds", "max_cost", "currency", "authorization_required"}
    required = {"id", "enabled", "required_capabilities", "excluded_failure_classes",
                "escalation_evidence_ids", "max_attempts", "authorization_required"}
    if not isinstance(raw, dict) or raw.keys() - allowed or required - raw.keys():
        raise ExecutionError("campos da ModelRoutingPolicy ausentes ou desconhecidos")
    for key in ("required_capabilities", "excluded_failure_classes", "escalation_evidence_ids"):
        if not isinstance(raw[key], list) or any(type(value) is not str for value in raw[key]):
            raise ExecutionError(f"{key} deve ser lista de strings")
    for key in ("max_tokens", "max_latency_seconds"):
        if key in raw and type(raw[key]) is not int:
            raise ExecutionError(f"{key} deve ser inteiro")
    if "max_cost" in raw and type(raw["max_cost"]) is not str:
        raise ExecutionError("max_cost deve ser string decimal")
    if "currency" in raw and type(raw["currency"]) is not str:
        raise ExecutionError("currency deve ser string")
    if type(raw["enabled"]) is not bool or type(raw["authorization_required"]) is not bool:
        raise ExecutionError("enabled e authorization_required devem ser booleanos")
    return ModelRoutingPolicy(
        raw["id"], 1, raw["enabled"], tuple(raw["required_capabilities"]),
        tuple(raw["excluded_failure_classes"]), tuple(raw["escalation_evidence_ids"]),
        raw["max_attempts"], raw.get("max_tokens"), raw.get("max_latency_seconds"),
        raw.get("max_cost"), raw.get("currency"), raw["authorization_required"])
