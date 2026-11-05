"""Explicit, deterministic registry for trusted built-in agent adapters."""

from dataclasses import dataclass
import re
from typing import Mapping

from .contracts import AgentAdapter, ExecutionError


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CAPABILITY_STATES = {"supported", "effective", "enforced", "unavailable", "unknown"}


@dataclass(frozen=True)
class CapabilityReport:
    name: str
    state: str
    source: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name) or self.state not in _CAPABILITY_STATES:
            raise ExecutionError("capability inválida")
        if not self.source.strip() or len(self.detail) > 512:
            raise ExecutionError("origem ou detalhe de capability inválido")


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    api_version: str
    implementation_version: str
    executable: str
    supported_platforms: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.adapter_id):
            raise ExecutionError("adapter_id inválido")
        for value in (self.api_version, self.implementation_version):
            if not value or len(value) > 64:
                raise ExecutionError("versão do adapter inválida")
        if not self.executable or "\x00" in self.executable:
            raise ExecutionError("executável inválido")
        if not self.supported_platforms or len(set(self.supported_platforms)) != len(self.supported_platforms):
            raise ExecutionError("plataformas do adapter inválidas")


class AgentRegistry:
    """Adapters are supplied by application code; no entry-point discovery/import occurs."""

    def __init__(self, adapters: Mapping[str, tuple[AdapterDescriptor, AgentAdapter]] | None = None):
        self._adapters: dict[str, tuple[AdapterDescriptor, AgentAdapter]] = {}
        for adapter_id, pair in (adapters or {}).items():
            self.register(pair[0], pair[1])
            if adapter_id != pair[0].adapter_id:
                raise ExecutionError("chave do registry diverge do adapter_id")

    def register(self, descriptor: AdapterDescriptor, adapter: AgentAdapter) -> None:
        if descriptor.adapter_id in self._adapters:
            raise ExecutionError(f"adapter duplicado: {descriptor.adapter_id}")
        if not callable(getattr(adapter, "prepare", None)) or not callable(getattr(adapter, "interpret", None)):
            raise ExecutionError("adapter não implementa o contrato")
        self._adapters[descriptor.adapter_id] = (descriptor, adapter)

    def descriptor(self, adapter_id: str) -> AdapterDescriptor:
        try:
            return self._adapters[adapter_id][0]
        except KeyError as exc:
            raise ExecutionError(f"adapter não registrado: {adapter_id}") from exc

    def resolve(self, adapter_id: str) -> AgentAdapter:
        try:
            return self._adapters[adapter_id][1]
        except KeyError as exc:
            raise ExecutionError(f"adapter não registrado: {adapter_id}") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
