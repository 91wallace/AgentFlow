"""Immutable executor selection linked to the execution contract policy digest."""

from dataclasses import asdict
import hashlib
import json

from .config import AgentChoice, ConfigError


def selection_path(root, cycle_id):
    return root / ".agentflow" / "executor-selections" / (hashlib.sha256(cycle_id.encode()).hexdigest() + ".json")


def read_selection(root, cycle_id):
    path = selection_path(root, cycle_id)
    if path.is_symlink() or path.parent.is_symlink() or (root / ".agentflow").is_symlink():
        raise ConfigError("executor_selection_symlink")
    if not path.exists():
        return None
    try:
        return AgentChoice(**json.loads(path.read_text(encoding="utf-8")))
    except (TypeError, ValueError, OSError) as exc:
        raise ConfigError("executor_selection_invalid") from exc


def persist_selection(root, cycle_id, choice):
    previous = read_selection(root, cycle_id)
    if previous is not None:
        if (previous.agent, previous.provider, previous.model) != (choice.agent, choice.provider, choice.model):
            raise ConfigError("executor_selection_conflict: inicie outro job para trocar o executor")
        return previous
    path = selection_path(root, cycle_id)
    if path.parent.is_symlink() or (root / ".agentflow").is_symlink():
        raise ConfigError("executor_selection_symlink")
    path.parent.mkdir(exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(asdict(choice), stream, sort_keys=True, separators=(",", ":"))
    except FileExistsError:
        return persist_selection(root, cycle_id, choice)
    return choice
