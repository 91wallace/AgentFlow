import hashlib
import base64
import json
import subprocess
from pathlib import Path

import pytest

from agentflow.apl.initial_planning import (
    InitialPlanningError, create_context, load_plan_manifest, plan_toml_documents,
    prepare_objective_plan, validate_initial_plan,
)
from agentflow.planning import load_plan


def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_existing.py").write_text("def test_marker():\n    assert True\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    return root


def proposed(context):
    paths = ["src", "tests"]
    return {
        "schema_version": 1,
        "title": "Add the requested behavior",
        "rationale": "The workspace is a Python project with pytest available.",
        "completion_condition": "The project pytest suite passes.",
        "tasks": [
            {"id": "implementation", "title": "Implement behavior",
             "description": "Implement the behavior and add focused tests.",
             "depends_on": [], "paths": paths,
             "acceptance_criteria": [{"id": "behavior-tests",
                                      "description": "Focused tests verify the behavior."}],
             "artifact_checks": [{"criterion_id": "behavior-tests",
                                  "path": "tests/test_requested.py", "kind": "exists", "expected": ""}]},
            {"id": "verification", "title": "Verify behavior",
             "description": "Run and review the focused project tests.",
             "depends_on": ["implementation"], "paths": ["tests"],
             "acceptance_criteria": [{"id": "suite-passes",
                                      "description": "The project test suite exits successfully."}],
             "artifact_checks": [{"criterion_id": "suite-passes",
                                  "path": "tests/test_requested.py", "kind": "exists", "expected": ""}]},
        ],
    }


class Planner:
    def __init__(self):
        self.calls = 0

    def plan_initial(self, context):
        self.calls += 1
        return validate_initial_plan(proposed(context), context)


def test_initial_plan_generates_loadable_plan_with_dependent_tasks(tmp_path):
    root = workspace(tmp_path)
    context = create_context(root, "Add a useful feature")
    checked = validate_initial_plan(proposed(context), context)
    plan = prepare_objective_plan(root, context.objective, "req-1", Planner())
    loaded, plan_dir, _ = plan
    assert loaded.tasks[1].depends_on == ("implementation",)
    assert loaded.tasks[0].gates[0].command_id == "verify-implementation"
    assert "pyproject.toml" in loaded.tasks[0].versioned_paths
    assert plan_dir == root / ".agentflow"
    assert (plan_dir / "spec.toml").is_file()
    assert (plan_dir / "tasks.toml").is_file()
    assert checked["title"] == loaded.specification.title


def test_existing_workspace_tomls_are_preserved_for_new_objective(tmp_path):
    root = workspace(tmp_path)
    original_spec = '[spec]\n'
    original_tasks = '[tasks]\n'
    control = root / ".agentflow"
    control.mkdir()
    (control / "spec.toml").write_text(original_spec, encoding="utf-8")
    (control / "tasks.toml").write_text(original_tasks, encoding="utf-8")
    context = create_context(root, "A different objective")
    plan, directory, _ = prepare_objective_plan(root, context.objective, "req-new", Planner())
    assert plan.specification.objective == context.objective
    assert directory == control / "objective-plans" / "req-new"
    assert (control / "spec.toml").read_text(encoding="utf-8") == original_spec
    assert (control / "tasks.toml").read_text(encoding="utf-8") == original_tasks


def test_plan_generation_is_recovered_without_second_planner_call(tmp_path):
    root = workspace(tmp_path)
    context = create_context(root, "Recover interrupted planning")
    planner = Planner()
    first, directory, _ = prepare_objective_plan(root, context.objective, "req-recover", planner)
    manifest = load_plan_manifest(root, "req-recover")
    assert manifest["status"] == "planned"
    recovered, recovered_dir, _ = prepare_objective_plan(
        root, context.objective, "req-recover", planner)
    assert recovered.spec_digest == first.spec_digest
    assert recovered_dir == directory
    assert planner.calls == 1


def test_complete_staging_plan_is_published_after_interruption_without_replanning(tmp_path):
    root = workspace(tmp_path)
    context = create_context(root, "Recover staged planning")
    data = validate_initial_plan(proposed(context), context)
    spec, tasks = plan_toml_documents(data, context)
    target = root / ".agentflow"
    target.mkdir()
    stage_name = ".initial-plan-stage-" + hashlib.sha256(
        (context.objective + context.workspace_digest).encode()).hexdigest()[:12]
    stage = target / stage_name
    stage.mkdir()
    (stage / "spec.toml").write_text(spec, encoding="utf-8")
    (stage / "tasks.toml").write_text(tasks, encoding="utf-8")
    planner = Planner()
    plan, directory, _ = prepare_objective_plan(
        root, context.objective, "req-staged", planner)
    assert plan.specification.objective == context.objective
    assert directory == target
    assert planner.calls == 0


def test_planner_output_outside_detected_scope_is_rejected(tmp_path):
    root = workspace(tmp_path)
    context = create_context(root, "Implement local change")
    mapping = proposed(context)
    mapping["tasks"][0]["paths"] = ["outside-workspace"]
    with pytest.raises(InitialPlanningError, match="scope"):
        validate_initial_plan(mapping, context)


def test_planner_dependency_cycle_is_rejected_before_persistence(tmp_path):
    root = workspace(tmp_path)
    context = create_context(root, "Implement a dependency cycle")
    mapping = proposed(context)
    mapping["tasks"][0]["depends_on"] = ["verification"]
    with pytest.raises(InitialPlanningError, match="dependency_cycle"):
        validate_initial_plan(mapping, context)


def test_project_without_framework_uses_objective_artifact_checks(tmp_path):
    root = tmp_path / "unsupported"
    root.mkdir()
    (root / "README.md").write_text("Generated output: report.json\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    context = create_context(root, "Create a report")
    assert context.strategy.checks == ()
    mapping = {
        "schema_version": 1, "title": "Create a report", "rationale": "No framework was detected.",
        "completion_condition": "The README describes the report.",
        "tasks": [{"id": "write-report", "title": "Write report", "description": "Create output.",
                   "depends_on": [], "paths": ["README.md"],
                   "acceptance_criteria": [{"id": "report-created", "description": "Report exists."}],
                   "artifact_checks": [{"criterion_id": "report-created", "path": "README.md",
                                        "kind": "contains", "expected": "report.json"}]}],
    }
    assert validate_initial_plan(mapping, context)["tasks"][0]["artifact_checks"][0]["kind"] == "contains"


def test_tests_directory_without_test_module_does_not_claim_a_framework_gate(tmp_path):
    root = tmp_path / "empty-tests"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "fixtures.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    context = create_context(root, "Add an objective")
    assert context.strategy.checks == ()


def test_javascript_next_scripts_are_discovered_without_running_them(tmp_path, monkeypatch):
    import sys
    from agentflow.apl import initial_planning

    root = tmp_path / "web"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({
        "name": "web", "dependencies": {"next": "1", "react": "1"},
        "scripts": {"test": "touch should-not-run", "lint": "exit 0",
                    "typecheck": "exit 0", "build": "exit 0", "unknown": "exit 1"},
    }), encoding="utf-8")
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "page.test.tsx").write_text("// synthetic test file", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    monkeypatch.setattr(initial_planning, "_npm_runtime",
                        lambda _root: (Path(sys.executable), (), "npm 10.synthetic"))
    context = create_context(root, "Add a page")
    assert {"node", "react", "next", "typescript"} <= set(context.technologies)
    assert context.to_mapping()["validation"]["objective_artifacts_required"] is True
    assert {item.label for item in context.strategy.checks} == {
        "npm run test", "npm run lint", "npm run typecheck", "npm run build"}
    assert not (root / "should-not-run").exists()
    task = {"id": "page", "title": "Build page", "description": "Implement and test page.",
            "depends_on": [], "paths": ["src"],
            "acceptance_criteria": [{"id": "page-test", "description": "Page behavior passes tests."}],
            "artifact_checks": [{"criterion_id": "page-test", "path": "src/page.test.tsx",
                                 "kind": "exists", "expected": ""}]}
    mapping = {"schema_version": 1, "title": "Add a page", "rationale": "Next project",
               "completion_condition": "Configured project checks pass.", "tasks": [task]}
    generated = validate_initial_plan(mapping, context)
    spec, _ = plan_toml_documents(generated, context)
    command_args = [line for line in spec.splitlines() if line.startswith('arguments = ')]
    assert len(command_args) == 1 and "--checks-b64" in command_args[0]
    encoded = command_args[0].split('"--checks-b64", "', 1)[1].split('"', 1)[0]
    checks = json.loads(base64.urlsafe_b64decode(encoded))
    assert {"npm run test", "npm run build"} <= {item["label"] for item in checks}
    assert "touch should-not-run" not in spec

    class SyntheticPlanner:
        def plan_initial(self, fresh_context):
            return validate_initial_plan(mapping, fresh_context)

    plan, plan_directory, _ = initial_planning.prepare_objective_plan(
        root, "Add a page", "web-plan", SyntheticPlanner())
    assert plan.specification.objective == "Add a page"
    assert plan_directory == root / ".agentflow"


def test_objective_criterion_requires_specific_evidence_even_with_project_tests(tmp_path):
    root = workspace(tmp_path)
    context = create_context(root, "Add a useful feature")
    mapping = proposed(context)
    mapping["tasks"][0]["artifact_checks"] = []
    with pytest.raises(InitialPlanningError, match="objective_artifacts_required"):
        validate_initial_plan(mapping, context)


def test_new_objective_plan_toml_does_not_accept_model_commands(tmp_path):
    root = workspace(tmp_path)
    context = create_context(root, "Write a feature")
    data = validate_initial_plan(proposed(context), context)
    spec, tasks = plan_toml_documents(data, context)
    assert "agentflow.validation.objective_gate" in spec
    assert "pytest" in spec
    assert "shell" not in spec.lower()
    assert "depends_on = [\"implementation\"]" in tasks
