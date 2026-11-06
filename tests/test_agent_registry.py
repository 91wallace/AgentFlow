import pytest

from agentflow.agents import (AdapterDescriptor, AgentRegistry, CapabilityReport,
                              ExecutionError)


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
