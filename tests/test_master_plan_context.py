from dataclasses import replace
import hashlib
import json

import pytest

from agentflow.planning import PlanError
from agentflow.planning.master_plan import inspect_master_plan
from agentflow.planning.task_queue import prepare_active_milestone
from agentflow.apl.codex_planner import CodexCLIPlannerAdapter
from agentflow.apl.initial_planning import create_context
from test_apl_initial_planning import workspace, proposed
from test_task_queue import queue, write_queue


def test_large_plan_has_streamed_digest_and_compact_index(tmp_path):
    path = tmp_path / "IMPLEMENTATION_PLAN.md"
    raw = ("# Master\n## Architecture\n" + "large body without prompt inflation\n" * 20000 + "## Contract\nSQLite invariant\n").encode()
    path.write_bytes(raw)
    reference = inspect_master_plan(tmp_path, path, context_bytes=2048)
    assert reference.sha256 == "sha256:" + hashlib.sha256(raw).hexdigest()
    context = reference.initial_context()
    assert len(context.encode()) < 2048
    assert "large body" not in context
    assert reference.sections[1].end_line == 20002
    assert json.loads(context)["toc"][1]["start_line"] == 2
    assert reference.read_lines(20004, 20004)["text"] == "SQLite invariant\n"
    assert reference.read_lines(1, reference.line_count)["error"] == "range_exceeds_context_budget"


def test_selected_sections_include_children_without_duplicate_text_and_ignore_fences(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("# Master\n## Architecture\nOnly relevant\n### Contract\nSQLite\n```md\n## Fake\n```\n## Other\nUnrelated\n")
    reference = inspect_master_plan(tmp_path, path)
    payload = json.loads(reference.initial_context(["## Architecture", "### Contract"]))
    assert len(payload["excerpts"]) == 1
    assert payload["excerpts"][0]["text"].count("SQLite") == 1
    assert "Unrelated" not in payload["excerpts"][0]["text"]
    assert "## Fake" not in [section.heading for section in reference.sections]


def test_ambiguous_missing_selector_and_drift_are_errors(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("# A\n## Contract\nFirst\n# B\n## Contract\nSecond\n")
    reference = inspect_master_plan(tmp_path, path)
    with pytest.raises(PlanError, match="ambígua"):
        reference.initial_context(["Contract"])
    with pytest.raises(PlanError):
        reference.initial_context(["Missing"])
    assert "Second" in reference.initial_context(["B / Contract"])
    assert "First" in reference.initial_context(["L2"])
    path.write_text("changed")
    with pytest.raises(PlanError, match="mudou"):
        reference.read_lines(1, 2)


def test_index_pagination_covers_every_header_without_oversized_prompt(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("".join(f"## Section {index}\nContent\n" for index in range(1000)))
    reference = inspect_master_plan(tmp_path, path, context_bytes=2048)
    cursor, found = 0, []
    while cursor is not None:
        page = reference.index_page(cursor)
        assert len(json.dumps(page).encode()) <= 2048
        found.extend(item["id"] for item in page["toc"])
        assert page["next_cursor"] is None or page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert found == [section.section_id for section in reference.sections]


def test_queue_selectors_and_budget_fallback_do_not_reject_plan(tmp_path):
    root = workspace(tmp_path)
    data = queue()
    data["plan_context_bytes"] = 2048
    data["tasks"][0]["plan_sections"] = ["## Architecture"]
    write_queue(root, data)
    (root / "IMPLEMENTATION_PLAN.md").write_text("# Master\n## Architecture\n" + "Long paragraph\n" * 10000)
    active = prepare_active_milestone(root, data)
    assert "toc" in json.loads(active.master_plan)
    assert active.source["milestone"]["plan_sections"] == ["## Architecture"]


def test_mutation_during_indexing_is_detected(tmp_path, monkeypatch):
    import agentflow.planning.master_plan as module
    path = tmp_path / "plan.md"
    path.write_text("# Original\n")
    original = module._digest
    def mutate(candidate):
        value = original(candidate)
        candidate.write_text("# Replacement\n")
        return value
    monkeypatch.setattr(module, "_digest", mutate)
    with pytest.raises(PlanError, match="alterado"):
        inspect_master_plan(tmp_path, path)


def test_progressive_read_works_without_mounting_original_workspace(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    path = root / "IMPLEMENTATION_PLAN.md"
    path.write_text("# Master\n## Contract\nSQLite is authoritative\n")
    reference = inspect_master_plan(root, path)
    context = replace(create_context(root, "Implement requested behavior"),
                      master_reference=reference, master_plan=reference.initial_context())
    planner = CodexCLIPlannerAdapter(root / ".agentflow" / "state.sqlite3", enabled=True,
                                    agent_adapter=object(), runtime=object())
    prompts = []
    def invoke(prompt, task):
        prompts.append(prompt)
        if len(prompts) == 1:
            assert "SQLite is authoritative" not in prompt
            return json.dumps({"plan_read": {"sha256": reference.sha256, "start_line": 2, "end_line": 3}})
        assert "SQLite is authoritative" in prompt
        return json.dumps(proposed(context))
    monkeypatch.setattr(planner, "_invoke_structured", invoke)
    plan = planner.plan_initial(context)
    assert plan["tasks"][0]["id"] == "implementation"
    assert len(prompts) == 2


def test_stale_request_and_unbounded_retrieval_loop_block_planning(tmp_path, monkeypatch):
    root = workspace(tmp_path)
    path = root / "plan.md"
    path.write_text("# Master\n")
    reference = inspect_master_plan(root, path)
    context = replace(create_context(root, "Feature"), master_reference=reference)
    planner = CodexCLIPlannerAdapter(root / "state.sqlite3", enabled=True, agent_adapter=object(),
                                    runtime=object(), max_plan_reads=1)
    monkeypatch.setattr(planner, "_invoke_structured", lambda *_: json.dumps({"plan_read": {
        "sha256": "wrong", "start_line": 1, "end_line": 1}}))
    with pytest.raises(ValueError, match="digest"):
        planner.plan_initial(context)
    monkeypatch.setattr(planner, "_invoke_structured", lambda *_: json.dumps({"plan_index": {
        "sha256": reference.sha256, "cursor": 0}}))
    with pytest.raises(RuntimeError, match="round limit"):
        planner.plan_initial(context)


def test_progressive_protocol_runs_in_empty_synthetic_workspace(tmp_path):
    from pathlib import Path
    from agentflow.agents import AgentResult, ProcessResult, ProcessSpec
    root = workspace(tmp_path)
    path = root / "IMPLEMENTATION_PLAN.md"
    path.write_text("# Master\nSQLite contract\n")
    reference = inspect_master_plan(root, path)
    context = replace(create_context(root, "Feature"), master_reference=reference,
                      master_plan=reference.initial_context())
    prompts = []

    class Adapter:
        def prepare(self, request):
            assert request.workspace != root
            assert (request.workspace / ".git").is_dir()
            assert not (request.workspace / path.name).exists()
            prompts.append(request.instruction)
            return ProcessSpec(Path("fake-cli"), (), request.workspace, request.instruction, (), "fake", "1", "unknown", "never")
        def interpret(self, result):
            return AgentResult("process_succeeded", "done")

    class Runtime:
        def run(self, *args):
            response = ({"plan_read": {"sha256": reference.sha256, "start_line": 1, "end_line": 2}}
                        if len(prompts) == 1 else proposed(context))
            out = tmp_path / f"response-{len(prompts)}.jsonl"
            out.write_text(json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": json.dumps(response)}}))
            err = tmp_path / "stderr"
            err.write_text("")
            return ProcessResult("process_succeeded", 0, out, err, "a", "b", out.stat().st_size, 0, "done")

    planner = CodexCLIPlannerAdapter(root / "state.sqlite3", enabled=True, agent_adapter=Adapter(), runtime=Runtime())
    assert planner.plan_initial(context)["tasks"]
    assert "SQLite contract" not in prompts[0]
    assert "SQLite contract" in prompts[1]
