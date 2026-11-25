"""Versioned project configuration."""

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Invalid or unreadable AgentFlow configuration."""


AGENTS = ("codex", "antigravity", "opencode", "claude-code", "gemini")


@dataclass(frozen=True)
class AgentChoice:
    agent: str
    provider: str = ""
    model: str = ""
    source: str = "default"

    def __post_init__(self):
        if any(type(value) is not str for value in (self.agent, self.provider, self.model, self.source)):
            raise ConfigError("seleção de agente deve conter strings")
        if self.agent not in AGENTS:
            raise ConfigError("agente desconhecido")
        if bool(self.provider) != bool(self.model):
            raise ConfigError("provider e model devem ser informados juntos")
        if self.model:
            from .agents.selection import ModelSelectionRequest
            ModelSelectionRequest(self.agent, self.provider, self.model, self.source)


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    runtime: str = "local"
    agent: str = "codex"
    selection: str = "invoker-or-default"
    profiles: dict = field(default_factory=dict)

    @property
    def effective_agent(self) -> str:
        """Legacy 'none' uses the default without rewriting user configuration."""
        return "codex" if self.agent == "none" else self.agent


DEFAULT_TOML = 'schema_version = 1\nruntime = "local"\nagent = "codex"\n'


def parse_config(data: bytes) -> Config:
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"TOML inválido: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuração deve ser uma tabela TOML")
    extra = set(raw) - {"schema_version", "runtime", "agent", "agents"}
    if extra:
        raise ConfigError(f"chaves desconhecidas: {', '.join(sorted(extra))}")
    schema = raw.get("schema_version", 1)
    runtime = raw.get("runtime", "local")
    agent = raw.get("agent", "codex")
    if type(schema) is not int or schema not in {1, 2}:
        raise ConfigError("schema_version deve ser 1 ou 2")
    if type(runtime) is not str or runtime not in {"local", "docker"}:
        raise ConfigError("runtime deve ser 'local' ou 'docker'")
    selection, profiles = "invoker-or-default", {}
    if schema == 1 and "agents" in raw:
        raise ConfigError("agents requer schema_version = 2")
    if schema == 2:
        if "agent" in raw:
            raise ConfigError("schema 2 usa agents.default, não agent")
        settings = raw.get("agents", {})
        if type(settings) is not dict or set(settings) - {"default", "selection", "profiles"}:
            raise ConfigError("tabela agents inválida")
        agent = settings.get("default", "codex")
        selection = settings.get("selection", "invoker-or-default")
        profiles = settings.get("profiles", {})
        if selection not in ("invoker-or-default", "project-default") or type(profiles) is not dict:
            raise ConfigError("seleção/perfis de agentes inválidos")
        for name, profile in profiles.items():
            if (name not in AGENTS or type(profile) is not dict or set(profile) - {"provider", "model"}
                    or any(type(value) is not str or not value.strip() for value in profile.values())):
                raise ConfigError("perfil de agente inválido")
            AgentChoice(name, profile.get("provider", ""), profile.get("model", ""))
    if type(agent) is not str or agent not in (*AGENTS, "none"):
        raise ConfigError("agente desconhecido")
    return Config(schema, runtime, agent, selection, profiles)


def resolve_agent(config: Config, *, agent=None, invoker=None, provider=None, model=None) -> AgentChoice:
    if (provider is None) != (model is None):
        raise ConfigError("informe --provider e --model juntos")
    if invoker is not None and invoker not in AGENTS:
        raise ConfigError("integração chamadora desconhecida")
    selected = agent or (invoker if config.selection == "invoker-or-default" else None) or config.effective_agent
    profile = config.profiles.get(selected, {})
    return AgentChoice(selected, provider if provider is not None else profile.get("provider", ""),
                       model if model is not None else profile.get("model", ""),
                       "explicit" if agent else "invoker" if selected == invoker else "project")


def load_config(path: Path) -> Config:
    try:
        return parse_config(path.read_bytes())
    except OSError as exc:
        raise ConfigError(f"não foi possível ler {path}: {exc}") from exc
