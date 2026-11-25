"""Role selection rules shared by interactive CLI and future client interfaces."""

from dataclasses import asdict
import json
import os

from agentflow.agents.roles import ExecutionRoles, ROLE_NAMES, RoleBinding
from agentflow.agents.selection import ModelSelectionRequest


def binding_for(report, *, provider="", model=""):
    observation = report.availability
    if observation.status != "available" or not observation.version:
        raise ValueError(f"role_agent_unavailable:{observation.adapter_id}:{observation.reason_code}")
    if report.capabilities and report.capabilities.task_initialization == "unavailable":
        raise ValueError("role_agent_cannot_initialize_tasks")
    if bool(provider) != bool(model):
        raise ValueError("role_provider_and_model_required_together")
    selection, status = None, "cli-default"
    if model:
        selection = ModelSelectionRequest(observation.adapter_id, provider, model, "execution-role")
        matching = [item for item in report.models if (item.provider_id, item.model_id) == (provider, model)]
        if any(item.availability == "unavailable" for item in matching):
            raise ValueError("role_model_unavailable")
        status = ("available" if matching and matching[0].availability == "available"
                  and not matching[0].availability_is_stale(300) else "configured-unverified")
    return RoleBinding(
        observation.adapter_id, observation.version, observation.executable,
        report.descriptor.api_version, selection, status,
        json.dumps(asdict(report.capabilities) if report.capabilities else None, sort_keys=True),
        observation.source)


def validate_roles(roles: ExecutionRoles, reports):
    """Revalidate installation without changing or silently upgrading a selection."""
    by_id = {report.descriptor.adapter_id: report for report in reports}
    for role in ROLE_NAMES:
        selected = getattr(roles, role)
        report = by_id.get(selected.agent_id)
        if report is None:
            raise ValueError(f"role_agent_unregistered:{role}:{selected.agent_id}")
        selection = selected.model_selection
        current = binding_for(report, provider=selection.provider_id if selection else "",
                              model=selection.model_id if selection else "")
        if (selected.adapter_version != current.adapter_version
                or selected.api_version != current.api_version
                or os.path.normcase(selected.executable) != os.path.normcase(current.executable)):
            raise ValueError(f"role_agent_identity_changed:{role}")
        if selected.capabilities_json != current.capabilities_json:
            raise ValueError(f"role_agent_capabilities_changed:{role}")
    return roles
