"""Local subprocess runtime independent of agent adapters."""

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from uuid import uuid4

from agentflow.agents import (Cancellation, ExecutionPolicy, ProcessIdentity,
                             ProcessResult, ProcessSpec, StartedCallback)


_KEY_VALUE = re.compile(r'(?i)((?:api[_-]?key|token|password|authorization)["\']?\s*[:=]\s*["\']?)([^"\'\s,}]+)')
_TOKEN = re.compile(r'sk-[A-Za-z0-9_-]{10,}')


class SpawnFailure(OSError):
    """The operating system confirmed that no child process was created."""


def redact(data: bytes) -> bytes:
    value = data.decode("utf-8", errors="replace")
    value = _KEY_VALUE.sub(lambda match: match.group(1) + "[REDACTED]", value)
    value = _TOKEN.sub("[REDACTED]", value)
    return value.encode("utf-8")


class LocalRuntime:
    def __init__(self, log_directory: Path):
        self.log_directory = log_directory

    @staticmethod
    def _linux_marker(pid: int) -> str:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        start_ticks = stat.rsplit(")", 1)[1].split()[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return f"{boot_id}:{start_ticks}"

    @staticmethod
    def _creation_marker(process: subprocess.Popen[bytes]) -> str:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
            values = [FILETIME() for _ in range(4)]
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
            api.GetProcessTimes.restype = wintypes.BOOL
            if not api.GetProcessTimes(int(process._handle), *(ctypes.byref(value) for value in values)):
                raise ctypes.WinError(ctypes.get_last_error())
            return str((values[0].high << 32) | values[0].low)
        return LocalRuntime._linux_marker(process.pid)

    @staticmethod
    def _self_creation_marker() -> str:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.GetCurrentProcess.restype = wintypes.HANDLE
            api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
            api.GetProcessTimes.restype = wintypes.BOOL
            times = [FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(api.GetCurrentProcess(), *(ctypes.byref(value) for value in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            return str((times[0].high << 32) | times[0].low)
        return LocalRuntime._linux_marker(os.getpid())

    @staticmethod
    def identity_matches(identity: ProcessIdentity) -> bool:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class FILETIME(ctypes.Structure):
                _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            api.OpenProcess.restype = wintypes.HANDLE
            api.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(FILETIME)] * 4)]
            api.GetProcessTimes.restype = wintypes.BOOL
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            api.CloseHandle.restype = wintypes.BOOL
            handle = api.OpenProcess(0x1000, False, identity.pid)
            if not handle:
                return False
            try:
                times = [FILETIME() for _ in range(4)]
                if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                    return False
                marker = str((times[0].high << 32) | times[0].low)
                return marker == identity.started_at and identity.group_id == str(identity.pid)
            finally:
                api.CloseHandle(handle)
        try:
            stat = Path(f"/proc/{identity.pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            return f"{boot_id}:{stat[19]}" == identity.started_at and stat[2] == identity.group_id
        except (OSError, IndexError):
            return False

    @classmethod
    def cancel_orphan(cls, identity: ProcessIdentity) -> bool:
        """Signal an orphan only when its PID creation marker and process group match."""
        if not cls.identity_matches(identity):
            return False
        if os.name == "nt":
            import ctypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            api.OpenProcess.restype = ctypes.c_void_p
            api.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            api.TerminateProcess.restype = ctypes.c_int
            api.CloseHandle.argtypes = [ctypes.c_void_p]
            api.CloseHandle.restype = ctypes.c_int
            handle = api.OpenProcess(0x0001, False, identity.pid)
            if not handle:
                return False
            try:
                if not cls.identity_matches(identity):
                    return False
                if not api.TerminateProcess(handle, 1):
                    return False
            finally:
                api.CloseHandle(handle)
            return True
        try:
            os.killpg(int(identity.group_id), signal.SIGTERM)
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(int(identity.group_id), 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        try:
            os.killpg(int(identity.group_id), signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[bytes], job=None) -> None:
        if os.name == "nt":
            if process.poll() is not None:
                if job is not None:
                    job.terminate()
                return
            if job is None:
                process.kill()
            else:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                    process.wait(timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                job.terminate()
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    process.poll()  # Reap the group leader before checking whether descendants remain.
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def run(self, spec: ProcessSpec, attempt_id: str, policy: ExecutionPolicy,
            on_started: StartedCallback, cancel: Cancellation) -> ProcessResult:
        if not spec.cwd.is_dir() or not spec.executable.is_file():
            raise SpawnFailure("executável ou diretório de trabalho inexistente")
        if not attempt_id.isascii() or not attempt_id.replace("-", "").isalnum():
            raise ValueError("attempt_id inválido para caminho de log")
        directory = self.log_directory / attempt_id
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise SpawnFailure("não foi possível preparar diretório de logs") from exc
        if os.name != "nt":
            os.chmod(directory, 0o700)
        stdout_path = directory / "stdout.log"
        stderr_path = directory / "stderr.log"
        handles = []
        try:
            handles = [open(stdout_path, "xb"), open(stderr_path, "xb")]
        except OSError as exc:
            for handle in handles:
                handle.close()
            raise SpawnFailure("não foi possível abrir logs antes do spawn") from exc
        if os.name != "nt":
            for path in (stdout_path, stderr_path):
                os.chmod(path, 0o600)
        digests = [hashlib.sha256(), hashlib.sha256()]
        counts = [0, 0]
        exceeded = threading.Event()
        log_failed = threading.Event()
        activity = [time.monotonic()]

        def drain(stream, index):
            pending = bytearray()
            while chunk := os.read(stream.fileno(), 4096):
                activity[0] = time.monotonic()
                if log_failed.is_set():
                    continue
                pending.extend(chunk)
                while b"\n" in pending or len(pending) >= 65536:
                    end = pending.find(b"\n") + 1 if b"\n" in pending else 65536
                    data = redact(bytes(pending[:end]))
                    del pending[:end]
                    remaining = max(0, policy.log_limit_bytes - counts[index])
                    captured = data[:remaining]
                    try:
                        handles[index].write(captured)
                        handles[index].flush()
                    except OSError:
                        log_failed.set()
                        continue
                    digests[index].update(captured)
                    counts[index] += len(captured)
                    if len(data) > remaining:
                        exceeded.set()
            if pending:
                data = redact(bytes(pending))
                remaining = max(0, policy.log_limit_bytes - counts[index])
                captured = data[:remaining]
                try:
                    handles[index].write(captured)
                    handles[index].flush()
                except OSError:
                    log_failed.set()
                    return
                digests[index].update(captured)
                counts[index] += len(captured)
                if len(data) > remaining:
                    exceeded.set()

        process = None
        job = None
        try:
            inherited = ("PATH", "HOME", "USERPROFILE", "CODEX_HOME", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA")
            env = {key: os.environ[key] for key in inherited if key in os.environ}
            env.update(dict(spec.environment))
            try:
                process = subprocess.Popen([str(spec.executable), *spec.arguments], cwd=spec.cwd,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           env=env, start_new_session=(os.name != "nt"),
                                           creationflags=((subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004) if os.name == "nt" else 0))
            except OSError as exc:
                raise SpawnFailure("sistema operacional não criou subprocesso") from exc
            if os.name == "nt":
                from .windows_job import WindowsJob
                job = WindowsJob(process)
            identity = ProcessIdentity(process.pid, self._creation_marker(process),
                                       str(process.pid), os.getpid(), attempt_id, uuid4().hex,
                                       self._self_creation_marker())
            try:
                on_started(identity)
            except BaseException:
                self._terminate_tree(process, job)
                raise
            workers = [threading.Thread(target=drain, args=(stream, index), daemon=True)
                       for index, stream in enumerate((process.stdout, process.stderr))]
            for worker in workers:
                worker.start()
            try:
                process.stdin.write(spec.stdin.encode("utf-8"))
                process.stdin.close()
            except BrokenPipeError:
                pass
            deadline = time.monotonic() + policy.timeout_seconds
            status = ""
            while process.poll() is None:
                flag = directory / "cancel.flag"
                if cancel.is_set() or flag.exists():
                    status = "cancelled"
                    break
                if exceeded.is_set():
                    status = "process_failed"
                    break
                if log_failed.is_set():
                    status = "process_failed"
                    break
                if time.monotonic() >= deadline:
                    status = "timed_out"
                    break
                if policy.idle_seconds is not None and time.monotonic() - activity[0] >= policy.idle_seconds:
                    status = "timed_out"
                    break
                time.sleep(0.02)
            if status:
                self._terminate_tree(process, job)
            process.wait()
            if os.name != "nt":
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    self._terminate_tree(process, job)
                    if not status:
                        status = "process_failed"
            for worker in workers:
                worker.join(timeout=5)
            if not status and (cancel.is_set() or (directory / "cancel.flag").exists()):
                status = "cancelled"
            if not status:
                status = "process_succeeded" if process.returncode == 0 else "process_failed"
            reason = "log_write_failed" if log_failed.is_set() else "log_limit" if exceeded.is_set() else status
            if log_failed.is_set():
                status = "process_failed"
            result = ProcessResult(status, process.returncode, stdout_path, stderr_path,
                                   digests[0].hexdigest(), digests[1].hexdigest(), counts[0], counts[1],
                                   reason, (("adapter_id", spec.adapter_name),
                                            ("adapter_api_version", spec.adapter_api_version),
                                            ("provider_id", spec.provider_id),
                                            ("model_id", spec.model_id)))
            metadata = {"attempt_id": attempt_id, "status": result.status, "exit_code": result.exit_code,
                        "reason": result.reason, "stdout_digest": result.stdout_digest,
                        "stderr_digest": result.stderr_digest, "stdout_bytes": result.stdout_bytes,
                        "stderr_bytes": result.stderr_bytes, "truncated": exceeded.is_set()}
            temporary = directory / "metadata.json.tmp"
            try:
                temporary.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                os.replace(temporary, directory / "metadata.json")
            except OSError:
                if result.status == "process_succeeded":
                    result = ProcessResult("process_failed", result.exit_code, result.stdout_path, result.stderr_path,
                                           result.stdout_digest, result.stderr_digest, result.stdout_bytes,
                                           result.stderr_bytes, "log_write_failed", result.request_metadata)
            return result
        finally:
            if process is not None and process.poll() is None:
                self._terminate_tree(process, job)
            if job is not None:
                job.close()
            for handle in handles:
                handle.close()
