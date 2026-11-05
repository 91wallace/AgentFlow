"""OpenCode adapter using its documented non-interactive CLI surface."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .contracts import AgentResult, ExecutionError, ExecutionRequest, ProcessResult, ProcessSpec


_VERSION = re.compile(r"\b(?:v)?(\d+)\.(\d+)\.(\d+)\b")
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class OpenCodeAdapter:
    """Manual provider/model integration. Unknown runtime controls fail closed."""

    adapter_id = "opencode"
    api_version = "1"

    def __init__(self, executable: Path | None = None, *, runner=subprocess.run):
        resolved = str(executable) if executable else shutil.which("opencode")
        if not resolved or not Path(resolved).is_file():
            raise ExecutionError("OpenCode CLI não encontrado")
        self.executable = Path(resolved).resolve()
        self.runner = runner

    @staticmethod
    def _probe_environment() -> dict[str, str]:
        names = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "APPDATA",
                 "LOCALAPPDATA", "TEMP", "TMP", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR")
        return {name: os.environ[name] for name in names if name in os.environ}

    def _probe(self, *args: str) -> str:
        try:
            result = self.runner([str(self.executable), *args], capture_output=True,
                                 text=True, timeout=15, check=False, encoding="utf-8",
                                 errors="replace", env=self._probe_environment())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"pré-checagem OpenCode falhou: {type(exc).__name__}") from exc
        if result.returncode:
            raise ExecutionError(f"pré-checagem OpenCode rejeitada: {args[-1]}")
        return result.stdout + result.stderr

    def prepare(self, request: ExecutionRequest) -> ProcessSpec:
        selection = request.model_selection
        if (selection is None or selection.mode != "manual" or selection.agent_id != self.adapter_id
                or not _ID.fullmatch(selection.provider_id) or not _ID.fullmatch(selection.model_id)):
            raise ExecutionError("OpenCode exige seleção manual de provider/modelo válido")
        if request.policy.sandbox is not None:
            raise ExecutionError("OpenCode não atesta enforcement de sandbox para este runtime")
        if not request.workspace.is_dir() or not (request.workspace / ".git").exists():
            raise ExecutionError("workspace Git inválido")
        version_output = self._probe("--pure", "--version")
        match = _VERSION.search(version_output)
        if not match:
            raise ExecutionError("versão OpenCode não identificável")
        version = ".".join(match.groups())
        global_help = self._probe("--help")
        run_help = self._probe("run", "--help")
        if "--pure" not in global_help or "--format" not in run_help or "--model" not in run_help:
            raise ExecutionError("OpenCode CLI sem capabilities headless obrigatórias")
        # Metadata-only inventory; do not refresh/download model catalogues.
        inventory = self._probe("--pure", "models", selection.provider_id)
        full_model = f"{selection.provider_id}/{selection.model_id}"
        if full_model not in inventory.split():
            raise ExecutionError("modelo OpenCode não confirmado no catálogo configurado")
        if len(request.instruction) > 8000:
            raise ExecutionError("instrução excede o limite seguro de argumento do OpenCode")
        args = ("--pure", "run", "--format", "json", "--model", full_model, request.instruction)
        return ProcessSpec(
            self.executable, args, request.workspace.resolve(), "", (), self.adapter_id,
            version, "unknown", "unknown",
            ("headless-run", "jsonl-output", "manual-model", "external-plugins-disabled-for-run",
             "configured-model-inventory-checked",
             "sandbox-enforcement-unknown", "network-enforcement-unknown",
             "effective-model-requires-result-attestation"), self.api_version,
            selection.provider_id, selection.model_id)

    def interpret(self, result: ProcessResult) -> AgentResult:
        if result.status != "process_succeeded":
            return AgentResult(result.status, "processo não concluído")
        try:
            events = [json.loads(line) for line in result.stdout_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
        except (OSError, ValueError) as exc:
            raise ExecutionError("stream JSON OpenCode inválido") from exc
        if not events or any(not isinstance(event, dict) for event in events):
            raise ExecutionError("stream JSON OpenCode vazio ou inválido")
        metadata = dict(result.request_metadata)
        requested_provider = metadata.get("provider_id", "")
        requested_model = metadata.get("model_id", "")
        observed = set()
        errors = []
        for event in events:
            # Only consume explicit identity fields; do not infer from text or session title.
            for candidate in (event, event.get("session"), event.get("info"), event.get("message")):
                if isinstance(candidate, dict):
                    provider, model = candidate.get("providerID"), candidate.get("modelID")
                    if isinstance(provider, str) and isinstance(model, str):
                        observed.add((provider, model))
            if event.get("type") in {"error", "session.error"}:
                errors.append("OpenCode reportou erro estruturado")
        if len(observed) > 1:
            return AgentResult("process_succeeded", "identidade efetiva ambígua",
                               selection_status="mismatch")
        if not observed:
            return AgentResult("process_succeeded", "modelo efetivo não atestado pelo stream",
                               selection_status="unknown")
        provider, model = next(iter(observed))
        full_model = f"{provider}/{model}"
        if provider != requested_provider or model != requested_model:
            return AgentResult("process_succeeded", "modelo efetivo diverge da seleção",
                               effective_provider_id=provider, effective_model_id=model,
                               selection_status="mismatch")
        if errors:
            return AgentResult("process_failed", errors[0], effective_provider_id=provider,
                               effective_model_id=model, selection_status="confirmed")
        return AgentResult("process_succeeded", "resultado de processo; conclusão requer validação externa",
                           effective_provider_id=provider, effective_model_id=model,
                           selection_status="confirmed")
