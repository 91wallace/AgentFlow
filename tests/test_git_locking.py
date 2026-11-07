import os
from pathlib import Path
import subprocess
import sys

import pytest

import agentflow
from agentflow.git.digests import operation_digest, pathset_digest
from agentflow.git.locking import WorkspaceLock, process_identity


if os.name == "nt":
    import msvcrt as _platform_lock_api  # noqa: F401
elif os.name == "posix":
    import fcntl as _platform_lock_api  # noqa: F401
else:
    raise RuntimeError(f"unsupported lock-test platform: {os.name}")


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    package_root = str(Path(agentflow.__file__).resolve().parent.parent)
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = package_root + (os.pathsep + inherited if inherited else "")
    return env


def test_canonical_digests_normalize_unicode_and_reject_duplicates():
    assert pathset_digest(("cafe\u0301.txt",)) == pathset_digest(("café.txt",))
    with pytest.raises(ValueError, match="duplicado"):
        pathset_digest(("café.txt", "cafe\u0301.txt"))
    assert operation_digest({"operation": "create", "label": "cafe\u0301"}) == operation_digest(
        {"label": "café", "operation": "create"})
    with pytest.raises(ValueError, match="floats"):
        operation_digest({"not_canonical": 1.5})


def test_process_identity_contains_platform_start_marker():
    identity = process_identity()
    assert identity["pid"] == os.getpid()
    assert identity["started"]
    assert identity["boot_id"]
    assert identity["host"]


def test_workspace_lock_excludes_another_process(tmp_path):
    lock_path = tmp_path / "workspaces" / "ws.lock"
    script = (
        "import sys; from pathlib import Path; from agentflow.git.locking import WorkspaceLock; "
        "lock=WorkspaceLock(Path(sys.argv[1])); lock.acquire('child-op'); print('locked', flush=True); "
        "sys.stdin.readline(); lock.release()"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(lock_path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env())
    try:
        assert child.stdout.readline().strip() == "locked"
        parent = WorkspaceLock(lock_path)
        with pytest.raises(Exception, match="ocupado"):
            parent.acquire("parent-op")
        if parent._file is not None:
            parent.release()
    finally:
        assert child.stdin is not None
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0


def test_abandoned_lock_is_reacquired_with_previous_owner_record(tmp_path):
    lock_path = tmp_path / "abandoned.lock"
    script = (
        "import sys,os; from pathlib import Path; from agentflow.git.locking import WorkspaceLock; "
        "lock=WorkspaceLock(Path(sys.argv[1])); lock.acquire('crashed-op'); print('locked '+str(os.getpid()), flush=True); os._exit(0)"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(lock_path)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=_child_env())
    assert child.stdout is not None
    output = child.stdout.readline().strip().split()
    assert output[0] == "locked"
    recorded_pid = int(output[1])
    assert child.wait(timeout=10) == 0
    replacement = WorkspaceLock(lock_path)
    previous = replacement.acquire("reconcile-op")
    try:
        assert previous is not None
        assert previous["operation_id"] == "crashed-op"
        assert previous["pid"] == recorded_pid
        assert lock_path.exists()  # lock files are intentionally persistent
    finally:
        replacement.release()
