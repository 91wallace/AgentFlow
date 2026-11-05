"""User-facing CLI; no agent execution commands exist in phases 1 or 2."""

import argparse
from pathlib import Path
import sys

from . import __version__
from .config import ConfigError, load_config
from .environment.doctor import diagnose
from .init import InitError, initialize, repository_root
from .planning import PlanError, load_plan
from .state import StateError
from .state.inspection import inspect_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentflow")
    parser.add_argument("--version", action="version", version=f"agentflow {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "doctor", "plan", "state"):
        command = commands.add_parser(name)
        command.add_argument("--path", type=Path, default=Path.cwd(), help="caminho dentro do repositório Git")
    try:
        args = parser.parse_args(argv)
        if args.command == "init":
            root, created = initialize(args.path)
            print(f"{root}: configuração {'criada' if created else 'existente; sem alterações'}")
            return 0
        root = repository_root(args.path.resolve())
        if args.command == "plan":
            plan = load_plan(root / ".agentflow")
            print(f"{plan.specification.project_id}: {len(plan.tasks)} tarefas; ordem: {', '.join(plan.order)}")
            return 0
        if args.command == "state":
            plan = load_plan(root / ".agentflow")
            for task in inspect_state(root / ".agentflow" / "state.sqlite3", plan):
                print(f"{task.task_id}: {task.status} (revisão {task.definition_revision})")
            return 0
        config = load_config(root / ".agentflow" / "config.toml")
        checks = diagnose(config)
        for check in checks:
            requirement = "obrigatório" if check.required else "opcional"
            print(f"{check.name}: {check.status} ({requirement}) — {check.detail}")
        return 1 if any(c.required and c.status != "ok" for c in checks) else 0
    except (ConfigError, InitError, PlanError, StateError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
