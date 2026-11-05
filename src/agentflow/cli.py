"""User-facing CLI; no agent execution commands exist in phase 1."""

import argparse
from pathlib import Path
import sys

from . import __version__
from .config import ConfigError, load_config
from .environment.doctor import diagnose
from .init import InitError, initialize, repository_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--version", action="version", version=f"agentflow {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, default=Path.cwd(), help="caminho dentro do repositório Git")
    try:
        args = parser.parse_args(argv)
        if args.command == "init":
            root, created = initialize(args.path)
            print(f"{root}: configuração {'criada' if created else 'existente; sem alterações'}")
            return 0
        root = repository_root(args.path.resolve())
        config = load_config(root / ".agentflow" / "config.toml")
        checks = diagnose(config)
        for check in checks:
            requirement = "obrigatório" if check.required else "opcional"
            print(f"{check.name}: {check.status} ({requirement}) — {check.detail}")
        return 1 if any(c.required and c.status != "ok" for c in checks) else 0
    except (ConfigError, InitError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
