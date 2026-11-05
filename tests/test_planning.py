from pathlib import Path

import pytest

from agentflow.planning import PlanError, load_plan


SPEC = '''schema_version = 1
project_id = "sample"
title = "Sample"
objective = "Deliver sample"
[[acceptance_criteria]]
id = "spec-ok"
description = "Specification is reviewed"
'''

TASKS = '''schema_version = 1
[[tasks]]
id = "second"
title = "Second"
description = "Implement second"
depends_on = ["first"]
[[tasks.acceptance_criteria]]
id = "ok"
description = "Second works"
[[tasks]]
id = "first"
title = "First"
description = "Implement first"
[[tasks.acceptance_criteria]]
id = "ok"
description = "First works"
'''


def files(path: Path, tasks: str = TASKS, spec: str = SPEC) -> Path:
    path.mkdir()
    (path / "spec.toml").write_text(spec, encoding="utf-8")
    (path / "tasks.toml").write_text(tasks, encoding="utf-8")
    return path


def test_plan_is_canonical_and_ordered(tmp_path):
    first = load_plan(files(tmp_path / "one"))
    sections = TASKS.split("[[tasks]]")
    reordered = sections[0] + "[[tasks]]" + sections[2] + "[[tasks]]" + sections[1]
    second = load_plan(files(tmp_path / "two", reordered + "\n"))
    assert first == second
    assert first.order == ("first", "second")


@pytest.mark.parametrize("change, message", [
    (lambda text: text.replace('depends_on = ["first"]', 'depends_on = ["missing"]'), "referência ausente"),
    (lambda text: text.replace('depends_on = ["first"]', 'depends_on = ["second"]'), "ciclo"),
    (lambda text: text.replace('description = "First works"', 'description = "First works"\n[[tasks]]\nid = "first"\ntitle = "Duplicate"\ndescription = "Duplicate"\n[[tasks.acceptance_criteria]]\nid = "ok"\ndescription = "Duplicate"'), "duplicado"),
    (lambda text: text.replace('schema_version = 1', 'schema_version = 2'), "versão 1"),
    (lambda text: text.replace('id = "first"', 'id = "Bad ID"'), "texto ID"),
    (lambda text: text.replace('description = "First works"', 'description = ""'), "não vazio"),
])
def test_rejects_invalid_task_files(tmp_path, change, message):
    with pytest.raises(PlanError, match=message):
        load_plan(files(tmp_path / "project", change(TASKS)))


def test_rejects_invalid_spec_and_preserves_files(tmp_path):
    directory = files(tmp_path / "project", spec=SPEC.replace("schema_version = 1", "schema_version = 3"))
    before = (directory / "spec.toml").read_bytes()
    with pytest.raises(PlanError, match="versão 1"):
        load_plan(directory)
    assert (directory / "spec.toml").read_bytes() == before
