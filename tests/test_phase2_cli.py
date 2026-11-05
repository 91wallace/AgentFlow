import subprocess

from agentflow.cli import main
from agentflow.planning import load_plan
from agentflow.state.sqlite import SQLiteStore
from test_planning import SPEC, TASKS


def test_plan_and_state_inspection_are_read_only(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    directory = root / ".agentflow"
    directory.mkdir()
    (directory / "spec.toml").write_text(SPEC, encoding="utf-8")
    (directory / "tasks.toml").write_text(TASKS, encoding="utf-8")
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert main(["plan", "--path", str(root)]) == 0
    assert "first, second" in capsys.readouterr().out
    assert main(["state", "--path", str(root)]) == 2
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    with SQLiteStore(directory / "state.sqlite3") as store:
        store.import_plan(load_plan(directory))
    before_state = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert main(["state", "--path", str(root)]) == 0
    assert "first: ready" in capsys.readouterr().out
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before_state


def test_state_inspection_rejects_active_wal_without_writing(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    directory = root / ".agentflow"
    directory.mkdir()
    (directory / "spec.toml").write_text(SPEC, encoding="utf-8")
    (directory / "tasks.toml").write_text(TASKS, encoding="utf-8")
    with SQLiteStore(directory / "state.sqlite3") as store:
        store.import_plan(load_plan(directory))
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        assert main(["state", "--path", str(root)]) == 2
        assert "WAL pendente" in capsys.readouterr().err
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


def test_only_two_toml_files_are_git_visible(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text(".agentflow/*\n!.agentflow/spec.toml\n!.agentflow/tasks.toml\n")
    directory = root / ".agentflow"
    directory.mkdir()
    for name in ("spec.toml", "tasks.toml", "config.toml", "state.sqlite3", "state.sqlite3-wal", "state.sqlite3.bak"):
        (directory / name).write_text("data")
    visible = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root,
                             capture_output=True, text=True, check=True).stdout
    assert ".agentflow/spec.toml" in visible
    assert ".agentflow/tasks.toml" in visible
    assert ".agentflow/config.toml" not in visible
    assert "state.sqlite3" not in visible
