"""Bounded, local-only Git subprocess adapter."""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
import unicodedata

from .contracts import (Checkpoint, CheckpointRequest, GitConflict, GitError,
                        TreeEntry, Workspace, WorkspaceRequest)
from .digests import pathset_digest
from .locking import WorkspaceLock


class GitLocalProvider:
    MAX_OUTPUT = 4 * 1024 * 1024
    MAX_STREAM = 1024 * 1024
    TIMEOUT = 30

    def __init__(self):
        executable = shutil.which("git")
        if executable is None:
            raise GitError("Git não encontrado no PATH")
        self.executable = Path(executable).resolve()
        self._temporary = tempfile.TemporaryDirectory(prefix="agentflow-git-")
        self._private = Path(self._temporary.name)
        self._empty_config = self._private / "empty.gitconfig"
        self._empty_config.write_bytes(b"")
        self._empty_hooks = self._private / "empty-hooks"
        self._empty_hooks.mkdir()
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self._temporary.cleanup()
            self.closed = True

    def __enter__(self) -> "GitLocalProvider":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _environment(self) -> dict[str, str]:
        names = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE",
                 "HOMEPATH", "HOME", "PATHEXT", "COMSPEC", "LANG", "LC_ALL")
        env = {name: os.environ[name] for name in names if name in os.environ}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": str(self._empty_config),
                    "GIT_CONFIG_GLOBAL": str(self._empty_config), "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "", "GIT_SEQUENCE_EDITOR": ""})
        # Do not inherit directory/index/object redirection from the parent process.
        for name in tuple(env):
            if name.startswith("GIT_"):
                env.pop(name, None)
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": str(self._empty_config),
                    "GIT_CONFIG_GLOBAL": str(self._empty_config), "GIT_ATTR_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "", "GIT_SEQUENCE_EDITOR": ""})
        return env

    def _run(self, cwd: Path, *args: str, input_bytes: bytes | None = None,
             env_extra: dict[str, str] | None = None) -> bytes:
        if self.closed:
            raise GitError("provider Git já encerrado")
        env = self._environment()
        if env_extra:
            env.update(env_extra)
        argv = [str(self.executable), "-c", f"core.hooksPath={self._empty_hooks}",
                "-c", "commit.gpgsign=false", "--literal-pathspecs", *args]
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env,
                                        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False)
        except OSError as exc:
            raise GitError(f"Git não pôde iniciar: {type(exc).__name__}") from exc
        stdout = bytearray()
        stderr = bytearray()
        output_lock = threading.Lock()
        exceeded = threading.Event()

        def drain(stream, buffer: bytearray) -> None:
            try:
                while chunk := stream.read(64 * 1024):
                    with output_lock:
                        too_large = (len(buffer) + len(chunk) > self.MAX_STREAM
                                     or len(stdout) + len(stderr) + len(chunk) > self.MAX_OUTPUT)
                        if not too_large:
                            buffer.extend(chunk)
                    if too_large:
                        exceeded.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
                        break
            finally:
                stream.close()

        readers = [threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
                   threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            if input_bytes is not None and process.stdin is not None:
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.flush()
                except BrokenPipeError:
                    pass
                finally:
                    process.stdin.close()
            process.wait(timeout=self.TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                process.kill()
            except OSError:
                pass
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            process.wait()
            for reader in readers:
                reader.join()
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        if timed_out:
            raise GitError("Git excedeu timeout operacional")
        if exceeded.is_set():
            raise GitError("saída do Git excedeu o limite operacional")
        if process.returncode != 0:
            detail = bytes(stderr[:512]).decode("utf-8", "replace").strip()
            raise GitError(f"Git retornou {process.returncode}: {detail}")
        return bytes(stdout)

    @staticmethod
    def _rooted(path: Path) -> Path:
        if not path.exists():
            raise GitError(f"repositório inexistente: {path}")
        return path.resolve(strict=True)

    def inspect(self, repository: Path) -> tuple[Path, Path, str]:
        cwd = self._rooted(repository)
        top = Path(os.fsdecode(self._run(cwd, "rev-parse", "--show-toplevel").strip())).resolve(strict=True)
        common_value = Path(os.fsdecode(self._run(cwd, "rev-parse", "--git-common-dir").strip()))
        common = (cwd / common_value).resolve(strict=True) if not common_value.is_absolute() else common_value.resolve(strict=True)
        head = self._run(cwd, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head):
            raise GitError("OID HEAD inválido")
        if not top.is_dir() or not common.is_dir():
            raise GitError("raiz Git não é diretório válido")
        return top, common, head

    def lock(self, repository: Path, workspace_id: str, operation_id: str) -> WorkspaceLock:
        _, common, _ = self.inspect(repository)
        control = common / "agentflow"
        if control.is_symlink():
            raise GitConflict("diretório Git agentflow não pode ser symlink")
        control.mkdir(exist_ok=True)
        locks = control / "locks"
        if locks.is_symlink():
            raise GitConflict("diretório de locks não pode ser symlink")
        locks.mkdir(exist_ok=True)
        if not re.fullmatch(r"(?:ws|repo)-[0-9a-f]{32}", workspace_id):
            raise GitConflict("workspace_id inválido")
        return WorkspaceLock(locks / f"{workspace_id[3:]}.lock", operation_id)

    def _configuration(self, root: Path, paths: tuple[str, ...] = ()) -> None:
        output = self._run(root, "config", "--local", "--null", "--list")
        for entry in output.split(b"\0"):
            if not entry:
                continue
            key = entry.partition(b"\n")[0].decode("ascii", "ignore").lower()
            if (key.startswith("alias.") or key == "core.fsmonitor" or key == "core.hookspath"
                    or key == "core.attributesfile" or key.startswith("filter.")):
                raise GitConflict(f"configuração executável/local não homologada: {key}")
        common = Path(os.fsdecode(self._run(root, "rev-parse", "--git-common-dir").strip()))
        common = (root / common).resolve(strict=True) if not common.is_absolute() else common.resolve(strict=True)
        attributes = {root / ".gitattributes", common / "info" / "attributes"}
        for item in paths:
            parent = PurePosixPath(item).parent
            current = root
            attributes.add(current / ".gitattributes")
            for part in parent.parts:
                current = current / part
                attributes.add(current / ".gitattributes")
        for path in attributes:
            if path.is_file() and re.search(rb"(?:^|\s)filter\s*=", path.read_bytes(), re.MULTILINE):
                raise GitConflict("atributo filter executável não é permitido")

    def validate_paths(self, workspace: Path, paths: tuple[str, ...], *, allow_missing: bool = True) -> tuple[Path, ...]:
        root = workspace.resolve(strict=True)
        if len(paths) > 128:
            raise GitConflict("checkpoint excede 128 caminhos")
        normalized = []
        folded: dict[str, str] = {}
        for path in paths:
            value = unicodedata.normalize("NFC", path)
            parts = value.split("/")
            if (not value or "\x00" in value or "\\" in value or value.startswith("/")
                    or re.match(r"^[A-Za-z]:", value) or any(part in ("", ".", "..") for part in parts)
                    or len(value.encode("utf-8")) > 1024 or any(len(part.encode("utf-8")) > 255 for part in parts)):
                raise GitConflict(f"caminho Git inválido: {path!r}")
            lower_parts = [part.casefold() for part in parts]
            if (".git" in lower_parts or (lower_parts[0] == ".agentflow"
                    and not (len(parts) == 2 and parts[1] in ("spec.toml", "tasks.toml")))
                    or any(part in ("autonomous-development-runner", ".venv", "__pycache__",
                                    ".pytest_cache", "node_modules") for part in lower_parts)
                    or value.casefold() == "autonomous-development-runner-v0.1.zip"
                    or parts[-1].casefold() in (".env", "credentials", "secrets", "id_rsa", "id_ed25519")
                    or parts[-1].casefold().endswith((".pem", ".p12", ".pfx", ".key", ".sqlite", ".sqlite3", ".db", ".log", ".pyc"))):
                raise GitConflict(f"arquivo operacional, legado, cache ou credencial não permitido: {value!r}")
            if os.name == "nt":
                if any(any(char in part for char in '<>:"|?*') or part.endswith((" ", "."))
                       or part.split(".", 1)[0].upper() in ("CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)))
                       for part in parts):
                    raise GitConflict(f"path não representável no Windows: {value!r}")
                key = value.casefold()
                if key in folded and folded[key] != value:
                    raise GitConflict("pathset contém colisão de caixa no Windows")
                folded[key] = value
            if value in normalized:
                raise GitConflict("pathset contém duplicata após normalização")
            normalized.append(value)
            candidate = root.joinpath(*parts)
            current = root
            for part in parts:
                current = current / part
                if current.is_symlink():
                    raise GitConflict(f"symlink não permitido no pathset: {value!r}")
            resolved = candidate.resolve(strict=False)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise GitConflict(f"path escapa do workspace: {value!r}") from exc
            if candidate.exists() and not candidate.is_file():
                raise GitConflict(f"somente arquivos regulares são permitidos: {value!r}")
            if not allow_missing and not candidate.exists():
                raise GitConflict(f"arquivo selecionado não existe: {value!r}")
        return tuple(root.joinpath(*path.split("/")) for path in normalized)

    def changed_paths(self, workspace: Path) -> tuple[str, ...]:
        status = self._run(workspace, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if len(status) > self.MAX_OUTPUT:
            raise GitConflict("saída de status excede o limite")
        entries = status.split(b"\0")
        changed: list[str] = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 4 or entry[2:3] != b" ":
                raise GitConflict("status Git não pôde ser interpretado com segurança")
            state = entry[:2]
            if b"R" in state or b"C" in state:
                raise GitConflict("rename/copy deve ser reconciliado explicitamente antes do checkpoint")
            try:
                name = entry[3:].decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise GitConflict("path fora de UTF-8 homologado") from exc
            changed.append(name)
        if len(changed) > 128:
            raise GitConflict("mais de 128 arquivos alterados no workspace")
        return tuple(changed)

    def preflight(self, repository: Path, paths: tuple[str, ...] = ()) -> dict[str, object]:
        top, common, head = self.inspect(repository)
        self._configuration(top, paths)
        validated = self.validate_paths(top, paths)
        index = self._run(top, "ls-files", "--stage", "-z")
        indexed: dict[str, int] = {}
        for entry in index.split(b"\0"):
            if not entry:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            if not separator:
                raise GitError("índice Git malformado")
            mode = int(metadata.split(b" ", 1)[0], 8)
            try:
                name = raw_path.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise GitConflict("repositório contém caminho fora de UTF-8 homologado") from exc
            indexed[name] = mode
        for name in paths:
            if indexed.get(name) == 0o160000:
                raise GitConflict(f"submódulo não suportado: {name}")
            if any(name.startswith(existing + "/") or existing.startswith(name + "/")
                   for existing, mode in indexed.items() if mode == 0o160000):
                raise GitConflict(f"pathset intersecta submódulo: {name}")
        if os.name == "nt":
            folded: dict[str, str] = {}
            for name in indexed:
                key = name.casefold()
                if key in folded and folded[key] != name:
                    raise GitConflict("repositório possui colisão de paths por caixa no Windows")
                folded[key] = name
        status = self._run(top, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if len(status) > self.MAX_OUTPUT:
            raise GitConflict("saída de status excede o limite")
        return {"root": top, "common_dir": common, "head": head,
                "dirty": status, "paths": validated, "indexed_modes": indexed}

    def create_workspace(self, request: WorkspaceRequest, workspace_id: str) -> Workspace:
        info = self.preflight(request.repository, request.authorized_paths)
        root = info["root"]
        common = info["common_dir"]
        if not isinstance(root, Path) or not isinstance(common, Path):
            raise GitError("preflight não retornou raízes verificadas")
        if info["head"] != request.base_oid:
            raise GitConflict("HEAD mudou desde a reserva do workspace")
        if not re.fullmatch(r"ws-[0-9a-f]{32}", workspace_id):
            raise GitConflict("workspace_id inválido")
        control = common / "agentflow"
        if control.is_symlink():
            raise GitConflict("diretório Git agentflow não pode ser symlink")
        control.mkdir(exist_ok=True)
        worktree_root = control / "worktrees"
        if worktree_root.is_symlink():
            raise GitConflict("diretório de worktrees não pode ser symlink")
        worktree_root.mkdir(exist_ok=True)
        target = worktree_root / workspace_id[3:]
        branch = f"agentflow/ws/{workspace_id[3:]}"
        if target.exists():
            raise GitConflict("caminho de worktree já existe")
        if self._run(root, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}").strip():
            raise GitConflict("branch do workspace já existe")
        self._run(root, "worktree", "add", "-b", branch, str(target), request.base_oid)
        created_root, created_common, head = self.inspect(target)
        if created_root != target.resolve(strict=True) or created_common != common or head != request.base_oid:
            raise GitConflict("worktree criado com identidade/base divergente")
        current_branch = self._run(target, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
        if current_branch != branch:
            raise GitConflict("branch ativa do worktree diverge da esperada")
        status = self._run(target, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if status:
            raise GitConflict("worktree recém-criado não está limpo")
        return Workspace(workspace_id, request.project_id, request.task_id,
                         request.definition_revision, target.resolve(strict=True), common,
                         branch, request.base_oid, "active", True, request.base_oid)

    def create_checkpoint(self, workspace: Workspace, request: CheckpointRequest) -> Checkpoint:
        root, common, head = self.inspect(workspace.path)
        if root != workspace.path.resolve(strict=True) or common != workspace.common_dir.resolve(strict=True):
            raise GitConflict("identidade do worktree/repositório divergente")
        branch = self._run(root, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
        if branch != workspace.branch or head != (workspace.head_oid or workspace.base_oid):
            raise GitConflict("branch ou HEAD mudou desde a última verificação")
        self._configuration(root, request.paths)
        paths = tuple(sorted(request.paths, key=lambda value: value.encode("utf-8")))
        validated = self.validate_paths(root, paths)
        if len(paths) != len(set(paths)) or not paths:
            raise GitConflict("checkpoint exige lista explícita, não vazia e sem duplicatas")
        if (not request.actor_name.strip() or len(request.actor_name.encode("utf-8")) > 200
                or not request.actor_email.strip() or len(request.actor_email.encode("utf-8")) > 254
                or any(ord(char) < 32 or ord(char) == 127 for char in request.actor_name + request.actor_email)):
            raise GitConflict("identidade de autoria inválida")
        if (not request.operation_id.isascii() or not 1 <= len(request.operation_id) <= 128
                or any(ord(char) < 33 or ord(char) > 126 for char in request.operation_id)):
            raise GitConflict("operation_id de checkpoint inválido")
        if (not request.message.strip() or len(request.message.encode("utf-8")) > 1024
                or any((ord(char) < 32 and char not in "\n\t") or ord(char) == 127 for char in request.message)):
            raise GitConflict("mensagem de checkpoint inválida")
        # Caller passes only selected paths, but provider independently guards lexical scope via DB/service.
        changed = set(self.changed_paths(root))
        if not changed <= set(paths):
            raise GitConflict("workspace tem alterações fora da lista selecionada; preservadas para revisão")
        self.validate_paths(root, paths)
        control = common / "agentflow"
        if control.is_symlink():
            raise GitConflict("diretório administrativo Git não pode ser symlink")
        indexes = control / "indexes"
        if indexes.is_symlink():
            raise GitConflict("diretório de índices não pode ser symlink")
        indexes.mkdir(parents=True, exist_ok=True)
        index_path = indexes / f"{uuid.uuid4().hex}.index"
        index_path.touch(exist_ok=False)
        env = {"GIT_INDEX_FILE": str(index_path)}
        parent = head
        staged_hashes: dict[str, tuple[bool, str, int]] = {}
        total = 0
        try:
            self._run(root, "read-tree", parent, env_extra=env)
            initial_modes: dict[str, int] = {}
            entries = self._run(root, "ls-files", "--stage", "-z")
            for entry in entries.split(b"\0"):
                if not entry:
                    continue
                metadata, _, raw_path = entry.partition(b"\t")
                try:
                    name = raw_path.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise GitConflict("path fora de UTF-8 homologado") from exc
                initial_modes[name] = int(metadata.split(b" ", 1)[0], 8)
            for name, file_path in zip(paths, validated, strict=True):
                if file_path.exists():
                    mode = file_path.stat().st_mode
                    if not stat.S_ISREG(mode):
                        raise GitConflict(f"arquivo não regular: {name!r}")
                    content = file_path.read_bytes()
                    total += len(content)
                    if total > 64 * 1024 * 1024:
                        raise GitConflict("checkpoint excede 64 MiB")
                    expected = self._blob_oid(content, len(head))
                    actual = self._run(root, "hash-object", "-w", "--no-filters", "--", name).decode("ascii").strip()
                    if actual != expected:
                        raise GitConflict("arquivo mudou durante o staging")
                    old_mode = initial_modes.get(name)
                    file_mode = old_mode if old_mode in (0o100644, 0o100755) else (
                        0o100755 if os.name != "nt" and mode & 0o111 else 0o100644)
                    index_info = f"{file_mode:o} {actual}\t".encode("ascii") + name.encode("utf-8") + b"\0"
                    self._run(root, "update-index", "-z", "--index-info", input_bytes=index_info, env_extra=env)
                    staged_hashes[name] = (True, hashlib.sha256(content).hexdigest(), file_mode)
                elif name in initial_modes:
                    if initial_modes[name] == 0o160000:
                        raise GitConflict(f"submódulo não suportado: {name}")
                    zeros = "0" * len(head)
                    index_info = f"0 {zeros}\t".encode("ascii") + name.encode("utf-8") + b"\0"
                    self._run(root, "update-index", "-z", "--index-info", input_bytes=index_info, env_extra=env)
                    staged_hashes[name] = (False, "", 0)
            self.validate_paths(root, paths)
            staged = self._run(root, "diff", "--cached", "--name-only", "-z", parent, env_extra=env)
            actual_paths = tuple(sorted((item.decode("utf-8", "strict") for item in staged.split(b"\0") if item),
                                        key=lambda value: value.encode("utf-8")))
            if not actual_paths or not set(actual_paths) <= set(paths):
                raise GitConflict("índice isolado não corresponde ao conjunto explícito")
            self._configuration(root, paths)
            # Second path/content validation is immediately before tree/commit creation.
            second_paths = self.validate_paths(root, paths)
            for name, file_path in zip(paths, second_paths, strict=True):
                expected = staged_hashes.get(name)
                if expected is None:
                    continue
                if expected[0]:
                    if not file_path.exists() or hashlib.sha256(file_path.read_bytes()).hexdigest() != expected[1]:
                        raise GitConflict(f"arquivo mudou depois do staging: {name!r}")
                elif file_path.exists():
                    raise GitConflict(f"arquivo removido reapareceu antes do commit: {name!r}")
            current_branch = self._run(root, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
            current_head = self._run(root, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
            if current_branch != workspace.branch or current_head != parent:
                raise GitConflict("branch/HEAD alterados imediatamente antes do commit")
            tree = self._run(root, "write-tree", env_extra=env).decode("ascii").strip()
            operation_line = f"\n\nAgentFlow-Operation: {request.operation_id}\n"
            message = request.message.strip() + operation_line
            commit_env = {"GIT_AUTHOR_NAME": request.actor_name, "GIT_AUTHOR_EMAIL": request.actor_email,
                          "GIT_COMMITTER_NAME": "AgentFlow", "GIT_COMMITTER_EMAIL": "agentflow@localhost"}
            commit = self._run(root, "commit-tree", tree, "-p", parent, "-F", "-", input_bytes=message.encode("utf-8"),
                               env_extra=commit_env).decode("ascii").strip()
            ref = f"refs/heads/{workspace.branch}"
            self._run(root, "update-ref", ref, commit, parent)
            # The task worktree has its own index. Refresh it after the verified commit;
            # this writes only index metadata and never checks out or overwrites files.
            self._run(root, "read-tree", commit)
            checkpoint_id = "cp-" + uuid.uuid4().hex
            provisional = Checkpoint(checkpoint_id, workspace.workspace_id, request.operation_id,
                                     commit, parent, tree, workspace.branch, actual_paths,
                                     "", False, request.actor_name, request.actor_email)
            provisional = Checkpoint(provisional.checkpoint_id, provisional.workspace_id,
                                     provisional.operation_id, provisional.commit_oid,
                                     provisional.parent_oid, provisional.tree_oid, provisional.branch,
                                     provisional.paths, pathset_digest(provisional.paths), False,
                                     provisional.actor_name, provisional.actor_email)
            if not self.verify_checkpoint(workspace, provisional):
                raise GitConflict("commit criado mas checkpoint não passou verificação")
            return Checkpoint(provisional.checkpoint_id, provisional.workspace_id,
                              provisional.operation_id, provisional.commit_oid,
                              provisional.parent_oid, provisional.tree_oid, provisional.branch,
                              provisional.paths, provisional.pathset_digest, True,
                              provisional.actor_name, provisional.actor_email)
        finally:
            index_path.unlink(missing_ok=True)

    @staticmethod
    def _blob_oid(content: bytes, oid_length: int) -> str:
        header = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
        algorithm = "sha256" if oid_length == 64 else "sha1"
        return hashlib.new(algorithm, header).hexdigest()

    def verify_checkpoint(self, workspace: Workspace, checkpoint: Checkpoint) -> bool:
        root, common, head = self.inspect(workspace.path)
        if root != workspace.path.resolve(strict=True) or common != workspace.common_dir.resolve(strict=True):
            return False
        branch = self._run(root, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
        ref_oid = self._run(root, "rev-parse", "--verify", f"refs/heads/{workspace.branch}").decode("ascii").strip()
        if branch != workspace.branch or head != checkpoint.commit_oid or ref_oid != checkpoint.commit_oid:
            return False
        data = self._run(root, "cat-file", "-p", checkpoint.commit_oid)
        headers = data.split(b"\n\n", 1)[0].splitlines()
        tree = next((line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")), "")
        parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
        body = data.split(b"\n\n", 1)[1].decode("utf-8", "strict") if b"\n\n" in data else ""
        trailer = f"AgentFlow-Operation: {checkpoint.operation_id}"
        if tree != checkpoint.tree_oid or parents != [checkpoint.parent_oid] or body.count(trailer) != 1:
            return False
        actual = self._run(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                           checkpoint.parent_oid, checkpoint.commit_oid)
        actual_paths = tuple(sorted((item.decode("utf-8", "strict") for item in actual.split(b"\0") if item),
                                    key=lambda value: value.encode("utf-8")))
        if actual_paths != checkpoint.paths:
            return False
        if pathset_digest(actual_paths) != checkpoint.pathset_digest:
            return False
        index = self._run(root, "ls-tree", "-r", "-z", checkpoint.commit_oid)
        modes = {}
        for entry in index.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            modes[raw_path.decode("utf-8", "strict")] = int(meta.split(b" ", 1)[0], 8)
        if any(modes.get(path) == 0o160000 for path in checkpoint.paths):
            return False
        author_prefix = f"author {checkpoint.actor_name} <{checkpoint.actor_email}> ".encode("utf-8")
        committer_prefix = b"committer AgentFlow <agentflow@localhost> "
        if not any(line.startswith(author_prefix) for line in headers):
            return False
        if not any(line.startswith(committer_prefix) for line in headers):
            return False
        if self.changed_paths(root):
            return False
        return True

    def tree_entries(self, repository: Path, tree_oid: str) -> tuple[TreeEntry, ...]:
        """Read a recursive tree listing without checkout, filters or path conversion."""
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree_oid):
            raise GitConflict("tree OID inválido")
        root = self._rooted(repository)
        raw = self._run(root, "ls-tree", "-r", "-z", tree_oid)
        result: list[TreeEntry] = []
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, separator, raw_path = entry.partition(b"\t")
            fields = metadata.split(b" ")
            if not separator or len(fields) != 3:
                raise GitError("tree listing malformado")
            try:
                mode = int(fields[0], 8)
                object_type = fields[1].decode("ascii")
                object_id = fields[2].decode("ascii")
                path = raw_path.decode("utf-8", "strict")
            except (ValueError, UnicodeDecodeError) as exc:
                raise GitConflict("tree contém entrada não suportada") from exc
            if object_type not in {"blob", "commit", "tree"} or not re.fullmatch(
                    r"[0-9a-f]{40}|[0-9a-f]{64}", object_id):
                raise GitConflict("entrada de tree não verificável")
            result.append(TreeEntry(path, mode, object_id, object_type))
        return tuple(result)

    def write_blob(self, repository: Path, object_id: str, destination: Path,
                   max_bytes: int) -> tuple[int, str]:
        """Stream one Git blob to a new file and verify its native object ID."""
        if (not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", object_id)
                or type(max_bytes) is not int or max_bytes < 0):
            raise GitConflict("blob OID ou limite inválido")
        root = self._rooted(repository)
        size_raw = self._run(root, "cat-file", "-s", object_id).strip()
        if not size_raw.isdigit():
            raise GitError("Git retornou tamanho de blob inválido")
        expected_size = int(size_raw)
        if expected_size > max_bytes:
            raise GitConflict("blob excede quota do snapshot")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise GitConflict("destino do blob já existe")
        algorithm = "sha256" if len(object_id) == 64 else "sha1"
        object_hash = hashlib.new(algorithm)
        object_hash.update(b"blob " + str(expected_size).encode("ascii") + b"\0")
        content_hash = hashlib.sha256()
        process = None
        try:
            with destination.open("xb") as output:
                env = self._environment()
                argv = [str(self.executable), "-c", f"core.hooksPath={self._empty_hooks}",
                        "-c", "commit.gpgsign=false", "--literal-pathspecs", "cat-file", "blob", object_id]
                process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False)
                copied = 0
                exceeded = threading.Event()
                failures: list[BaseException] = []

                def drain_blob() -> None:
                    nonlocal copied
                    assert process is not None and process.stdout is not None
                    try:
                        while chunk := process.stdout.read(64 * 1024):
                            if copied + len(chunk) > min(max_bytes, expected_size):
                                exceeded.set()
                                process.kill()
                                break
                            output.write(chunk)
                            object_hash.update(chunk)
                            content_hash.update(chunk)
                            copied += len(chunk)
                    except BaseException as exc:
                        failures.append(exc)
                        try:
                            process.kill()
                        except OSError:
                            pass
                    finally:
                        process.stdout.close()

                reader = threading.Thread(target=drain_blob, name="agentflow-git-blob-reader",
                                          daemon=True)
                reader.start()
                try:
                    process.wait(timeout=self.TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise GitError("leitura de blob excedeu timeout")
                finally:
                    reader.join(timeout=self.TIMEOUT)
                    if reader.is_alive():
                        process.kill()
                        reader.join()
                if exceeded.is_set():
                    raise GitConflict("blob excede a quota do snapshot")
                if failures:
                    raise GitError("falha ao gravar blob materializado") from failures[0]
            if process.returncode != 0:
                raise GitError("Git não conseguiu ler blob")
            actual_size = destination.stat().st_size
            if actual_size != expected_size or copied != expected_size or actual_size > max_bytes:
                raise GitConflict("tamanho do blob mudou durante leitura")
            if object_hash.hexdigest() != object_id:
                raise GitConflict("conteúdo do blob não corresponde ao OID do tree")
            return actual_size, "sha256:" + content_hash.hexdigest()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

    def verify_no_changes(self, workspace: Workspace) -> bool:
        """Confirm the workspace still matches its recorded head and has no staged/unstaged files."""
        try:
            root, common, head = self.inspect(workspace.path)
            branch = self._run(root, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
            return (root == workspace.path.resolve(strict=True)
                    and common == workspace.common_dir.resolve(strict=True)
                    and branch == workspace.branch
                    and head == (workspace.head_oid or workspace.base_oid)
                    and not self.changed_paths(root))
        except (GitError, OSError, UnicodeError):
            return False

    def verify_workspace(self, workspace: Workspace) -> bool:
        try:
            root, common, head = self.inspect(workspace.path)
            branch = self._run(root, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
            return (root == workspace.path.resolve(strict=True)
                    and common == workspace.common_dir.resolve(strict=True)
                    and branch == workspace.branch
                    and head == (workspace.head_oid or workspace.base_oid))
        except (GitError, OSError, UnicodeError):
            return False

    def reconcile_operation(self, workspace: Workspace, kind: str, operation_id: str,
                            request_json: str) -> Workspace | Checkpoint | None:
        """Observe existing refs/worktrees only; never repeats an unresolved Git command."""
        request = json.loads(request_json)
        if kind == "workspace_create":
            target = workspace.path
            if not target.exists():
                return None
            root, common, head = self.inspect(target)
            branch = self._run(root, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
            status = self._run(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            if (root != target.resolve(strict=True) or common != workspace.common_dir.resolve(strict=True)
                    or branch != workspace.branch or head != workspace.base_oid or status):
                return None
            return Workspace(workspace.workspace_id, workspace.project_id, workspace.task_id,
                             workspace.definition_revision, root, common, branch, workspace.base_oid,
                             "active", False, head, workspace.repository_root)
        if kind == "reconcile" and request.get("action") == "verify_no_changes":
            if self.verify_no_changes(workspace):
                return Workspace(workspace.workspace_id, workspace.project_id, workspace.task_id,
                                 workspace.definition_revision, workspace.path, workspace.common_dir,
                                 workspace.branch, workspace.base_oid, "active", False,
                                 workspace.head_oid or workspace.base_oid, workspace.repository_root)
            return None
        if kind != "checkpoint" or not workspace.path.exists():
            return None
        root, common, head = self.inspect(workspace.path)
        if root != workspace.path.resolve(strict=True) or common != workspace.common_dir.resolve(strict=True):
            return None
        branch = self._run(root, "symbolic-ref", "--short", "HEAD").decode("utf-8", "strict").strip()
        if branch != workspace.branch:
            return None
        ref_oid = self._run(root, "rev-parse", "--verify", f"refs/heads/{workspace.branch}").decode("ascii").strip()
        if head != ref_oid or head == workspace.head_oid:
            return None
        data = self._run(root, "cat-file", "-p", head)
        headers = data.split(b"\n\n", 1)[0].splitlines()
        body = data.split(b"\n\n", 1)[1].decode("utf-8", "strict") if b"\n\n" in data else ""
        trailer = f"AgentFlow-Operation: {operation_id}"
        parents = [line[7:].decode("ascii") for line in headers if line.startswith(b"parent ")]
        tree_oid = next((line[5:].decode("ascii") for line in headers if line.startswith(b"tree ")), "")
        if parents != [workspace.head_oid] or body.count(trailer) != 1:
            return None
        actor_name, actor_email = request.get("actor_name", ""), request.get("actor_email", "")
        if (not any(line.startswith(f"author {actor_name} <{actor_email}> ".encode("utf-8")) for line in headers)
                or not any(line.startswith(b"committer AgentFlow <agentflow@localhost> ") for line in headers)):
            return None
        diff = self._run(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parents[0], head)
        paths = tuple(sorted((item.decode("utf-8", "strict") for item in diff.split(b"\0") if item),
                             key=lambda value: value.encode("utf-8")))
        selected = set(request.get("paths", ()))
        if not paths or not set(paths) <= selected:
            return None
        tree = self._run(root, "ls-tree", "-r", "-z", head)
        blobs: dict[str, tuple[int, str]] = {}
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            mode_raw, object_type, oid_raw = meta.split(b" ", 2)
            name = raw_path.decode("utf-8", "strict")
            mode = int(mode_raw, 8)
            if mode == 0o160000 or object_type == b"commit":
                return None
            blobs[name] = (mode, oid_raw.decode("ascii"))
        if any(blobs.get(path, (0, ""))[0] not in (0, 0o100644, 0o100755) for path in paths):
            return None
        for path in paths:
            local = root.joinpath(*path.split("/"))
            if path in blobs:
                if not local.is_file() or local.is_symlink():
                    return None
                content = local.read_bytes()
                if self._blob_oid(content, len(head)) != blobs[path][1]:
                    return None
            elif local.exists():
                return None
        changed = set(self.changed_paths(root))
        if not changed <= set(paths):
            return None
        # The only automatic repair is refreshing this private worktree index after
        # the branch already points at the uniquely identified operation commit.
        try:
            index_tree = self._run(root, "write-tree").decode("ascii").strip()
        except GitError:
            return None
        parent_tree = self._run(root, "rev-parse", "--verify", f"{workspace.head_oid}^{{tree}}").decode("ascii").strip()
        if index_tree == parent_tree:
            self._run(root, "read-tree", head)
        elif index_tree != tree_oid:
            return None
        checkpoint = Checkpoint("cp-" + hashlib.sha256(operation_id.encode("ascii")).hexdigest()[:32],
                                workspace.workspace_id, operation_id, head, parents[0], tree_oid,
                                workspace.branch, paths, pathset_digest(paths), True,
                                actor_name, actor_email)
        if not self.verify_checkpoint(workspace, checkpoint):
            return None
        return checkpoint
