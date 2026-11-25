"""Specialized subagent dispatcher connected to GlobalConfig.subagents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .global_config import GlobalConfig, RoleModelConfig, SUBAGENT_ROLES, load_global_config


@dataclass
class SubagentDispatchRecord:
    task_id: str
    role: str
    agent: str
    provider: str
    model: str
    supported: bool
    result: Any = None


class SubagentDispatcher:
    """Resolves and dispatches tasks to configured specialized subagents."""

    def __init__(self, config: GlobalConfig | None = None) -> None:
        self.config = config or load_global_config()
        self.records: list[SubagentDispatchRecord] = []

    def resolve_role(self, role: str) -> RoleModelConfig:
        if self.config is not None:
            if role in self.config.subagents:
                return self.config.subagents[role]
            return self.config.worker_default
        return RoleModelConfig(agent="codex")

    def dispatch(
        self,
        task_id: str,
        role: str,
        executor: Callable[[RoleModelConfig, str, str], Any],
        *,
        supports_delegation: Callable[[RoleModelConfig], bool] | None = None,
    ) -> SubagentDispatchRecord:
        if role not in SUBAGENT_ROLES and role != "adversarial":
            raise ValueError(f"Unsupported subagent role: {role!r}")
        if role == "adversarial" and self.config is not None and not self.config.adversarial_enabled:
            raise RuntimeError("Adversarial subagent is disabled by configuration")

        selected = self.resolve_role(role)
        supported = True if supports_delegation is None else bool(supports_delegation(selected))
        if not supported and self.config is not None:
            selected = self.config.worker_default

        result = executor(selected, role, task_id)
        record = SubagentDispatchRecord(
            task_id=task_id,
            role=role,
            agent=selected.agent,
            provider=selected.provider,
            model=selected.model,
            supported=supported,
            result=result,
        )
        self.records.append(record)
        return record
