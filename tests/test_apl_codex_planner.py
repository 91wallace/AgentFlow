import json
from pathlib import Path
from threading import Event

import pytest

from agentflow.agents import AgentResult, ProcessResult, ProcessSpec
from agentflow.apl import (CodexCLIPlannerAdapter, EvidenceReference, JobProposal,
                           PlannerUnavailable, ReplanContext, RemainingLimits,
                           ValidatedResult)


def context(objective="synthetic objective"):
    job = JobProposal("first", "synthetic task", (), ("task:first",), ("done",))
    evidence = EvidenceReference(3, "first", "done")
    return ReplanContext(objective, ("task:first",), (job,), "revision-1", (),
                         (ValidatedResult("first", "attempt-1", "succeeded", (evidence,)),),
                         (evidence,), (), RemainingLimits(2, 3, 30, 0), 4)


def valid_mapping():
    return {"proposal_id": "ignored-by-service", "source_revision_id": "revision-1",
            "source_cycle_revision": 4, "rationale": "continue synthetic plan",
            "changes": [{"action": "add", "task": {
                "job_id": "second", "instruction": "synthetic follow-up",
                "depends_on": ["first"], "required_scope": ["task:first"],
                "acceptance_criteria": ["done"]}}],
            "completion_condition": "second has verified evidence",
            "evidence_used": [3], "resolves_blockers": [], "schema_version": 1}


class FakeAdapter:
    def prepare(self, request):
        self.request = request
        return ProcessSpec(Path("codex"), (), request.workspace, request.instruction,
                           (), "fake", "test", "read-only", "never")

    def interpret(self, result):
        return AgentResult(result.status, "turn.completed")


class FakeRuntime:
    def __init__(self, tmp_path, text=None, status="process_succeeded"):
        self.tmp_path, self.text, self.status = tmp_path, text, status
        self.policy = None

    def run(self, spec, attempt_id, policy, on_started, cancel):
        import subprocess
        self.policy = policy
        self.workspace_root = subprocess.run(
            ["git", "-C", str(spec.cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.workspace_remotes = subprocess.run(
            ["git", "-C", str(spec.cwd), "remote", "-v"],
            capture_output=True, text=True, check=True).stdout.strip()
        output = self.tmp_path / "stdout.jsonl"
        content = "" if self.text is None else json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": self.text}}) + "\n"
        output.write_text(content, encoding="utf-8")
        stderr = self.tmp_path / "stderr.log"
        stderr.write_text("", encoding="utf-8")
        return ProcessResult(self.status, 0 if self.status == "process_succeeded" else None,
                             output, stderr, "out", "err", len(content.encode()), 0, self.status)


def adapter(tmp_path, *, text=None, status="process_succeeded", cancel=None, **options):
    return CodexCLIPlannerAdapter(tmp_path / "state.sqlite3", enabled=True,
                                  agent_adapter=FakeAdapter(),
                                  runtime=FakeRuntime(tmp_path, text, status),
                                  cancel=cancel, **options)


def test_codex_planner_is_explicit_opt_in(tmp_path):
    with pytest.raises(PlannerUnavailable, match="opt-in"):
        CodexCLIPlannerAdapter(tmp_path / "state.sqlite3")


def test_synthetic_workspace_is_a_real_git_root_without_remotes(tmp_path):
    workspace = tmp_path / "synthetic"
    workspace.mkdir()
    CodexCLIPlannerAdapter._initialize_synthetic_git_workspace(workspace)
    import subprocess
    top = subprocess.run(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=True).stdout.strip()
    remotes = subprocess.run(["git", "-C", str(workspace), "remote", "-v"],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert Path(top).resolve() == workspace.resolve()
    assert remotes == ""


def test_codex_planner_decodes_structured_proposal_and_uses_read_only_request(tmp_path):
    provider = adapter(tmp_path, text=json.dumps(valid_mapping()))
    proposal = provider.propose(context())
    assert proposal.source_revision_id == "revision-1"
    assert proposal.changes[0].task.job_id == "second"
    assert provider.agent_adapter.request.policy.sandbox == "read-only"
    assert provider.agent_adapter.request.policy.approval == "never"
    assert Path(provider.runtime.workspace_root).resolve() == provider.agent_adapter.request.workspace.resolve()
    assert provider.runtime.workspace_remotes == ""


@pytest.mark.parametrize("text", ["not json", json.dumps({"command": "do something"})])
def test_codex_planner_rejects_malformed_or_command_shaped_output(tmp_path, text):
    with pytest.raises(ValueError):
        adapter(tmp_path, text=text).propose(context())


def test_codex_planner_enforces_input_output_timeout_and_cancel_limits(tmp_path):
    with pytest.raises(ValueError, match="input exceeds configured byte limit"):
        adapter(tmp_path, text=json.dumps(valid_mapping()), max_input_bytes=1024).propose(
            context("x" * 2000))
    oversized = valid_mapping()
    oversized["rationale"] = "x" * 2000
    with pytest.raises(ValueError, match="output exceeded configured byte limit"):
        adapter(tmp_path, text=json.dumps(oversized), max_output_bytes=1024).propose(context())
    with pytest.raises(TimeoutError):
        adapter(tmp_path, status="timed_out").propose(context())
    cancel = Event()
    cancel.set()
    with pytest.raises(InterruptedError, match="cancelled"):
        adapter(tmp_path, text=json.dumps(valid_mapping()), cancel=cancel).propose(context())


def test_codex_planner_rejects_stale_revision_and_failed_process(tmp_path):
    payload = valid_mapping()
    payload["source_cycle_revision"] = 3
    with pytest.raises(ValueError, match="stale"):
        adapter(tmp_path, text=json.dumps(payload)).propose(context())
    with pytest.raises(PlannerUnavailable, match="process failed"):
        adapter(tmp_path, status="process_failed").propose(context())
