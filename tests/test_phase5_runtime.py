import hashlib
import os
from pathlib import Path
import sys
from threading import Event

import pytest

from agentflow.validation import (CapabilityState, CommandDefinition, RuntimeLimits,
                                  RuntimeProfile, RuntimeRequest)
from agentflow.validation.runtime import LocalProcessRuntime


def make_request(tmp_path, arguments, *, expected_version=None, output_limit=4096,
                 environment=(), timeout=10):
    executable = Path(sys.executable).resolve()
    digest = "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest()
    expected_version = expected_version or f"Python {sys.version_info.major}.{sys.version_info.minor}"
    snapshot = tmp_path / "snapshot"
    output = tmp_path / "output"
    snapshot.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    command = CommandDefinition("python", str(executable), digest, tuple(arguments),
                                "snapshot", expected_version)
    limits = RuntimeLimits(timeout_seconds=timeout, output_bytes=output_limit)
    return RuntimeRequest(command, tuple(arguments), snapshot, tuple(environment),
                          limits, output, network_authorized=True)


def test_local_runtime_uses_argv_checks_executable_and_sanitizes_logs(tmp_path):
    request = make_request(tmp_path, ("-c", "import json; print(json.dumps({'api_key':'topsecret','authorization':'Bearer bearer-secret','password':'abc123'}))"))
    runtime = LocalProcessRuntime()
    spawns, probes = [], []
    result = runtime.run(request, Event(), spawns.append, probes.append)
    assert len(probes) == len(spawns) == 1
    assert result.exit_code == 0
    assert result.process_stopped
    assert result.logs_sanitized
    output = result.stdout_path.read_text(encoding="utf-8")
    assert "[REDACTED]" in output
    assert "topsecret" not in output and "abc123" not in output
    assert "bearer-secret" not in output and "Bearer" not in output
    assert result.stdout_bytes > 0
    assert result.process_identity == spawns[0]
    assert result.process_identity != probes[0]
    assert '"pid":' in result.process_identity
    assert not (request.output_directory / ".tmp").exists()


def test_local_runtime_bounds_logs_and_hashes_the_full_stream(tmp_path):
    payload_size = 200_000
    request = make_request(tmp_path, ("-c", f"print('x' * {payload_size})"), output_limit=1024)
    result = LocalProcessRuntime().run(request, Event(), lambda _: None, lambda _: None)
    raw = ("x" * payload_size + os.linesep).encode()
    assert result.exit_code == 0
    assert result.truncated
    assert result.stdout_bytes == len(raw)
    assert result.stdout_digest == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert result.stdout_path.stat().st_size + result.stderr_path.stat().st_size <= 1024


def test_timeout_does_not_claim_unverified_process_tree_cleanup(tmp_path):
    request = make_request(tmp_path, ("-c", "import time; time.sleep(30)"), timeout=1)
    result = LocalProcessRuntime().run(request, Event(), lambda _: None, lambda _: None)
    assert result.timed_out
    assert not result.process_stopped
    assert result.process_identity
    assert (request.output_directory / ".tmp").is_dir(), "ambiguous temp remains for recovery"


def test_cancellation_is_recorded_and_ambiguous_temp_is_preserved(tmp_path):
    request = make_request(tmp_path, ("-c", "import time; time.sleep(30)"), timeout=10)
    cancellation = Event()
    cancellation.set()
    result = LocalProcessRuntime().run(request, cancellation, lambda _: None, lambda _: None)
    assert result.cancelled
    assert not result.process_stopped
    assert (request.output_directory / ".tmp").is_dir()


def test_bad_digest_shell_and_sensitive_environment_are_rejected_before_gate_spawn(tmp_path):
    request = make_request(tmp_path, ("--version",))
    bad_command = CommandDefinition("python", request.command.executable,
                                    "sha256:" + "0" * 64, request.arguments,
                                    "snapshot", request.command.expected_version)
    bad = RuntimeRequest(bad_command, request.arguments, request.cwd, (),
                         request.limits, request.output_directory, True)
    runtime = LocalProcessRuntime()
    spawns = []
    result = runtime.run(bad, Event(), spawns.append, lambda _: None)
    assert result.spawn_failed and not spawns
    assert "digest" in result.reason

    shell = CommandDefinition("shell", "/bin/sh", request.command.executable_digest,
                              (), "snapshot", "1")
    shell_request = RuntimeRequest(shell, (), request.cwd, (), request.limits,
                                   request.output_directory, True)
    result = runtime.run(shell_request, Event(), spawns.append, lambda _: None)
    assert result.spawn_failed and not spawns

    secret_env = RuntimeRequest(request.command, request.arguments, request.cwd,
                                (("OPENAI_API_KEY", "secret"),), request.limits,
                                request.output_directory, True)
    result = runtime.run(secret_env, Event(), spawns.append, lambda _: None)
    assert result.spawn_failed and not spawns
    assert "sensitive" in result.reason


def test_version_mismatch_stops_before_the_gate_process(tmp_path):
    request = make_request(tmp_path, ("--version",), expected_version="not-the-installed-version")
    runtime = LocalProcessRuntime()
    probes, spawns = [], []
    result = runtime.run(request, Event(), spawns.append, probes.append)
    assert result.spawn_failed
    assert result.process_stopped
    assert probes and not spawns
    assert "version diverges" in result.reason


def test_gate_spawn_failure_is_controlled_and_cleans_only_owned_temp(tmp_path, monkeypatch):
    request = make_request(tmp_path, ("--version",))
    original = __import__("subprocess").Popen
    calls = 0

    def fail_second_spawn(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated spawn denial")
        return original(*args, **kwargs)

    monkeypatch.setattr("agentflow.validation.runtime.subprocess.Popen", fail_second_spawn)
    result = LocalProcessRuntime().run(request, Event(), lambda _: None, lambda _: None)
    assert result.spawn_failed and result.process_stopped
    assert "validator spawn failed: OSError" == result.reason
    assert not (request.output_directory / ".tmp").exists()


def test_local_capabilities_report_unavailable_controls_truthfully():
    capabilities = LocalProcessRuntime().inspect_capabilities(RuntimeProfile.LOCAL_EXPLICIT)
    states = {item.name: item.state for item in capabilities.capabilities}
    assert states["network_none"] is CapabilityState.UNAVAILABLE
    assert states["cpu_limit"] is CapabilityState.UNAVAILABLE
    assert states["memory_limit"] is CapabilityState.UNAVAILABLE
    assert states["readonly_source"] is CapabilityState.UNAVAILABLE
    if os.name == "nt":
        assert states["private_temp"] is CapabilityState.UNAVAILABLE
