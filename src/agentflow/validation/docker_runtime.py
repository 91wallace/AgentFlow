"""Opt-in Docker CLI runtime with inspected security/resource settings.

Docker is never pulled, installed, or selected automatically. If the local daemon
or any requested setting cannot be verified, this runtime refuses the gate.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Callable, Protocol

from agentflow.planning import canonical

from .contracts import (CapabilityState, RuntimeCapabilities, RuntimeCapability,
                        RuntimeProfile, RuntimeRequest, RuntimeResult)
from .runtime import _LogCollector, _redact


IMAGE = re.compile(r"^[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DockerEngine(Protocol):
    def info(self) -> dict: ...
    def image_exists(self, image: str) -> bool: ...
    def create(self, request: RuntimeRequest, argv: tuple[str, ...]) -> str: ...
    def inspect(self, container_id: str) -> dict: ...
    def start(self, container_id: str) -> None: ...
    def stop(self, container_id: str, timeout: int) -> None: ...
    def logs(self, container_id: str) -> tuple[bytes, bytes]: ...
    def remove(self, container_id: str) -> None: ...
    def find_labeled(self, label: str, value: str) -> tuple[str, ...]: ...


class DockerCLIEngine:
    """Small shell-free adapter; all dynamic values are argv items."""

    def __init__(self, executable: str | None = None):
        self.executable = executable or shutil.which("docker") or ""

    def _call(self, *arguments: str, timeout: int = 15) -> str:
        if not self.executable:
            raise OSError("Docker CLI indisponível")
        completed = subprocess.run([self.executable, *arguments], shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=timeout, check=False)
        if len(completed.stdout) > 1024 * 1024 or len(completed.stderr) > 64 * 1024:
            raise OSError("resposta de controle Docker excedeu o limite")
        if completed.returncode:
            raise OSError("comando Docker recusado")
        return completed.stdout.decode("utf-8", "strict").strip()

    def info(self) -> dict:
        version = self._call("version", "--format", "{{.Server.Version}}")
        info = json.loads(self._call("info", "--format", "{{json .}}"))
        context_host = self._call("context", "inspect", "--format",
                                  "{{json .Endpoints.docker.Host}}")
        try:
            endpoint = json.loads(context_host)
        except ValueError:
            endpoint = ""
        controller_path = Path("/sys/fs/cgroup/cgroup.controllers")
        try:
            controllers = set(controller_path.read_text(encoding="ascii").split())
        except OSError:
            controllers = set()
        local_cgroup_enforcement = (
            os.name == "posix" and str(endpoint).startswith("unix://")
            and info.get("Cgroup Version") == "2"
            and {"cpu", "memory", "pids"} <= controllers
        )
        return {"server_version": version, "enforcement_supported": local_cgroup_enforcement,
                "cgroup_controllers": sorted(controllers), **info}

    def image_exists(self, image: str) -> bool:
        try:
            result = json.loads(self._call("image", "inspect", image,
                                           "--format", "{{json .RepoDigests}}"))
            return image in result
        except (OSError, ValueError):
            return False

    def create(self, request: RuntimeRequest, argv: tuple[str, ...]) -> str:
        source = (request.snapshot_path or request.cwd).resolve(strict=True)
        output = request.output_directory.resolve(strict=True)
        for path in (source, output):
            if "," in str(path) or "\n" in str(path) or "\r" in str(path):
                raise OSError("mount path cannot be represented safely by Docker CLI")
        if source == output or source in output.parents or output in source.parents:
            raise OSError("source and output mounts must be disjoint")
        image = request.container_image
        if not IMAGE.fullmatch(image):
            raise OSError("container image must be pinned by sha256 digest")
        if not self.image_exists(image):
            raise OSError("pinned image is not present locally; automatic pull is disabled")
        user = "65534:65534"
        if os.name == "posix":
            info = source.stat()
            user = f"{info.st_uid}:{info.st_gid}"
            if info.st_uid == 0:
                raise OSError("refusing Docker execution as host root")
        args = [self.executable, "create", "--network", "none", "--read-only",
                "--label", f"agentflow.validation_run={request.validation_run_id}",
                "--label", f"agentflow.gate={request.gate_id}",
                "--cpus", str(request.limits.cpu_count), "--memory",
                str(request.limits.memory_bytes), "--pids-limit",
                str(request.limits.process_count), "--user", user,
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--tmpfs", f"/tmp:rw,noexec,nosuid,size={request.limits.workspace_bytes},mode=1777",
                "--tmpfs", f"/output:rw,nosuid,size={request.limits.workspace_bytes},mode=1777",
                "--mount", f"type=bind,src={source},dst=/workspace,readonly",
                "--workdir", "/workspace" if request.command.working_directory == "snapshot" else "/output",
                "--entrypoint", argv[0], image,
                *argv[1:]]
        completed = subprocess.run(args, shell=False, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=30, check=False)
        if completed.returncode or len(completed.stdout) > 256:
            raise OSError("container create failed")
        container_id = completed.stdout.decode("ascii", "strict").strip()
        if not CONTAINER_ID.fullmatch(container_id):
            raise OSError("Docker returned an invalid container identity")
        return container_id

    def inspect(self, container_id: str) -> dict:
        if not CONTAINER_ID.fullmatch(container_id):
            raise OSError("invalid container ID")
        return json.loads(self._call("inspect", container_id, "--format", "{{json .}}"))

    def start(self, container_id: str) -> None:
        self._call("start", container_id)

    def stop(self, container_id: str, timeout: int) -> None:
        self._call("stop", "--time", str(timeout), container_id, timeout=timeout + 10)

    def logs(self, container_id: str) -> tuple[bytes, bytes]:
        # Docker control CLI may produce large logs: drain to bounded rolling files.
        if not CONTAINER_ID.fullmatch(container_id) or not self.executable:
            raise OSError("invalid container identity")
        process = subprocess.Popen([self.executable, "logs", container_id], shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        collector = _LogCollector(8 * 1024**2)

        def drain(pipe, stream):
            while chunk := pipe.read(64 * 1024):
                collector.add(stream, chunk)

        assert process.stdout is not None and process.stderr is not None
        threads = [threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)]
        for thread in threads:
            thread.start()
        process.wait(timeout=30)
        for thread in threads:
            thread.join(timeout=2)
        if process.returncode:
            raise OSError("could not retrieve container logs")
        with tempfile.TemporaryDirectory(prefix="agentflow-docker-logs-") as directory:
            paths = Path(directory)
            collector.finish(paths)
            return ((paths / "stdout.log").read_bytes(),
                    (paths / "stderr.log").read_bytes())

    def remove(self, container_id: str) -> None:
        self._call("rm", container_id)

    def find_labeled(self, label: str, value: str) -> tuple[str, ...]:
        output = self._call("ps", "-aq", "--filter", f"label={label}={value}")
        return tuple(item for item in output.splitlines() if CONTAINER_ID.fullmatch(item))


class DockerRuntime:
    """Run exact allowlisted argv in a locally available, pinned image."""

    def __init__(self, engine: DockerEngine | None = None, *, poll_seconds: float = 0.05):
        self.engine = engine or DockerCLIEngine()
        self.poll_seconds = min(max(poll_seconds, 0.01), 0.25)

    def inspect_capabilities(self, profile: RuntimeProfile) -> RuntimeCapabilities:
        if profile is not RuntimeProfile.DOCKER_ISOLATED:
            return RuntimeCapabilities(profile, "docker", "unselected", ())
        try:
            info = self.engine.info()
            if not info.get("server_version"):
                raise OSError("Docker server version missing")
        except Exception as exc:
            return RuntimeCapabilities(profile, "docker", "unavailable", tuple(
                RuntimeCapability(name, CapabilityState.UNAVAILABLE,
                                  f"Docker capability probe failed: {type(exc).__name__}")
                for name in self._capability_names()))
        resource_names = {"cpu_limit", "memory_limit", "process_limit"}
        return RuntimeCapabilities(profile, "docker", str(info["server_version"]), tuple(
            RuntimeCapability(name,
                              CapabilityState.SUPPORTED if (info.get("enforcement_supported")
                                                            or name not in resource_names)
                              else CapabilityState.UNAVAILABLE,
                              "host cgroup v2 controllers and local Docker endpoint verified"
                              if info.get("enforcement_supported") else
                              "resource enforcement cannot be verified for this daemon/platform")
            for name in self._capability_names()))

    def reconcile_process(self, identity: str) -> bool | None:
        """Return stopped/live only for the exact immutable container/start identity."""
        try:
            value = json.loads(identity)
            container_id = value["container_id"]
            if (value.get("runtime") != "docker" or not CONTAINER_ID.fullmatch(container_id)):
                return None
            state = self.engine.inspect(container_id).get("State", {})
            if state.get("StartedAt") != value.get("started_at"):
                return None
            return not bool(state.get("Running"))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def reconcile_intent(self, validation_run_id: str, gate_id: str) -> tuple[str, bool | None] | None:
        """Find a pre-start container by its persisted gate intent labels."""
        finder = getattr(self.engine, "find_labeled", None)
        if finder is None:
            return None
        try:
            if not SAFE_LABEL.fullmatch(validation_run_id) or not SAFE_LABEL.fullmatch(gate_id):
                return None
            by_run = set(finder("agentflow.validation_run", validation_run_id))
            by_gate = set(finder("agentflow.gate", gate_id))
            matches = by_run & by_gate
            if len(matches) != 1:
                return None
            container_id = matches.pop()
            state = self.engine.inspect(container_id).get("State", {})
            if state.get("Running") is True:
                return container_id, False
            if type(state.get("ExitCode")) is int:
                return container_id, True
            return None
        except (OSError, TypeError, ValueError):
            return None

    def cancel_process(self, identity: str) -> bool:
        """Stop only a container whose full ID and start timestamp still match."""
        try:
            value = json.loads(identity)
            container_id = value["container_id"]
            state = self.engine.inspect(container_id).get("State", {})
            if (not CONTAINER_ID.fullmatch(container_id)
                    or state.get("StartedAt") != value.get("started_at")):
                return False
            if state.get("Running"):
                self.engine.stop(container_id, 5)
            final = self.engine.inspect(container_id).get("State", {})
            return (final.get("StartedAt") == value.get("started_at")
                    and not final.get("Running"))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def run(self, request: RuntimeRequest, cancellation,
            on_spawn: Callable[[str], None],
            on_probe: Callable[[str], None]) -> RuntimeResult:
        self._last_identity = ""
        self._last_stopped = True
        output = request.output_directory.resolve(strict=True)
        if (not request.container_executable.startswith("/")
                or not IMAGE.fullmatch(request.container_image)
                or request.command.executable.casefold().endswith((".bat", ".cmd", ".ps1"))):
            return self._failed(request, "container allowlist incomplete or invalid")
        try:
            image_info = self.engine.info()
            if not image_info.get("server_version") or not self.engine.image_exists(
                    request.container_image):
                return self._failed(request, "Docker daemon or pinned local image unavailable")
            version_logs, version_code, version_stopped, _, _ = self._invoke(
                request, (request.container_executable, "--version"), cancellation,
                on_probe, output, probe=True)
            version_text = (version_logs[0] + version_logs[1]).decode("utf-8", "replace")
            if not version_stopped:
                return self._failed(request, "version probe termination unresolved",
                                    stopped=False)
            if version_code != 0 or request.command.expected_version.casefold() not in version_text.casefold():
                return self._failed(request, "container executable version diverges")
            argv = (request.container_executable, *self._map_arguments(request))
            logs, exit_code, stopped, timed_out, cancelled = self._invoke(
                request, argv, cancellation, on_spawn, output, probe=False)
            if not stopped:
                return self._failed(request, "container stop could not be verified", stopped=False)
            stdout, stderr = logs
            if len(stdout) + len(stderr) > request.limits.output_bytes:
                return self._failed(request, "Docker log quota exceeded")
            stdout = _redact(stdout)
            stderr = _redact(stderr)
            stdout_path, stderr_path = output / "stdout.log", output / "stderr.log"
            stdout_path.write_bytes(stdout)
            stderr_path.write_bytes(stderr)
            if os.name != "nt":
                stdout_path.chmod(0o600)
                stderr_path.chmod(0o600)
            limits = request.limits
            capabilities = self._effective_capabilities(image_info, limits)
            return RuntimeResult(exit_code, timed_out, cancelled, False,
                                 getattr(self, "_last_identity", ""), stdout_path,
                                 stderr_path, "sha256:" + hashlib.sha256(stdout).hexdigest(),
                                 "sha256:" + hashlib.sha256(stderr).hexdigest(),
                                 len(stdout), len(stderr), False, capabilities, "", True, True)
        except Exception as exc:
            return self._failed(request, f"Docker runtime failed: {type(exc).__name__}")

    def _invoke(self, request, argv, cancellation, callback, output, *, probe):
        container_id = self.engine.create(request, argv)
        inspect = self.engine.inspect(container_id)
        try:
            self._verify_configuration(inspect, request)
        except BaseException:
            if inspect.get("State", {}).get("Running") is False:
                try:
                    self.engine.remove(container_id)
                except Exception:
                    pass
            raise
        self.engine.start(container_id)
        state = self.engine.inspect(container_id).get("State", {})
        if (not state.get("StartedAt")
                or (not state.get("Running") and type(state.get("ExitCode")) is not int)):
            raise OSError("Docker did not confirm a started container")
        identity = canonical({"container_id": container_id,
                              "started_at": state["StartedAt"],
                              "image": request.container_image,
                              "runtime": "docker", "server_version": self.engine.info()["server_version"]})
        self._last_identity = identity
        self._last_stopped = False
        try:
            callback(identity)
        except BaseException:
            try:
                self.engine.stop(container_id, 5)
                if not self.engine.inspect(container_id).get("State", {}).get("Running"):
                    self.engine.remove(container_id)
                else:
                    self._last_identity = identity
            except Exception:
                self._last_identity = identity
            raise
        deadline = time.monotonic() + request.limits.timeout_seconds
        cancelled = False
        timed_out = False
        while True:
            state = self.engine.inspect(container_id).get("State", {})
            if not state.get("Running"):
                code = state.get("ExitCode")
                if type(code) is not int:
                    raise OSError("container exit code unavailable")
                self._last_stopped = True
                break
            if cancellation.is_set() or time.monotonic() >= deadline:
                cancelled = cancellation.is_set()
                timed_out = not cancelled
                self.engine.stop(container_id, 5)
                stopped_state = self.engine.inspect(container_id).get("State", {})
                if stopped_state.get("Running"):
                    raise OSError("container remains running after stop")
                self._last_stopped = True
                code = stopped_state.get("ExitCode")
                break
            time.sleep(self.poll_seconds)
        logs = self.engine.logs(container_id)
        for name, data in zip(("stdout", "stderr"), logs):
            if len(data) > request.limits.output_bytes:
                raise OSError("Docker log quota exceeded")
        self.engine.remove(container_id)
        self._last_stopped = True
        if not probe:
            self._last_exit_code = code
        return logs, code, True, timed_out, cancelled

    def _verify_configuration(self, inspected: dict, request: RuntimeRequest) -> None:
        config = inspected.get("Config", {})
        host = inspected.get("HostConfig", {})
        state = inspected.get("State", {})
        mounts = inspected.get("Mounts", [])
        networks = inspected.get("NetworkSettings", {}).get("Networks", {})
        expected_source = str((request.snapshot_path or request.cwd).resolve(strict=True))
        if (config.get("Image") != request.container_image or host.get("Privileged") is not False
                or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
                or config.get("User", "").split(":", 1)[0] in {"", "0", "root"}
                or host.get("UsernsMode") == "host" or host.get("PidMode") == "host"
                or host.get("IpcMode") == "host" or host.get("UTSMode") == "host"
                or "ALL" not in host.get("CapDrop", [])
                or host.get("CapAdd") or host.get("Devices") or host.get("DeviceRequests")
                or "no-new-privileges" not in host.get("SecurityOpt", [])
                or host.get("PidsLimit") != request.limits.process_count
                or not {"/tmp", "/output"} <= host.get("Tmpfs", {}).keys()
                or host.get("Memory") != request.limits.memory_bytes
                or host.get("NanoCpus") != request.limits.cpu_count * 1_000_000_000
                or state.get("Running") is True
                or any(name != "none" or endpoint.get("IPAddress")
                       for name, endpoint in networks.items())):
            raise OSError("effective Docker HostConfig diverges from required profile")
        by_destination = {item.get("Destination"): item for item in mounts}
        source = by_destination.get("/workspace", {})
        tmpfs = host.get("Tmpfs", {})
        for destination in ("/tmp", "/output"):
            options = set(tmpfs.get(destination, "").split(","))
            if (f"size={request.limits.workspace_bytes}" not in options
                    or "rw" not in options or "nosuid" not in options):
                raise OSError(f"Docker tmpfs quota/options unavailable for {destination}")
        if (set(by_destination) != {"/workspace", "/tmp", "/output"}
                or source.get("Source") != expected_source
                or source.get("RW") is not False
                or any(by_destination[target].get("Type") != "tmpfs"
                       or by_destination[target].get("RW") is not True
                       for target in ("/tmp", "/output"))):
            raise OSError("Docker mount set diverges from isolated source/output policy")

    def _map_arguments(self, request: RuntimeRequest) -> tuple[str, ...]:
        source = str((request.snapshot_path or request.cwd).resolve(strict=True))
        output = str(request.output_directory.resolve(strict=True))
        mapped = []
        for argument in request.arguments:
            value = argument.replace(source, "/workspace").replace(output, "/output")
            if re.search(r"(?:^|[=,])(?:[A-Za-z]:[\\/]|\\\\|/(?!/))", value):
                absolute = re.search(r"(?:^|[=,])((?:[A-Za-z]:[\\/]|\\\\|/(?!/)).*)$", value)
                path_value = absolute.group(1) if absolute else ""
                normalized = path_value.replace("\\", "/")
                if "/../" in normalized or normalized.endswith("/.."):
                    raise OSError("path traversal in container argv is forbidden")
                if not normalized.startswith(("/workspace", "/output")):
                    raise OSError("argument contains an absolute path/escape outside mounted roots")
            if "../" in value.replace("\\", "/") or value.replace("\\", "/").endswith("/.."):
                raise OSError("path traversal in container argv is forbidden")
            mapped.append(value)
        return tuple(mapped)

    def _effective_capabilities(self, info: dict, limits) -> RuntimeCapabilities:
        detail = f"verified container settings; engine {info['server_version']}"
        names = self._capability_names()
        return RuntimeCapabilities(RuntimeProfile.DOCKER_ISOLATED, "docker",
                                   str(info["server_version"]), tuple(
            RuntimeCapability(name,
                              CapabilityState.UNAVAILABLE if (
                                  name in {"cpu_limit", "memory_limit", "process_limit"}
                                  and not info.get("enforcement_supported"))
                              else CapabilityState.ENFORCED, detail)
            for name in names))

    @staticmethod
    def _capability_names():
        return ("argv_only", "bounded_output", "cpu_limit", "memory_limit",
                "network_none", "no_host_socket", "no_privileges", "pinned_image",
                "private_temp", "process_count", "process_identity",
                "process_limit", "readonly_source", "snapshot_digest", "timeout",
                "workspace_limit")

    def _failed(self, request, reason, *, stopped=True):
        empty = "sha256:" + hashlib.sha256(b"").hexdigest()
        return RuntimeResult(None, False, False, True, getattr(self, "_last_identity", ""),
                             request.output_directory / "stdout.log",
                             request.output_directory / "stderr.log", empty, empty, 0, 0,
                             False, self.inspect_capabilities(RuntimeProfile.DOCKER_ISOLATED),
                             reason[:512], stopped and getattr(self, "_last_stopped", True), True)
