"""Read-only Git snapshots and conservative attribution, over the local provider."""

import difflib
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import unicodedata

from .contracts import GitConflict, GitError
from .digests import operation_digest
from .locking import WorkspaceLock


def content_digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def private_path(name):
    return any(part.casefold() in {".env", ".npmrc", ".pypirc", ".netrc", ".ssh", ".aws", "credentials.json", "id_rsa"}
               or part.casefold().startswith(".env.") for part in PurePosixPath(name).parts)


def safe_path(root, name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
            or ":" in name or any(part.casefold() in {".git", ".agentflow"} for part in path.parts)):
        raise GitConflict("review_path_forbidden")
    target = root.joinpath(*path.parts)
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise GitConflict("review_symlink_forbidden")
    return target


def file_state(root, name, *, text_limit=262144):
    target = safe_path(root, name)
    try:
        before = target.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise GitConflict("review_nonregular_or_linked_file")
    digest = hashlib.sha256()
    chunks = []
    with target.open("rb") as stream:
        while data := stream.read(65536):
            digest.update(data)
            if before.st_size <= text_limit:
                chunks.append(data)
    after = target.lstat()
    signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if signature(before) != signature(after):
        raise GitConflict("review_file_changed_during_capture")
    try:
        text = b"".join(chunks).decode("utf-8") if before.st_size <= text_limit else None
        if text is not None and "\x00" in text:
            text = None
    except UnicodeError:
        text = None
    if private_path(name):
        text = None
    return {"sha256": "sha256:" + digest.hexdigest(), "size": before.st_size,
            "mode": stat.S_IMODE(before.st_mode), "text": text}


class GitChangeTracker:
    def __init__(self, provider, root, *, max_files=4000):
        self.provider, self.root, self.max_files = provider, root.resolve(strict=True), max_files

    def _read(self, *arguments):
        return self.provider._run(self.root, *arguments, env_extra={"GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"})

    def check_scope(self, scope):
        # Ignored source is absent from Git's normal untracked inventory. Never
        # allow an unobserved preimage to be overwritten or approved as reviewed.
        if not scope:
            raise GitConflict("review_empty_scope")
        # The shared provider already sets --literal-pathspecs globally.
        ignored = self._read("ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", *scope)
        operational = {".git", ".agentflow", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        if any(not any(part.casefold() in operational for part in PurePosixPath(name.decode("utf-8")).parts)
               for name in ignored.split(b"\0") if name):
            raise GitConflict("review_ignored_source_in_task_scope")

    def _identity(self):
        top = Path(os.fsdecode(self._read("rev-parse", "--show-toplevel").strip())).resolve(strict=True)
        if top != self.root:
            raise GitConflict("review_repository_root_changed")
        common = Path(os.fsdecode(self._read("rev-parse", "--git-common-dir").strip()))
        common = (self.root / common).resolve(strict=True) if not common.is_absolute() else common.resolve(strict=True)
        try:
            head = self._read("rev-parse", "--verify", "HEAD").decode("ascii").strip()
        except GitError:
            ref = self._read("symbolic-ref", "HEAD").decode("ascii").strip()
            if self._read("for-each-ref", "--format=%(objectname)", ref).strip():
                raise GitConflict("review_invalid_head")
            head = "unborn:" + ref
        index = Path(os.fsdecode(self._read("rev-parse", "--git-path", "index").strip()))
        index = index if index.is_absolute() else self.root / index
        if index.is_symlink() or common.is_symlink():
            raise GitConflict("review_git_identity_symlink")
        root_stat = self.root.stat()
        return {"head": head, "common": str(common), "root": str(self.root),
                "head_ref": head.removeprefix("unborn:") if head.startswith("unborn:") else self._read("rev-parse", "--symbolic-full-name", "HEAD").decode("utf-8").strip(),
                "root_identity": [root_stat.st_dev, root_stat.st_ino],
                "index_sha256": content_digest(index.read_bytes()) if index.exists() else None}

    def lock(self, operation_id):
        identity = self._identity()
        common = Path(identity["common"])
        key = hashlib.sha256(str(common).encode("utf-8")).hexdigest()[:32]
        # Same repository lease as core.git, also available on unborn repositories.
        directory = common / "agentflow" / "locks"
        if (common / "agentflow").is_symlink() or directory.is_symlink():
            raise GitConflict("review_lock_symlink")
        # GitLocalProvider uses workspace_id[3:] for both ws-/repo- IDs.
        # Keep that historical repo lock filename instead of creating another lease.
        return WorkspaceLock(directory / ("o-" + key + ".lock"), operation_id)

    def capture(self):
        self.provider._configuration(self.root)
        first = self._capture()
        second = self._capture()
        if first != second:
            raise GitConflict("review_repository_changed_during_capture")
        first["fingerprint"] = operation_digest(first)
        return first

    def _capture(self):
        identity = self._identity()
        staged = self._read("ls-files", "--stage", "-z")
        index = {}
        for entry in staged.split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            if stage != "0" or mode == "160000":
                raise GitConflict("review_conflict_or_submodule")
            index[path.decode("utf-8")] = [mode, oid]
        names = set(index)
        if not identity["head"].startswith("unborn:"):
            names.update(name.decode("utf-8") for name in self._read("ls-tree", "-r", "--name-only", "-z", "HEAD").split(b"\0") if name)
        untracked = {name.decode("utf-8") for name in self._read("ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if name}
        untracked = {name for name in untracked if not any(part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
                                                          for part in PurePosixPath(name).parts)}
        names.update(untracked)
        dirty = set(untracked)
        for arguments in (("diff", "--name-only", "-z", "--no-ext-diff", "--no-textconv"),
                          ("diff", "--cached", "--name-only", "-z", "--no-ext-diff", "--no-textconv")):
            dirty.update(name.decode("utf-8") for name in self._read(*arguments).split(b"\0") if name)
        # AgentFlow operational data is not project source/context. Tracked control
        # paths still affect HEAD/index identity; they can never be corrected here.
        names = {name for name in names if not any(part.casefold() in {".agentflow", ".git"} for part in PurePosixPath(name).parts)}
        if len(names) > self.max_files:
            raise GitConflict("review_inventory_limit")
        if any(name != unicodedata.normalize("NFC", name) for name in names):
            raise GitConflict("review_noncanonical_unicode_path")
        if os.name == "nt" and len({name.casefold() for name in names}) != len(names):
            raise GitConflict("review_case_collision")
        files = {name: file_state(self.root, name) for name in sorted(names)}
        return {"schema_version": 1, **identity, "index": index, "dirty": sorted(dirty & names), "files": files}

    def _index_text(self, entry):
        if entry is None:
            return ""
        oid = entry[1]
        size = int(self._read("cat-file", "-s", oid))
        if size > 262144:
            return None
        data = self._read("cat-file", "blob", oid)
        algorithm = hashlib.sha256 if len(oid) == 64 else hashlib.sha1
        if algorithm(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != oid:
            raise GitConflict("review_corrupt_index_blob")
        try:
            return data.decode("utf-8") if b"\0" not in data else None
        except UnicodeError:
            return None

    def changes(self, before, after, scope, prior_owned=()):
        changes = []
        metadata_changed = any(before[key] != after[key] for key in ("head", "head_ref", "root_identity", "root", "common"))
        for name in sorted(set(before["files"]) | set(after["files"]) | set(before["index"]) | set(after["index"])):
            old, new = before["files"].get(name), after["files"].get(name)
            index_changed = before["index"].get(name) != after["index"].get(name)
            if old == new and not index_changed:
                continue
            in_scope = any(name == path or name.startswith(path.rstrip("/") + "/") for path in scope)
            ambiguous = metadata_changed or name in before["dirty"] and name not in prior_owned or not in_scope
            old_text, new_text = (old or {}).get("text"), (new or {}).get("text")
            diff = None
            if (old is None or old_text is not None) and (new is None or new_text is not None):
                diff = "".join(difflib.unified_diff((old_text or "").splitlines(keepends=True),
                    (new_text or "").splitlines(keepends=True), fromfile="a/" + name, tofile="b/" + name, n=5))
            changes.append({"path": name, "kind": "added" if old is None else "deleted" if new is None else "modified",
                "before": old, "after": new, "index_changed": index_changed, "diff": diff,
                "attribution": "ambiguous" if ambiguous else "executor_candidate"})
            if index_changed:
                old_index = None if private_path(name) else self._index_text(before["index"].get(name))
                new_index = None if private_path(name) else self._index_text(after["index"].get(name))
                changes[-1]["index_before_text"] = old_index
                changes[-1]["index_after_text"] = new_index
                changes[-1]["staged_diff"] = ("".join(difflib.unified_diff(
                    old_index.splitlines(keepends=True), new_index.splitlines(keepends=True),
                    fromfile="index-before/" + name, tofile="index-after/" + name))
                    if old_index is not None and new_index is not None else None)
        deleted = [item for item in changes if item["kind"] == "deleted"]
        added = [item for item in changes if item["kind"] == "added"]
        for old in deleted:
            matches = [new for new in added if new["after"]["sha256"] == old["before"]["sha256"]]
            if len(matches) == 1 and sum(item["before"]["sha256"] == old["before"]["sha256"] for item in deleted) == 1:
                matches[0]["kind"] = "renamed"
                matches[0]["old_path"] = old["path"]
                if old["attribution"] == "ambiguous":
                    matches[0]["attribution"] = "ambiguous"
                old["renamed_to"] = matches[0]["path"]
        return changes

    def publish(self, expected, edits, allowed):
        """Guarded working-tree writes only. Never stages, resets or rolls back Git."""
        if self.capture()["fingerprint"] != expected["fingerprint"]:
            raise GitConflict("review_state_changed_before_publication")
        if type(edits) is not list or not edits:
            raise GitConflict("review_empty_correction")
        paths = set()
        encoded = {}
        for edit in edits:
            if type(edit) is not dict or set(edit) != {"path", "before_sha256", "content"}:
                raise GitConflict("review_correction_schema_invalid")
            name = edit["path"]
            if type(name) is not str or name not in allowed or name in paths or type(edit["content"]) is not str:
                raise GitConflict("review_correction_path_not_owned")
            paths.add(name)
            encoded[name] = edit["content"].encode("utf-8")
            if b"\0" in encoded[name] or len(encoded[name]) > 262144:
                raise GitConflict("review_correction_not_reviewable_text")
            current = expected["files"].get(name)
            if not current or current["sha256"] != edit["before_sha256"]:
                raise GitConflict("review_correction_preimage_mismatch")
            safe_path(self.root, name)
        # Reuse the provider's canonical path policy, including Windows aliases,
        # protected prototype files, caches and credentials, before any write.
        self.provider.validate_paths(self.root, tuple(sorted(paths)), allow_missing=False)
        for edit in edits:
            name = edit["path"]
            target = safe_path(self.root, name)
            current = file_state(self.root, name)
            if current != expected["files"][name]:
                raise GitConflict("review_correction_concurrent_writer")
            handle, temporary_name = tempfile.mkstemp(prefix=".agentflow-edit-", dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(encoded[name])
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(current["mode"])
                if file_state(self.root, name) != current:
                    raise GitConflict("review_correction_concurrent_writer")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        result = self.capture()
        if any(expected[key] != result[key] for key in ("head", "head_ref", "root_identity", "index", "index_sha256", "common")):
            raise GitConflict("review_git_changed_during_publication")
        for name in set(expected["files"]) | set(result["files"]):
            if name not in paths and expected["files"].get(name) != result["files"].get(name):
                raise GitConflict("review_unrelated_writer_during_publication")
        for edit in edits:
            if result["files"][edit["path"]]["sha256"] != content_digest(edit["content"].encode("utf-8")):
                raise GitConflict("review_publication_content_changed")
        return result
