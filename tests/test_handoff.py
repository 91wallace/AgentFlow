import json

import pytest

from agentflow.apl.contracts import ExecutionOutcome
from agentflow.planning.handoff import (
    dependency_context, extract_codex_handoff, validate_handoff,
)
from agentflow.planning.prompts import next_task_prompt


def handoff():
    return {"ok": True, "files": ["src/foo.py", "tests/test_foo.py"],
            "exports": ["parse_file", "SymbolNode"], "decisions": ["sqlite v4 sem nova tabela"],
            "verified": ["unittest: 83 ok"], "next_notes": ["Reutilizar SymbolNode.span"], "blocker": None}


def test_large_handoff_is_preserved_without_truncation(tmp_path):
    value = handoff()
    value["next_notes"] = ["SymbolNode.span " * 10000]
    path = tmp_path / "output.jsonl"
    path.write_text(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": json.dumps(value)}}), encoding="utf-8")
    assert extract_codex_handoff(path) == value
    context = dependency_context(("parent",), {"parent": ExecutionOutcome("succeeded", "done", handoff=value)})
    assert value["next_notes"][0] in next_task_prompt("next", "Continue", ("src",), context)


def test_large_backlog_is_read_without_json_byte_limit(tmp_path):
    from agentflow.planning.task_source import read_task_source, task_source_objective
    value = {"tasks": [{"goal": "Build the feature", "notes": "context " * 20000}]}
    path = tmp_path / "TASKS.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert read_task_source(path) == value
    assert value["tasks"][0]["notes"] in task_source_objective(value)


def test_direct_dependencies_only_no_transitive_history_and_minified_context():
    outcomes = {key: ExecutionOutcome("succeeded", "done", handoff=handoff())
                for key in ("parent", "unrelated", "grandparent")}
    context = dependency_context(("parent",), outcomes)
    prompt = next_task_prompt("next", "Implement next behavior", ("src",), context)
    assert '"task_id":"parent"' in prompt
    assert "unrelated" not in prompt and "grandparent" not in prompt
    assert '"verified":["unittest: 83 ok"]' in prompt
    assert "gates" not in handoff()
    assert "como prova de aceite" in prompt


@pytest.mark.parametrize("mode", ["missing", "bad", "failed"])
def test_missing_or_invalid_dependency_context_is_not_forwarded(mode):
    value = handoff()
    if mode == "oversize":
        value["next_notes"] = ["á" * MAX_HANDOFF_BYTES]
    elif mode == "bad":
        value["verified"] = True
    outcomes = {"parent": ExecutionOutcome("failed" if mode == "failed" else "succeeded", "done",
                                           handoff=None if mode == "missing" else value)}
    assert dependency_context(("parent",), outcomes) == []


def test_final_message_extraction_excludes_tools_intermediate_and_truncation(tmp_path):
    path = tmp_path / "stdout.jsonl"
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(handoff())}},
        {"type": "item.completed", "item": {"type": "command_execution", "text": json.dumps(handoff())}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "not valid JSON"}},
        {"type": "turn.completed"},
    ]
    path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    assert extract_codex_handoff(path) is None
    events[2]["item"]["text"] = json.dumps(handoff(), separators=(",", ":"))
    path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    assert extract_codex_handoff(path) == handoff()
    events[2]["item"]["text"] = " " * (20000)
    path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    assert extract_codex_handoff(path) is None


def test_outcome_survives_sqlite_reopen_and_checkpoint_recovery(tmp_path):
    from test_apl_authorized_execution import _setup, SyntheticExecutor
    from agentflow.apl import SQLiteCycleStore, SQLiteAuthorizedExecutionStore

    outcome = ExecutionOutcome("succeeded", "done", ({"passed": True},), handoff=handoff())
    path, cycles, runs, resolver, controller, snapshot = _setup(
        tmp_path, executor=SyntheticExecutor(outcomes=[outcome, outcome]))
    result = controller.start(snapshot.cycle_id, resolver.grant.grant_id)
    assert result["status"] == "completed"
    loaded_run = SQLiteAuthorizedExecutionStore(str(path), initialize=False).load(result["run_id"])
    assert json.loads(loaded_run["tasks"][0]["outcome_json"])["handoff"] == handoff()
    loaded = SQLiteCycleStore(path, initialize=False).load(snapshot.cycle_id)
    assert dict(loaded.outcomes)["first"].handoff == handoff()
    assert dependency_context(("first",), dict(loaded.outcomes))[0]["handoff"] == handoff()


def test_self_reported_ok_and_verified_do_not_bypass_independent_gate(tmp_path):
    from test_apl_authorized_execution import _setup, SyntheticExecutor

    class Reject:
        def validate(self, *args):
            return False

    outcome = ExecutionOutcome("succeeded", "claims success", ({"passed": True},), handoff=handoff())
    path, cycles, runs, resolver, controller, snapshot = _setup(
        tmp_path, executor=SyntheticExecutor(outcomes=[outcome]))
    # The controller's injected gate owns validation; handoff never participates.
    controller.evidence = Reject()
    from agentflow.apl.authorized_execution import AuthorizedExecutionError
    with pytest.raises(AuthorizedExecutionError):
        controller.start(snapshot.cycle_id, resolver.grant.grant_id)
    assert not cycles.load(snapshot.cycle_id).outcomes


def test_legacy_snapshot_without_handoff_remains_readable(tmp_path):
    from agentflow.apl.persistence import SQLiteCycleStore
    from test_apl_authorized_execution import _snapshot
    from dataclasses import replace

    snapshot = replace(_snapshot(), outcomes=(("first", ExecutionOutcome("succeeded", "old")),))
    encoded = SQLiteCycleStore._encode(snapshot)
    data = json.loads(encoded)
    data["outcomes"][0][1].pop("handoff")
    assert SQLiteCycleStore._decode(json.dumps(data)).outcomes[0][1].handoff is None
