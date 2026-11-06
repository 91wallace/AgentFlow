"""Claude Code adapter over its documented non-interactive CLI protocol."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .contracts import AgentResult, ExecutionError, ExecutionRequest, ProcessResult, ProcessSpec


_VERSION = re.compile(r"\b(\d+)\.(\d+)\.(\d+)\b")
_MIN_VERSION = (2, 1, 259)  # --permission-prompts none and stable stream contract.


class ClaudeCodeAdapter:
    adapter_id = "claude-code"
    api_version = "1"

    def __init__(self, executable: Path | None = None, *, runner=subprocess.run):
        resolved = str(executable) if executable else shutil.which("claude")
        if not resolved or not Path(resolved).is_file():
            raise ExecutionError("Claude Code CLI não encontrado")
        self.executable = Path(resolved).resolve()
        self.runner = runner

    @staticmethod
    def _probe_environment() -> dict[str, str]:
        names = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "APPDATA",
                 "LOCALAPPDATA", "TEMP", "TMP", "CLAUDE_CONFIG_DIR")
        return {name: os.environ[name] for name in names if name in os.environ}

    def _probe(self, *args: str) -> str:
        try:
            result = self.runner([str(self.executable), *args], capture_output=True,
                                 text=True, timeout=15, check=False, encoding="utf-8",
                                 errors="replace", env=self._probe_environment())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"pré-checagem Claude Code falhou: {type(exc).__name__}") from exc
        if result.returncode:
            raise ExecutionError(f"pré-checagem Claude Code rejeitada: {args[0]}")
        return result.stdout + result.stderr

    def prepare(self, request: ExecutionRequest) -> ProcessSpec:
        selection = request.model_selection
        if (selection is None or selection.mode != "manual"
                or selection.agent_id != self.adapter_id or selection.provider_id != "anthropic"):
            raise ExecutionError("Claude Code exige seleção manual de provider/modelo")
        if request.policy.sandbox is not None:
            raise ExecutionError("sandbox do Claude Code cobre apenas comandos shell; política genérica não comprovada")
        if request.policy.approval == "on-request":
            raise ExecutionError("aprovação interativa incompatível com Claude Code headless")
        if not request.workspace.is_dir() or not (request.workspace / ".git").exists():
            raise ExecutionError("workspace Git inválido")
        version_output = self._probe("--version")
        match = _VERSION.search(version_output)
        if not match:
            raise ExecutionError("versão Claude Code não identificável")
        version_tuple = tuple(int(value) for value in match.groups())
        if version_tuple < _MIN_VERSION:
            raise ExecutionError("Claude Code anterior às capabilities headless exigidas")
        version = ".".join(match.groups())
        try:
            auth = json.loads(self._probe("auth", "status", "--json"))
        except ValueError as exc:
            raise ExecutionError("estado de autenticação Claude Code não estruturado") from exc
        auth_method = auth.get("authMethod") if isinstance(auth, dict) else None
        if auth_method not in {"claude.ai", "oauth_token", "api_key", "api_key_helper"}:
            raise ExecutionError("provider Anthropic não confirmado pelo estado de autenticação")
        # These are session-only controls: no config file is modified. Approval prompts
        # are denied rather than left waiting for a human in headless mode.
        args = ("-p", "--input-format", "text", "--output-format", "stream-json",
                "--verbose", "--model", selection.model_id,
                "--permission-prompts", "none", "--no-session-persistence",
                "--disable-slash-commands")
        return ProcessSpec(
            self.executable, args, request.workspace.resolve(), request.instruction,
            (), self.adapter_id, version, "unknown", "no-interactive-prompts",
            ("headless", "stream-json", "manual-model", "model-reported-in-init",
             "permission-prompts-denied", "sandbox-scope-shell-only",
             "provider-anthropic-auth-confirmed"), self.api_version,
            "anthropic", selection.model_id)

    def interpret(self, result: ProcessResult) -> AgentResult:
        if result.status != "process_succeeded":
            return AgentResult(result.status, "processo não concluído")
        try:
            events = [json.loads(line) for line in result.stdout_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
        except (OSError, ValueError) as exc:
            raise ExecutionError("stream JSON Claude Code inválido") from exc
        if not events or any(not isinstance(event, dict) for event in events):
            raise ExecutionError("stream Claude Code vazio ou inválido")
        init = [event for event in events if event.get("type") == "system" and event.get("subtype") == "init"]
        terminal = [event for event in events if event.get("type") == "result"]
        if len(init) != 1 or len(terminal) != 1:
            raise ExecutionError("stream Claude Code sem init/result únicos")
        requested_model = dict(result.request_metadata).get("model_id", "")
        effective_model = init[0].get("model")
        if not isinstance(effective_model, str) or not effective_model:
            return AgentResult("process_succeeded", "modelo efetivo desconhecido",
                               selection_status="unknown")
        if effective_model != requested_model:
            return AgentResult("process_succeeded", "modelo efetivo diverge da seleção",
                               effective_provider_id="anthropic", effective_model_id=effective_model,
                               selection_status="mismatch")
        final = terminal[0]
        if final.get("is_error") is True or final.get("subtype") not in (None, "success"):
            return AgentResult("process_failed", f"resultado Claude Code: {final.get('subtype', 'error')}",
                               effective_provider_id="anthropic", effective_model_id=effective_model,
                               selection_status="confirmed")
        retry_events = sum(event.get("type") == "system" and event.get("subtype") == "api_retry"
                           for event in events)
        denials = final.get("permission_denials", [])
        summary = "permission_denied" if denials else f"result.success; provider_api_retries={retry_events}"
        return AgentResult("process_succeeded", summary,
                           effective_provider_id="anthropic", effective_model_id=effective_model,
                           selection_status="confirmed")
