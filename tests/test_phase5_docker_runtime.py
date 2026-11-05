import hashlib
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from agentflow.validation import (CapabilityState, CommandDefinition, DockerRuntime,
                                  RuntimeLimits, RuntimeProfile, RuntimeRequest)


IMAGE = "example.test/validator@sha256:" + "a" * 64


class FakeDocker:
    def __init__(self, *, image=True, mismatch=False, block=False):
        self.present = image
        self.mismatch = mismatch
        self.block = block
        self.started = set()
        self.calls = []
        self.ids = iter(("1" * 64, "2" * 64, "3" * 64))
        self.commands = {}

    def info(self):
        return {"server_version": "27.0", "enforcement_supported": True}

    def image_exists(self, image):
        return image == IMAGE and self.present

    def create(self, request, argv):
        container_id = next(self.ids)
        self.calls.append(("create", container_id))
        self.commands[container_id] = argv
        return container_id

    def inspect(self, container_id):
        probe = self.commands.get(container_id, ())[-1:] == ("--version",)
        running = container_id in self.started and self.block and not probe
        finished = container_id in self.started and (not self.block or probe)
        host = {"Privileged": False, "NetworkMode": "bridge" if self.mismatch else "none",
                "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges"], "PidsLimit": 64,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=4294967296,mode=1777",
                          "/output": "rw,nosuid,size=4294967296,mode=1777"},
                "Memory": 4 * 1024**3, "NanoCpus": 2_000_000_000}
        return {"Config": {"Image": IMAGE, "User": "1000:1000"}, "HostConfig": host,
                "NetworkSettings": {"Networks": {}},
                "Mounts": [{"Destination": "/workspace", "Source": str(self.source), "RW": False},
                           {"Destination": "/tmp", "Type": "tmpfs", "RW": True},
                           {"Destination": "/output", "Type": "tmpfs", "RW": True}],
                "State": {"Running": running, "StartedAt": "2026-11-05T10:00:00Z",
                          "ExitCode": 0 if finished else None}}

    def start(self, container_id):
        self.started.add(container_id)
        self.calls.append(("start", container_id))

    def stop(self, container_id, timeout):
        self.block = False
        self.calls.append(("stop", container_id))

    def logs(self, container_id):
        if self.commands[container_id][-1] == "--version":
            return b"validator 1.0\n", b""
        return b'{"status":"passed"}\n', b""

    def remove(self, container_id):
        self.calls.append(("remove", container_id))


def make_request(tmp_path, engine):
    snapshot, output = tmp_path / "snapshot", tmp_path / "output"
    snapshot.mkdir(parents=True)
    output.mkdir(parents=True)
    engine.source, engine.output = snapshot, output
    executable = Path("C:/validator.exe")
    command = CommandDefinition("gate", str(executable), "sha256:" + "b" * 64,
                                ("--check",), "snapshot", "validator 1.0")
    return RuntimeRequest(command, ("--check",), snapshot, (), RuntimeLimits(), output,
                          False, IMAGE, "/usr/local/bin/validator", snapshot)


def test_docker_runtime_verifies_effective_settings_and_runs_allowlisted_argv(tmp_path):
    engine = FakeDocker()
    request = make_request(tmp_path, engine)
    runtime = DockerRuntime(engine, poll_seconds=0.01)
    capabilities = runtime.inspect_capabilities(RuntimeProfile.DOCKER_ISOLATED)
    assert all(item.state is CapabilityState.SUPPORTED for item in capabilities.capabilities)
    spawns, probes = [], []
    result = runtime.run(request, Event(), spawns.append, probes.append)
    assert result.exit_code == 0 and result.process_stopped and result.logs_sanitized
    assert len(probes) == len(spawns) == 1
    assert '"container_id":"' in spawns[0]
    assert all(item.state is CapabilityState.ENFORCED for item in result.capabilities.capabilities)
    created = [cid for kind, cid in engine.calls if kind == "create"]
    assert engine.commands[created[1]][-1] == "--check"
    assert len([call for call in engine.calls if call[0] == "remove"]) == 2


def test_docker_runtime_refuses_unavailable_image_and_misconfigured_container(tmp_path):
    absent = FakeDocker(image=False)
    request = make_request(tmp_path / "absent", absent)
    result = DockerRuntime(absent).run(request, Event(), lambda _: None, lambda _: None)
    assert result.spawn_failed and result.process_stopped
    assert not absent.calls

    invalid = FakeDocker(mismatch=True)
    request = make_request(tmp_path / "invalid", invalid)
    result = DockerRuntime(invalid).run(request, Event(), lambda _: None, lambda _: None)
    assert result.spawn_failed
    assert not any(call[0] == "start" for call in invalid.calls)


def test_docker_runtime_cancellation_stops_and_removes_verified_container(tmp_path):
    engine = FakeDocker(block=True)
    request = make_request(tmp_path, engine)
    cancellation = Event()
    runtime = DockerRuntime(engine, poll_seconds=0.01)
    result = runtime.run(request, cancellation, lambda _: cancellation.set(), lambda _: None)
    assert result.cancelled and result.process_stopped
    assert any(call[0] == "stop" for call in engine.calls)
    assert any(call[0] == "remove" for call in engine.calls)


def test_docker_runtime_rejects_host_paths_and_path_traversal_in_argv(tmp_path):
    engine = FakeDocker()
    request = make_request(tmp_path, engine)
    runtime = DockerRuntime(engine)
    escaped = replace(request, arguments=("--config=/etc/passwd",))
    with pytest.raises(OSError, match="absolute path"):
        runtime._map_arguments(escaped)
    traversal = replace(request, arguments=("--config=/workspace/../etc/passwd",))
    with pytest.raises(OSError, match="traversal"):
        runtime._map_arguments(traversal)
