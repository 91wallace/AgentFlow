"""Immutable provider/model catalogue contracts; no network discovery occurs here."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import re

from agentflow.planning import digest

from .contracts import ExecutionError


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")
_COST = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
_AVAILABILITY = {"available", "unavailable", "unknown"}


def _valid_id(value: str, label: str) -> None:
    if not _ID.fullmatch(value):
        raise ExecutionError(f"{label} inválido")


@dataclass(frozen=True)
class ProviderDescriptor:
    provider_id: str
    version: str
    api_version: str
    capabilities: tuple[str, ...] = ()
    auth_schemes: tuple[str, ...] = ()
    availability: str = "unknown"
    observed_at: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        _valid_id(self.provider_id, "provider_id")
        if not self.version or not self.api_version:
            raise ExecutionError("versões do provider obrigatórias")
        if self.availability not in _AVAILABILITY:
            raise ExecutionError("disponibilidade do provider inválida")
        if not self.source.strip() or len(set(self.capabilities)) != len(self.capabilities):
            raise ExecutionError("fonte ou capabilities do provider inválidas")
        if any(not item.strip() or len(item) > 128 for item in self.auth_schemes):
            raise ExecutionError("esquema de autenticação inválido")
        if self.observed_at:
            _timestamp(self.observed_at)

    @property
    def descriptor_digest(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class ModelDescriptor:
    provider_id: str
    model_id: str
    revision: str
    capabilities: tuple[str, ...] = ()
    modalities: tuple[str, ...] = ("text",)
    context_tokens: int | None = None
    availability: str = "unknown"
    observed_at: str = ""
    source: str = ""
    input_cost_per_million: str | None = None
    output_cost_per_million: str | None = None
    cost_currency: str | None = None
    cost_source: str | None = None

    def __post_init__(self) -> None:
        _valid_id(self.provider_id, "provider_id")
        _valid_id(self.model_id, "model_id")
        if not self.revision or len(self.revision) > 128:
            raise ExecutionError("revisão do modelo obrigatória")
        if self.availability not in _AVAILABILITY or not self.source.strip():
            raise ExecutionError("disponibilidade/fonte do modelo inválida")
        if len(set(self.capabilities)) != len(self.capabilities) or len(set(self.modalities)) != len(self.modalities):
            raise ExecutionError("capabilities/modalidades duplicadas")
        if self.context_tokens is not None and (type(self.context_tokens) is not int or self.context_tokens < 1):
            raise ExecutionError("limite de contexto inválido")
        for cost in (self.input_cost_per_million, self.output_cost_per_million):
            if cost is not None and not _COST.fullmatch(cost):
                raise ExecutionError("custo deve ser decimal positivo sem arredondamento implícito")
        costs_present = self.input_cost_per_million is not None or self.output_cost_per_million is not None
        if costs_present and (not self.cost_currency or not self.cost_source):
            raise ExecutionError("custo requer moeda e fonte")
        if not costs_present and (self.cost_currency is not None or self.cost_source is not None):
            raise ExecutionError("metadados de custo sem valor")
        if self.observed_at:
            _timestamp(self.observed_at)

    @property
    def descriptor_digest(self) -> str:
        return digest(asdict(self))

    def availability_is_stale(self, max_age_seconds: int, *, now: datetime | None = None) -> bool:
        if type(max_age_seconds) is not int or max_age_seconds < 0:
            raise ExecutionError("idade máxima inválida")
        if not self.observed_at:
            return True
        observed = _timestamp(self.observed_at)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ExecutionError("now precisa conter timezone")
        return current - observed > timedelta(seconds=max_age_seconds)


@dataclass(frozen=True)
class ModelCatalogEntry:
    """Catalog envelope separating declared capabilities from verified evidence."""

    descriptor: ModelDescriptor
    verified_capabilities: tuple[str, ...] = ()
    verification_source: str = ""

    def __post_init__(self) -> None:
        if len(set(self.verified_capabilities)) != len(self.verified_capabilities):
            raise ExecutionError("duplicate verified model capability")
        if not set(self.verified_capabilities) <= set(self.descriptor.capabilities):
            raise ExecutionError("verified capability is absent from the descriptor")
        if self.verified_capabilities and not self.verification_source.strip():
            raise ExecutionError("verified capabilities require provenance")

    @property
    def unknown_capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.descriptor.capabilities) - set(self.verified_capabilities)))


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionError("timestamp de observação inválido") from exc
    if parsed.tzinfo is None:
        raise ExecutionError("timestamp de observação precisa conter timezone")
    return parsed.astimezone(timezone.utc)
