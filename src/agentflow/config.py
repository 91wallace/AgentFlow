"""Versioned project configuration."""

from dataclasses import dataclass
from pathlib import Path
import tomllib


class ConfigError(ValueError):
    """Invalid or unreadable AgentFlow configuration."""


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    runtime: str = "local"
    agent: str = "none"


DEFAULT_TOML = 'schema_version = 1\nruntime = "local"\nagent = "none"\n'


def parse_config(data: bytes) -> Config:
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"TOML inválido: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuração deve ser uma tabela TOML")
    extra = set(raw) - {"schema_version", "runtime", "agent"}
    if extra:
        raise ConfigError(f"chaves desconhecidas: {', '.join(sorted(extra))}")
    schema = raw.get("schema_version", 1)
    runtime = raw.get("runtime", "local")
    agent = raw.get("agent", "none")
    if type(schema) is not int or schema != 1:
        raise ConfigError("schema_version deve ser o inteiro 1")
    if type(runtime) is not str or runtime not in {"local", "docker"}:
        raise ConfigError("runtime deve ser 'local' ou 'docker'")
    if type(agent) is not str or agent not in {"none", "codex"}:
        raise ConfigError("agent deve ser 'none' ou 'codex'")
    return Config(schema, runtime, agent)


def load_config(path: Path) -> Config:
    try:
        return parse_config(path.read_bytes())
    except OSError as exc:
        raise ConfigError(f"não foi possível ler {path}: {exc}") from exc
