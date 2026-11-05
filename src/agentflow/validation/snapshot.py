"""Materialize a validation source tree from a verified Git checkpoint only."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import unicodedata
from uuid import uuid4

from agentflow.git.contracts import Checkpoint, GitConflict, GitProvider, Workspace
from agentflow.planning import canonical, digest

from .contracts import SnapshotEntry, ValidationSnapshot


MAX_SNAPSHOT_BYTES = 2 * 1024**3
SNAPSHOT_ID = re.compile(r"^snap-[0-9a-f]{32}$")


class SnapshotError(ValueError):
    """A checkpoint cannot safely be represented as an immutable snapshot."""


def _safe_path(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    parts = normalized.split("/")
    forbidden_windows = '<>:"|?*'
    if (not normalized or normalized != value or "\x00" in value or "\\" in value
            or normalized.startswith("/") or any(part in ("", ".", "..") for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
            or any(any(char in forbidden_windows for char in part) or part.endswith((" ", "."))
                   or part.split(".", 1)[0].upper() in
                   ("CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                    *(f"LPT{i}" for i in range(1, 10))) for part in parts)
            or any(part.casefold() == ".git" for part in parts)):
        raise SnapshotError(f"caminho do Git não é portável e seguro: {value!r}")
    if parts[0].casefold() == ".agentflow" and not (
            len(parts) == 2 and parts[1] in {"spec.toml", "tasks.toml"}):
        raise SnapshotError("snapshot contém dado operacional .agentflow não permitido")
    if (parts[-1].casefold() in {".env", "credentials", "secrets", "id_rsa", "id_ed25519"}
            or parts[-1].casefold().endswith((".pem", ".p12", ".pfx", ".key", ".sqlite",
                                              ".sqlite3", ".db", ".log", ".pyc"))):
        raise SnapshotError("snapshot contém caminho de credencial ou dado operacional")
    return normalized


class SnapshotManager:
    def __init__(self, provider: GitProvider, *, max_snapshot_bytes: int = MAX_SNAPSHOT_BYTES):
        if type(max_snapshot_bytes) is not int or not 1 <= max_snapshot_bytes <= MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot quota must be between 1 byte and 2 GiB")
        self.provider = provider
        self.max_snapshot_bytes = max_snapshot_bytes

    def materialize(self, workspace: Workspace, checkpoint: Checkpoint,
                    parent: Path) -> ValidationSnapshot:
        if not checkpoint.verified or not self.provider.verify_checkpoint(workspace, checkpoint):
            raise SnapshotError("checkpoint não tem identidade verificada e vigente")
        repository = workspace.repository_root
        if not repository:
            raise SnapshotError("workspace não contém raiz de repositório verificada")
        root = parent.resolve(strict=True)
        if parent.is_symlink() or not root.is_dir():
            raise SnapshotError("diretório de snapshots inválido")
        snapshot_id = "snap-" + uuid4().hex
        final = root / snapshot_id
        temporary = root / ("." + snapshot_id + ".tmp")
        entries = self.provider.tree_entries(repository, checkpoint.tree_oid)
        paths: set[str] = set()
        folded: dict[str, str] = {}
        total = 0
        manifest_entries: list[SnapshotEntry] = []
        try:
            temporary.mkdir(mode=0o700, exist_ok=False)
            for entry in entries:
                path = _safe_path(entry.path)
                if path in paths or path.casefold() in folded and folded[path.casefold()] != path:
                    raise SnapshotError("tree contém paths duplicados ou colisão de caixa")
                paths.add(path)
                folded[path.casefold()] = path
                if entry.mode not in (0o100644, 0o100755) or entry.object_type != "blob":
                    raise SnapshotError(f"modo Git/symlink/submódulo não suportado: {path!r}")
                target = temporary.joinpath(*PurePosixPath(path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                size, content_digest = self.provider.write_blob(
                    repository, entry.object_id, target, self.max_snapshot_bytes - total)
                total += size
                if total > self.max_snapshot_bytes:
                    raise SnapshotError("snapshot excede 2 GiB")
                if os.name != "nt":
                    target.chmod(0o555 if entry.mode == 0o100755 else 0o444)
                manifest_entries.append(SnapshotEntry(path, entry.mode, entry.object_id,
                                                       size, content_digest))
            manifest_entries.sort(key=lambda item: item.path.encode("utf-8"))
            manifest_data = {
                "version": 1,
                "checkpoint_id": checkpoint.checkpoint_id,
                "commit_oid": checkpoint.commit_oid,
                "tree_oid": checkpoint.tree_oid,
                "entries": [asdict(item) for item in manifest_entries],
            }
            manifest_json = canonical(manifest_data)
            snapshot_digest = digest(manifest_data)
            self._verify_materialized(temporary, tuple(manifest_entries), total)
            if not self.provider.verify_checkpoint(workspace, checkpoint):
                raise SnapshotError("checkpoint mudou durante materialização")
            os.replace(temporary, final)
            return ValidationSnapshot(snapshot_id, checkpoint.checkpoint_id,
                                      checkpoint.commit_oid, checkpoint.tree_oid,
                                      tuple(manifest_entries), manifest_json,
                                      snapshot_digest, final, total)
        except BaseException:
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary)
            raise

    def verify(self, snapshot: ValidationSnapshot) -> bool:
        """Recheck exact file set, executable modes, sizes, and content digests."""
        try:
            self.verify_or_raise(snapshot)
            return True
        except (OSError, SnapshotError, ValueError):
            return False

    def verify_or_raise(self, snapshot: ValidationSnapshot) -> None:
        """Raise on a known content/mode mismatch; OS errors remain distinguishable."""
        if (not SNAPSHOT_ID.fullmatch(snapshot.snapshot_id)
                or snapshot.path.name != snapshot.snapshot_id
                or snapshot.path.is_symlink()):
            raise SnapshotError("snapshot path identity is invalid")
        root = snapshot.path.resolve(strict=True)
        expected_files = {entry.path for entry in snapshot.entries}
        observed_files: set[str] = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise SnapshotError("snapshot contains a symlink")
            relative = path.relative_to(root).as_posix()
            if path.is_file():
                observed_files.add(relative)
        if observed_files != expected_files:
            raise SnapshotError("snapshot file set differs from manifest")
        manifest_data = {
            "version": 1,
            "checkpoint_id": snapshot.checkpoint_id,
            "commit_oid": snapshot.commit_oid,
            "tree_oid": snapshot.tree_oid,
            "entries": [asdict(item) for item in snapshot.entries],
        }
        if (canonical(manifest_data) != snapshot.manifest_json
                or digest(manifest_data) != snapshot.snapshot_digest):
            raise SnapshotError("snapshot manifest digest differs")
        self._verify_materialized(root, snapshot.entries, snapshot.size_bytes)

    @staticmethod
    def _verify_materialized(root: Path, entries: tuple[SnapshotEntry, ...],
                             total: int) -> None:
        observed = 0
        for entry in entries:
            path = root.joinpath(*PurePosixPath(entry.path).parts)
            if path.is_symlink() or not path.is_file():
                raise SnapshotError(f"snapshot materializado não corresponde ao manifesto: {entry.path!r}")
            size = path.stat().st_size
            if size != entry.size_bytes:
                raise SnapshotError(f"tamanho divergente no snapshot: {entry.path!r}")
            if os.name != "nt" and bool(path.stat().st_mode & 0o111) != (entry.mode == 0o100755):
                raise SnapshotError(f"modo executável divergente no snapshot: {entry.path!r}")
            observed += size
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    hasher.update(chunk)
            if "sha256:" + hasher.hexdigest() != entry.content_digest:
                raise SnapshotError(f"digest divergente no snapshot: {entry.path!r}")
        if observed != total or observed > MAX_SNAPSHOT_BYTES:
            raise SnapshotError("quota/digest de snapshot divergente")
