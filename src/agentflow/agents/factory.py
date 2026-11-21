"""Composition boundary for native CLIs; no fallback between agents."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import re

from .contracts import AgentAvailability, ExecutionError, normalize_execution_state
from .selection import ModelSelectionRequest


class SelectedAdapter:
    def __init__(self, native, choice):
        self.native, self.choice = native, choice
        self.adapter_id = choice.agent
        self.api_version = native.api_version
        self.executable = native.executable

    def discover(self):
        if self.adapter_id == "codex":
            return self.native.discover()
        args = ("--pure", "--version") if self.adapter_id == "opencode" else ("--version",)
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", self.native._probe(*args))
        if not match:
            raise ExecutionError("versão do executor não identificável")
        return AgentAvailability(self.adapter_id, "available", str(self.executable), match[1],
                                 "cli_probe", "version_observed", datetime.now(timezone.utc).isoformat())

    def prepare_request(self, request):
        if self.adapter_id != "codex" and request.policy.approval == "on-request":
            raise ExecutionError("aprovação interativa não suportada neste fluxo headless")
        if request.model_selection is not None and (
                request.model_selection.agent_id, request.model_selection.provider_id, request.model_selection.model_id
                ) != (self.choice.agent, self.choice.provider, self.choice.model):
            raise ExecutionError("seleção da tentativa diverge do executor escolhido")
        selection = (ModelSelectionRequest(self.choice.agent, self.choice.provider, self.choice.model,
                                           "agentflow:" + self.choice.source)
                     if self.choice.model else None)
        return replace(request, model_selection=selection,
            policy=replace(request.policy, approval="never" if self.adapter_id == "codex" else None))

    def prepare_task(self, request):
        return self.native.prepare(self.prepare_request(request))

    prepare = prepare_task

    def collect_result(self, result):
        value = self.native.interpret(result)
        if value.process_status != "process_succeeded":
            return value
        if self.choice.model and (value.selection_status != "confirmed"
                or value.effective_model_id != self.choice.model
                or value.effective_provider_id != self.choice.provider):
            raise ExecutionError("identidade efetiva do modelo não confirmada")
        return value

    interpret = collect_result
    normalize_state = staticmethod(normalize_execution_state)

    def message_text(self, path):
        return final_message(path, self.adapter_id)


def create_adapter(choice):
    if choice.agent == "codex" and choice.model:
        raise ExecutionError("seleção manual de modelo Codex ainda não é atestável por este adaptador")
    from .codex import CodexAdapter
    from .antigravity import AntigravityAdapter
    from .opencode import OpenCodeAdapter
    from .claude_code import ClaudeCodeAdapter
    constructors = {"codex": CodexAdapter, "antigravity": AntigravityAdapter,
                    "opencode": OpenCodeAdapter, "claude-code": ClaudeCodeAdapter}
    # Preserve the native Codex path and its tested capabilities.
    native = constructors[choice.agent]()
    return native if choice.agent == "codex" and not choice.model else SelectedAdapter(native, choice)


def final_message(path, agent):
    messages = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            if agent == "antigravity" and event.get("event") == "result":
                result = event.get("result", {})
                text = result.get("response", result.get("text", result.get("result")))
                if isinstance(text, str):
                    messages = [text]
            elif agent == "claude-code" and event.get("type") == "result":
                if isinstance(event.get("result"), str):
                    messages = [event["result"]]
            elif agent == "opencode" and event.get("type") == "text":
                text = event.get("part", {}).get("text")
                if isinstance(text, str):
                    messages.append(text)
            elif agent == "codex" and event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    messages = [item.get("text", "")]
    if not messages:
        raise ExecutionError("mensagem final estruturada ausente")
    return "".join(messages)
