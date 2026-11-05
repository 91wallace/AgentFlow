"""Explicit local subprocess runtime with bounded, sanitized output."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable
import uuid

from agentflow.planning import canonical

from .contracts import (CapabilityState, RuntimeCapabilities, RuntimeCapability,
                        RuntimeProfile, RuntimeRequest, RuntimeResult)


SECRET_PATTERN = re.compile(
    rb"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|authorization)[\"']?"
    rb"\s*[:=]\s*)(\"[^\r\n]*?\"|'[^\r\n]*'|[^\r\n,;]+)"
)
SENSITIVE_ENV = re.compile(r"(?i)(secret|token|password|credential|api[_-]?key)")
SAFE_ENV = {"CI", "LANG", "LC_ALL", "TZ", "PYTHONUTF8", "PYTHONIOENCODING",
            "PIP_DISABLE_PIP_VERSION_CHECK", "NO_COLOR", "FORCE_COLOR"}
DEFAULT_GATE_LOG_LIMIT = 8 * 1024**2


def _redact(data: bytes) -> bytes:
    def replace_secret(match):
        value = match.group(2).strip()
        if len(value) >= 2 and value[:1] in (b"\"", b"'") and value[-1:] == value[:1]:
            redacted = value[:1] + b"[REDACTED]" + value[-1:]
        else:
            redacted = b"[REDACTED]"
        return match.group(1) + redacted
    return SECRET_PATTERN.sub(replace_secret, data)


class _LogCollector:
    """Drain both pipes while retaining bounded combined prefix/suffix bytes."""

    def __init__(self, limit: int):
        self.limit = limit
        self.counts = {"stdout": 0, "stderr": 0}
        self.hashes = {"stdout": hashlib.sha256(), "stderr": hashlib.sha256()}
        self.prefix = {"stdout": bytearray(), "stderr": bytearray()}
        self.suffix: list[tuple[str, bytes]] = []
        self.suffix_size = 0
        self.lock = threading.Lock()
        self.content_limit = max(0, limit - 256)
        self.prefix_limit = self.content_limit // 2
        self.suffix_limit = self.content_limit - self.prefix_limit
        self.prefix_size = 0

    def add(self, stream: str, chunk: bytes) -> None:
        with self.lock:
            self.counts[stream] += len(chunk)
            self.hashes[stream].update(chunk)
            prefix_room = self.prefix_limit - self.prefix_size
            take = min(prefix_room, len(chunk))
            if take:
                self.prefix[stream].extend(chunk[:take])
                self.prefix_size += take
            tail = chunk[take:]
            if not tail or self.suffix_limit <= 0:
                return
            if len(tail) >= self.suffix_limit:
                self.suffix = [(stream, tail[-self.suffix_limit:])]
                self.suffix_size = self.suffix_limit
                return
            self.suffix.append((stream, tail))
            self.suffix_size += len(tail)
            while self.suffix_size > self.suffix_limit and self.suffix:
                name, first = self.suffix[0]
                overflow = self.suffix_size - self.suffix_limit
                if overflow >= len(first):
                    self.suffix.pop(0)
                    self.suffix_size -= len(first)
                else:
                    self.suffix[0] = (name, first[overflow:])
                    self.suffix_size -= overflow

    def finish(self, output: Path) -> tuple[dict[str, str], dict[str, int], bool, bool]:
        suffix_by_stream = {"stdout": bytearray(), "stderr": bytearray()}
        for stream, chunk in self.suffix:
            suffix_by_stream[stream].extend(chunk)
        total = sum(self.counts.values())
        truncated = total > self.limit
        digests = {name: "sha256:" + self.hashes[name].hexdigest()
                   for name in self.counts}
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        saved: dict[str, bytes] = {}
        for name in ("stdout", "stderr"):
            data = bytes(self.prefix[name]) + bytes(suffix_by_stream[name])
            if truncated:
                marker = (f"\n[truncated; sha256={digests[name]}]\n".encode("ascii")
                          if self.limit > 256 else b"")
                data = bytes(self.prefix[name]) + marker + bytes(suffix_by_stream[name])
            saved[name] = _redact(data)
        combined = len(saved["stdout"]) + len(saved["stderr"])
        if combined > self.limit:
            excess = combined - self.limit
            for name in ("stderr", "stdout"):
                remove = min(excess, len(saved[name]))
                if remove:
                    saved[name] = saved[name][:-remove]
                    excess -= remove
                if not excess:
                    break
        try:
            for name, content in saved.items():
                path = output / f"{name}.log"
                with path.open("xb") as stream:
                    stream.write(content)
                if os.name != "nt":
                    path.chmod(0o600)
        except BaseException:
            for name in ("stdout", "stderr"):
                (output / f"{name}.log").unlink(missing_ok=True)
            return digests, self.counts.copy(), truncated, False
        return digests, self.counts.copy(), truncated, True


def _read_process_start(pid: int) -> str | None:
    if os.name == "posix":
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2:].split()
            start_ticks = fields[19]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            return f"linux:{boot_id}:{start_ticks}"
        except (OSError, IndexError, ValueError):
            return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME),
                                               ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                               ctypes.POINTER(FILETIME)]
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                              ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                value = (created.high << 32) | created.low
                return f"windows:{value}"
            finally:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError):
            return None
    return None


def process_identity(pid: int, *, process_group: int | None = None,
                     job_managed: bool = False, executable_digest: str = "") -> str:
    start = _read_process_start(pid)
    if start is None:
        raise RuntimeError("process start identity could not be verified")
    return canonical({"pid": pid, "start": start,
                      "process_group": process_group if process_group is not None else pid,
                      "job_managed": job_managed,
                      "executable_digest": executable_digest,
                      "platform": "windows" if os.name == "nt" else "linux"})


class LocalProcessRuntime:
    """Run the exact allowlisted executable without a shell or inherited secrets."""

    def __init__(self, *, grace_seconds: float = 5.0, probe_timeout_seconds: float = 10.0):
        if grace_seconds != 5.0 or not 1 <= probe_timeout_seconds <= 10:
            raise ValueError("local runtime uses fixed 5s termination grace and <=10s version probe")
        self.grace_seconds = grace_seconds
        self.probe_timeout_seconds = probe_timeout_seconds

    def inspect_capabilities(self, profile: RuntimeProfile) -> RuntimeCapabilities:
        if profile is not RuntimeProfile.LOCAL_EXPLICIT:
            return RuntimeCapabilities(profile, "local-process", "1", ())
        capabilities = [
            RuntimeCapability("argv_only", CapabilityState.ENFORCED, "shell=False"),
            RuntimeCapability("timeout", CapabilityState.ENFORCED, "monotonic deadline"),
            RuntimeCapability("process_identity", CapabilityState.ENFORCED, "PID plus OS start identity"),
            RuntimeCapability("bounded_output", CapabilityState.ENFORCED, "8 MiB combined capture"),
            RuntimeCapability("snapshot_digest", CapabilityState.ENFORCED, "checked by snapshot manager"),
            RuntimeCapability("network_none", CapabilityState.UNAVAILABLE,
                              "local backend cannot isolate networking"),
            RuntimeCapability("cpu_limit", CapabilityState.UNAVAILABLE,
                              "no delegated cgroup/Job CPU cap configured"),
            RuntimeCapability("memory_limit", CapabilityState.UNAVAILABLE,
                              "no delegated cgroup/Job memory cap configured"),
            RuntimeCapability("process_limit", CapabilityState.UNAVAILABLE,
                              "no delegated cgroup/Job PID cap configured"),
            RuntimeCapability("readonly_source", CapabilityState.UNAVAILABLE,
                              "file permissions are not an OS isolation boundary"),
            RuntimeCapability("process_tree_termination", CapabilityState.UNAVAILABLE,
                              "process-group termination cannot prove no escaped descendants"),
            RuntimeCapability("workspace_limit", CapabilityState.UNAVAILABLE,
                              "local backend cannot impose an OS disk quota"),
        ]
        private = CapabilityState.UNAVAILABLE
        try:
            with tempfile.TemporaryDirectory(prefix="agentflow-capability-") as directory:
                path = Path(directory)
                if os.name == "posix" and path.stat().st_mode & 0o077 == 0:
                    private = CapabilityState.ENFORCED
        except OSError:
            pass
        capabilities.append(RuntimeCapability("private_temp", private,
                                              "private temporary directory" if private is CapabilityState.ENFORCED
                                              else "private temporary ACL could not be verified"))
        return RuntimeCapabilities(profile, "local-process", "1",
                                   tuple(sorted(capabilities, key=lambda item: item.name)))

    def run(self, request: RuntimeRequest, cancellation,
            on_spawn: Callable[[str], None],
            on_probe: Callable[[str], None]) -> RuntimeResult:
        try:
            executable, executable_digest = self._verify_executable(request)
            environment = self._environment(request)
        except (OSError, ValueError) as exc:
            return self._spawn_failure(request,
                                       f"allowlist preflight failed: {type(exc).__name__}: {exc}")
        try:
            version, probe_identity, probe_stopped = self._probe_version(
                executable, request, environment, on_probe, executable_digest)
        except OSError as exc:
            return self._spawn_failure(request, f"version probe spawn failed: {type(exc).__name__}")
        if not probe_stopped:
            return self._spawn_failure(request, "version probe termination could not be verified",
                                       timed_out=True, process_identity_value=probe_identity,
                                       process_stopped=False)
        if request.command.expected_version.casefold() not in version.casefold():
            return self._spawn_failure(request, "installed executable version diverges from allowlist",
                                       process_identity_value=probe_identity)

        argv = [str(executable), *request.arguments]
        cwd = request.cwd.resolve(strict=True)
        output = request.output_directory.resolve(strict=True)
        if request.cwd.is_symlink() or request.output_directory.is_symlink():
            return self._spawn_failure(request, "cwd/output may not be symlinks")
        if not cwd.is_dir() or not output.is_dir():
            return self._spawn_failure(request, "cwd/output directory is invalid")
        temporary = output / ".tmp"
        try:
            temporary.mkdir(mode=0o700, exist_ok=False)
        except OSError as exc:
            return self._spawn_failure(request, f"private temp unavailable: {type(exc).__name__}")
        if os.name == "posix" and temporary.stat().st_mode & 0o077:
            shutil.rmtree(temporary, ignore_errors=True)
            return self._spawn_failure(request, "private temp permissions not verified")

        capture = _LogCollector(request.limits.output_bytes)
        process = None
        readers: list[threading.Thread] = []
        process_group = None
        identity = ""
        timed_out = cancelled = False
        stopped = False
        reason = ""
        process_started = False
        cleanup_safe = True
        try:
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       shell=False, start_new_session=os.name == "posix",
                                       creationflags=flags)
        except OSError as exc:
            self._remove_owned_temp(temporary)
            return self._spawn_failure(
                request, f"validator spawn failed: {type(exc).__name__}")
        try:
            process_started = True
            cleanup_safe = False
            process_group = process.pid if os.name == "posix" else None
            identity = process_identity(process.pid, process_group=process_group,
                                        executable_digest=executable_digest)
            on_spawn(identity)
            readers = self._start_readers(process, capture)
            deadline = time.monotonic() + request.limits.timeout_seconds
            while process.poll() is None:
                if cancellation.is_set():
                    cancelled = True
                    stopped = self._terminate(process, identity, process_group)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    stopped = self._terminate(process, identity, process_group)
                    break
                time.sleep(0.025)
            if process.poll() is not None and not timed_out and not cancelled:
                stopped = self._clean_residual_group(process, identity, process_group)
            if process.poll() is None:
                process.wait(timeout=self.grace_seconds + 1)
            for reader in readers:
                reader.join(timeout=self.grace_seconds + 1)
            if any(reader.is_alive() for reader in readers):
                stopped = False
                self._kill_root(process, identity)
                for reader in readers:
                    reader.join(timeout=1)
            exit_code = process.returncode
            cleanup_safe = stopped
        except BaseException:
            if process is not None and process.poll() is None:
                stopped = self._terminate(process, identity, process_group)
                cleanup_safe = stopped
            raise
        finally:
            if not process_started or cleanup_safe:
                self._remove_owned_temp(temporary)

        digests, counts, truncated, sanitized = capture.finish(output)
        capabilities = self.inspect_capabilities(RuntimeProfile.LOCAL_EXPLICIT)
        return RuntimeResult(exit_code, timed_out, cancelled, False, identity,
                             output / "stdout.log", output / "stderr.log",
                             digests["stdout"], digests["stderr"],
                             counts["stdout"], counts["stderr"], truncated,
                             capabilities, reason, stopped, sanitized)

    def reconcile_process(self, identity: str) -> bool | None:
        try:
            data = json.loads(identity)
            pid = int(data["pid"])
            expected_start = data["start"]
            current = _read_process_start(pid)
            if current == expected_start:
                return False
            if current is not None:
                return None  # PID belongs to a different process now.
            if data.get("platform") == "linux":
                group = int(data.get("process_group", pid))
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    return None  # detached descendants cannot be disproved
                except PermissionError:
                    return False
                return False
            return None
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
            return None

    def _verify_executable(self, request: RuntimeRequest) -> tuple[Path, str]:
        command = request.command
        raw = Path(command.executable)
        if (not raw.is_absolute() or raw.suffix.casefold() in {".bat", ".cmd", ".ps1"}
                or raw.name.casefold() in {"sh", "bash", "zsh", "fish", "dash", "cmd",
                                           "cmd.exe", "powershell", "powershell.exe", "pwsh",
                                           "pwsh.exe"}):
            raise ValueError("executable must be absolute and cannot be a Windows shell script")
        resolved = raw.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("allowlisted executable is not a regular file")
        hasher = hashlib.sha256()
        with resolved.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                hasher.update(chunk)
        actual = "sha256:" + hasher.hexdigest()
        if actual != command.executable_digest:
            raise ValueError("allowlisted executable digest diverges")
        return resolved, actual

    def _environment(self, request: RuntimeRequest) -> dict[str, str]:
        supplied = dict(request.environment)
        if len(supplied) != len(request.environment):
            raise ValueError("duplicate runtime environment key")
        if any(name not in SAFE_ENV or SENSITIVE_ENV.search(name) for name in supplied):
            raise ValueError("runtime environment contains an unapproved or sensitive key")
        environment = {name: value for name, value in supplied.items()}
        if os.name == "nt":
            for name in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
                value = os.environ.get(name)
                if value:
                    environment[name] = value
        else:
            environment["PATH"] = "/usr/local/bin:/usr/bin:/bin"
            environment["LANG"] = environment.get("LANG", "C.UTF-8")
        return environment

    def _probe_version(self, executable: Path, request: RuntimeRequest,
                       environment: dict[str, str], on_probe,
                       executable_digest: str) -> tuple[str, str, bool]:
        process = subprocess.Popen([str(executable), "--version"], cwd=request.cwd,
                                   env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   shell=False, start_new_session=os.name == "posix",
                                   creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                                                 if os.name == "nt" else 0))
        identity = process_identity(process.pid,
                                    process_group=process.pid if os.name == "posix" else None,
                                    executable_digest=executable_digest)
        try:
            on_probe(identity)
        except BaseException:
            self._terminate(process, identity, process.pid if os.name == "posix" else None)
            raise
        output = bytearray()
        exceeded = threading.Event()

        def read_output():
            assert process.stdout is not None
            while chunk := process.stdout.read(1024):
                if len(output) + len(chunk) > 8192:
                    exceeded.set()
                    self._kill_root(process, identity)
                    break
                output.extend(chunk)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            process.wait(timeout=self.probe_timeout_seconds)
        except subprocess.TimeoutExpired:
            self._terminate(process, identity,
                            process.pid if os.name == "posix" else None)
            reader.join(timeout=1)
            return "", identity, False
        reader.join(timeout=1)
        if reader.is_alive() or exceeded.is_set() or process.returncode != 0:
            stopped = (process.poll() is not None and not reader.is_alive()
                       and self._clean_residual_group(
                           process, identity, process.pid if os.name == "posix" else None))
            return "", identity, stopped
        if not self._clean_residual_group(process, identity,
                                          process.pid if os.name == "posix" else None):
            return "", identity, False
        return output.decode("utf-8", "replace").strip(), identity, True

    @staticmethod
    def _start_readers(process: subprocess.Popen, capture: _LogCollector) -> list[threading.Thread]:
        def drain(stream, name):
            try:
                while chunk := stream.read(64 * 1024):
                    capture.add(name, chunk)
            finally:
                stream.close()
        assert process.stdout is not None and process.stderr is not None
        readers = [threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True)]
        for reader in readers:
            reader.start()
        return readers

    def _terminate(self, process: subprocess.Popen, identity: str,
                   process_group: int | None) -> bool:
        self._signal_group(process, identity, process_group, force=False)
        deadline = time.monotonic() + self.grace_seconds
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.025)
        if process.poll() is None:
            self._signal_group(process, identity, process_group, force=True)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self._kill_root(process, identity)
            process.wait(timeout=1)
        return False  # local process-group killing cannot prove no detached child exists

    @staticmethod
    def _signal_group(process: subprocess.Popen, identity: str,
                      process_group: int | None, *, force: bool) -> None:
        if process.poll() is None and not LocalProcessRuntime._identity_matches(identity):
            return
        try:
            if os.name == "posix" and process_group is not None:
                os.killpg(process_group, signal.SIGKILL if force else signal.SIGTERM)
            elif process.poll() is None:
                process.kill() if force else process.terminate()
        except (OSError, ProcessLookupError):
            pass

    @staticmethod
    def _identity_matches(identity: str) -> bool:
        try:
            value = json.loads(identity)
            return _read_process_start(int(value["pid"])) == value["start"]
        except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
            return False

    @classmethod
    def _kill_root(cls, process: subprocess.Popen, identity: str) -> None:
        if process.poll() is None and cls._identity_matches(identity):
            try:
                process.kill()
            except OSError:
                pass

    def _clean_residual_group(self, process: subprocess.Popen, identity: str,
                              process_group: int | None) -> bool:
        if os.name != "posix" or process_group is None:
            return process.poll() is not None
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        # Never signal a process group after its leader identity is gone: PGID reuse
        # could target an unrelated process. Report it unresolved instead.
        if not self._identity_matches(identity):
            return False
        self._signal_group(process, identity, process_group, force=True)
        return False

    @staticmethod
    def _remove_owned_temp(path: Path) -> None:
        try:
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            # Leave uncertain material for explicit recovery; never follow a link.
            pass

    def _spawn_failure(self, request: RuntimeRequest, reason: str, *,
                       timed_out: bool = False, process_identity_value: str = "",
                       process_stopped: bool = True) -> RuntimeResult:
        try:
            request.output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            pass
        empty = hashlib.sha256(b"").hexdigest()
        return RuntimeResult(None, timed_out, False, True, process_identity_value,
                             request.output_directory / "stdout.log",
                             request.output_directory / "stderr.log",
                             "sha256:" + empty, "sha256:" + empty, 0, 0, False,
                             self.inspect_capabilities(RuntimeProfile.LOCAL_EXPLICIT),
                             reason[:512], process_stopped, True)
