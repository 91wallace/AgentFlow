"""Gemini CLI headless adapter; uses AgentFlow's existing process runtime.

Protocol references and validation limits are recorded in ADR 0024. Native
permissions, authentication, sandbox and workspace trust remain CLI-owned.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .contracts import (AgentAvailability, AgentCapabilities, AgentResult,
                        ExecutionError, ProcessSpec, normalize_execution_state)


_VERSION = re.compile(r"(?<![\w.])(?:v)?(\d+\.\d+\.\d+(?:-[\w.-]+)?)\b")
_EVENTS = {"init", "message", "tool_use", "tool_result", "error", "result"}


class GeminiAdapter:
    adapter_id = "gemini"
    api_version = "1"

    def __init__(self, executable: Path | None = None, *, runner=subprocess.run):
        resolved = str(executable) if executable else shutil.which("gemini")
        if not resolved or not Path(resolved).is_file():
            raise ExecutionError("Gemini CLI não encontrado")
        self.executable = Path(resolved).resolve()
        self.runner = runner

    def _probe(self, *args):
        names = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
                 "APPDATA", "LOCALAPPDATA")
        try:
            result = self.runner([str(self.executable), *args], capture_output=True, text=True,
                                 timeout=10, check=False, encoding="utf-8", errors="replace",
                                 env={key: os.environ[key] for key in names if key in os.environ})
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError("pré-checagem Gemini falhou") from exc
        if result.returncode:
            raise ExecutionError("pré-checagem Gemini rejeitada")
        return result.stdout + result.stderr

    def discover(self):
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            match = _VERSION.search(self._probe("--version"))
        except ExecutionError:
            return AgentAvailability(self.adapter_id, "unknown", str(self.executable), None,
                                     "cli_probe", "probe_failed", checked_at)
        return AgentAvailability(
            self.adapter_id, "available" if match else "unknown", str(self.executable),
            match[1] if match else None, "cli_probe",
            "version_observed" if match else "version_unrecognized", checked_at)

    def capabilities(self):
        return AgentCapabilities(
            "supported", ("jsonl",), False, False, False, "agentflow.runtime.local",
            "agentflow.runtime.local", "agentflow.state.execution", "agentflow.state.execution")

    def prepare(self, request):
        if request.policy.approval == "on-request":
            raise ExecutionError("aprovação interativa incompatível com Gemini headless")
        selection = request.model_selection
        if selection is not None and (selection.agent_id != self.adapter_id or selection.mode != "manual"):
            raise ExecutionError("seleção Gemini inválida")
        if not request.workspace.is_dir() or not (request.workspace / ".git").exists():
            raise ExecutionError("workspace Git inválido")
        match = _VERSION.search(self._probe("--version"))
        if match is None:
            raise ExecutionError("versão Gemini não identificável")
        help_text = self._probe("--help")
        if any(flag not in help_text for flag in ("--output-format", "stream-json", "--model")):
            raise ExecutionError("Gemini CLI sem capabilities headless exigidas")
        # Non-TTY stdin selects headless mode. Don't add --yolo, --skip-trust,
        # or permission/sandbox overrides; native denials remain denials.
        arguments = ("--output-format", "stream-json")
        if selection:
            arguments += ("--model", selection.model_id)
        return ProcessSpec(
            self.executable, arguments, request.workspace.resolve(), request.instruction, (),
            self.adapter_id, match[1], "unknown", "unknown",
            ("headless", "jsonl", "stdin", "manual-model" if selection else "native-default-model",
             "native-sandbox-config-unmodified", "native-permissions-unmodified",
             "provider-not-attested"), self.api_version,
            selection.provider_id if selection else "", selection.model_id if selection else "")

    def interpret(self, result):
        if result.status != "process_succeeded":
            return AgentResult(result.status, "processo Gemini não concluído")
        if result.exit_code != 0:
            raise ExecutionError("exit_code Gemini inconsistente com sucesso")
        try:
            events = [json.loads(line) for line in result.stdout_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
        except (OSError, ValueError) as exc:
            raise ExecutionError("stream JSON Gemini inválido") from exc
        if not events or any(not isinstance(event, dict) or event.get("type") not in _EVENTS
                             for event in events):
            raise ExecutionError("stream Gemini vazio ou inválido")
        if (events[0]["type"] != "init" or events[-1]["type"] != "result"
                or sum(event["type"] == "init" for event in events) != 1
                or sum(event["type"] == "result" for event in events) != 1):
            raise ExecutionError("stream Gemini exige init e result terminal únicos")
        terminal = events[-1]
        if terminal.get("status") not in {"success", "error"}:
            raise ExecutionError("estado terminal Gemini inválido")
        if (terminal["status"] == "error" or terminal.get("error")
                or any(event["type"] == "error" and event.get("severity") != "warning" for event in events)):
            return AgentResult("process_failed", "Gemini reportou erro; requer reconciliação")
        # init.model may be a routing alias such as "auto". Only usage naming
        # one model gives an observed model identity; don't freeze the CLI default.
        stats = terminal.get("stats")
        used_models = stats.get("models") if isinstance(stats, dict) else None
        effective_model = (next(iter(used_models)) if isinstance(used_models, dict)
                           and len(used_models) == 1 else "unknown")
        if not isinstance(effective_model, str) or not effective_model:
            effective_model = "unknown"
        requested = dict(result.request_metadata).get("model_id", "")
        status = "mismatch" if requested and effective_model not in ("unknown", requested) else "unknown"
        # The init event reports a model but does not attest the serving provider.
        # Explicit provider/model selections therefore remain inconclusive and the
        # SelectedAdapter gate prevents accepting them as confirmed identities.
        return AgentResult("process_succeeded", "Gemini result.success; provider não atestado",
                           effective_model_id=effective_model, selection_status=status)

    prepare_task = prepare
    collect_result = interpret
    normalize_state = staticmethod(normalize_execution_state)
