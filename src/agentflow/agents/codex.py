"""Codex CLI adapter. Probing never starts an agent session."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .contracts import AgentResult, ExecutionError, ExecutionRequest, ProcessResult, ProcessSpec


TESTED_VERSIONS = {"0.166.0"}


class CodexAdapter:
    def __init__(self, executable: Path | None = None):
        resolved = str(executable) if executable else shutil.which("codex")
        if not resolved or not Path(resolved).is_file():
            raise ExecutionError("Codex CLI não encontrado")
        self.executable = Path(resolved).resolve()

    def _probe(self, *args: str) -> str:
        inherited = ("PATH", "HOME", "USERPROFILE", "CODEX_HOME", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA")
        environment = {key: os.environ[key] for key in inherited if key in os.environ}
        try:
            completed = subprocess.run([str(self.executable), *args], capture_output=True, text=True,
                                       timeout=10, check=False, encoding="utf-8", errors="replace",
                                       env=environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"pré-checagem Codex falhou: {type(exc).__name__}") from exc
        if completed.returncode:
            raise ExecutionError(f"pré-checagem Codex rejeitada: {' '.join(args)}")
        return completed.stdout + completed.stderr

    def prepare(self, request: ExecutionRequest) -> ProcessSpec:
        version_text = self._probe("--version")
        match = re.search(r"codex-cli (\d+\.\d+\.\d+)", version_text)
        if not match or match.group(1) not in TESTED_VERSIONS:
            raise ExecutionError("versão Codex CLI não homologada")
        help_text = self._probe("exec", "--help")
        required = ("--sandbox", "--config", "--json", "--cd", "--strict-config")
        if any(flag not in help_text for flag in required) or any(value not in help_text for value in
                ("read-only", "workspace-write", "danger-full-access")):
            raise ExecutionError("Codex CLI não oferece capacidades exigidas")
        global_help = self._probe("--help")
        if "--ask-for-approval" not in global_help or "never" not in global_help or "on-request" not in global_help:
            raise ExecutionError("Codex CLI sem opção verificável de aprovação")
        self._probe("login", "status")
        sandbox = request.policy.sandbox
        approval = request.policy.approval
        if sandbox not in ("read-only", "workspace-write", "danger-full-access") or approval not in ("never", "on-request"):
            raise ExecutionError("sandbox ou aprovação efetiva não verificável")
        if approval == "on-request":
            raise ExecutionError("aprovação interativa incompatível com execução não interativa")
        if not request.workspace.is_dir() or not (request.workspace / ".git").exists():
            raise ExecutionError("workspace Git inválido")
        args = ["exec", "--strict-config", "--json", "-C", str(request.workspace.resolve())]
        args.extend(("--sandbox", sandbox, "--ask-for-approval", approval))
        args.append("-")
        return ProcessSpec(self.executable, tuple(args), request.workspace.resolve(), request.instruction,
                           (), "codex", match.group(1), sandbox, approval,
                           ("jsonl", "stdin", "strict-config", "sandbox", "approval-policy"))

    def interpret(self, result: ProcessResult) -> AgentResult:
        if result.status != "process_succeeded":
            return AgentResult(result.status, "processo não concluído")
        try:
            events = [json.loads(line) for line in result.stdout_path.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, ValueError) as exc:
            raise ExecutionError("saída JSONL do Codex inválida") from exc
        if not events or any(not isinstance(event, dict) or "type" not in event for event in events):
            raise ExecutionError("eventos Codex inválidos")
        if not any(event["type"] == "turn.completed" for event in events):
            raise ExecutionError("Codex não confirmou conclusão do turno")
        return AgentResult(result.status, "turn.completed")
