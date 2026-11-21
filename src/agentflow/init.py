"""Safe repository initialization."""

from pathlib import Path
import subprocess

from .config import AGENTS, DEFAULT_TOML, ConfigError, load_config


class InitError(ValueError):
    """Unsafe or unsuccessful initialization."""


def repository_root(path: Path) -> Path:
    if not path.is_dir():
        raise InitError(f"diretório inexistente: {path}")
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InitError(f"não foi possível consultar Git: {exc}") from exc
    if result.returncode:
        raise InitError(f"{path} não está em um repositório Git")
    return Path(result.stdout.strip()).resolve()


def initialize(path: Path, *, agent: str | None = None) -> tuple[Path, bool]:
    if agent is not None and agent not in AGENTS:
        raise InitError(f"agente não suportado: {agent}")
    root = repository_root(path.resolve())
    directory = root / ".agentflow"
    target = directory / "config.toml"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise InitError(f"conflito: {directory} não é um diretório")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file():
            raise InitError(f"conflito: {target} não é um arquivo regular")
        try:
            existing = load_config(target)
            if agent is not None and existing.effective_agent != agent:
                raise InitError("conflito: agente existente difere do solicitado")
        except ConfigError as exc:
            raise InitError(f"conflito em configuração existente: {exc}") from exc
        return root, False
    try:
        directory.mkdir(exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(DEFAULT_TOML if agent in (None, "codex") else
                         f'schema_version = 2\nruntime = "local"\n\n[agents]\ndefault = "{agent}"\nselection = "invoker-or-default"\n')
    except FileExistsError:
        raise InitError(f"conflito: {target} já existe") from None
    except OSError as exc:
        raise InitError(f"não foi possível criar {target}: {exc}") from exc
    return root, True
