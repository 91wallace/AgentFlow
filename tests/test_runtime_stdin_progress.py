from dataclasses import replace
from threading import Event, Timer, enumerate as threads
import time

import pytest

from agentflow.agents import ExecutionPolicy
from agentflow.runtime.local import LocalRuntime
from test_local_runtime import spec


@pytest.mark.parametrize("cancelled", [False, True])
def test_large_stdin_to_non_reader_is_cancelled_or_timed_out_promptly(tmp_path, cancelled):
    request = replace(spec(tmp_path, "import time; time.sleep(12)"), stdin="x" * (4 * 1024 * 1024))
    cancel = Event()
    timer = Timer(0.2, cancel.set) if cancelled else None
    if timer:
        timer.start()
    start = time.monotonic()
    try:
        result = LocalRuntime(tmp_path / "logs").run(request, "blocked-stdin",
            ExecutionPolicy(timeout_seconds=1), lambda _: None, cancel)
    finally:
        if timer:
            timer.join()
    assert result.status == ("cancelled" if cancelled else "timed_out")
    assert time.monotonic() - start < 6
    assert not any(thread.name == "agentflow-stdin-blocked-stdin" for thread in threads())


def test_stdout_is_drained_while_child_waits_for_large_stdin(tmp_path):
    code = "import sys; sys.stdout.write('o'*200000); sys.stdout.flush(); data=sys.stdin.buffer.read(); print(len(data),flush=True)"
    request = replace(spec(tmp_path, code), stdin="i" * (1024 * 1024))
    result = LocalRuntime(tmp_path / "logs").run(request, "duplex",
        ExecutionPolicy(timeout_seconds=5), lambda _: None, Event())
    assert result.status == "process_succeeded"
    assert result.stdout_path.read_text().endswith("1048576\n")


def test_idle_reason_is_distinct_and_heartbeat_prevents_false_timeout(tmp_path):
    runtime = LocalRuntime(tmp_path / "logs", default_idle_seconds=0.6)
    result = runtime.run(spec(tmp_path, "import time;time.sleep(8)"), "silent",
        ExecutionPolicy(timeout_seconds=3), lambda _: None, Event())
    assert result.status == "timed_out" and result.reason == "idle_timeout"
    result = runtime.run(spec(tmp_path, "import time\nfor i in range(12):\n print('tick',flush=True);time.sleep(.1)"),
        "heartbeat", ExecutionPolicy(timeout_seconds=4), lambda _: None, Event())
    assert result.status == "process_succeeded"


def test_early_close_and_disabled_idle_do_not_leave_writer_thread(tmp_path):
    runtime = LocalRuntime(tmp_path / "logs", default_idle_seconds=None)
    result = runtime.run(replace(spec(tmp_path, "print('done')"), stdin="x" * 1000000), "early-close",
        ExecutionPolicy(timeout_seconds=3), lambda _: None, Event())
    assert result.status == "process_succeeded"
    assert not any(thread.name == "agentflow-stdin-early-close" for thread in threads())


def test_stdin_encoding_failure_is_reported_and_stops_child(tmp_path):
    request = replace(spec(tmp_path, "import time;time.sleep(12)"), stdin="\ud800")
    result = LocalRuntime(tmp_path / "logs").run(request, "encoding-error",
        ExecutionPolicy(timeout_seconds=3), lambda _: None, Event())
    assert result.status == "process_failed" and result.reason == "stdin_write_failed"
