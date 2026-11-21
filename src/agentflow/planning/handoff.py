"""Informational context without byte caps; never acceptance evidence."""

import json

FIELDS = {"ok", "files", "exports", "decisions", "verified", "next_notes", "blocker"}


def validate_handoff(value):
    if not isinstance(value, dict):
        raise ValueError("handoff must be an object")
    value = dict(value)
    if "symbols" in value and "exports" not in value:
        value["exports"] = value.pop("symbols")
    if set(value) != FIELDS or type(value["ok"]) is not bool:
        raise ValueError("invalid handoff fields")
    for key in FIELDS - {"ok", "blocker"}:
        if (not isinstance(value[key], list)
                or any(type(item) is not str for item in value[key])):
            raise ValueError("handoff lists must contain strings")
    if value["blocker"] is not None and type(value["blocker"]) is not str:
        raise ValueError("invalid handoff blocker")
    return value


def dependency_context(depends_on, outcomes):
    context = []
    for task_id in dict.fromkeys(depends_on):
        outcome = outcomes.get(task_id)
        if outcome is None or outcome.status != "succeeded" or outcome.handoff is None:
            continue
        try:
            handoff = validate_handoff(outcome.handoff)
        except ValueError:
            continue
        context.append({"task_id": task_id, "handoff": handoff})
    return context


def extract_codex_handoff(path):
    """Read only the final agent message, not tool output or intermediate messages."""
    final = None
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                event = json.loads(line)
                item = event.get("item", {})
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    final = item.get("text")
        if not isinstance(final, str):
            return None
        return validate_handoff(json.loads(final))
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None
