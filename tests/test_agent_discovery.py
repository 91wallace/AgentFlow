from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.agents.contracts import AgentAvailability, ExecutionError
from agentflow.agents.discovery import discover_agents
from agentflow.agents.factory import RegisteredCLIAdapter, create_registry
from agentflow.agents.models import ModelDescriptor
from agentflow.agents.registry import AdapterDescriptor, AgentRegistry
from agentflow.cli import main


class Adapter:
    def prepare(self, request):
        raise AssertionError("inventory must not execute tasks")

    def interpret(self, result):
        raise AssertionError("inventory must not execute tasks")

    def discover(self):
        return AgentAvailability("custom", "available", "/custom", "1.0.0", "test",
                                 "version_observed", "2026-11-23T12:00:00Z")


def registry(adapter=None):
    result = AgentRegistry()
    result.register(AdapterDescriptor("custom", "1", "1", "custom", ("windows", "linux")),
                    adapter or Adapter())
    return result


def test_discovery_is_extensible_and_configuration_is_not_model_access():
    reports = discover_agents(registry(), profiles={"custom": {"provider": "p", "model": "m"}})
    report, = reports
    assert report.availability.status == "available"
    assert report.configured_model == "m"
    assert report.models == ()
    assert report.authentication == "unknown"
    assert "configured_model_does_not_prove_access" in report.limitations
    assert "model_listing_unsupported" in report.limitations
    json.dumps(asdict(report))


def test_supported_model_inventory_preserves_evidence():
    adapter = Adapter()
    model = ModelDescriptor("p", "m", "1", availability="available",
                            observed_at="2026-11-23T12:00:00Z", source="supported-cli-inventory")
    adapter.discover_models = lambda: (model,)
    report, = discover_agents(registry(adapter))
    assert report.models == (model,)
    assert report.model_listing == "supported"
    assert report.authentication == "unknown"


@pytest.mark.parametrize("models", [(object(),),
    (ModelDescriptor("p", "m", "1", source="no-observation"),)])
def test_invalid_model_inventory_is_inconclusive(models):
    adapter = Adapter()
    adapter.discover_models = lambda: models
    report, = discover_agents(registry(adapter))
    assert report.models == ()
    assert report.model_listing == "failed"
    assert report.availability.status == "available"


def test_failure_is_isolated_without_leaking_cli_output():
    class Broken(Adapter):
        def discover(self):
            raise ExecutionError("secret-from-cli")
    result = registry(Broken())
    result.register(AdapterDescriptor("legacy", "1", "1", "legacy", ("linux",)), SimpleNamespace(
        prepare=lambda request: None, interpret=lambda result: None))
    reports = discover_agents(result)
    assert [report.availability.status for report in reports] == ["unknown", "unknown"]
    assert "secret-from-cli" not in json.dumps([asdict(report) for report in reports])


def test_wrong_discovery_identity_is_rejected():
    adapter = Adapter()
    adapter.discover = lambda: AgentAvailability("other", "available", "other", "1", "test",
                                                "version_observed", "2026-11-23T12:00:00Z")
    report, = discover_agents(registry(adapter))
    assert report.availability.adapter_id == "custom"
    assert report.availability.status == "unknown"


def test_capability_probe_failure_preserves_installation_observation():
    adapter = Adapter()
    adapter.capabilities = lambda: None
    report, = discover_agents(registry(adapter))
    assert report.availability.status == "available"
    assert report.capabilities is None
    assert report.capabilities_source == "unknown"
    assert "capability_probe_failed" in report.limitations


def test_builtins_can_be_listed_with_no_clis_installed(monkeypatch):
    monkeypatch.setattr("agentflow.agents.factory.shutil.which", lambda name: None)
    reports = discover_agents(create_registry())
    assert "gemini" in {report.descriptor.adapter_id for report in reports}
    assert all(report.availability.status == "unavailable" for report in reports)
    assert all(report.availability.reason_code == "executable_missing" for report in reports)


def test_cli_discovery_needs_no_repository_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agentflow.agents.factory.shutil.which", lambda name: None)
    assert main(["agents", "--path", str(tmp_path), "--format", "json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1
    assert len(output["agents"]) == 5
    assert list(tmp_path.iterdir()) == []


def test_registration_construction_is_lazy():
    descriptor = AdapterDescriptor("custom", "1", "1", "custom", ("linux",))
    RegisteredCLIAdapter(descriptor, lambda: pytest.fail("must not construct native CLI"))
