"""Composition boundary for native CLIs; no fallback between agents."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import re
import shutil
import os

from .registry import AdapterDescriptor, AgentRegistry

from .contracts import AgentAvailability, AgentCapabilities, ExecutionError, normalize_execution_state
from .selection import ModelSelectionRequest


class SelectedAdapter:
    def __init__(self, native, choice, *, allow_unverified_model=False):
        self.native, self.choice = native, choice
        self.adapter_id = choice.agent
        self.api_version = native.api_version
        self.executable = native.executable
        self.allow_unverified_model = allow_unverified_model

    def discover(self):
        if callable(getattr(self.native, "discover", None)):
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
            if (not self.allow_unverified_model or value.selection_status == "mismatch"
                    or value.effective_model_id not in {"unknown", self.choice.model}
                    or self.choice.provider != "native" and value.effective_provider_id not in {"unknown", self.choice.provider}):
                raise ExecutionError("identidade efetiva do modelo não confirmada")
        return value

    interpret = collect_result
    normalize_state = staticmethod(normalize_execution_state)

    def message_text(self, path):
        return final_message(path, self.adapter_id)


def create_adapter(choice, *, allow_unverified_model=False):
    if choice.agent == "codex" and choice.model:
        raise ExecutionError("seleção manual de modelo Codex ainda não é atestável por este adaptador")
    if choice.agent == "gemini" and choice.model and not allow_unverified_model:
        raise ExecutionError("seleção manual Gemini sem atestação de provider; use o padrão nativo do CLI")
    if choice.agent == "gemini" and choice.model and choice.provider != "native":
        raise ExecutionError("Gemini não oferece seleção de provider; use provider=native")
    # Preserve the native Codex path and its tested capabilities.
    native = create_registry().resolve(choice.agent)._native()
    return native if choice.agent == "codex" and not choice.model else SelectedAdapter(
        native, choice, allow_unverified_model=allow_unverified_model)


class RegisteredCLIAdapter:
    """Lazy registry entry: absent installations can still appear in inventory."""

    def __init__(self, descriptor, constructor):
        self.descriptor = descriptor
        self.constructor = constructor
        self.adapter_id = descriptor.adapter_id
        self.api_version = descriptor.api_version

    def _native(self):
        return self.constructor()

    def discover(self):
        checked_at = datetime.now(timezone.utc).isoformat()
        executable = shutil.which(self.descriptor.executable)
        if executable is None:
            return AgentAvailability(self.adapter_id, "unavailable", self.descriptor.executable,
                                     None, "path", "executable_missing", checked_at)
        native = self._native()
        if callable(getattr(native, "discover", None)):
            return native.discover()
        args = ("--pure", "--version") if self.adapter_id == "opencode" else ("--version",)
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", native._probe(*args))
        return AgentAvailability(
            self.adapter_id, "available" if match else "unknown", str(native.executable),
            match[1] if match else None, "cli_probe",
            "version_observed" if match else "version_unrecognized", checked_at)

    def capabilities(self):
        native = self._native()
        hook = getattr(native, "capabilities", None)
        if hook:
            return hook()
        return AgentCapabilities(
            "unknown", (), False, False, False, "agentflow.runtime.local",
            "agentflow.runtime.local", "agentflow.state.execution", "agentflow.state.execution")

    def prepare(self, request):
        return self._native().prepare(request)

    def interpret(self, result):
        return self._native().interpret(result)


def create_registry():
    """Compose trusted built-ins; inventory consumers don't contain vendor lists."""
    from .codex import CodexAdapter
    from .antigravity import AntigravityAdapter
    from .opencode import OpenCodeAdapter
    from .claude_code import ClaudeCodeAdapter
    from .gemini import GeminiAdapter
    registry = AgentRegistry()
    for adapter_id, executable, constructor in (
            ("codex", "codex", CodexAdapter), ("antigravity", "agy", AntigravityAdapter),
            ("opencode", "opencode", OpenCodeAdapter), ("claude-code", "claude", ClaudeCodeAdapter),
            ("gemini", "gemini", GeminiAdapter)):
        descriptor = AdapterDescriptor(adapter_id, "1", "1", executable,
                                       ("windows", "linux"))
        registry.register(descriptor, RegisteredCLIAdapter(descriptor, constructor))
    return registry


def create_role_adapter(binding):
    """Pin all roles to the confirmed adapter identity, including the planner."""
    native = create_adapter(binding, allow_unverified_model=True)
    return PinnedRoleAdapter(native, binding)


class PinnedRoleAdapter:
    def __init__(self, adapter, binding):
        self.adapter, self.binding = adapter, binding
        self.adapter_id = binding.agent_id
        self.api_version = binding.api_version
        self.executable = adapter.executable

    def discover(self):
        return self.adapter.discover()

    def prepare(self, request):
        spec = self.adapter.prepare(request)
        if (spec.adapter_name != self.binding.agent_id or spec.adapter_version != self.binding.adapter_version
                or spec.adapter_api_version != self.binding.api_version
                or os.path.normcase(str(spec.executable)) != os.path.normcase(self.binding.executable)):
            raise ExecutionError("role_adapter_identity_changed_before_spawn")
        environment = tuple((key, value) for key, value in spec.environment if key != "AGENTFLOW_MANAGED_EXECUTION")
        return replace(spec, environment=environment + (("AGENTFLOW_MANAGED_EXECUTION", "1"),))

    prepare_task = prepare

    def interpret(self, result):
        return self.adapter.interpret(result)

    collect_result = interpret
    normalize_state = staticmethod(normalize_execution_state)

    def message_text(self, path):
        return final_message(path, self.adapter_id)


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
            elif agent == "gemini" and event.get("type") == "message" and event.get("role") == "assistant":
                content = event.get("content")
                if isinstance(content, str):
                    messages.append(content)
    if not messages:
        raise ExecutionError("mensagem final estruturada ausente")
    return "".join(messages)
