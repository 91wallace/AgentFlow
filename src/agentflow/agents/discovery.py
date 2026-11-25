"""Read-only inventory over an explicitly composed adapter registry.

Installation, adapter compatibility, authentication, and model access are separate
observations. A configured model is never evidence of access to that model.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from .contracts import AgentAvailability, AgentCapabilities, ExecutionError
from .models import ModelDescriptor
from .registry import AdapterDescriptor, AgentRegistry


@dataclass(frozen=True)
class AgentDiscovery:
    descriptor: AdapterDescriptor
    availability: AgentAvailability
    capabilities: AgentCapabilities | None = None
    capabilities_source: str = "unknown"
    configured_provider: str = ""
    configured_model: str = ""
    models: tuple[ModelDescriptor, ...] = ()
    model_listing: str = "unsupported"
    authentication: str = "unknown"
    limitations: tuple[str, ...] = ()


def discover_agents(registry: AgentRegistry, *, profiles=None) -> tuple[AgentDiscovery, ...]:
    """Inspect every registration, isolating expected probe failures per adapter.

    Optional ``discover_models`` hooks must return ModelDescriptor values backed
    by a provider-supported inventory. No inferred catalogue or model prompt is
    used. Registry construction remains at the application boundary.
    """
    reports = []
    for adapter_id in registry.ids():
        descriptor = registry.descriptor(adapter_id)
        adapter = registry.resolve(adapter_id)
        profile = (profiles or {}).get(adapter_id, {})
        limitations = ["authentication_not_probed"]
        capabilities = None
        models = ()
        listing = "unsupported"
        try:
            hook = getattr(adapter, "discover", None)
            if hook is None:
                availability = AgentAvailability(
                    adapter_id, "unknown", descriptor.executable, None,
                    "registry", "discovery_unsupported", datetime.now(timezone.utc).isoformat())
            else:
                availability = hook()
                if not isinstance(availability, AgentAvailability) or availability.adapter_id != adapter_id:
                    raise ExecutionError("identidade da descoberta diverge do registry")
        except (ExecutionError, OSError):
            availability = AgentAvailability(
                adapter_id, "unknown", descriptor.executable, None,
                "registry", "probe_failed", datetime.now(timezone.utc).isoformat())
            capabilities = None
            limitations.append("adapter_probe_failed")
        capability_hook = getattr(adapter, "capabilities", None)
        if capability_hook and availability.status == "available":
            try:
                capabilities = capability_hook()
                if not isinstance(capabilities, AgentCapabilities):
                    raise ExecutionError("capabilities de descoberta inválidas")
            except (ExecutionError, OSError):
                capabilities = None
                limitations.append("capability_probe_failed")
        model_hook = getattr(adapter, "discover_models", None)
        if model_hook and availability.status == "available":
            try:
                models = tuple(model_hook())
                if (any(not isinstance(model, ModelDescriptor) for model in models)
                        or any(not model.observed_at for model in models)
                        or len({(model.provider_id, model.model_id) for model in models}) != len(models)):
                    raise ExecutionError("catálogo de modelos inválido")
                listing = "supported"
            except (ValueError, TypeError, OSError):
                models = ()
                listing = "failed"
                limitations.append("model_probe_failed")
        if listing == "unsupported":
            limitations.append("model_listing_unsupported")
        if profile.get("model"):
            limitations.append("configured_model_does_not_prove_access")
        reports.append(AgentDiscovery(
            descriptor, availability, capabilities,
            "adapter_declaration" if capabilities is not None else "unknown",
            profile.get("provider", ""), profile.get("model", ""), models, listing,
            limitations=tuple(limitations)))
    return tuple(reports)
