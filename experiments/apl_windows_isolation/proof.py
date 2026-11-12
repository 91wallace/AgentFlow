"""Experimental AppContainer proof for APL workers (Windows only).

This script is not imported by AgentFlow. It creates a uniquely named,
capability-free AppContainer profile and synthetic resources under
the existing Windows temporary directory, runs a local synthetic worker in a Job Object,
records results, then removes the profile and temporary tree.
"""

from __future__ import annotations

import ctypes
import json
import os
import msvcrt
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from uuid import uuid4


if os.name != "nt":
    raise SystemExit("Windows only")

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
userenv = ctypes.WinDLL("userenv", use_last_error=True)


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", ctypes.c_void_p),
                ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


def bind(dll, name, restype, argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, argtypes
    return fn


CreateProfile = bind(userenv, "CreateAppContainerProfile", ctypes.c_long,
    [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
     wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)])
DeleteProfile = bind(userenv, "DeleteAppContainerProfile", ctypes.c_long, [wintypes.LPCWSTR])
FreeSid = bind(advapi, "FreeSid", ctypes.c_void_p, [ctypes.c_void_p])
ConvertSid = bind(advapi, "ConvertSidToStringSidW", wintypes.BOOL,
                  [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)])
LocalFree = bind(kernel, "LocalFree", ctypes.c_void_p, [ctypes.c_void_p])
CreateJob = bind(kernel, "CreateJobObjectW", wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR])
SetJobInfo = bind(kernel, "SetInformationJobObject", wintypes.BOOL,
                  [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD])
AssignJob = bind(kernel, "AssignProcessToJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE])
TerminateJob = bind(kernel, "TerminateJobObject", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT])
QueryJobInfo = bind(kernel, "QueryInformationJobObject", wintypes.BOOL,
                    [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                     ctypes.POINTER(wintypes.DWORD)])
ResumeThread = bind(kernel, "ResumeThread", wintypes.DWORD, [wintypes.HANDLE])
WaitForSingleObject = bind(kernel, "WaitForSingleObject", wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD])
GetExitCode = bind(kernel, "GetExitCodeProcess", wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)])
CloseHandle = bind(kernel, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE])
TerminateProcess = bind(kernel, "TerminateProcess", wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT])
InitAttrs = bind(kernel, "InitializeProcThreadAttributeList", wintypes.BOOL,
                 [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)])
UpdateAttr = bind(kernel, "UpdateProcThreadAttribute", wintypes.BOOL,
                  [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                   ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p])
DeleteAttrs = bind(kernel, "DeleteProcThreadAttributeList", None, [ctypes.c_void_p])
CreateProcess = bind(kernel, "CreateProcessW", wintypes.BOOL,
    [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
     wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOEXW),
     ctypes.POINTER(PROCESS_INFORMATION)])


def winerror(where):
    return OSError(ctypes.get_last_error(), f"{where} failed", None, ctypes.get_last_error())


def set_acl(path: Path, *args: str):
    result = subprocess.run(["icacls.exe", str(path), *args], capture_output=True,
                            text=True, check=False, timeout=10)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().replace("\r", " ").replace("\n", " ")
        raise RuntimeError(f"icacls rejected synthetic path ({result.returncode}): {detail[:300]}")


def describe_acl(path: Path, app_sid: str) -> str:
    result = subprocess.run(["icacls.exe", str(path)], capture_output=True, text=True,
                            check=False, timeout=5)
    if result.returncode:
        return f"icacls_query_failed:{result.returncode}"
    return result.stdout.replace(app_sid, "<appcontainer-sid>").strip()


def set_synthetic_acls(root: Path, protected: Path, output: Path, app_sid: str):
    # Every ACL mutation targets a newly-created experiment directory only.
    set_acl(root, "/inheritance:r", "/grant:r", "*S-1-5-32-544:(OI)(CI)(F)",
            f"*{app_sid}:(X)")
    set_acl(root, "/setowner", "*S-1-5-18")
    set_acl(protected, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)(F)",
            "*S-1-5-32-544:(OI)(CI)(F)", f"*{app_sid}:(X)")
    set_acl(protected, "/setowner", "*S-1-5-18")
    for protected_item in (protected / "protected-data.txt", protected / "synthetic-credential.txt"):
        set_acl(protected_item, "/inheritance:r", "/grant:r", "*S-1-5-18:(F)",
                "*S-1-5-32-544:(F)")
        set_acl(protected_item, "/setowner", "*S-1-5-18")
    set_acl(output, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)(F)",
            "*S-1-5-32-544:(OI)(CI)(F)", f"*{app_sid}:(OI)(CI)(M)", "/T", "/C")
    set_acl(output, "/setowner", "*S-1-5-18", "/T", "/C")


def appcontainer_attributes(app_sid_ptr):
    size = ctypes.c_size_t()
    InitAttrs(None, 1, 0, ctypes.byref(size))
    if not size.value:
        raise winerror("InitializeProcThreadAttributeList(size)")
    storage = ctypes.create_string_buffer(size.value)
    attrs = ctypes.cast(storage, ctypes.c_void_p)
    if not InitAttrs(attrs, 1, 0, ctypes.byref(size)):
        raise winerror("InitializeProcThreadAttributeList")
    caps = SECURITY_CAPABILITIES(app_sid_ptr, None, 0, 0)
    # PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009.
    if not UpdateAttr(attrs, 0, 0x00020009, ctypes.byref(caps), ctypes.sizeof(caps), None, None):
        DeleteAttrs(attrs)
        raise winerror("UpdateProcThreadAttribute(AppContainer)")
    return storage, attrs, caps


def job_snapshot(job):
    accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    if not QueryJobInfo(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
        raise winerror("QueryInformationJobObject(accounting)")
    limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not QueryJobInfo(job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None):
        raise winerror("QueryInformationJobObject(limits)")
    return {"active_processes": accounting.ActiveProcesses,
            "total_processes": accounting.TotalProcesses,
            "peak_job_memory_bytes": limits.PeakJobMemoryUsed,
            "job_memory_limit_bytes": limits.JobMemoryLimit,
            "limit_flags": limits.BasicLimitInformation.LimitFlags}


def run_worker(app_sid_ptr, script, *, timeout_seconds=12, memory_limit=512 * 1024 * 1024,
               active_process_limit=1, cancel_after=None, executable=None, arguments=(),
               observe_active_processes=1):
    executable = str(executable or (Path(os.environ["WINDIR"]) / "System32" /
                                    "WindowsPowerShell" / "v1.0" / "powershell.exe"))
    if not arguments:
        if Path(executable).name.lower() == "powershell.exe":
            arguments = ("-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                         __import__("base64").b64encode(script.encode("utf-16le")).decode("ascii"))
        else:
            arguments = ("-e", script)
    command = subprocess.list2cmdline([executable, *arguments])
    size = ctypes.c_size_t()
    InitAttrs(None, 2, 0, ctypes.byref(size))
    storage = ctypes.create_string_buffer(size.value)
    attrs = ctypes.cast(storage, ctypes.c_void_p)
    if not InitAttrs(attrs, 2, 0, ctypes.byref(size)):
        raise winerror("InitializeProcThreadAttributeList")
    caps = SECURITY_CAPABILITIES(app_sid_ptr, None, 0, 0)
    if not UpdateAttr(attrs, 0, 0x00020009, ctypes.byref(caps), ctypes.sizeof(caps), None, None):
        DeleteAttrs(attrs)
        raise winerror("UpdateProcThreadAttribute(AppContainer)")

    job = CreateJob(None, None)
    if not job:
        DeleteAttrs(attrs)
        raise winerror("CreateJobObjectW")
    process_info = PROCESS_INFORMATION()
    stdout_path = None
    stderr_path = None
    try:
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x0200 | 0x0008  # kill-on-close, job memory, active process count
        limits.BasicLimitInformation.ActiveProcessLimit = active_process_limit
        limits.JobMemoryLimit = memory_limit
        if not SetJobInfo(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise winerror("SetInformationJobObject")
        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 1 | 0x00000100  # STARTF_USESHOWWINDOW | STARTF_USESTDHANDLES
        startup.StartupInfo.wShowWindow = 0
        stdout_path = tempfile.TemporaryFile()
        stderr_path = tempfile.TemporaryFile()
        stdout_handle = wintypes.HANDLE(msvcrt.get_osfhandle(stdout_path.fileno()))
        stderr_handle = wintypes.HANDLE(msvcrt.get_osfhandle(stderr_path.fileno()))
        for handle in (stdout_handle, stderr_handle):
            if not kernel.SetHandleInformation(handle, 1, 1):
                raise winerror("SetHandleInformation(inheritable stdout/stderr)")
        startup.StartupInfo.hStdInput = wintypes.HANDLE(0)
        startup.StartupInfo.hStdOutput = stdout_handle
        startup.StartupInfo.hStdError = stderr_handle
        inherited_handles = (wintypes.HANDLE * 2)(stdout_handle, stderr_handle)
        startup.lpAttributeList = attrs
        # PROC_THREAD_ATTRIBUTE_HANDLE_LIST limits inheritance to the two
        # capture files. No unrelated inheritable parent handles cross boundary.
        if not UpdateAttr(attrs, 0, 0x00020002, ctypes.byref(inherited_handles),
                          ctypes.sizeof(inherited_handles), None, None):
            raise winerror("UpdateProcThreadAttribute(HANDLE_LIST)")
        allowed_env = ("SystemDrive", "SystemRoot", "WINDIR", "USERPROFILE", "APPDATA",
                       "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC", "OS")
        env_values = [f"{key}={os.environ[key]}" for key in allowed_env if key in os.environ]
        env_values.extend((f"PATH={Path(os.environ['WINDIR']) / 'System32'}",
                           f"TEMP={os.environ['WINDIR']}\\Temp", f"TMP={os.environ['WINDIR']}\\Temp"))
        env_values = sorted(env_values, key=str.casefold)
        # create_unicode_buffer appends one terminator; append one here so the
        # Windows environment block ends with exactly two NUL wide characters.
        env = ctypes.create_unicode_buffer("\0".join(env_values) + "\0")
        cmdline = ctypes.create_unicode_buffer(command)
        flags = 0x00080000 | 0x00000004 | 0x08000000 | 0x00000400  # EXTENDED, SUSPENDED, NO_WINDOW, UNICODE_ENV
        if not CreateProcess(executable, cmdline, None, None, True, flags,
                             ctypes.cast(env, ctypes.c_void_p), str(Path(executable).parent),
                             ctypes.byref(startup), ctypes.byref(process_info)):
            raise winerror("CreateProcessW(AppContainer)")
        if not AssignJob(job, process_info.hProcess):
            TerminateProcess(process_info.hProcess, 1)
            raise winerror("AssignProcessToJobObject")
        if ResumeThread(process_info.hThread) == 0xFFFFFFFF:
            TerminateJob(job, 1)
            raise winerror("ResumeThread")

        started_at = time.monotonic()
        deadline = started_at + (cancel_after if cancel_after is not None else timeout_seconds)
        deadline_expired = False
        maximum_active_processes = 0
        active_before_termination = 0
        while True:
            current = job_snapshot(job)
            maximum_active_processes = max(maximum_active_processes, current["active_processes"])
            if current["active_processes"] >= observe_active_processes:
                active_before_termination = current["active_processes"]
            if WaitForSingleObject(process_info.hProcess, 50) == 0:
                status = "exited"
                break
            if time.monotonic() >= deadline:
                deadline_expired = True
                status = "cancelled" if cancel_after is not None else "timed_out"
                termination_started = time.monotonic()
                if not TerminateJob(job, 1):
                    raise winerror("TerminateJobObject")
                if WaitForSingleObject(process_info.hProcess, 10000) != 0:
                    raise RuntimeError("worker process did not terminate after Job Object termination")
                termination_duration = time.monotonic() - termination_started
                break
        if not deadline_expired:
            termination_started = time.monotonic()
        exit_code = wintypes.DWORD()
        if not GetExitCode(process_info.hProcess, ctypes.byref(exit_code)):
            raise winerror("GetExitCodeProcess")
        if status == "exited" and not TerminateJob(job, 1):
            raise winerror("TerminateJobObject(after worker exit)")
        if not deadline_expired:
            termination_duration = time.monotonic() - termination_started
        snapshot = job_snapshot(job)
        settle_deadline = time.monotonic() + 5
        while snapshot["active_processes"] and time.monotonic() < settle_deadline:
            time.sleep(0.05)
            snapshot = job_snapshot(job)
        if snapshot["active_processes"]:
            raise RuntimeError("Job Object still reports active descendants after termination")
        stdout_path.seek(0)
        stderr_path.seek(0)
        stdout_text = stdout_path.read(4096).decode("utf-8", errors="replace")
        stderr_text = stderr_path.read(4096).decode("utf-8", errors="replace")
        if status == "exited" and exit_code.value in (0xC0000142, 106):
            status = "runtime_initialization_failed"
        if status == "exited" and exit_code.value == 0xC000013A:
            status = "terminated_by_job"
        return {"status": status, "deadline_expired": deadline_expired,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "termination_seconds": round(termination_duration, 3),
                "active_before_termination": active_before_termination,
                "maximum_active_processes": maximum_active_processes,
                "exit_code": exit_code.value,
                "stdout": stdout_text, "stderr": stderr_text,
                "process_id": process_info.dwProcessId, **snapshot}
    finally:
        if process_info.hProcess:
            # On every exceptional path, the job is killed before handles close.
            TerminateJob(job, 1)
        if process_info.hThread:
            CloseHandle(process_info.hThread)
        if process_info.hProcess:
            CloseHandle(process_info.hProcess)
        CloseHandle(job)
        DeleteAttrs(attrs)
        if stdout_path is not None:
            stdout_path.close()
        if stderr_path is not None:
            stderr_path.close()


def ps_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    run_id = uuid4().hex
    name = "AgentFlowAplProof-" + run_id
    temp_root = Path(os.environ["WINDIR"]) / "Temp"
    root = temp_root / name
    protected, output = root / "protected", root / "output"
    evidence = {"run_id": run_id, "platform": sys.platform, "status": "blocked",
                "appcontainer_profile_created": False, "appcontainer_profile_removed": False,
                "tests": {}, "cleanup": {}}
    sid_ptr = ctypes.c_void_p()
    profile_created = False
    listener = None
    try:
        if not temp_root.is_dir() or root.exists():
            raise RuntimeError("synthetic experiment root is unavailable or collides")
        hr = CreateProfile(name, name, "Temporary AgentFlow APL proof; zero capabilities",
                           None, 0, ctypes.byref(sid_ptr))
        if hr < 0:
            raise OSError(f"CreateAppContainerProfile HRESULT=0x{hr & 0xffffffff:08X}")
        profile_created = True
        evidence["appcontainer_profile_created"] = True
        if not sid_ptr.value:
            raise RuntimeError("AppContainer profile returned no SID")
        rendered = wintypes.LPWSTR()
        if not ConvertSid(sid_ptr, ctypes.byref(rendered)):
            raise winerror("ConvertSidToStringSidW")
        app_sid = rendered.value
        LocalFree(rendered)
        protected.mkdir(parents=True)
        output.mkdir()
        protected_file = protected / "protected-data.txt"
        credential_file = protected / "synthetic-credential.txt"
        protected_file.write_text("SYNTHETIC-PROTECTED-DATA", encoding="utf-8")
        credential_file.write_text("SYNTHETIC-API-TOKEN-NOT-REAL", encoding="utf-8")
        set_synthetic_acls(root, protected, output, app_sid)
        evidence["acl_setup"] = {"scope": "new experiment directories only", "owner_protected": "SYSTEM",
                                 "parent_access": "SYSTEM and local Administrators",
                                 "protected_read_granted_to_appcontainer": False,
                                 "output_write_granted_to_appcontainer": True,
                                 "protected_acl": describe_acl(protected_file, app_sid),
                                 "output_acl": describe_acl(output, app_sid)}

        cmd_exe = str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe")
        smoke_file = output / "appcontainer-smoke.txt"
        smoke_args = ("/d", "/c", f"echo appcontainer-smoke>{smoke_file}")
        smoke = run_worker(sid_ptr, "", executable=cmd_exe, arguments=smoke_args,
                           timeout_seconds=5, memory_limit=256 * 1024 * 1024,
                           active_process_limit=1)
        evidence["tests"]["appcontainer_smoke"] = {"worker": smoke,
            "output_written": smoke_file.exists() and smoke_file.read_text(encoding="utf-8").strip() == "appcontainer-smoke"}
        if smoke["status"] != "exited" or smoke["exit_code"] != 0 or not evidence["tests"]["appcontainer_smoke"]["output_written"]:
            raise RuntimeError("AppContainer smoke worker did not execute and write its synthetic output")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]
        try:
            control = socket.create_connection(("127.0.0.1", port), timeout=2)
            accepted, _ = listener.accept()
            accepted.close()
            control.close()
            host_loopback_control = True
        except OSError:
            host_loopback_control = False
        # Copy the interpreter/runtime into this run's output directory so the
        # AppContainer receives execute/read access without granting workspace access.
        import shutil as _shutil
        py_root = Path(sys.base_prefix)
        worker_root = output / "runtime"
        worker_root.mkdir()
        for executable_name in ("python.exe", "python3.dll", "python313.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            source = py_root / executable_name
            if source.exists():
                _shutil.copy2(source, worker_root / executable_name)
        _shutil.copytree(py_root / "DLLs", worker_root / "DLLs",
                         ignore=_shutil.ignore_patterns("tcl*.dll", "tk*.dll"))
        _shutil.copytree(py_root / "Lib", worker_root / "Lib",
                         ignore=_shutil.ignore_patterns("__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo"))
        (worker_root / "python._pth").write_text("python313.zip\n.\\Lib\n.\\DLLs\n", encoding="ascii")
        # The standard library archive is available in some official installs;
        # if absent, make a small standard-library archive from Lib ourselves.
        zip_candidates = (py_root / "python313.zip", py_root / "DLLs" / "python313.zip")
        zip_source = next((candidate for candidate in zip_candidates if candidate.exists()), None)
        if zip_source is not None:
            _shutil.copy2(zip_source, worker_root / "python313.zip")
        else:
            import zipfile
            with zipfile.ZipFile(worker_root / "python313.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                for item in (worker_root / "Lib").rglob("*"):
                    if item.is_file() and item.suffix in (".py", ".pyc"):
                        archive.write(item, item.relative_to(worker_root / "Lib").as_posix())
        py_worker = worker_root / "python.exe"
        read_code = "import pathlib,sys\np=pathlib.Path(sys.argv[1])\ntry:\n p.read_text(); print('READ_ALLOWED'); raise SystemExit(0)\nexcept PermissionError as e:\n print(type(e).__name__, e.errno); raise SystemExit(13)\nexcept OSError as e:\n print(type(e).__name__, e.errno); raise SystemExit(14)"
        write_code = "import pathlib,sys\np=pathlib.Path(sys.argv[1])\ntry:\n p.write_text('tamper'); print('WRITE_ALLOWED'); raise SystemExit(0)\nexcept PermissionError as e:\n print(type(e).__name__, e.errno); raise SystemExit(13)\nexcept OSError as e:\n print(type(e).__name__, e.errno); raise SystemExit(14)"
        read_worker = run_worker(sid_ptr, "", executable=str(py_worker),
            arguments=("-c", read_code, str(protected_file)), timeout_seconds=5,
            memory_limit=512 * 1024 * 1024, active_process_limit=1)
        write_worker = run_worker(sid_ptr, "", executable=str(py_worker),
            arguments=("-c", write_code, str(protected_file)), timeout_seconds=5,
            memory_limit=512 * 1024 * 1024, active_process_limit=1)
        credential_worker = run_worker(sid_ptr, "", executable=str(py_worker),
            arguments=("-c", read_code, str(credential_file)), timeout_seconds=5,
            memory_limit=512 * 1024 * 1024, active_process_limit=1)
        curl_exe = Path(os.environ["WINDIR"]) / "System32" / "curl.exe"
        if curl_exe.is_file():
            curl_error = output / "curl.err"
            network_worker = run_worker(sid_ptr, "", executable=str(py_worker),
                arguments=("-c", "import socket,sys; s=socket.socket(); s.settimeout(2);\n"
                           "try: s.connect(('127.0.0.1',int(sys.argv[1]))); print('CONNECTED'); raise SystemExit(0)\n"
                           "except OSError as e: print(type(e).__name__,e.errno); raise SystemExit(13)", str(port)),
                timeout_seconds=8, memory_limit=512 * 1024 * 1024, active_process_limit=2)
        else:
            network_worker = None
        try:
            conn, _ = listener.accept()
            conn.close()
            loopback_connected = True
        except (OSError, TimeoutError):
            loopback_connected = False
        listener.close()
        listener = None
        read_code, write_code, credential_code = (read_worker["exit_code"], write_worker["exit_code"],
                                                  credential_worker["exit_code"])
        curl_code = network_worker["exit_code"] if network_worker else None
        network_text = (network_worker["stdout"] + network_worker["stderr"]) if network_worker else ""
        if network_worker is None:
            network_classification = "not_executed"
        elif network_worker["status"] == "runtime_initialization_failed":
            network_classification = "bootstrap_failure"
        elif curl_code == 0 and "CONNECTED" in network_text:
            network_classification = "connection_allowed"
        elif "PermissionError" in network_text or "10013" in network_text:
            network_classification = "connection_denied"
        elif "TimeoutError" in network_text:
            network_classification = "timeout_inconclusive"
        else:
            network_classification = "worker_or_service_error"
        try:
            protected_unchanged = protected_file.read_text(encoding="utf-8") == "SYNTHETIC-PROTECTED-DATA"
        except OSError:
            protected_unchanged = None
        try:
            credential_unchanged = credential_file.read_text(encoding="utf-8") == "SYNTHETIC-API-TOKEN-NOT-REAL"
        except OSError:
            credential_unchanged = None
        read_stdout = read_worker["stdout"] + read_worker["stderr"]
        write_stdout = write_worker["stdout"] + write_worker["stderr"]
        credential_stdout = credential_worker["stdout"] + credential_worker["stderr"]
        network_output = (network_worker["stdout"] + network_worker["stderr"]) if network_worker else "curl not available"
        evidence["tests"]["filesystem_and_network"] = {
            "dispatch": "launched_in_appcontainer", "read_worker": read_worker,
            "read_exit_code": read_code, "read_stdout": read_stdout,
            "write_worker": write_worker, "write_exit_code": write_code,
            "write_stdout": write_stdout,
            "credential_worker": credential_worker, "credential_exit_code": credential_code,
            "credential_stdout": credential_stdout,
            "network_worker": network_worker, "curl_exit_code": curl_code,
            "curl_stderr": network_output,
            "network_classification": network_classification,
            "host_loopback_control": host_loopback_control,
            "loopback_listener_received_connection": loopback_connected,
            "synthetic_file_unchanged": protected_unchanged,
            "synthetic_credential_unchanged": credential_unchanged,
            "read_denied_by_os": read_code == 13 and "PermissionError" in read_stdout,
            "write_denied_by_os": write_code == 13 and "PermissionError" in write_stdout and protected_unchanged,
            "credential_read_denied_by_os": credential_code == 13 and "PermissionError" in credential_stdout and credential_unchanged,
            "network_denied_by_os": curl_code == 13 and "OSError" in network_output and host_loopback_control and not loopback_connected}
        # Python, unlike cmd.exe, makes OS access-denied exceptions observable.
        for scenario, worker in (("protected_read", read_worker), ("protected_write", write_worker),
                                 ("synthetic_credential_read", credential_worker)):
            evidence["tests"][scenario] = {"status": "os_denied" if worker["exit_code"] == 13 else "failed_or_allowed",
                                             "worker": worker}
        persistent_code = "import time; time.sleep(30)"
        timeout_worker = run_worker(sid_ptr, "", executable=str(py_worker),
            arguments=("-c", persistent_code), timeout_seconds=2,
            memory_limit=128 * 1024 * 1024, active_process_limit=1)
        cancel_worker = run_worker(sid_ptr, "", executable=str(py_worker),
            arguments=("-c", persistent_code), cancel_after=2,
            memory_limit=128 * 1024 * 1024, active_process_limit=1)
        evidence["tests"]["timeout"] = {"worker": timeout_worker,
            "status": "passed" if timeout_worker["status"] == "timed_out" and timeout_worker["active_processes"] == 0 else timeout_worker["status"]}
        evidence["tests"]["cancellation"] = {"worker": cancel_worker,
            "status": "passed" if cancel_worker["status"] == "cancelled" and cancel_worker["active_processes"] == 0 else cancel_worker["status"]}

        child_code = ("import subprocess,sys,time; "
                      "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                      "print('child',p.pid,flush=True); time.sleep(30)")
        descendant_worker = run_worker(sid_ptr, "", executable=str(py_worker),
            arguments=("-c", child_code), timeout_seconds=3,
            memory_limit=128 * 1024 * 1024, active_process_limit=2,
            observe_active_processes=2)
        descendants_confirmed = (descendant_worker["maximum_active_processes"] >= 3 and
                                 "child " in descendant_worker["stdout"])
        evidence["tests"]["tree_termination"] = {"status": "passed" if descendants_confirmed and descendant_worker["active_processes"] == 0 else "not_proven",
            "worker": descendant_worker, "descendant_observed": descendants_confirmed}
        evidence["tests"]["child_process_limit"] = {"status": "not_run", "reason": "A separate over-limit spawn attempt was not made; the descendant test uses the configured two-process allowance."}

        memory_code = ("import time; blocks=[]\nwhile True:\n"
                       " b=bytearray(4*1024*1024)\n"
                       " for i in range(0,len(b),4096): b[i]=1\n"
                       " blocks.append(b); time.sleep(.02)")
        memory_worker = run_worker(sid_ptr, "", executable=str(py_worker),
            arguments=("-c", memory_code), timeout_seconds=10,
            memory_limit=64 * 1024 * 1024, active_process_limit=1)
        memory_limited = (memory_worker["job_memory_limit_bytes"] == 64 * 1024 * 1024 and
                          0 < memory_worker["peak_job_memory_bytes"] <= 64 * 1024 * 1024 and
                          memory_worker["status"] == "exited" and memory_worker["exit_code"] != 0)
        evidence["tests"]["job_memory_limit"] = {"status": "enforced" if memory_limited else "not_proven",
            "worker": memory_worker, "configured_limit_bytes": 64 * 1024 * 1024,
            "peak_over_limit_bytes": max(0, memory_worker["peak_job_memory_bytes"] - 64 * 1024 * 1024)}
        evidence["status"] = "partial"
    except Exception as exc:
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
        evidence["status"] = "blocked"
    finally:
        for scenario in ("filesystem_and_network", "child_process_limit", "tree_timeout",
                         "cancellation", "job_memory_limit"):
            evidence["tests"].setdefault(scenario, {"status": "not_run",
                                                   "reason": "preflight or an earlier scenario did not complete"})
        if sid_ptr.value:
            FreeSid(sid_ptr)
        if listener is not None:
            listener.close()
        if root.exists():
            try:
                shutil.rmtree(root)
                evidence["cleanup"]["synthetic_directory_removed"] = True
            except OSError as exc:
                evidence["cleanup"]["synthetic_directory_removed"] = False
                evidence["cleanup"]["error"] = type(exc).__name__
        if profile_created:
            hr = DeleteProfile(name)
            evidence["appcontainer_profile_removed"] = hr >= 0
            evidence["cleanup"]["profile_delete_hresult"] = f"0x{hr & 0xffffffff:08X}"
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence["status"] == "passed" and evidence["appcontainer_profile_removed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
