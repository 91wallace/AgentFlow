import pytest

from agentflow.agents import (AdapterDescriptor, AgentRegistry, CapabilityReport,
                              ExecutionError)
from agentflow.agents.contracts import (AgentAvailability, AgentCapabilities,
                                        NormalizedAgentState)


class FakeAdapter:
    def prepare(self, request):
        return request

    def interpret(self, result):
        return result


def descriptor(adapter_id="fake"):
    return AdapterDescriptor(adapter_id, "1", "1.0", "fake", ("linux", "windows"))


def test_registry_resolves_only_explicit_builtins_and_has_deterministic_ids():
    registry = AgentRegistry()
    fake = FakeAdapter()
    registry.register(descriptor("second"), fake)
    registry.register(descriptor("first"), FakeAdapter())
    assert registry.resolve("second") is fake
    assert registry.descriptor("second").api_version == "1"
    assert registry.ids() == ("first", "second")
    with pytest.raises(ExecutionError, match="não registrado"):
        registry.resolve("external")


def test_registry_rejects_duplicates_and_invalid_adapter_contract():
    registry = AgentRegistry()
    registry.register(descriptor(), FakeAdapter())
    with pytest.raises(ExecutionError, match="duplicado"):
        registry.register(descriptor(), FakeAdapter())
    with pytest.raises(ExecutionError, match="não implementa"):
        registry.register(descriptor("broken"), object())


def test_adapter_descriptor_and_capability_report_are_validated():
    with pytest.raises(ExecutionError):
        AdapterDescriptor("Bad ID", "1", "1", "fake", ("linux",))
    with pytest.raises(ExecutionError):
        CapabilityReport("sandbox", "enforced", "", "")
    assert CapabilityReport("network", "unknown", "official-docs").state == "unknown"


def test_registry_resolves_rich_lifecycle_contract_without_changing_legacy_contract():
    class RichAdapter(FakeAdapter):
        adapter_id = "rich"
        api_version = "1"

        def discover(self):
            return AgentAvailability("rich", "available", "rich-cli", "1.0", "test",
                                     "version_tested", "2026-11-11T00:00:00Z")

        def capabilities(self):
            return AgentCapabilities("supported", ("jsonl",), False, False, False,
                                     "agentflow.runtime.local", "agentflow.runtime.local",
                                     "agentflow.state.execution", "agentflow.state.execution")

        def prepare_task(self, request):
            return self.prepare(request)

        def collect_result(self, result):
            return self.interpret(result)

        def normalize_state(self, status):
            return NormalizedAgentState.UNKNOWN

    registry = AgentRegistry()
    adapter = RichAdapter()
    registry.register(descriptor("rich"), adapter)
    assert registry.resolve_lifecycle("rich") is adapter
    legacy = AgentRegistry()
    legacy.register(descriptor("fake"), FakeAdapter())
    with pytest.raises(ExecutionError, match="lifecycle v1"):
        legacy.resolve_lifecycle("fake")
