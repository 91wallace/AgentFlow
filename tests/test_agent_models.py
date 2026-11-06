from datetime import datetime, timezone

import pytest

from agentflow.agents import ExecutionError, ModelDescriptor, ProviderDescriptor


def test_provider_and_model_descriptors_are_immutable_and_digest_stable():
    provider = ProviderDescriptor("google", "cloud", "api-v1", ("text",), ("oauth",),
                                  "available", "2026-11-05T12:00:00Z", "official")
    model = ModelDescriptor("google", "gemini-pro", "2026-11-01", ("reasoning",),
                            context_tokens=10000, availability="available",
                            observed_at="2026-11-05T12:00:00Z", source="official",
                            input_cost_per_million="1.25", output_cost_per_million="4",
                            cost_currency="USD", cost_source="official-pricing")
    assert provider.descriptor_digest.startswith("sha256:")
    assert model.descriptor_digest.startswith("sha256:")
    assert not model.availability_is_stale(60, now=datetime(2026, 11, 5, 12, 0, 30,
                                                            tzinfo=timezone.utc))
    assert model.availability_is_stale(60, now=datetime(2026, 11, 5, 12, 2,
                                                        tzinfo=timezone.utc))
    with pytest.raises((AttributeError, TypeError)):
        model.model_id = "other"


def test_unknowns_and_costs_are_explicit_not_inferred():
    unknown = ModelDescriptor("unknown", "opaque", "alias-1", availability="unknown",
                              source="unverified")
    assert unknown.availability == "unknown"
    assert unknown.availability_is_stale(600)
    with pytest.raises(ExecutionError, match="moeda e fonte"):
        ModelDescriptor("p", "m", "1", source="catalog",
                        input_cost_per_million="1")
    with pytest.raises(ExecutionError):
        ModelDescriptor("p", "m", "1", source="catalog", input_cost_per_million="NaN",
                        cost_currency="USD", cost_source="pricing")


def test_provider_descriptor_rejects_duplicate_or_invalid_capabilities():
    with pytest.raises(ExecutionError):
        ProviderDescriptor("p", "1", "1", ("text", "text"), source="catalog")
    with pytest.raises(ExecutionError):
        ProviderDescriptor("p", "1", "1", availability="maybe", source="catalog")
