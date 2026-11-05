from pathlib import Path

import pytest

from agentflow.planning import PlanError, digest, load_plan


LEGACY_SPEC = '''schema_version = 1
project_id = "demo"
title = "Demo"
objective = "Validate a demo project"
[[acceptance_criteria]]
id = "spec-ok"
description = "Spec is reviewed"
'''

LEGACY_TASKS = '''schema_version = 2
[[tasks]]
id = "build"
title = "Build"
description = "Build project"
versioned_paths = []
[[tasks.acceptance_criteria]]
id = "built"
description = "Build succeeds"
'''

SPEC_V2 = '''schema_version = 2
project_id = "demo"
title = "Demo"
objective = "Validate a demo project"
[[acceptance_criteria]]
id = "spec-ok"
description = "Spec is reviewed"
[validation]
[[validation.commands]]
id = "pytest"
platform = "linux"
executable = "/opt/venv/bin/python"
sha256 = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
arguments = ["-m", "pytest", "{snapshot_dir}"]
working_directory = "snapshot"
version = "3.13.1"
[[validation.commands]]
id = "pytest"
platform = "windows"
executable = "C:/venv/Scripts/python.exe"
sha256 = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
arguments = ["-m", "pytest", "{snapshot_dir}"]
working_directory = "snapshot"
version = "3.13.1"
'''

TASKS_V3 = '''schema_version = 3
[[tasks]]
id = "build"
title = "Build"
description = "Build project"
versioned_paths = []
[[tasks.acceptance_criteria]]
id = "built"
description = "Build succeeds"
[[tasks.gates]]
id = "package"
version = 1
criterion_ids = ["built"]
command_id = "pytest"
[[tasks.gates]]
id = "unit-tests"
version = 1
criterion_ids = ["built"]
command_id = "pytest"
depends_on = ["package"]
timeout_seconds = 600
output_limit_bytes = 8388608
'''


def write_project(tmp_path: Path, spec: str, tasks: str) -> Path:
    directory = tmp_path / "project"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "spec.toml").write_text(spec, encoding="utf-8")
    (directory / "tasks.toml").write_text(tasks, encoding="utf-8")
    return directory


def test_legacy_toml_digests_remain_unchanged_with_empty_gate_fields(tmp_path):
    project = write_project(tmp_path, LEGACY_SPEC, LEGACY_TASKS)
    plan = load_plan(project)
    expected_spec = digest({
        "project_id": "demo", "title": "Demo", "objective": "Validate a demo project",
        "acceptance_criteria": [{"id": "spec-ok", "description": "Spec is reviewed"}],
    })
    expected_tasks = digest([{
        "id": "build", "title": "Build", "description": "Build project",
        "acceptance_criteria": [{"id": "built", "description": "Build succeeds"}],
        "depends_on": [],
    }])
    assert plan.spec_digest == expected_spec
    assert plan.tasks_digest == expected_tasks
    assert plan.gate_order == ()


def test_versioned_allowlist_gates_and_dag_are_loaded_deterministically(tmp_path):
    plan = load_plan(write_project(tmp_path, SPEC_V2, TASKS_V3))
    assert plan.spec_schema_version == 2
    assert plan.tasks_schema_version == 3
    assert plan.gate_order == ("package", "unit-tests")
    assert {command.platform for command in plan.specification.validation_commands} == {"linux", "windows"}
    assert plan.spec_digest.startswith("sha256:")
    assert plan.tasks_digest.startswith("sha256:")


@pytest.mark.parametrize("old, new, message", [
    ('depends_on = ["package"]', 'depends_on = ["missing"]', "referência ausente"),
    ('command_id = "pytest"', 'command_id = "unlisted"', "não está na allowlist"),
    ('criterion_ids = ["built"]', 'criterion_ids = ["missing"]', "referência ausente"),
    ('timeout_seconds = 600', 'timeout_seconds = 3601', "3600"),
])
def test_rejects_invalid_gate_or_allowlist_definition(tmp_path, old, new, message):
    tasks = TASKS_V3.replace(old, new)
    with pytest.raises(PlanError, match=message):
        load_plan(write_project(tmp_path, SPEC_V2, tasks))


def test_rejects_gate_dependency_cycle(tmp_path):
    tasks = TASKS_V3.replace('id = "package"\nversion = 1',
                             'id = "package"\nversion = 1\ndepends_on = ["unit-tests"]')
    with pytest.raises(PlanError, match="ciclo de gates"):
        load_plan(write_project(tmp_path, SPEC_V2, tasks))


def test_gate_ids_are_project_unique_and_dependencies_cannot_cross_tasks(tmp_path):
    duplicate = TASKS_V3 + '''\n[[tasks]]
id = "other"
title = "Other"
description = "Other task"
versioned_paths = []
[[tasks.acceptance_criteria]]
id = "other-ok"
description = "Other works"
[[tasks.gates]]
id = "package"
version = 1
criterion_ids = ["other-ok"]
command_id = "pytest"
'''
    with pytest.raises(PlanError, match="IDs duplicados no projeto"):
        load_plan(write_project(tmp_path / "duplicate", SPEC_V2, duplicate))

    cross_task = TASKS_V3.replace('depends_on = ["package"]', 'depends_on = []')
    cross_task += '''\n[[tasks]]
id = "other"
title = "Other"
description = "Other task"
versioned_paths = []
[[tasks.acceptance_criteria]]
id = "other-ok"
description = "Other works"
[[tasks.gates]]
id = "other-gate"
version = 1
criterion_ids = ["other-ok"]
command_id = "pytest"
depends_on = ["package"]
'''
    with pytest.raises(PlanError, match="referência cruzada"):
        load_plan(write_project(tmp_path / "cross", SPEC_V2, cross_task))


@pytest.mark.parametrize("arguments, message", [
    ('arguments = ["-c", "print(1)"]', "shell"),
    ('arguments = ["{arbitrary}"]', "placeholder não permitido"),
])
def test_rejects_inline_execution_and_unapproved_placeholders(tmp_path, arguments, message):
    spec = SPEC_V2.replace('arguments = ["-m", "pytest", "{snapshot_dir}"]', arguments)
    with pytest.raises(PlanError, match=message):
        load_plan(write_project(tmp_path, spec, TASKS_V3))


def test_rejects_gates_in_legacy_schema_and_requires_explicit_empty_list_in_v3(tmp_path):
    with pytest.raises(PlanError, match="exige schema_version=3"):
        load_plan(write_project(tmp_path, SPEC_V2, TASKS_V3.replace("schema_version = 3", "schema_version = 2")))

    no_gates = TASKS_V3[:TASKS_V3.index("[[tasks.gates]]")]
    no_gates = no_gates.replace('versioned_paths = []\n[[tasks.acceptance_criteria]]',
                                'versioned_paths = []\ngates = []\n[[tasks.acceptance_criteria]]')
    plan = load_plan(write_project(tmp_path / "empty", SPEC_V2, no_gates))
    assert plan.tasks[0].gates == ()
    with pytest.raises(PlanError, match="gates: obrigatório"):
        load_plan(write_project(tmp_path / "missing", SPEC_V2,
                                 no_gates.replace("gates = []\n", "")))


def test_spec_v2_requires_allowlist_table_and_unique_command_platform(tmp_path):
    with pytest.raises(PlanError, match="allowlist obrigatória"):
        load_plan(write_project(tmp_path, SPEC_V2.split("[validation]")[0], TASKS_V3))
    duplicate = SPEC_V2 + SPEC_V2[SPEC_V2.index("[[validation.commands]]"):]
    with pytest.raises(PlanError, match="duplicado"):
        load_plan(write_project(tmp_path, duplicate, TASKS_V3))
