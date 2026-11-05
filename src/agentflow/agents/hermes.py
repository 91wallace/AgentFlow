"""Nous Research Hermes Agent adapter over its documented JSONL CLI protocol."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .contracts import AgentResult, ExecutionError, ExecutionRequest, ProcessResult, ProcessSpec


_VERSION = re.compile(r"\b(?:Hermes Agent\s+)?v?(\d+)\.(\d+)\.(\d+)\b", re.IGNORECASE)
_IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")
_EVENT_TYPES = {"system", "text", "tool_use", "tool_result", "result"}
_FILE_TOOLS = {"patch", "read_file", "search_files", "write_file"}


class HermesAdapter:
    """Explicit manual selection; no Hermes package imports or config mutation."""

    adapter_id = "hermes"
    api_version = "1"

    def __init__(self, executable: Path | None = None, *, runner=subprocess.run):
        resolved = str(executable) if executable else shutil.which("hermes")
        if not resolved or not Path(resolved).is_file():
            raise ExecutionError("Hermes Agent CLI não encontrado")
        self.executable = Path(resolved).resolve()
        self.runner = runner

    @staticmethod
    def _probe_environment() -> dict[str, str]:
        # Preserve executable/config lookup only. Provider secrets are never copied
        # into ProcessSpec or preflight diagnostics.
        names = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "COMSPEC", "APPDATA",
                 "LOCALAPPDATA", "TEMP", "TMP", "HERMES_HOME")
        return {name: os.environ[name] for name in names if name in os.environ}

    def _probe(self, *args: str) -> str:
        try:
            result = self.runner([str(self.executable), *args], capture_output=True,
                                 text=True, timeout=15, check=False, encoding="utf-8",
                                 errors="replace", env=self._probe_environment())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError(f"preflight Hermes falhou: {type(exc).__name__}") from exc
        if result.returncode:
            # Never include CLI output in an exception: it may contain user data.
            raise ExecutionError(f"preflight Hermes rejeitou {args[-1]}")
        return result.stdout + result.stderr

    def prepare(self, request: ExecutionRequest) -> ProcessSpec:
        selection = request.model_selection
        if (selection is None or selection.mode != "manual" or selection.agent_id != self.adapter_id
                or selection.provider_id.casefold() == "auto"
                or not _IDENTIFIER.fullmatch(selection.provider_id)
                or not _IDENTIFIER.fullmatch(selection.model_id)):
            raise ExecutionError("Hermes exige seleção manual e explícita de provider/modelo")
        if request.policy.sandbox is not None:
            raise ExecutionError("Hermes não atesta enforcement do sandbox solicitado")
        if request.policy.approval is not None:
            raise ExecutionError("Hermes headless não comprova a política de aprovação solicitada")
        if not request.workspace.is_dir() or not (request.workspace / ".git").exists():
            raise ExecutionError("workspace Git inválido")

        version_text = self._probe("--version")
        version_match = _VERSION.search(version_text)
        if not version_match:
            raise ExecutionError("versão Hermes Agent não identificável")
        version = ".".join(version_match.groups())
        global_help = self._probe("--help")
        chat_help = self._probe("chat", "--help")
        required_global = ("--safe-mode", "--ignore-rules")
        required_chat = ("--oneshot", "--query-file", "--provider", "--model", "--format",
                         "stream-json", "--toolsets")
        if (any(flag not in global_help for flag in required_global)
                or any(flag not in chat_help for flag in required_chat)):
            raise ExecutionError("Hermes CLI não suporta o perfil headless restrito exigido")

        args = (
            "chat", "--oneshot", "--query-file", "-", "--provider", selection.provider_id,
            "--model", selection.model_id, "--format", "stream-json", "--toolsets", "file",
            "--safe-mode", "--ignore-rules",
        )
        return ProcessSpec(
            self.executable, args, request.workspace.resolve(), request.instruction, (),
            self.adapter_id, version, "unknown", "unknown",
            ("headless-oneshot", "query-file-stdin", "stream-json", "manual-provider-model",
             "file-toolset-only-requested", "safe-mode-requested", "rules-memory-skills-disabled-requested",
             "effective-provider-unreported-by-protocol", "sandbox-enforcement-unknown",
             "network-enforcement-unknown"),
            self.api_version, selection.provider_id, selection.model_id)

    @staticmethod
    def _read_events(path: Path) -> tuple[list[dict], str | None]:
        events: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        return events, f"JSONL Hermes malformado na linha {number}"
                    if not isinstance(event, dict) or event.get("type") not in _EVENT_TYPES:
                        return events, f"evento Hermes inesperado na linha {number}"
                    if events and events[-1].get("type") == "result":
                        return events, "evento após resultado terminal Hermes"
                    if event.get("type") == "tool_use" and event.get("name") not in _FILE_TOOLS:
                        return events, "Hermes reportou ferramenta fora do toolset file"
                    events.append(event)
        except OSError:
            return events, "stream Hermes indisponível"
        return events, None

    def interpret(self, result: ProcessResult) -> AgentResult:
        if result.status in {"cancelled", "timed_out", "interrupted"}:
            return AgentResult(result.status, "execução Hermes interrompida sem conclusão")
        events, parse_error = self._read_events(result.stdout_path)
        if parse_error:
            raise ExecutionError(parse_error)
        if not events:
            if result.status == "process_failed":
                return AgentResult("process_failed", "Hermes terminou sem stream; provável falha de ambiente/auth")
            raise ExecutionError("stream Hermes vazio; ausência de evidência não é sucesso")

        init_events = [event for event in events
                       if event.get("type") == "system" and event.get("subtype") == "init"]
        terminal = [event for event in events if event.get("type") == "result"]
        if len(init_events) != 1 or len(terminal) != 1:
            raise ExecutionError("stream Hermes exige um system/init e um result terminal")
        if events[-1].get("type") != "result":
            raise ExecutionError("stream Hermes sem result terminal")

        terminal_code = terminal[0].get("exit_code")
        if type(terminal_code) is not int:
            raise ExecutionError("result Hermes sem exit_code inteiro")
        if result.exit_code != terminal_code:
            return AgentResult("process_failed", "exit_code do processo diverge do result Hermes")
        if terminal_code == 130:
            return AgentResult("cancelled", "Hermes reportou cancelamento")
        if result.status != "process_succeeded" or terminal_code != 0 or terminal[0].get("error"):
            return AgentResult("process_failed", "Hermes reportou falha, partial ou erro de inicialização")

        requested = dict(result.request_metadata)
        requested_provider = requested.get("provider_id", "unknown")
        requested_model = requested.get("model_id", "unknown")
        effective_model = init_events[0].get("model")
        effective_provider = init_events[0].get("provider")
        if not isinstance(effective_model, str) or not effective_model:
            return AgentResult("process_succeeded", "modelo efetivo não informado pelo system/init",
                               selection_status="unknown")
        if effective_model != requested_model:
            return AgentResult("process_succeeded", "modelo efetivo diverge da seleção",
                               effective_provider_id=(effective_provider if isinstance(effective_provider, str)
                                                      else "unknown"),
                               effective_model_id=effective_model, selection_status="mismatch")
        if isinstance(effective_provider, str) and effective_provider != requested_provider:
            return AgentResult("process_succeeded", "provider efetivo diverge da seleção",
                               effective_provider_id=effective_provider, effective_model_id=effective_model,
                               selection_status="mismatch")
        if not isinstance(effective_provider, str) or not effective_provider:
            return AgentResult("process_succeeded", "modelo confirmado; provider efetivo não informado",
                               effective_provider_id="unknown", effective_model_id=effective_model,
                               selection_status="unknown")
        return AgentResult("process_succeeded", "provider/model confirmados; conclusão requer validação externa",
                           effective_provider_id=effective_provider, effective_model_id=effective_model,
                           selection_status="confirmed")
