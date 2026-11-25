"""Detached APL execution process and its persistent SQLite control plane."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid


class WorkerError(RuntimeError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _db(root):
    return Path(root).resolve() / ".agentflow" / "state.sqlite3"


def _identity(root):
    path = Path(root).resolve(strict=True)
    info = path.stat()
    return hashlib.sha256(f"{os.path.normcase(str(path))}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()


def _connect(path):
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.execute("PRAGMA busy_timeout=10000")
    return db


def _pid_alive(pid, marker):
    if not pid or not marker:
        return False
    try:
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
            handle = api.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                values = [FILETIME() for _ in range(4)]
                if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                    return False
                actual_marker = str((values[0].high << 32) | values[0].low)
                return actual_marker == marker
            finally:
                api.CloseHandle(handle)
        os.kill(pid, 0)
        from .runtime.local import LocalRuntime
        from .agents import ProcessIdentity
        actual_marker = LocalRuntime._linux_marker(pid)
        identity = ProcessIdentity(pid, str(Path(f"/proc/{pid}/exe").resolve()), str(pid), actual_marker, actual_marker)
        return actual_marker == marker and LocalRuntime.identity_matches(identity)
    except (OSError, ValueError, ImportError):
        return False


def _worker_marker():
    executable = sys.executable
    return hashlib.sha256(os.path.normcase(str(Path(executable).resolve())).encode()).hexdigest()


def _pid_marker(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class FILETIME(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            values = [FILETIME() for _ in range(4)]
            if not api.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                return ""
            return str((values[0].high << 32) | values[0].low)
        finally:
            api.CloseHandle(handle)
    from .runtime.local import LocalRuntime
    return LocalRuntime._linux_marker(pid)


def _registered_child(pid):
    from dataclasses import dataclass
    @dataclass(frozen=True)
    class Identity:
        pid: int
        executable: str
        process_group_id: int | None
        start_time: str
        creation_marker: str
    executable = str(Path(sys.executable).resolve())
    try:
        from .runtime.local import LocalRuntime
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
            handle = api.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    values = [FILETIME() for _ in range(4)]
                    if api.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
                        marker = str((values[0].high << 32) | values[0].low)
                        return Identity(pid, executable, pid, marker, marker)
                finally:
                    api.CloseHandle(handle)
        else:
            values = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
            boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            marker = LocalRuntime._linux_marker(pid)
            return Identity(pid, str(Path(f"/proc/{pid}/exe").resolve()), int(values[2]),
                            f"{boot}:{values[19]}", marker)
    except (ImportError, OSError, ValueError, IndexError):
        pass
    return Identity(pid, executable, None, "", "")


def _ensure_schema(path):
    from .state.sqlite import SQLiteStore
    with SQLiteStore(path, execution=True, git=True, validation=True, agent_models=True,
                     sdd=True, roles=True, review=True, workers=True):
        pass


def _record_worker(root, workspace_id, worker_id, pid, marker):
    path = _db(root)
    _ensure_schema(path)
    identity = _identity(root)
    now = _now()
    process_marker = _pid_marker(pid)
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT worker_id,root_path,root_identity,pid,process_marker FROM managed_workers WHERE workspace_id=?", (workspace_id,)).fetchone()
        if existing and existing[1:3] != (str(Path(root).resolve()), identity):
            db.rollback()
            raise WorkerError("worker_workspace_identity_conflict")
        if existing and _pid_alive(existing[3], existing[4]) and existing[0] != worker_id:
            db.rollback()
            raise WorkerError("worker_already_running:" + existing[0])
        run = db.execute("SELECT run_id FROM managed_worker_runs WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1", (workspace_id,)).fetchone()
        active_run_id = run[0] if run else None
        db.execute("INSERT INTO managed_workers(workspace_id,worker_id,state_db,active_run_id,root_path,root_identity,pid,process_marker,started_at,heartbeat_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET worker_id=excluded.worker_id,state_db=excluded.state_db,active_run_id=COALESCE(managed_workers.active_run_id,excluded.active_run_id),pid=excluded.pid,process_marker=excluded.process_marker,heartbeat_at=excluded.heartbeat_at,updated_at=excluded.updated_at",
                   (workspace_id, worker_id, str(path), active_run_id, str(Path(root).resolve()), identity, pid, process_marker, now, now, now))
        db.commit()


def _attach_run(root, workspace_id, run_id, start_digest):
    path = _db(root)
    now = _now()
    with _connect(path) as db:
        db.execute("BEGIN IMMEDIATE")
        worker = db.execute("SELECT 1 FROM managed_workers WHERE workspace_id=?", (workspace_id,)).fetchone()
        if not worker:
            db.rollback()
            raise WorkerError("worker_missing")
        prior = db.execute("SELECT workspace_id,start_digest FROM managed_worker_runs WHERE run_id=?", (run_id,)).fetchone()
        if prior and prior != (workspace_id, start_digest):
            db.rollback()
            raise WorkerError("worker_run_idempotency_conflict")
        db.execute("INSERT OR IGNORE INTO managed_worker_runs VALUES(?,?,?,?,?,?)",
                   (run_id, workspace_id, start_digest, "queued", now, now))
        db.execute("UPDATE managed_workers SET state_db=?,active_run_id=? WHERE workspace_id=?",
                   (str(path), run_id, workspace_id))
        db.commit()


def launch(root: Path, argv: list[str], *, operation_id: str):
    root = Path(root).resolve(strict=True)
    if not operation_id or len(operation_id) > 256:
        raise WorkerError("worker_operation_id_invalid")
    path = _db(root)
    if not path.is_file():
        raise WorkerError("worker_state_database_missing")
    workspace_id = _identity(root)
    identity_operation = next((argv[index + 1] for index, value in enumerate(argv[:-1])
                               if value == "--operation-id"), None)
    resume_run = next((argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--run-id"), None)
    if identity_operation != operation_id and not resume_run:
        raise WorkerError("managed_operation_id_mismatch")
    target_id = resume_run or operation_id
    if resume_run:
        with closing(_connect(path)) as db:
            prior = db.execute("SELECT start_digest FROM managed_worker_runs WHERE run_id=? AND workspace_id=?", (resume_run, workspace_id)).fetchone()
        if not prior:
            raise WorkerError("managed_resume_requires_existing_run")
        start_digest = prior[0]
    else:
        start_digest = hashlib.sha256(json.dumps({"root": str(root), "operation_id": operation_id}, sort_keys=True).encode()).hexdigest()
    with closing(_connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT start_digest,state FROM managed_worker_runs WHERE run_id=?", (target_id,)).fetchone()
        if prior and prior[0] != start_digest:
            db.rollback()
            raise WorkerError("worker_run_idempotency_conflict")
        if prior and not resume_run:
            worker = db.execute("SELECT worker_id FROM managed_workers WHERE workspace_id=?", (workspace_id,)).fetchone()
            db.rollback()
            return {"worker_id": worker[0] if worker else "", "workspace_id": workspace_id,
                    "run_id": target_id, "state": prior[1], "detached": True, "reused": True}
        row = db.execute("SELECT worker_id,pid,process_marker,root_path,root_identity FROM managed_workers WHERE workspace_id=?", (workspace_id,)).fetchone()
        if row and row[3:] != (str(root), workspace_id):
            db.rollback()
            raise WorkerError("worker_workspace_identity_conflict")
        if row and _pid_alive(row[1], row[2]):
            worker_id = row[0]
            db.rollback()
        else:
            db.rollback()
            worker_id = "af-worker-" + uuid.uuid4().hex
            command = [sys.executable, "-m", "agentflow.worker", "serve", str(root), worker_id]
            options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                       "stderr": subprocess.DEVNULL, "close_fds": True}
            if os.name == "nt":
                options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(command, **options)
            try:
                from .runtime.local import LocalRuntime
                LocalRuntime._register_child(_registered_child(process.pid))
            except (ImportError, AttributeError, OSError):
                pass
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with closing(_connect(path)) as check:
                    attached = check.execute("SELECT worker_id,pid,process_marker FROM managed_workers WHERE workspace_id=?", (workspace_id,)).fetchone()
                if attached and attached[0] == worker_id and _pid_alive(attached[1], attached[2]):
                    break
                time.sleep(0.05)
            else:
                db.rollback()
                if process.poll() is not None:
                    raise WorkerError("worker_start_failed")
                raise WorkerError("worker_start_timeout")
    payload = json.dumps({"root": str(root), "argv": argv, "operation_id": operation_id,
                          "workspace_id": workspace_id, "start_digest": start_digest}, separators=(",", ":"))
    try:
        with _connect(path) as db:
            db.execute("BEGIN IMMEDIATE")
            if resume_run:
                updated = db.execute("UPDATE managed_worker_runs SET state='queued',updated_at=? WHERE run_id=? AND workspace_id=?",
                                     (_now(), target_id, workspace_id)).rowcount
                if updated != 1:
                    raise WorkerError("managed_resume_requires_existing_run")
            else:
                db.execute("INSERT INTO managed_worker_runs VALUES(?,?,?,?,?,?)",
                           (target_id, workspace_id, start_digest, "queued", _now(), _now()))
            db.execute("INSERT INTO worker_commands(workspace_id,command_id,kind,payload_json,state,created_at) VALUES(?,?,?,?,?,?)",
                       (workspace_id, uuid.uuid4().hex, "start", payload, "queued", _now()))
            db.execute("UPDATE managed_workers SET state_db=?,active_run_id=? WHERE workspace_id=?",
                       (str(path), target_id, workspace_id))
            db.commit()
    except sqlite3.OperationalError as exc:
        # Command queue is part of the same migration and this transaction.
        raise WorkerError("worker_command_store_unavailable") from exc
    return {"worker_id": worker_id, "workspace_id": workspace_id, "run_id": target_id,
            "state": "queued", "detached": True}


def inspect(root, run_id=None):
    path = _db(root)
    if not path.is_file():
        return None
    with closing(_connect(path)) as db:
        worker = db.execute("SELECT worker_id,pid,process_marker,heartbeat_at,root_identity,state_db,active_run_id FROM managed_workers WHERE workspace_id=?", (_identity(root),)).fetchone()
        if not worker:
            return None
        if run_id:
            row = db.execute("SELECT r.run_id,r.state,r.start_digest,r.created_at,r.updated_at,w.workspace_id FROM managed_worker_runs r JOIN managed_workers w USING(workspace_id) WHERE r.run_id=? AND w.workspace_id=?", (run_id, _identity(root))).fetchone()
            if row is None:
                return None
            run = {"run_id": row[0], "state": row[1], "start_digest": row[2], "created_at": row[3], "updated_at": row[4]}
            try:
                apl = db.execute("SELECT status,reason,jobs_started,attempts,consumed_seconds FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                if apl:
                    run["apl"] = dict(zip(("status", "reason", "jobs_started", "attempts", "consumed_seconds"), apl))
            except sqlite3.OperationalError:
                pass
        else:
            run = None
        base, mapped_run = worker[5], worker[6]
        active = _pid_alive(worker[1], worker[2])
        if not active:
            try:
                from .runtime.local import LocalRuntime
                from .agents import ProcessIdentity
                marker = LocalRuntime._linux_marker(worker[1]) if os.name != "nt" else ""
                child = ProcessIdentity(worker[1], "", str(worker[1]), marker, marker)
                if marker and LocalRuntime.identity_matches(child):
                    LocalRuntime.cancel_orphan(child)
            except Exception:
                pass
        return {"worker_id": worker[0], "pid": worker[1], "active": active,
                "heartbeat_at": worker[3], "workspace_id": _identity(root), "run": run,
                "mapped_run_id": mapped_run or None, "state_db": base}


def command(root, kind, run_id=None):
    root = Path(root).resolve(strict=True)
    path, workspace_id = _db(root), _identity(root)
    if kind not in {"pause", "resume", "cancel"}:
        raise WorkerError("worker_command_invalid")
    status = inspect(root, run_id)
    if status is None or status["run"] is None:
        raise WorkerError("worker_run_not_found")
    if not status["active"]:
        raise WorkerError("worker_not_active; explicit safe restart required")
    run = status["run"].get("apl")
    apl_status = run["status"] if run else status["run"]["state"]
    if kind == "pause":
        if apl_status != "running":
            return {"run_id": run_id, "state": "not_running", "apl_status": apl_status}
        payload = json.dumps({"root": str(root), "run_id": run_id}, separators=(",", ":"))
        with _connect(path) as db:
            db.execute("UPDATE managed_worker_runs SET state='pause_requested',updated_at=? WHERE run_id=?", (_now(), run_id))
            db.execute("INSERT INTO worker_commands(workspace_id,command_id,kind,payload_json,state,created_at) VALUES(?,?,?,?,?,?)",
                       (workspace_id, uuid.uuid4().hex, "pause", payload, "queued", _now()))
        return {"run_id": run_id, "state": "pause_queued"}
    if kind == "resume":
        if apl_status == "running":
            return {"run_id": run_id, "state": "already_running"}
        payload = json.dumps({"root": str(root), "run_id": run_id}, separators=(",", ":"))
        with _connect(path) as db:
            db.execute("UPDATE managed_worker_runs SET state='queued',updated_at=? WHERE run_id=?", (_now(), run_id))
            db.execute("INSERT INTO worker_commands(workspace_id,command_id,kind,payload_json,state,created_at) VALUES(?,?,?,?,?,?)",
                       (workspace_id, uuid.uuid4().hex, "resume", payload, "queued", _now()))
        return {"run_id": run_id, "state": "resume_queued"}
    payload = json.dumps({"root": str(root), "run_id": run_id}, separators=(",", ":"))
    with _connect(path) as db:
        db.execute("UPDATE managed_worker_runs SET state='cancel_requested',updated_at=? WHERE run_id=?", (_now(), run_id))
        db.execute("INSERT INTO worker_commands(workspace_id,command_id,kind,payload_json,state,created_at) VALUES(?,?,?,?,?,?)",
                   (workspace_id, uuid.uuid4().hex, "cancel", payload, "queued", _now()))
    return {"run_id": run_id, "state": "cancel_queued"}


def _run_managed_command(path, command_id, kind, request):
    from .observability import LiveEventBus
    bus = LiveEventBus(path)
    event_run_id = request.get("run_id") or request.get("operation_id") or command_id
    bus.emit(event_run_id, f"worker_command_{kind}_started", payload={"command_id": command_id})
    try:
        if kind == "start":
            from .apl import SQLiteAuthorizedExecutionStore
            store = SQLiteAuthorizedExecutionStore(str(path), initialize=False)
            start_operation = request["operation_id"]
            local = store.local_contract_for_operation(start_operation)
            run = store.run_for_contract(local[0].contract_id) if local else None
            if run:
                with closing(_connect(path)) as db:
                    db.execute("UPDATE managed_worker_runs SET run_id=?,state='running',updated_at=? WHERE run_id=?",
                               (run["run_id"], _now(), start_operation))
            args = request["argv"]
        elif kind == "resume":
            args = ["apl", "run-resume", "--path", request["root"], "--run-id", request["run_id"]]
        else:
            args = ["apl", "run-cancel", "--path", request["root"], "--run-id", request["run_id"]]
        env = os.environ.copy()
        if kind == "start":
            env["AGENTFLOW_MANAGED_OPERATION"] = request["operation_id"]
        elif kind == "resume":
            env["AGENTFLOW_MANAGED_OPERATION"] = "resume:" + request["run_id"]
        completed = subprocess.run(
            [sys.executable, "-c", "import sys; from agentflow.cli import main; raise SystemExit(main(sys.argv[1:]))", *args],
            env=env,
        )
        result = completed.returncode
        state = "completed" if result == 0 else "failed"
        if kind == "start":
            with closing(_connect(path)) as db:
                runs = db.execute("SELECT run_id,status FROM apl_authorized_runs WHERE grant_id IN (SELECT grant_id FROM apl_local_execution_contracts WHERE operation_id=?)", (request["operation_id"],)).fetchall()
                for run_id, apl_state in runs:
                    managed_state = "completed" if apl_state == "completed" else "cancelled" if apl_state == "cancelled" else state
                    db.execute("UPDATE managed_worker_runs SET state=?,updated_at=? WHERE run_id=? AND start_digest=?", (managed_state, _now(), run_id, request["start_digest"]))
        else:
            with closing(_connect(path)) as db:
                apl = db.execute("SELECT status,reason FROM apl_authorized_runs WHERE run_id=?", (request["run_id"],)).fetchone()
                managed_state = "paused" if apl and apl[0] == "blocked" and apl[1] == "paused_at_task_boundary" else "completed" if apl and apl[0] == "completed" else "cancelled" if apl and apl[0] == "cancelled" else state
                db.execute("UPDATE managed_worker_runs SET state=?,updated_at=? WHERE run_id=?", (managed_state, _now(), request["run_id"]))
    except BaseException as exc:
        state = "unknown" if isinstance(exc, KeyboardInterrupt) else "failed"
        error = type(exc).__name__
        with closing(_connect(path)) as db:
            if kind == "start":
                runs = db.execute("SELECT run_id FROM apl_authorized_runs WHERE grant_id IN (SELECT grant_id FROM apl_local_execution_contracts WHERE operation_id=?)", (request.get("operation_id", ""),)).fetchall()
                for (run_id,) in runs:
                    db.execute("UPDATE managed_worker_runs SET state=?,updated_at=? WHERE run_id=?", (state, _now(), run_id))
            else:
                db.execute("UPDATE managed_worker_runs SET state=?,updated_at=? WHERE run_id=?", (state, _now(), request.get("run_id", "")))
            db.execute("UPDATE worker_commands SET state='failed',result_json=? WHERE command_id=?", (json.dumps({"error": error}), command_id))
    else:
        with closing(_connect(path)) as db:
            db.execute("UPDATE worker_commands SET state='completed',result_json=? WHERE command_id=?", (json.dumps({"exit_code": result}), command_id))


def _serve(root, worker_id):
    import threading
    root = Path(root).resolve(strict=True)
    workspace_id = _identity(root)
    _record_worker(root, workspace_id, worker_id, os.getpid(), _worker_marker())
    path = _db(root)
    active_thread = None
    while True:
        if active_thread is not None and not active_thread.is_alive():
            active_thread = None
        with _connect(path) as db:
            now = _now()
            db.execute("UPDATE managed_workers SET heartbeat_at=?,updated_at=? WHERE workspace_id=? AND worker_id=? AND pid=?",
                       (now, now, workspace_id, worker_id, os.getpid()))
            if active_thread is None:
                row = db.execute("SELECT command_id,kind,payload_json FROM worker_commands WHERE workspace_id=? AND state='queued' ORDER BY created_at,command_id LIMIT 1", (workspace_id,)).fetchone()
            else:
                row = db.execute("SELECT command_id,kind,payload_json FROM worker_commands WHERE workspace_id=? AND state='queued' AND kind IN ('pause','cancel') ORDER BY created_at,command_id LIMIT 1", (workspace_id,)).fetchone()
            if row:
                command_id, kind, payload = row
                changed = db.execute("UPDATE worker_commands SET state='running' WHERE command_id=? AND state='queued'", (command_id,)).rowcount
                if not changed:
                    row = None
        if row:
            request = json.loads(payload)
            if kind == "pause":
                run_id = request["run_id"]
                with closing(_connect(path)) as db:
                    run = db.execute("SELECT status FROM apl_authorized_runs WHERE run_id=?", (run_id,)).fetchone()
                    tasks = db.execute("SELECT status FROM apl_authorized_run_tasks WHERE run_id=?", (run_id,)).fetchall()
                    if run and run[0] == "running" and all(item[0] not in {"reserved", "running"} for item in tasks):
                        db.execute("UPDATE apl_authorized_runs SET status='blocked',reason='paused_at_task_boundary',owner_pid=NULL,owner_marker=NULL WHERE run_id=?", (run_id,))
                        db.execute("UPDATE managed_worker_runs SET state='paused',updated_at=? WHERE run_id=?", (_now(), run_id))
                        db.execute("UPDATE worker_commands SET state='completed',result_json=? WHERE command_id=?", (json.dumps({"paused": True}), command_id))
                        continue
                    db.execute("UPDATE worker_commands SET state='queued' WHERE command_id=?", (command_id,))
                time.sleep(0.2)
                continue
            if kind == "cancel":
                _run_managed_command(path, command_id, kind, request)
                continue
            active_thread = threading.Thread(target=_run_managed_command, args=(path, command_id, kind, request), daemon=True)
            active_thread.start()
            continue
        time.sleep(0.15)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="agentflow.worker")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("root", type=Path)
    serve.add_argument("worker_id")
    args = parser.parse_args(argv)
    _serve(args.root, args.worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
