"""Global user-level AgentFlow configuration for agents, models, and roles."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any

from .config import AGENTS, ConfigError


class GlobalConfigError(ConfigError):
    """Invalid, corrupted, or unreadable global AgentFlow configuration."""


CURRENT_SCHEMA_VERSION = 1
SUBAGENT_ROLES = ("development", "research", "code_analysis", "testing", "documentation")
FORBIDDEN_SECRET_KEYS = frozenset({"api_key", "apikey", "token", "secret", "password", "passwd", "auth_token"})


def global_config_path() -> Path:
    """Return the OS-appropriate global config.toml path."""
    override = os.environ.get("AGENTFLOW_GLOBAL_CONFIG")
    if override:
        return Path(override)
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "AgentFlow" / "config.toml"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "agentflow" / "config.toml"


@dataclass
class RoleModelConfig:
    agent: str = "codex"
    provider: str = ""
    model: str = ""

    def validate(self, *, allow_empty: bool = False) -> None:
        if allow_empty and not self.agent and not self.provider and not self.model:
            return
        if self.agent not in AGENTS:
            raise GlobalConfigError(f"agente desconhecido na configuração global: {self.agent!r}")
        if bool(self.provider) != bool(self.model):
            raise GlobalConfigError("provider e model devem ser informados juntos")


@dataclass
class GlobalConfig:
    schema_version: int = CURRENT_SCHEMA_VERSION
    orchestrator: RoleModelConfig = field(default_factory=lambda: RoleModelConfig(agent="codex"))
    worker_default: RoleModelConfig = field(default_factory=lambda: RoleModelConfig(agent="codex"))
    max_concurrent_workers: int = 2
    worker_distribution_policy: str = "round-robin"
    subagents: dict[str, RoleModelConfig] = field(default_factory=dict)
    adversarial_enabled: bool = False
    adversarial_independent: bool = True
    adversarial: RoleModelConfig = field(default_factory=lambda: RoleModelConfig(agent=""))
    fallback_policy: str = "none"
    fallback_models: list[str] = field(default_factory=list)
    execution_preferences: dict[str, Any] = field(default_factory=lambda: {"auto_discover": True})

    def validate(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise GlobalConfigError(f"schema_version global não suportado: {self.schema_version}")
        self.orchestrator.validate()
        self.worker_default.validate()
        if not isinstance(self.max_concurrent_workers, int) or self.max_concurrent_workers < 1:
            raise GlobalConfigError("max_concurrent_workers deve ser >= 1")
        if self.worker_distribution_policy not in ("round-robin", "reuse-first", "isolated"):
            raise GlobalConfigError(f"política de distribuição inválida: {self.worker_distribution_policy!r}")
        for role_name, cfg in self.subagents.items():
            if role_name not in SUBAGENT_ROLES:
                raise GlobalConfigError(f"papel de subagente desconhecido: {role_name!r}")
            cfg.validate(allow_empty=True)
        if self.adversarial_enabled:
            self.adversarial.validate()
        else:
            self.adversarial.validate(allow_empty=True)
        if self.fallback_policy not in ("none", "ordered", "same-provider"):
            raise GlobalConfigError(f"política de fallback inválida: {self.fallback_policy!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "orchestrator": asdict(self.orchestrator),
            "workers": {
                "default": asdict(self.worker_default),
                "max_concurrent": self.max_concurrent_workers,
                "distribution_policy": self.worker_distribution_policy,
            },
            "subagents": {k: asdict(v) for k, v in self.subagents.items()},
            "adversarial": {
                "enabled": self.adversarial_enabled,
                "independent_from_executor": self.adversarial_independent,
                **asdict(self.adversarial),
            },
            "fallback": {
                "policy": self.fallback_policy,
                "models": list(self.fallback_models),
            },
            "preferences": dict(self.execution_preferences),
        }


def _check_no_secrets(data: Any, prefix: str = "") -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_SECRET_KEYS and value:
                raise GlobalConfigError(f"credenciais/segredos não podem ser armazenados na configuração global ({prefix}{key})")
            _check_no_secrets(value, f"{prefix}{key}.")


def _toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _serialize_toml(cfg: GlobalConfig) -> str:
    lines = [f"schema_version = {cfg.schema_version}", ""]
    lines.append("[orchestrator]")
    lines.append(f"agent = {_toml_quote(cfg.orchestrator.agent)}")
    lines.append(f"provider = {_toml_quote(cfg.orchestrator.provider)}")
    lines.append(f"model = {_toml_quote(cfg.orchestrator.model)}")
    lines.append("")
    lines.append("[workers]")
    lines.append(f"max_concurrent = {cfg.max_concurrent_workers}")
    lines.append(f"distribution_policy = {_toml_quote(cfg.worker_distribution_policy)}")
    lines.append("")
    lines.append("[workers.default]")
    lines.append(f"agent = {_toml_quote(cfg.worker_default.agent)}")
    lines.append(f"provider = {_toml_quote(cfg.worker_default.provider)}")
    lines.append(f"model = {_toml_quote(cfg.worker_default.model)}")
    lines.append("")
    for role_name, sub_cfg in sorted(cfg.subagents.items()):
        lines.append(f"[subagents.{role_name}]")
        lines.append(f"agent = {_toml_quote(sub_cfg.agent)}")
        lines.append(f"provider = {_toml_quote(sub_cfg.provider)}")
        lines.append(f"model = {_toml_quote(sub_cfg.model)}")
        lines.append("")
    lines.append("[adversarial]")
    lines.append(f"enabled = {'true' if cfg.adversarial_enabled else 'false'}")
    lines.append(f"independent_from_executor = {'true' if cfg.adversarial_independent else 'false'}")
    lines.append(f"agent = {_toml_quote(cfg.adversarial.agent)}")
    lines.append(f"provider = {_toml_quote(cfg.adversarial.provider)}")
    lines.append(f"model = {_toml_quote(cfg.adversarial.model)}")
    lines.append("")
    lines.append("[fallback]")
    lines.append(f"policy = {_toml_quote(cfg.fallback_policy)}")
    models_str = ", ".join(_toml_quote(m) for m in cfg.fallback_models)
    lines.append(f"models = [{models_str}]")
    lines.append("")
    lines.append("[preferences]")
    auto_disc = cfg.execution_preferences.get("auto_discover", True)
    lines.append(f"auto_discover = {'true' if auto_disc else 'false'}")
    lines.append("")
    return "\n".join(lines)


def _migrate_raw(raw: dict[str, Any]) -> dict[str, Any]:
    version = raw.get("schema_version", 0)
    if version == 0:
        # Migrate legacy/minimal v0 dict into v1 structure
        default_agent = raw.get("agent", "codex")
        return {
            "schema_version": 1,
            "orchestrator": {"agent": default_agent, "provider": "", "model": ""},
            "workers": {"default": {"agent": default_agent, "provider": "", "model": ""}, "max_concurrent": 2, "distribution_policy": "round-robin"},
        }
    return raw


def load_global_config(path: Path | None = None) -> GlobalConfig | None:
    """Load and validate global config. Returns None if file does not exist."""
    target = path or global_config_path()
    if not target.is_file():
        return None
    try:
        raw_bytes = target.read_bytes()
        raw = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise GlobalConfigError(f"configuração global corrompida ou ilegível: {exc}") from exc
    if not isinstance(raw, dict):
        raise GlobalConfigError("configuração global deve ser uma tabela TOML")
    _check_no_secrets(raw)
    raw = _migrate_raw(raw)
    orch = raw.get("orchestrator", {})
    workers = raw.get("workers", {})
    w_def = workers.get("default", {})
    adv = raw.get("adversarial", {})
    fb = raw.get("fallback", {})
    prefs = raw.get("preferences", {})
    subagents_raw = raw.get("subagents", {})
    if not all(isinstance(x, dict) for x in (orch, workers, w_def, adv, fb, prefs, subagents_raw)):
        raise GlobalConfigError("estrutura inválida na configuração global")
    subagents = {
        k: RoleModelConfig(
            agent=str(v.get("agent", "")),
            provider=str(v.get("provider", "")),
            model=str(v.get("model", "")),
        )
        for k, v in subagents_raw.items()
        if isinstance(v, dict)
    }
    cfg = GlobalConfig(
        schema_version=int(raw.get("schema_version", CURRENT_SCHEMA_VERSION)),
        orchestrator=RoleModelConfig(
            agent=str(orch.get("agent", "codex")),
            provider=str(orch.get("provider", "")),
            model=str(orch.get("model", "")),
        ),
        worker_default=RoleModelConfig(
            agent=str(w_def.get("agent", "codex")),
            provider=str(w_def.get("provider", "")),
            model=str(w_def.get("model", "")),
        ),
        max_concurrent_workers=int(workers.get("max_concurrent", 2)),
        worker_distribution_policy=str(workers.get("distribution_policy", "round-robin")),
        subagents=subagents,
        adversarial_enabled=bool(adv.get("enabled", False)),
        adversarial_independent=bool(adv.get("independent_from_executor", True)),
        adversarial=RoleModelConfig(
            agent=str(adv.get("agent", "")),
            provider=str(adv.get("provider", "")),
            model=str(adv.get("model", "")),
        ),
        fallback_policy=str(fb.get("policy", "none")),
        fallback_models=[str(m) for m in fb.get("models", [])],
        execution_preferences=dict(prefs) if prefs else {"auto_discover": True},
    )
    cfg.validate()
    return cfg


def save_global_config(cfg: GlobalConfig, path: Path | None = None) -> Path:
    """Validate and atomically persist global configuration."""
    cfg.validate()
    target = path or global_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _serialize_toml(cfg)
    fd, tmp_name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def default_global_config(preferred_agent: str = "codex") -> GlobalConfig:
    """Build a default GlobalConfig using the preferred or fallback agent."""
    agent = preferred_agent if preferred_agent in AGENTS else "codex"
    return GlobalConfig(
        orchestrator=RoleModelConfig(agent=agent),
        worker_default=RoleModelConfig(agent=agent),
    )
