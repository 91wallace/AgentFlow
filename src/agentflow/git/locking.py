"""Portable nonblocking workspace locks with durable owner records."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import uuid

from .contracts import GitConflict, GitError


def process_identity(pid: int | None = None) -> dict[str, object]:
    pid = os.getpid() if pid is None else pid
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                          ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME)]
        kernel.GetProcessTimes.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.GetCurrentProcess() if pid == os.getpid() else kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            raise GitError("não foi possível verificar identidade do processo no Windows")
        created, exited, kernel_time, user_time = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        try:
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise GitError("GetProcessTimes falhou")
            start = (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            if pid != os.getpid():
                kernel.CloseHandle(handle)
        return {"pid": pid, "started": str(start), "boot_id": "windows-boot-unknown",
                "host": socket.gethostname()}

    if os.name == "posix" and Path(f"/proc/{pid}/stat").is_file():
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        if len(fields) <= 19:
            raise GitError("/proc stat não contém horário de início")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        return {"pid": pid, "started": fields[19], "boot_id": boot, "host": socket.gethostname()}
    raise GitError("identidade de processo verificável indisponível nesta plataforma")


class WorkspaceLock:
    """Kernel lock; owner JSON is diagnostic and never authorizes stale-lock theft."""

    def __init__(self, path: Path, operation_id: str = ""):
        self.path = path
        self.operation_id = operation_id
        self._file = None
        self.previous_owner: dict[str, object] | None = None
        self.owner: dict[str, object] | None = None
        self._locked = False

    def acquire(self, operation_id: str) -> dict[str, object] | None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        self._file = os.fdopen(fd, "r+b", buffering=0)
        if self.path.stat().st_size == 0:
            self._file.write(b"\0")
            self._file.flush()
        try:
            self._file.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise GitConflict("workspace ocupado por outro processo") from exc
                self._locked = True
            elif os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise GitConflict("workspace ocupado por outro processo") from exc
                self._locked = True
            else:
                raise GitError("locks suportados somente em Windows e Linux")
            self._file.seek(1)
            previous = self._file.read()
            try:
                parsed = json.loads(previous.decode("utf-8")) if previous else None
                self.previous_owner = parsed if isinstance(parsed, dict) else None
            except (UnicodeError, json.JSONDecodeError):
                self.previous_owner = {"corrupt": True}
            self.owner = {"token": uuid.uuid4().hex, **process_identity(), "operation_id": operation_id}
            encoded = json.dumps(self.owner, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self._file.seek(1)
            self._file.truncate(1)
            self._file.write(encoded)
            self._file.flush()
            os.fsync(self._file.fileno())
            return self.previous_owner
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        if self._file is None:
            return
        try:
            self._file.seek(0)
            if self._locked and os.name == "nt":
                import msvcrt
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._locked and os.name == "posix":
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._locked = False

    def __enter__(self) -> "WorkspaceLock":
        self.acquire(self.operation_id)
        return self

    def __exit__(self, *_: object) -> None:
        self.release()
