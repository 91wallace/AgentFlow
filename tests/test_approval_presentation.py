import sqlite3

import pytest

from agentflow.approval.persistence import BrokerStore
from agentflow.approval.presentation import (
    PresentationError, build_presentation_readonly, render_consent_preview,
)
from test_approval_webauthn import _challenge_bundle
from test_approval_identity import _intent


def test_presentation_is_complete_deterministic_and_roundtrips(tmp_path):
    path = tmp_path / "broker.sqlite"
    with BrokerStore(path) as store:
        _, _, challenge = _challenge_bundle(store, "presentation-op")
        presentation = store.build_consent_presentation(challenge.binding.request_id)
        assert presentation == store.build_consent_presentation(challenge.binding.request_id)
        assert type(presentation).from_json(presentation.to_json()) == presentation
        assert presentation.presentation_digest == challenge.binding.presentation_digest
        assert [item.kind for item in presentation.accepted_snapshots] == [
            "specification", "plan", "checkpoint", "payload"
        ]
        assert presentation.expected_effect_codes == (
            "provider_request", "payload_transmission", "possible_workspace_changes"
        )
        assert presentation.destination_id == "trusted-provider-gateway"
        assert presentation.provider_id == "provider-ci" and presentation.model_id == "model-ci"
        assert presentation.impact_json
        assert presentation.budget_json == '{"cost_status":"estimated"}'
        assert presentation.presentation_digest.startswith("sha256:")
        changed = presentation.to_dict()
        changed["plan_digest"] = "sha256:" + "0" * 64
        with pytest.raises(PresentationError, match="presentation_digest_mismatch"):
            type(presentation).from_dict(changed)


def test_preview_is_static_escaped_complete_and_explicitly_untrusted(tmp_path):
    malicious = "</pre><script>alert(1)</script><img src=x onerror=alert(2)>" + "x" * 16000
    with BrokerStore(tmp_path / "broker.sqlite") as store:
        _, _, challenge = _challenge_bundle(store, "xss-op", provider_payload={"prompt": malicious})
        presentation = store.build_consent_presentation(challenge.binding.request_id)
        rendered = render_consent_preview(presentation)
        assert "AMBIENTE DE DEMONSTRAÇÃO — NÃO HOMOLOGADO" in rendered
        assert "&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
        assert "</script>" not in rendered
        assert "<img src=x onerror" not in rendered
        assert malicious not in rendered
        assert "x" * 16000 in rendered
        assert presentation.presentation_digest in rendered
        assert "<form" not in rendered and "<button" not in rendered and "<a " not in rendered


def test_readonly_preview_does_not_mutate_broker_database(tmp_path):
    path = tmp_path / "broker.sqlite"
    with BrokerStore(path) as store:
        _, _, challenge = _challenge_bundle(store, "readonly-op")
        request_id = challenge.binding.request_id
        event_count = store.db.execute("SELECT count(*) FROM broker_events").fetchone()[0]
    before = path.read_bytes()
    preview = build_presentation_readonly(path, request_id)
    after = path.read_bytes()
    assert preview.request_id == request_id
    assert before == after
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM broker_events").fetchone()[0] == event_count
        assert db.execute("SELECT count(*) FROM execution_authorizations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM approval_decisions").fetchone()[0] == 0


def test_cli_preview_is_read_only_and_produces_html(tmp_path, capsys):
    from agentflow.cli import main

    path = tmp_path / "broker.sqlite"
    with BrokerStore(path) as store:
        _, _, challenge = _challenge_bundle(store, "cli-preview-op")
        request_id = challenge.binding.request_id
    before = path.read_bytes()
    assert main(["approval-preview", "--broker-db", str(path), "--request-id", request_id]) == 0
    rendered = capsys.readouterr().out
    assert "AMBIENTE DE DEMONSTRAÇÃO" in rendered
    assert "Não há botão" in rendered
    assert path.read_bytes() == before


@pytest.mark.parametrize("options", [
    {"include_approval_context": False},
    {"include_approval_context": True, "unexpected_context_field": True},
    {"include_approval_context": True, "mismatched_approval_revision": True},
])
def test_missing_unknown_or_divergent_approval_context_is_rejected(tmp_path, options):
    with BrokerStore(tmp_path / f"context-{len(options)}.sqlite") as store:
        # Unknown context keys cannot be silently reinterpreted as a broker-authored description.
        intent = _intent(store, f"bad-context-{len(options)}", **options)
        assert intent.operation_id.startswith("bad-context-")
        # No valid request may be created from a payload whose context is incomplete/ambiguous.
        with pytest.raises(PresentationError):
            from agentflow.approval.presentation import _validate_context
            row = store.db.execute("SELECT content_json FROM broker_snapshots WHERE snapshot_id=?",
                                   (intent.payload_snapshot_id,)).fetchone()
            payload = __import__("json").loads(row[0])
            _validate_context(payload, intent, ("execution:propose",))


def test_challenge_creation_rejects_changed_presentation_digest(tmp_path):
    from agentflow.approval.persistence import BrokerConflict

    with BrokerStore(tmp_path / "challenge.sqlite") as store:
        _, _, challenge = _challenge_bundle(store, "changed-presentation-op")
        with pytest.raises(BrokerConflict, match="displayed presentation changed"):
            store._create_webauthn_challenge(
                challenge.binding.request_id, "approver-human", "example.test",
                ("https://approver.example.test",), presentation_digest="sha256:" + "0" * 64,
            )
        assert store.db.execute("SELECT count(*) FROM webauthn_challenges").fetchone()[0] == 1


def test_request_presentation_rejects_expired_and_non_pending(tmp_path):
    from datetime import datetime, timedelta, timezone
    from agentflow.approval.persistence import BrokerConflict

    with BrokerStore(tmp_path / "expiry.sqlite") as store:
        _, _, challenge = _challenge_bundle(store, "expired-presentation-op")
        with pytest.raises(PresentationError, match="operation_expired"):
            store.build_consent_presentation(challenge.binding.request_id,
                                             now=datetime.now(timezone.utc) + timedelta(minutes=5))
        assert store.db.execute("SELECT count(*) FROM execution_authorizations").fetchone()[0] == 0
