"""Trusted-source consent presentation and inert HTML preview.

This module only reconstructs and displays an accepted operation. It has no approval,
WebAuthn enrollment, grant, provider, listener, or execution capability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import sqlite3
from typing import Any, ClassVar

from .protocol import (
    PROTOCOL_VERSION, AcceptedSnapshot, ApprovalRequest, OperationIntent, ProtocolError,
    _digest, _identifier, _timestamp, canonical_json, digest_json, parse_canonical_json,
)

PRESENTATION_VERSION = 1
MAX_PRESENTATION_INPUT_BYTES = 4 * 64 * 1024
MAX_PRESENTATION_HTML_BYTES = 1024 * 1024
_SNAPSHOT_ORDER = ("specification", "plan", "checkpoint", "payload")
_EFFECTS_BY_ACTION = {
    "provider_call": ("provider_request", "payload_transmission", "possible_workspace_changes"),
    "provider_model_change": ("provider_selection_change", "payload_transmission",
                              "possible_workspace_changes"),
    "model_escalation": ("model_escalation", "payload_transmission",
                         "possible_workspace_changes"),
}


class PresentationError(ValueError):
    """The operation cannot be shown completely and unambiguously."""


def _canonical_presentation(value: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PresentationError("presentation_content_invalid") from exc
    if len(encoded.encode("utf-8")) > MAX_PRESENTATION_INPUT_BYTES:
        raise PresentationError("presentation_exceeds_size_limit")
    return encoded


def _digest_contract(value: dict[str, Any], digest_field: str) -> str:
    content = dict(value)
    content.pop(digest_field, None)
    if type(content.get("accepted_snapshots")) is tuple:
        content["accepted_snapshots"] = [
            asdict(item) if isinstance(item, PresentedSnapshot) else item
            for item in content["accepted_snapshots"]
        ]
    return "sha256:" + hashlib.sha256(_canonical_presentation(content).encode("utf-8")).hexdigest()


def _string_tuple(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not tuple or (not allow_empty and not value):
        raise PresentationError(f"{label}_invalid")
    if len(set(value)) != len(value) or value != tuple(sorted(value)):
        raise PresentationError(f"{label}_not_canonical")
    for item in value:
        _identifier(item, label)
    return value


@dataclass(frozen=True)
class PresentedSnapshot:
    kind: str
    snapshot_id: str
    project_id: str
    revision_id: str
    schema_version: int
    canonical_digest: str
    content_json: str
    accepted_by: str
    accepted_at: str
    parent_digest: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __post_init__(self) -> None:
        if self.kind not in _SNAPSHOT_ORDER:
            raise PresentationError("snapshot_kind_invalid")
        for name in ("snapshot_id", "project_id", "revision_id", "accepted_by"):
            _identifier(getattr(self, name), name)
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise PresentationError("snapshot_schema_version_invalid")
        content = parse_canonical_json(self.content_json)
        if digest_json(content) != self.canonical_digest:
            raise PresentationError("snapshot_digest_mismatch")
        _digest(self.canonical_digest, "snapshot_digest")
        _timestamp(self.accepted_at, "snapshot_accepted_at")
        if self.parent_digest is not None:
            _digest(self.parent_digest, "snapshot_parent_digest")


@dataclass(frozen=True)
class ConsentPresentation:
    """Immutable display model rebuilt from accepted Broker records."""

    protocol_version: int
    presentation_version: int
    request_id: str
    request_digest: str
    operation_id: str
    intent_digest: str
    action_code: str
    requester_principal: str
    technical_client_id: str
    project_id: str
    task_id: str
    attempt_id: str
    scopes: tuple[str, ...]
    destination_id: str
    provider_id: str
    model_id: str
    specification_revision_id: str
    specification_digest: str
    plan_revision_id: str
    plan_digest: str
    checkpoint_digest: str
    payload_digest: str
    policy_digest: str
    catalog_digest: str
    budget_json: str
    parameters_json: str
    impact_json: str
    expected_effect_codes: tuple[str, ...]
    accepted_snapshots: tuple[PresentedSnapshot, ...]
    expires_at: str
    presentation_digest: str

    DIGEST_FIELD: ClassVar[str] = "presentation_digest"

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != PROTOCOL_VERSION:
            raise PresentationError("protocol_version_unsupported")
        if type(self.presentation_version) is not int or self.presentation_version != PRESENTATION_VERSION:
            raise PresentationError("presentation_version_unsupported")
        for name in ("request_id", "operation_id", "requester_principal", "technical_client_id",
                     "project_id", "task_id", "attempt_id", "destination_id", "provider_id",
                     "model_id", "specification_revision_id", "plan_revision_id"):
            _identifier(getattr(self, name), name)
        if self.action_code not in _EFFECTS_BY_ACTION:
            raise PresentationError("operation_action_not_presentable")
        for name in ("request_digest", "intent_digest", "specification_digest", "plan_digest",
                     "checkpoint_digest", "payload_digest", "policy_digest", "catalog_digest",
                     "presentation_digest"):
            _digest(getattr(self, name), name)
        _string_tuple(self.scopes, "scope")
        if self.expected_effect_codes != _EFFECTS_BY_ACTION[self.action_code]:
            raise PresentationError("expected_effects_incomplete_or_ambiguous")
        if (type(self.accepted_snapshots) is not tuple
                or tuple(item.kind for item in self.accepted_snapshots) != _SNAPSHOT_ORDER):
            raise PresentationError("accepted_snapshots_incomplete_or_ambiguous")
        if sum(len(item.content_json.encode("utf-8")) for item in self.accepted_snapshots) > MAX_PRESENTATION_INPUT_BYTES:
            raise PresentationError("presentation_exceeds_size_limit")
        parse_canonical_json(self.budget_json)
        parse_canonical_json(self.parameters_json)
        parse_canonical_json(self.impact_json)
        _timestamp(self.expires_at, "presentation_expiry")
        if _digest_contract(self.to_dict(), self.DIGEST_FIELD) != self.presentation_digest:
            raise PresentationError("presentation_digest_mismatch")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["accepted_snapshots"] = [item.to_dict() for item in self.accepted_snapshots]
        return value

    def to_json(self) -> str:
        return _canonical_presentation(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "ConsentPresentation":
        if type(value) is not str or len(value.encode("utf-8")) > MAX_PRESENTATION_INPUT_BYTES:
            raise PresentationError("presentation_size_invalid")

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise PresentationError("presentation_duplicate_field")
                result[key] = item
            return result

        try:
            parsed = json.loads(value, object_pairs_hook=pairs,
                                parse_constant=lambda _: (_ for _ in ()).throw(
                                    PresentationError("presentation_json_invalid")))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise PresentationError("presentation_json_invalid") from exc
        if _canonical_presentation(parsed) != value:
            raise PresentationError("presentation_json_not_canonical")
        return cls.from_dict(parsed)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ConsentPresentation":
        expected = {field.name for field in fields(cls)}
        if type(value) is not dict or set(value) != expected:
            raise PresentationError("presentation_fields_mismatch")
        normalized = dict(value)
        snapshots = normalized.get("accepted_snapshots")
        if type(snapshots) is not list:
            raise PresentationError("accepted_snapshots_invalid")
        try:
            normalized["accepted_snapshots"] = tuple(PresentedSnapshot(**item) for item in snapshots)
            if type(normalized.get("scopes")) is list:
                normalized["scopes"] = tuple(normalized["scopes"])
            if type(normalized.get("expected_effect_codes")) is list:
                normalized["expected_effect_codes"] = tuple(normalized["expected_effect_codes"])
            return cls(**normalized)
        except (TypeError, KeyError) as exc:
            raise PresentationError("presentation_fields_invalid") from exc


def _validate_context(payload: Any, intent: OperationIntent, scopes: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    if type(payload) is not dict or set(payload) != {"provider_payload", "approval_context"}:
        raise PresentationError("payload_snapshot_lacks_explicit_approval_context")
    context = payload["approval_context"]
    expected = {"context_version", "operation_id", "project_id", "task_id", "attempt_id",
                "specification_revision_id", "specification_digest", "plan_revision_id",
                "plan_digest", "checkpoint_digest", "scope", "impact"}
    if type(context) is not dict or set(context) != expected:
        raise PresentationError("approval_context_fields_incomplete_or_unknown")
    if type(context["context_version"]) is not int or context["context_version"] != 1:
        raise PresentationError("approval_context_version_unsupported")
    for key in ("operation_id", "project_id", "task_id", "attempt_id",
                "specification_revision_id", "specification_digest", "plan_revision_id",
                "plan_digest", "checkpoint_digest"):
        if context[key] != getattr(intent, key):
            raise PresentationError("approval_context_operation_mismatch")
    context_scopes = context["scope"]
    if type(context_scopes) is not list:
        raise PresentationError("approval_context_scope_invalid")
    context_scope_tuple = tuple(context_scopes)
    _string_tuple(context_scope_tuple, "scope")
    if context_scope_tuple != scopes:
        raise PresentationError("approval_context_scope_mismatch")
    impact = context["impact"]
    impact_fields = {"kind", "affected_requirement_ids", "affected_task_ids", "impact_digest"}
    if type(impact) is not dict or set(impact) != impact_fields or impact["kind"] != "task_attempt":
        raise PresentationError("impact_context_incomplete_or_ambiguous")
    reqs, tasks = impact["affected_requirement_ids"], impact["affected_task_ids"]
    if type(reqs) is not list or type(tasks) is not list:
        raise PresentationError("impact_identifiers_invalid")
    req_tuple, task_tuple = tuple(reqs), tuple(tasks)
    _string_tuple(req_tuple, "requirement_id")
    _string_tuple(task_tuple, "affected_task_id")
    if intent.task_id not in task_tuple:
        raise PresentationError("impact_does_not_cover_requested_task")
    _digest(impact["impact_digest"], "impact_digest")
    impact_body = {key: value for key, value in impact.items() if key != "impact_digest"}
    if digest_json(impact_body) != impact["impact_digest"]:
        raise PresentationError("impact_digest_mismatch")
    effect_codes = _EFFECTS_BY_ACTION.get(intent.action_code)
    if effect_codes is None:
        raise PresentationError("operation_action_not_presentable")
    impact_json = canonical_json({
        "kind": impact["kind"], "affected_requirement_ids": reqs,
        "affected_task_ids": tasks, "impact_digest": impact["impact_digest"],
        "expected_effect_codes": list(effect_codes),
    })
    # Force validation now; provider payload is also shown, verbatim but HTML-escaped.
    canonical_json(payload["provider_payload"])
    return impact_json, effect_codes


def build_presentation_from_connection(db: sqlite3.Connection, request_id: str,
                                       *, now: datetime | None = None) -> ConsentPresentation:
    """Rebuild a complete presentation solely from immutable Broker records."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise PresentationError("invalid_clock")
    current = instant.astimezone(timezone.utc)
    row = db.execute(
        "SELECT r.request_json,r.request_digest,r.operation_id,s.status "
        "FROM approval_requests r JOIN approval_request_state s USING(request_id) WHERE r.request_id=?",
        (request_id,),
    ).fetchone()
    if row is None or row[3] != "pending":
        raise PresentationError("request_missing_or_not_pending")
    request_data = json.loads(row[0])
    request_data["status"] = row[3]
    request = ApprovalRequest.from_dict(request_data)
    if request.request_digest != row[1] or request.operation_id != row[2]:
        raise PresentationError("request_integrity_mismatch")
    intent_row = db.execute("SELECT intent_json,intent_digest FROM operation_intents WHERE operation_id=?",
                            (request.operation_id,)).fetchone()
    if intent_row is None:
        raise PresentationError("operation_intent_missing")
    intent = OperationIntent.from_json(intent_row[0])
    if (intent.intent_digest != intent_row[1] or intent.intent_digest != request.intent_digest
            or request.action_code != intent.action_code or request.requester_principal != intent.principal_id):
        raise PresentationError("request_intent_mismatch")
    if intent.action_code not in _EFFECTS_BY_ACTION:
        raise PresentationError("operation_action_not_presentable")
    client = db.execute("SELECT identity_json,status FROM broker_clients WHERE client_id=?",
                        (intent.client_id,)).fetchone()
    policy_claim = db.execute(
        "SELECT o.request_digest,p.policy_json,p.policy_digest,p.status,c.status "
        "FROM broker_client_operations o JOIN broker_client_policies p "
        "ON p.client_id=o.client_id AND p.policy_id=o.policy_id AND p.version=o.policy_version "
        "JOIN broker_clients c ON c.client_id=o.client_id WHERE o.operation_id=?",
        (intent.operation_id,),
    ).fetchone()
    if client is None or client[1] != "active" or policy_claim is None:
        raise PresentationError("technical_requester_identity_unavailable")
    from .identity import ClientIdentity, ClientPolicy
    identity = ClientIdentity.from_json(client[0])
    policy = ClientPolicy.from_json(policy_claim[1])
    if (identity.client_id != intent.client_id or identity.principal_id != intent.principal_id
            or _timestamp(identity.expires_at, "client_identity_expiry") <= current
            or not set(policy.required_scopes) <= set(identity.scopes)
            # broker_client_operations binds the original intent digest, not the later
            # request envelope digest. The request is independently tied to the intent above.
            or policy_claim[0] != intent.intent_digest or policy_claim[2] != policy.policy_digest
            or policy_claim[3] != "active" or policy_claim[4] != "active"):
        raise PresentationError("technical_requester_identity_mismatch")
    client_credential = db.execute(
        "SELECT 1 FROM broker_client_credentials WHERE client_id=? AND status='active' AND expires_at>? LIMIT 1",
        (intent.client_id, current.isoformat(timespec="microseconds").replace("+00:00", "Z")),
    ).fetchone()
    if client_credential is None:
        raise PresentationError("technical_client_credential_expired_or_revoked")
    scopes = policy.required_scopes
    accepted = []
    snapshot_digests = {}
    expected_snapshot_fields = {
        "specification": (intent.specification_snapshot_id, intent.specification_revision_id,
                          intent.specification_digest),
        "plan": (intent.plan_snapshot_id, intent.plan_revision_id, intent.plan_digest),
        "checkpoint": (intent.checkpoint_snapshot_id, None, intent.checkpoint_digest),
        "payload": (intent.payload_snapshot_id, None, intent.payload_digest),
    }
    for kind in _SNAPSHOT_ORDER:
        snapshot_id, expected_revision, expected_digest = expected_snapshot_fields[kind]
        snap = db.execute(
            "SELECT b.snapshot_id,b.kind,b.project_id,b.revision_id,b.schema_version,b.content_json,"
            "b.canonical_digest,b.provenance_json,b.parent_digest,s.status,s.actor_id,s.changed_at "
            "FROM broker_snapshots b JOIN snapshot_state s USING(snapshot_id) WHERE b.snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if snap is None or snap[1] != kind or snap[9] != "accepted":
            raise PresentationError("accepted_snapshot_missing_or_divergent")
        if snap[2] != intent.project_id or snap[6] != expected_digest:
            raise PresentationError("accepted_snapshot_project_or_digest_mismatch")
        if expected_revision is not None and snap[3] != expected_revision:
            raise PresentationError("accepted_snapshot_revision_mismatch")
        snapshot = AcceptedSnapshot(
            PROTOCOL_VERSION, snap[0], snap[1], snap[2], snap[3], snap[4], snap[5],
            snap[6], snap[7], snap[10], snap[11], snap[8],
        )
        accepted.append(PresentedSnapshot(
            snapshot.kind, snapshot.snapshot_id, snapshot.project_id, snapshot.revision_id,
            snapshot.schema_version, snapshot.canonical_digest, snapshot.content_json,
            snapshot.accepted_by, snapshot.accepted_at, snapshot.parent_digest,
        ))
        snapshot_digests[kind] = snapshot.canonical_digest
    payload_content = parse_canonical_json(accepted[-1].content_json)
    impact_json, effects = _validate_context(payload_content, intent, scopes)
    if current >= min(_timestamp(request.expires_at, "request expiry"),
                      _timestamp(intent.expires_at, "intent expiry")):
        raise PresentationError("operation_expired")
    budget = parse_canonical_json(intent.budget_json)
    parameters = parse_canonical_json(intent.parameters_json)
    expires_at = min(_timestamp(request.expires_at, "request expiry"),
                     _timestamp(intent.expires_at, "intent expiry"))
    expires_text = expires_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
    values = dict(
        protocol_version=PROTOCOL_VERSION, presentation_version=PRESENTATION_VERSION,
        request_id=request.request_id, request_digest=request.request_digest,
        operation_id=intent.operation_id, intent_digest=intent.intent_digest,
        action_code=intent.action_code, requester_principal=intent.principal_id,
        technical_client_id=intent.client_id, project_id=intent.project_id,
        task_id=intent.task_id, attempt_id=intent.attempt_id, scopes=scopes,
        destination_id=intent.target_id, provider_id=intent.provider_id, model_id=intent.model_id,
        specification_revision_id=intent.specification_revision_id,
        specification_digest=snapshot_digests["specification"],
        plan_revision_id=intent.plan_revision_id, plan_digest=snapshot_digests["plan"],
        checkpoint_digest=snapshot_digests["checkpoint"], payload_digest=snapshot_digests["payload"],
        policy_digest=intent.policy_digest, catalog_digest=intent.catalog_digest,
        budget_json=canonical_json(budget), parameters_json=canonical_json(parameters),
        impact_json=impact_json, expected_effect_codes=effects,
        accepted_snapshots=tuple(accepted), expires_at=expires_text,
    )
    candidate = dict(values)
    candidate["presentation_digest"] = _digest_contract(candidate, "presentation_digest")
    return ConsentPresentation(**candidate)


def build_presentation_readonly(database_path: str | Path, request_id: str,
                                *, now: datetime | None = None) -> ConsentPresentation:
    """Read and validate a Broker DB without migration, lock-sidecar, or write access."""
    try:
        path = Path(database_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise PresentationError("broker_database_not_found") from exc
    if any(part.casefold() == ".agentflow" for part in path.parts):
        raise PresentationError("broker_database_must_not_be_project_operational_state")
    if not path.is_file():
        raise PresentationError("broker_database_not_found")
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        from .persistence import APPLICATION_ID, SCHEMA_VERSION, BrokerIntegrityError, BrokerStore
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise PresentationError("database_is_not_approval_broker_store")
        if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise PresentationError("broker_schema_version_unsupported")
        checker = object.__new__(BrokerStore)
        checker.db = db
        checker.verify_integrity()
        presentation = build_presentation_from_connection(db, request_id, now=now)
        db.rollback()
        return presentation
    except (sqlite3.DatabaseError, ProtocolError, BrokerIntegrityError, OSError) as exc:
        raise PresentationError("broker_database_invalid_or_unreadable") from exc
    finally:
        if "db" in locals():
            db.close()


_ACTION_LABELS = {
    "provider_call": "Chamada explícita a provider",
    "provider_model_change": "Alteração explícita de provider/modelo",
    "model_escalation": "Escalonamento explícito de modelo",
}
_EFFECT_LABELS = {
    "provider_request": "Solicitar geração ao provider e modelo indicados.",
    "provider_selection_change": "Alterar a seleção para o provider/modelo indicados.",
    "model_escalation": "Escalonar para o modelo indicado.",
    "payload_transmission": "Transmitir ao provider o payload do snapshot aceito.",
    "possible_workspace_changes": "A saída pode resultar em mudanças na tarefa, limitadas pelo escopo e checkpoint exibidos.",
}
_SNAPSHOT_LABELS = {
    "specification": "Especificação aceita", "plan": "Plano aceito",
    "checkpoint": "Checkpoint aceito", "payload": "Payload aceito",
}


def render_consent_preview(presentation: ConsentPresentation) -> str:
    """Render static HTML with complete values escaped as text; no scripts or actions."""
    if not isinstance(presentation, ConsentPresentation):
        raise PresentationError("typed_presentation_required")
    e = lambda value: html.escape(str(value), quote=True)
    rows = [
        ("ID da operação", presentation.operation_id),
        ("ID da solicitação", presentation.request_id),
        ("Ação", _ACTION_LABELS[presentation.action_code]),
        ("Projeto / tarefa / tentativa", f"{presentation.project_id} / {presentation.task_id} / {presentation.attempt_id}"),
        ("Identidade técnica solicitante", f"{presentation.requester_principal} (client {presentation.technical_client_id})"),
        ("Escopo", ", ".join(presentation.scopes)),
        ("Destino", presentation.destination_id),
        ("Provider / modelo", f"{presentation.provider_id} / {presentation.model_id}"),
        ("Revisão da especificação", f"{presentation.specification_revision_id} — {presentation.specification_digest}"),
        ("Revisão do plano", f"{presentation.plan_revision_id} — {presentation.plan_digest}"),
        ("Checkpoint", presentation.checkpoint_digest),
        ("Digest do payload", presentation.payload_digest),
        ("Policy / catálogo", f"{presentation.policy_digest} / {presentation.catalog_digest}"),
        ("Expira em", presentation.expires_at),
    ]
    facts = "\n".join(f"<dt>{e(label)}</dt><dd><code>{e(value)}</code></dd>" for label, value in rows)
    effect_items = "\n".join(f"<li>{e(_EFFECT_LABELS[code])} <code>{e(code)}</code></li>"
                              for code in presentation.expected_effect_codes)
    impact = parse_canonical_json(presentation.impact_json)
    impact_markup = (
        "<dl>"
        f"<dt>Tipo de impacto</dt><dd><code>{e(impact['kind'])}</code></dd>"
        f"<dt>Requisitos afetados</dt><dd><code>{e(', '.join(impact['affected_requirement_ids']))}</code></dd>"
        f"<dt>Tarefas afetadas</dt><dd><code>{e(', '.join(impact['affected_task_ids']))}</code></dd>"
        f"<dt>Digest de impacto</dt><dd><code>{e(impact['impact_digest'])}</code></dd></dl>"
    )
    snapshots = []
    for snapshot in presentation.accepted_snapshots:
        snapshots.append(
            f"<section><h3>{e(_SNAPSHOT_LABELS[snapshot.kind])}</h3>"
            f"<dl><dt>Snapshot ID</dt><dd><code>{e(snapshot.snapshot_id)}</code></dd>"
            f"<dt>Revisão / schema</dt><dd><code>{e(snapshot.revision_id)} / {e(snapshot.schema_version)}</code></dd>"
            f"<dt>Digest completo</dt><dd><code>{e(snapshot.canonical_digest)}</code></dd>"
            f"<dt>Aceito por / em</dt><dd><code>{e(snapshot.accepted_by)} / {e(snapshot.accepted_at)}</code></dd></dl>"
            f"<details open><summary>Conteúdo canônico integral (sem truncamento)</summary>"
            f"<pre>{e(snapshot.content_json)}</pre></details></section>"
        )
    document = (
        "<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Pré-visualização de consentimento</title>"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        "<style>"
        "body{font:16px system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#18212b}"
        ".warning{background:#fff1c2;border:2px solid #a56a00;padding:1rem;font-weight:700}"
        "dl{display:grid;grid-template-columns:minmax(11rem,1fr) 3fr;gap:.5rem}dd{margin:0;overflow-wrap:anywhere}"
        "section{border-top:1px solid #bbb;margin-top:1.5rem;padding-top:1rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;"
        "max-height:none;border:1px solid #ccc;padding:1rem;background:#f6f7f8}code{overflow-wrap:anywhere}"
        "</style></head><body><header><p class=\"warning\">AMBIENTE DE DEMONSTRAÇÃO — NÃO HOMOLOGADO. "
        "Esta tela não concede autorização e não executa a operação.</p>"
        "<h1>Operação proposta</h1><p>Dados reconstruídos pelo Broker a partir de identidade, intent e snapshots aceitos. "
        "Conteúdo externo é mostrado como texto literal.</p></header>"
        f"<section><h2>Identidade e operação</h2><dl>{facts}</dl></section>"
        f"<section><h2>Impacto estruturado</h2>{impact_markup}<h3>Efeitos esperados</h3><ul>{effect_items}</ul>"
        "<p>Este preview não inicia o provider, não altera workspace/Git e não aprova a operação.</p></section>"
        f"<section><h2>Orçamento e limites</h2><pre>{e(presentation.budget_json)}</pre>"
        f"<h3>Parâmetros da operação</h3><pre>{e(presentation.parameters_json)}</pre></section>"
        f"<section><h2>Snapshots aceitos</h2>{''.join(snapshots)}</section>"
        f"<footer><p>Digest integral desta apresentação: <code>{e(presentation.presentation_digest)}</code></p>"
        "<p>Não há botão, link, formulário ou ação de autorização nesta demonstração.</p></footer></body></html>"
    )
    if len(document.encode("utf-8")) > MAX_PRESENTATION_HTML_BYTES:
        raise PresentationError("rendered_presentation_exceeds_size_limit")
    return document


def render_readonly_preview(database_path: str | Path, request_id: str) -> str:
    return render_consent_preview(build_presentation_readonly(database_path, request_id))
