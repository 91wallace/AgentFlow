"""Execution-scoped role selections, independent of CLI interaction and stores."""

from dataclasses import asdict, dataclass
import re

from .selection import ModelSelectionRequest


ROLE_NAMES = ("orchestrator", "executor", "reviewer")


@dataclass(frozen=True)
class RoleBinding:
    agent_id: str
    adapter_version: str
    executable: str
    api_version: str
    model_selection: ModelSelectionRequest | None = None
    model_status: str = "cli-default"
    capabilities_json: str = "null"
    availability_source: str = "unknown"

    def __post_init__(self):
        if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", self.agent_id):
            raise ValueError("invalid role agent")
        if not all(type(value) is str and value for value in (
                self.adapter_version, self.executable, self.api_version, self.availability_source)):
            raise ValueError("role requires observed adapter identity")
        if self.model_status not in {"cli-default", "configured-unverified", "available"}:
            raise ValueError("invalid role model status")
        if (self.model_selection is None) != (self.model_status == "cli-default"):
            raise ValueError("model selection/status mismatch")
        if self.model_selection and self.model_selection.agent_id != self.agent_id:
            raise ValueError("role/model agent mismatch")

    @property
    def agent(self):
        return self.agent_id

    @property
    def provider(self):
        return self.model_selection.provider_id if self.model_selection else ""

    @property
    def model(self):
        return self.model_selection.model_id if self.model_selection else ""

    @property
    def source(self):
        return "roles"


@dataclass(frozen=True)
class ExecutionRoles:
    orchestrator: RoleBinding
    executor: RoleBinding
    reviewer: RoleBinding
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported role configuration")
        if any(not isinstance(getattr(self, role), RoleBinding) for role in ROLE_NAMES):
            raise ValueError("all three roles are required")

    def to_mapping(self):
        return asdict(self)

    @classmethod
    def from_mapping(cls, value):
        if type(value) is not dict or set(value) != {*ROLE_NAMES, "schema_version"}:
            raise ValueError("invalid role configuration fields")
        bindings = {}
        for role in ROLE_NAMES:
            data = dict(value[role])
            selection = data.get("model_selection")
            data["model_selection"] = ModelSelectionRequest(**selection) if selection is not None else None
            bindings[role] = RoleBinding(**data)
        return cls(**bindings, schema_version=value["schema_version"])
