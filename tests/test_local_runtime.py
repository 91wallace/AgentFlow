import sys
from pathlib import Path
from threading import Event, Timer

import pytest

from agentflow.agents import ExecutionPolicy, ProcessSpec
from agentflow.runtime.local import LocalRuntime
import agentflow.runtime.local as local_module


def spec(tmp_path, code):
    return ProcessSpec(Path(sys.executable), ("-c", code), tmp_path, "", (), "fake", "1", "inherited", "inherited")


def test_runtime_timeout_and_incremental_logs(tmp_path):
    runtime = LocalRuntime(tmp_path / "logs")
    identities = []
    result = runtime.run(spec(tmp_path, "import time;print('started',flush=True);time.sleep(5)"),
                         "timeout", ExecutionPolicy(timeout_seconds=1), identities.append, Event())
    assert result.status == "timed_out"
    assert result.stdout_path.read_text().strip() == "started"
    assert identities[0].attempt_id == "timeout"
    assert identities[0].started_at and identities[0].parent_started_at


def test_runtime_cancel_and_log_cap(tmp_path):
    runtime = LocalRuntime(tmp_path / "logs")
    cancel = Event()
    timer = Timer(0.1, cancel.set)
    timer.start()
    try:
        result = runtime.run(spec(tmp_path, "import time;time.sleep(5)"), "cancel",
                             ExecutionPolicy(timeout_seconds=5), lambda _: None, cancel)
    finally:
        timer.join()
    assert result.status == "cancelled"
    result = runtime.run(spec(tmp_path, "print('x'*100000)"), "limit",
                         ExecutionPolicy(timeout_seconds=5, log_limit_bytes=100), lambda _: None, Event())
    assert result.stdout_bytes == 100
    assert result.reason == "log_limit"


def test_spawn_failure_does_not_call_started(tmp_path, monkeypatch):
    called = []
    executable = tmp_path / "fake-executable"
    executable.write_bytes(b"test")
    def fail_spawn(*args, **kwargs):
        raise OSError("simulated spawn failure")
    monkeypatch.setattr(local_module.subprocess, "Popen", fail_spawn)
    with pytest.raises(OSError, match="simulated"):
        LocalRuntime(tmp_path / "logs").run(ProcessSpec(
            executable, (), tmp_path, "", (), "fake", "1", "inherited", "inherited"),
            "failed", ExecutionPolicy(), called.append, Event())
    assert not called


def test_logs_redact_secrets_and_write_atomic_metadata(tmp_path):
    fake_key = "sk-" + "abcdefghijklmnop"
    result = LocalRuntime(tmp_path / "logs").run(
        spec(tmp_path, f"print('token=supersecretvalue'); print({fake_key!r})"),
        "redact", ExecutionPolicy(timeout_seconds=5), lambda _: None, Event())
    content = result.stdout_path.read_text()
    assert "supersecretvalue" not in content and fake_key not in content
    assert "[REDACTED]" in content
    assert (result.stdout_path.parent / "metadata.json").is_file()


def test_log_write_failure_stops_process_and_is_recorded(tmp_path, monkeypatch):
    original = open
    class BrokenWriter:
        def __init__(self, handle):
            self.handle = handle
        def write(self, data):
            raise OSError("disk full")
        def flush(self):
            self.handle.flush()
        def close(self):
            self.handle.close()
    monkeypatch.setattr(local_module, "open", lambda *args, **kwargs: BrokenWriter(original(*args, **kwargs)), raising=False)
    result = LocalRuntime(tmp_path / "logs").run(spec(tmp_path, "import time; print('x'*100000,flush=True); time.sleep(5)"),
                                                  "logfailure", ExecutionPolicy(timeout_seconds=5), lambda _: None, Event())
    assert result.status == "process_failed"
    assert result.reason == "log_write_failed"
