"""Explicit retention cleanup for AgentFlow-owned validation payloads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def cleanup_validation_storage(store, snapshot_root: Path, output_root: Path,
                               artifact_root: Path, *,
                               current_time: datetime | None = None) -> tuple[str, ...]:
    """Remove only expired, reconciled payloads whose recorded identities verify.

    The command is explicit. Runs with unresolved processes, unknown delivery,
    pinned artifacts, symlinks, or changed payloads are preserved.
    """
    current_time = current_time or datetime.now(timezone.utc)
    removed: list[str] = []
    snapshot_root = snapshot_root.resolve()
    output_root = output_root.resolve()
    artifact_root = artifact_root.resolve()

    for run_id, status, unresolved, finished in store.db.execute(
            "SELECT validation_run_id,status,unresolved,finished_at FROM validation_runs "
            "ORDER BY validation_run_id").fetchall():
        if (not SAFE.fullmatch(run_id) or status not in
                {"passed", "failed", "inconclusive", "cancelled"}
                or unresolved or not finished):
            continue
        finished_at = _parse_time(finished)
        if finished_at is None:
            continue
        gate_ids = [row[0] for row in store.db.execute(
            "SELECT gate_id FROM validation_gate_results WHERE validation_run_id=?",
            (run_id,)).fetchall()]
        for gate_id in gate_ids:
            if not SAFE.fullmatch(gate_id):
                continue
            gate_dir = output_root / run_id / gate_id
            temporary = gate_dir / ".tmp"
            if (_inside(gate_dir, output_root)
                    and not _has_symlink_ancestor(gate_dir, output_root)
                    and temporary.is_dir() and not temporary.is_symlink()):
                try:
                    if not any(temporary.iterdir()):
                        temporary.rmdir()
                        removed.append(f"temp:{run_id}:{gate_id}")
                except OSError:
                    pass
        if finished_at <= current_time - timedelta(days=30):
            for gate_id in gate_ids:
                if not SAFE.fullmatch(gate_id):
                    continue
                gate_dir = output_root / run_id / gate_id
                if (not _inside(gate_dir, output_root)
                        or _has_symlink_ancestor(gate_dir, output_root)):
                    continue
                for name in ("stdout.log", "stderr.log"):
                    path = gate_dir / name
                    if path.is_file() and not path.is_symlink():
                        try:
                            path.unlink()
                            removed.append(f"log:{run_id}:{gate_id}:{name}")
                        except OSError:
                            pass
                if not (gate_dir / "stdout.log").exists() and not (gate_dir / "stderr.log").exists():
                    cursor = store.db.execute(
                        "UPDATE validation_gate_results SET logs_retained=0 "
                        "WHERE validation_run_id=? AND gate_id=? AND logs_retained=1",
                        (run_id, gate_id))
                    if cursor.rowcount:
                        operation_id = store.db.execute(
                            "SELECT operation_id FROM validation_runs WHERE validation_run_id=?",
                            (run_id,)).fetchone()[0]
                        store._validation_event(db=store.db, operation_id=operation_id,
                                                run_id=run_id, kind="validation_logs_expired",
                                                payload={"gate_id": gate_id})

        if finished_at <= current_time - timedelta(days=7):
            row = store.db.execute(
                "SELECT snapshot_id,storage_path,manifest_json,state FROM validation_snapshots "
                "WHERE validation_run_id=?", (run_id,)).fetchone()
            if row and row[3] == "verified":
                snapshot_id, recorded_path, manifest_json, _ = row
                path = Path(recorded_path)
                if (SAFE.fullmatch(snapshot_id) and path == snapshot_root / snapshot_id
                        and _inside(path, snapshot_root)
                        and not _has_symlink_ancestor(path, snapshot_root)
                        and _snapshot_matches(path, manifest_json)):
                    try:
                        shutil.rmtree(path)
                        store.db.execute("UPDATE validation_snapshots SET state='expired' "
                                         "WHERE snapshot_id=? AND state='verified'", (snapshot_id,))
                        operation_id = store.db.execute(
                            "SELECT operation_id FROM validation_runs WHERE validation_run_id=?",
                            (run_id,)).fetchone()[0]
                        store._validation_event(store.db, operation_id, run_id,
                                                "validation_snapshot_expired",
                                                {"snapshot_id": snapshot_id})
                        removed.append(f"snapshot:{snapshot_id}")
                    except OSError:
                        pass

    for artifact_id, path_text, artifact_digest, status, pinned, expires in store.db.execute(
            "SELECT artifact_id,storage_path,artifact_digest,status,pinned,expires_at "
            "FROM validation_artifacts ORDER BY artifact_id").fetchall():
        if (status != "verified" or pinned or not expires
                or _parse_time(expires) is None
                or _parse_time(expires) > current_time):
            continue
        pending = store.db.execute(
            "SELECT 1 FROM validation_deliveries WHERE artifact_id=? "
            "AND status IN ('prepared','running','unknown') LIMIT 1", (artifact_id,)).fetchone()
        path = Path(path_text)
        if (pending or not SAFE.fullmatch(artifact_id) or not _inside(path, artifact_root)
                or _has_symlink_ancestor(path, artifact_root) or not path.is_file()
                or _sha256(path) != artifact_digest):
            continue
        try:
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
            store.db.execute("UPDATE validation_artifacts SET status='expired' "
                             "WHERE artifact_id=? AND status='verified' AND pinned=0",
                             (artifact_id,))
            operation_id = store.db.execute(
                "SELECT operation_id FROM validation_artifacts WHERE artifact_id=?",
                (artifact_id,)).fetchone()[0]
            store._validation_event(store.db, operation_id, None,
                                    "validation_artifact_expired",
                                    {"artifact_id": artifact_id})
            removed.append(f"artifact:{artifact_id}")
        except OSError:
            pass
    return tuple(removed)


def _inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        return resolved != root and root in resolved.parents
    except OSError:
        return False


def _has_symlink_ancestor(path: Path, root: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return True
    current = root.absolute()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return "sha256:" + hasher.hexdigest()


def _snapshot_matches(path: Path, manifest_json: str) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        manifest = json.loads(manifest_json)
        expected = {item["path"] for item in manifest["entries"]}
        observed: set[str] = set()
        for item in path.rglob("*"):
            if item.is_symlink():
                return False
            if item.is_file():
                observed.add(item.relative_to(path).as_posix())
        return observed == expected
    except (OSError, ValueError, KeyError, TypeError):
        return False
