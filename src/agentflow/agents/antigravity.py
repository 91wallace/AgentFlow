"""Google Antigravity CLI adapter using only its documented headless protocol."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .contracts import (AgentResult, ExecutionError, ExecutionRequest, ProcessResult,
                       ProcessSpec)


_VERSION = re.compile(r"\b(?:agy\s*)?v?(\d+)\.(\d+)\.(\d+)\b", re.IGNORECASE)
_MODEL_LINE = re.compile(r"^\s*([a-z0-9][a-z0-9._:/-]*)\s{2,}", re.IGNORECASE)
_MIN_VERSION = (1, 2, 0)


class AntigravityAdapter:
    """Adapter with metadata-only preflight; prepare never starts an agent session."""

    adapter_id = "antigravity"
    api_version = "1"

    def __init__(self, executable: Path | None = None, *, runner=subprocess.run):
        resolved = str(executable) if executable else shutil.which("agy")
        if not resolved or not Path(resolved).is_file():
            raise ExecutionError("Antigravity CLI agy não encontrado")
        self.executable = Path(resolved).resolve()
        self.runner = runner

    @staticmethod
    def _probe_environment() -> dict[str, str]:
        # Preserve account/config lookup and path resolution; never forward arbitrary
        # environment variables (including API keys) to metadata-only probes.
        names = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "APPDATA",
                 "LOCALAPPDATA", "TEMP", "TMP")
        return {name: os.environ[name] for name in names if name in os.environ}

    def _probe(self, *args: str) -> str:
        try:
            result = self.runner([str(self.executable), *args], capture_output=True,
                                 text=True, timeout=10, check=False, encoding="utf-8",
                                 errors="replace", env=self._probe_environment())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"pré-checagem Antigravity falhou: {type(exc).__name__}") from exc
        if result.returncode:
            raise ExecutionError(f"pré-checagem Antigravity rejeitada: {args[0]}")
        return result.stdout + result.stderr

    def prepare(self, request: ExecutionRequest) -> ProcessSpec:
        selection = request.model_selection
        if selection is None or selection.mode != "manual" or selection.agent_id != self.adapter_id:
            raise ExecutionError("Antigravity exige seleção explícita de modelo por tentativa")
        # Codex's named sandbox/approval levels do not map to Antigravity semantics.
        # Inherit the user's settings unchanged; block requests that imply a mapping.
        if request.policy.sandbox is not None or request.policy.approval is not None:
            raise ExecutionError("política sandbox/aprovação não mapeável com equivalência comprovada")
        if not request.workspace.is_dir() or not (request.workspace / ".git").exists():
            raise ExecutionError("workspace Git inválido")
        version_output = self._probe("--version")
        match = _VERSION.search(version_output)
        if not match:
            raise ExecutionError("versão Antigravity não identificável")
        version_tuple = tuple(int(part) for part in match.groups())
        if version_tuple < _MIN_VERSION:
            raise ExecutionError("versão Antigravity CLI anterior à interface headless documentada")
        version = ".".join(match.groups())
        help_text = self._probe("--help")
        required = ("--input-format", "--output-format", "--model", "--print-timeout")
        if any(flag not in help_text for flag in required):
            raise ExecutionError("Antigravity CLI sem capabilities headless obrigatórias")
        available_models = self._probe("models")
        model = selection.model_id
        if not any(line_match and line_match.group(1) == model
                   for line in available_models.splitlines()
                   for line_match in [_MODEL_LINE.match(line)]):
            raise ExecutionError("modelo Antigravity não confirmado no catálogo local")
        message = json.dumps({"event": "user", "message": {"content": request.instruction}},
                             ensure_ascii=False, separators=(",", ":")) + "\n"
        return ProcessSpec(
            self.executable,
            ("--input-format", "stream-json", "--output-format", "stream-json",
             "--model", model, "--print-timeout", f"{request.policy.timeout_seconds}s"),
            request.workspace.resolve(), message, (), self.adapter_id, version,
            "unknown", "unknown",
            ("headless", "stream-json", "manual-model", "model-inventory-checked",
             "permission-mode-observable", "sandbox-enforcement-unknown",
             "network-enforcement-unknown"), self.api_version,
            selection.provider_id, model)

    def interpret(self, result: ProcessResult) -> AgentResult:
        if result.status != "process_succeeded":
            return AgentResult(result.status, "processo não concluído")
        metadata = dict(result.request_metadata)
        requested_model = metadata.get("model_id", "")
        try:
            events = [json.loads(line) for line in result.stdout_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
        except (OSError, ValueError) as exc:
            raise ExecutionError("stream JSON do Antigravity inválido") from exc
        if not events or any(not isinstance(item, dict) for item in events):
            raise ExecutionError("stream JSON do Antigravity vazio ou inválido")
        init = [item.get("init") for item in events if item.get("event") == "init"]
        terminal = [item.get("result") for item in events if item.get("event") == "result"]
        if len(init) != 1 or len(terminal) != 1 or not isinstance(init[0], dict) or not isinstance(terminal[0], dict):
            raise ExecutionError("stream Antigravity sem init/result únicos")
        effective_model = init[0].get("model")
        if not isinstance(effective_model, str) or not effective_model:
            return AgentResult("process_succeeded", "modelo efetivo desconhecido",
                               effective_model_id="unknown", selection_status="unknown")
        if effective_model != requested_model:
            return AgentResult("process_succeeded", "modelo efetivo diverge da seleção",
                               effective_model_id=effective_model, selection_status="mismatch")
        status = terminal[0].get("status")
        if status != "SUCCESS":
            return AgentResult("process_failed", f"status Antigravity: {status or 'unknown'}",
                               effective_model_id=effective_model, selection_status="confirmed")
        try:
            diagnostics = result.stderr_path.read_text(encoding="utf-8").casefold()
        except OSError:
            diagnostics = ""
        if any(marker in diagnostics for marker in ("soft-denied", "soft denied", "requires approval")):
            summary = "headless_permission_soft_denied"
        else:
            summary = "result.success"
        return AgentResult("process_succeeded", summary,
                           effective_provider_id=metadata.get("provider_id", "unknown"),
                           effective_model_id=effective_model, selection_status="confirmed")
