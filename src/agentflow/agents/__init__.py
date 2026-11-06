"""Agent-independent execution contracts and adapters."""

from .contracts import (AgentAdapter, AgentResult, Cancellation, ExecutionError,
                        ExecutionPolicy, ExecutionRequest, ProcessIdentity, ProcessResult,
                        ProcessRuntime, ProcessSpec, StartedCallback)
from .registry import AdapterDescriptor, AgentRegistry, CapabilityReport
from .models import ModelCatalogEntry, ModelDescriptor, ProviderDescriptor
from .selection import ModelSelectionObservation, ModelSelectionRequest
from .routing_policy import ModelRoutingPolicy, parse_model_routing_policy_toml
from .antigravity import AntigravityAdapter
from .claude_code import ClaudeCodeAdapter
from .opencode import OpenCodeAdapter
from .hermes import HermesAdapter
from .routing import TaskClassification, RoutingDecision, classify_task, evaluate_routing

__all__ = ["AgentAdapter", "AgentResult", "Cancellation", "ExecutionError", "ExecutionPolicy",
           "ExecutionRequest", "ProcessIdentity", "ProcessResult", "ProcessRuntime", "ProcessSpec", "StartedCallback",
           "AdapterDescriptor", "AgentRegistry", "CapabilityReport"]
__all__ += ["ModelDescriptor", "ProviderDescriptor"]
__all__ += ["ModelCatalogEntry"]
__all__ += ["ModelSelectionObservation", "ModelSelectionRequest"]
__all__ += ["ModelRoutingPolicy", "parse_model_routing_policy_toml"]
__all__ += ["AntigravityAdapter"]
__all__ += ["ClaudeCodeAdapter"]
__all__ += ["OpenCodeAdapter"]
__all__ += ["HermesAdapter"]
__all__ += ["TaskClassification", "RoutingDecision", "classify_task", "evaluate_routing"]
