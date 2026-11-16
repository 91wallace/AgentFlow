"""CLI composition for reviewing one authoritative, persisted APL handoff."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import getpass
import hashlib
import json
import math
import os
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, TextIO
import sys

from .authorization import (
    Ed25519LocalGrantVerifier,
    LocalAuthorizationAuthority,
    LocalAuthorizationOutcome,
    LocalAuthorizationPolicy,
    ResolvedLocalProposal,
    UnavailableLocalConsumerAuthenticator,
)
from .authoritative_consent import AuthoritativeConsentError, AuthoritativeConsentResolver
from .consent_ui import ConsentReviewError, LocalConsentReviewService, TerminalConsentReview
from .local_authentication import SQLiteLocalAuthenticationChallengeStore
from .local_passphrase import (
    LocalPassphraseGrantSigner,
    LocalPassphraseProofProvider,
    LocalPassphraseSigningSession,
    PassphraseLocalOperatorAuthenticator,
    SQLiteLocalOperatorCredentialStore,
    current_local_operator_principal,
)
from .profile import (ConsentLimits, ExecutionPlanConsentBinding,
                      LocalConsentRequest, LocalExecutionConsentRequest, SecurityProfile)


class LocalConsentConfigurationError(ValueError):
    """Local review policy is missing or ambiguous."""


@dataclass(frozen=True)
class LocalConsentRuntimeConfiguration:
    broker_database: Path
    project_id: str
    authorized_project_ids: tuple[str, ...]
    proposer_principal: str
    workspace_root: Path
    limits: ConsentLimits
    authorized_consumer_principals: tuple[str, ...]
    consent_ttl_seconds: int
    max_grant_ttl_seconds: int


def load_local_consent_configuration(path: Path, *, project_root: Path,
                                     state_database: Path) -> LocalConsentRuntimeConfiguration:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise LocalConsentConfigurationError("local_consent_configuration_unavailable") from exc
    required = {
        "schema_version", "broker_database", "project_id", "authorized_project_ids",
        "proposer_principal", "workspace_root", "max_jobs", "max_iterations",
        "max_timeout_seconds", "max_estimated_budget", "budget_currency",
        "authorized_consumer_principals", "consent_ttl_seconds", "max_grant_ttl_seconds",
    }
    if (type(raw) is not dict or set(raw) != required
            or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1):
        raise LocalConsentConfigurationError("local_consent_configuration_fields_invalid")
    string_fields = ("broker_database", "project_id", "proposer_principal",
                     "workspace_root", "max_estimated_budget", "budget_currency")
    if any(type(raw[name]) is not str or not raw[name] for name in string_fields):
        raise LocalConsentConfigurationError("local_consent_configuration_value_invalid")
    array_fields = ("authorized_project_ids", "authorized_consumer_principals")
    if any(type(raw[name]) is not list or not raw[name]
           or any(type(item) is not str or not item for item in raw[name])
           or len(set(raw[name])) != len(raw[name]) for name in array_fields):
        raise LocalConsentConfigurationError("local_consent_configuration_scope_invalid")
    integer_fields = ("max_jobs", "max_iterations", "max_timeout_seconds",
                      "consent_ttl_seconds", "max_grant_ttl_seconds")
    if any(type(raw[name]) is not int for name in integer_fields):
        raise LocalConsentConfigurationError("local_consent_configuration_limits_invalid")
    try:
        budget = Decimal(raw["max_estimated_budget"])
    except InvalidOperation as exc:
        raise LocalConsentConfigurationError("local_consent_budget_invalid") from exc
    if (not budget.is_finite() or budget < 0
            or not re.fullmatch(r"[A-Z]{3}", raw["budget_currency"])):
        raise LocalConsentConfigurationError("local_consent_budget_invalid")
    if raw["project_id"] not in raw["authorized_project_ids"]:
        raise LocalConsentConfigurationError("local_consent_project_not_allowlisted")
    try:
        broker = Path(raw["broker_database"])
        if not broker.is_absolute():
            broker = project_root / broker
        broker = broker.resolve(strict=True)
        workspace = Path(raw["workspace_root"])
        if not workspace.is_absolute():
            workspace = project_root / workspace
        workspace = workspace.resolve(strict=True)
        state_real = state_database.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LocalConsentConfigurationError("local_consent_store_or_workspace_unavailable") from exc
    if not broker.is_file() or not workspace.is_dir() or broker == state_real:
        raise LocalConsentConfigurationError("local_consent_store_or_workspace_invalid")
    try:
        limits = ConsentLimits(raw["max_jobs"], raw["max_iterations"],
                               raw["max_timeout_seconds"], budget, raw["budget_currency"])
    except ValueError as exc:
        raise LocalConsentConfigurationError("local_consent_limits_invalid") from exc
    if (not 1 <= raw["consent_ttl_seconds"] <= 120
            or not 1 <= raw["max_grant_ttl_seconds"] <= 300):
        raise LocalConsentConfigurationError("local_consent_ttl_invalid")
    return LocalConsentRuntimeConfiguration(
        broker, raw["project_id"], tuple(raw["authorized_project_ids"]),
        raw["proposer_principal"], workspace, limits,
        tuple(raw["authorized_consumer_principals"]),
        raw["consent_ttl_seconds"], raw["max_grant_ttl_seconds"],
    )


def prepare_execution_plan_consent_request(authoritative, *, executor_id: str,
                                           executor_version: str, execution_limits,
                                           task_limits_by_job: dict,
                                           financial_budget_policy,
                                           strategies_by_job: dict | None = None):
    """Build a one-time request from persisted plan/workspace facts and local policy."""
    from agentflow.apl.authorized_execution import (
        ExecutionLimits, FinancialBudgetMode, FinancialBudgetPolicy,
        TaskExecutionLimits,
    )
    if (not authoritative.target_jobs
            or not isinstance(execution_limits, ExecutionLimits)
            or not isinstance(financial_budget_policy, FinancialBudgetPolicy)):
        raise ConsentReviewError("authoritative_executable_plan_required")
    if type(getattr(authoritative, "objective", None)) is not str or not authoritative.objective.strip():
        raise ConsentReviewError("authoritative_objective_required")
    base = authoritative.request
    if (execution_limits.max_jobs > base.limits.max_jobs
            or execution_limits.max_iterations > base.limits.max_iterations
            or execution_limits.max_seconds > base.limits.timeout_seconds
            or execution_limits.max_concurrency != 1):
        raise ConsentReviewError("execution_limits_exceed_authoritative_policy")
    if financial_budget_policy.mode is FinancialBudgetMode.ESTIMATED_COST_ACCEPTED:
        estimate = Decimal(str(financial_budget_policy.estimated_amount))
        currency = financial_budget_policy.currency
        if estimate > base.limits.estimated_budget or currency != base.limits.budget_currency:
            raise ConsentReviewError("execution_budget_exceeds_authoritative_policy")
    else:
        estimate, currency = Decimal("0"), base.limits.budget_currency
    job_ids = {job.job_id for job in authoritative.target_jobs}
    if (len(job_ids) > execution_limits.max_jobs
            or set(task_limits_by_job) != job_ids or any(
            not isinstance(value, TaskExecutionLimits)
            or value.max_attempts > execution_limits.max_attempts_per_job
            or value.max_seconds > execution_limits.max_seconds
            or value.max_estimated_budget > execution_limits.max_estimated_budget
            for value in task_limits_by_job.values())):
        raise ConsentReviewError("execution_task_quotas_do_not_cover_plan")
    strategies_by_job = strategies_by_job or {job_id: ("default",) for job_id in job_ids}
    if set(strategies_by_job) != job_ids:
        raise ConsentReviewError("execution_strategies_do_not_cover_plan")
    limits = ConsentLimits(
        execution_limits.max_jobs, execution_limits.max_iterations,
        math.ceil(execution_limits.max_seconds), estimate, currency)
    binding = ExecutionPlanConsentBinding(
        cycle_id=authoritative.cycle_id,
        plan_revision_id=authoritative.plan_revision_id,
        workspace_path=authoritative.workspace_path,
        workspace_identity_digest=authoritative.workspace_identity_digest,
        plan_content_digest=authoritative.plan_content_digest,
        objective_digest="sha256:" + hashlib.sha256(
            authoritative.objective.encode("utf-8")).hexdigest(),
        authorized_scope=authoritative.authorized_scope,
        task_graph=tuple((job.job_id, tuple(job.depends_on), tuple(job.required_scope))
                         for job in authoritative.target_jobs),
        executor_id=executor_id, executor_version=executor_version,
        max_attempts_per_job=execution_limits.max_attempts_per_job,
        max_duration_seconds=float(execution_limits.max_seconds),
        max_concurrency=execution_limits.max_concurrency,
        financial_budget_mode=financial_budget_policy.mode.value,
        strategies_by_job=tuple(sorted((key, tuple(value))
                                       for key, value in strategies_by_job.items())),
        task_limits_by_job=tuple(sorted((key, value.max_attempts, float(value.max_seconds),
                                         float(value.max_estimated_budget))
                                        for key, value in task_limits_by_job.items())),
    )
    return LocalExecutionConsentRequest(
        profile=base.profile, proposal_id=base.proposal_id,
        proposal_digest=base.proposal_digest, revision=base.revision,
        workspace_id=base.workspace_id,
        allowed_operations=tuple(sorted(authoritative.operations)),
        limits=limits, expires_at=base.expires_at,
        operator_identity=base.operator_identity, execution_binding=binding,
    )


def make_execution_binding_resolver(authoritative_resolver, *, cycle_id: str,
                                    executor_id: str, executor_version: str,
                                    execution_limits, task_limits_by_job: dict,
                                    financial_budget_policy,
                                    strategies_by_job: dict | None = None):
    """Authority callback that recomputes the binding from current persisted facts."""
    def resolve(proposal_id: str):
        facts = authoritative_resolver.resolve(cycle_id, proposal_id)
        request = prepare_execution_plan_consent_request(
            facts, executor_id=executor_id, executor_version=executor_version,
            execution_limits=execution_limits,
            task_limits_by_job=task_limits_by_job,
            financial_budget_policy=financial_budget_policy,
            strategies_by_job=strategies_by_job)
        return request.execution_binding
    return resolve


def _accepted_execution_context(*, cycle_store, broker_store, config, cycle_id: str,
                                executor_id: str, executor_version: str,
                                execution_limits, task_limits_by_job: dict,
                                financial_budget_policy, strategies_by_job=None,
                                now: datetime | None = None):
    from .authoritative_consent import AuthoritativeConsentResolver
    instant = now or datetime.now(timezone.utc)
    snapshot = cycle_store.load(cycle_id)
    if snapshot.state.value != "READY" or not snapshot.jobs:
        raise ConsentReviewError("accepted_ready_plan_required")
    authoritative = AuthoritativeConsentResolver(
        apl_store=cycle_store, broker_store=broker_store,
        project_id=config.project_id, proposer_principal=config.proposer_principal,
        authorized_project_ids=config.authorized_project_ids,
        operator_identity=current_local_operator_principal(), limits=config.limits,
        workspace_root=config.workspace_root,
        consent_ttl_seconds=config.consent_ttl_seconds)
    workspace_id = authoritative.register_workspace(cycle_id)
    path, identity_digest, _workspace_id = authoritative._validate_workspace_binding(
        cycle_id, snapshot.authorized_scope)
    with cycle_store._connect(readonly=True) as db:
        revision = db.execute("SELECT sequence,plan_json,plan_digest FROM apl_plan_revisions "
                              "WHERE cycle_id=? AND revision_id=?",
                              (cycle_id, snapshot.plan_revision_id)).fetchone()
    if revision is None or json.loads(revision[1]) != json.loads(
            json.dumps([asdict(job) for job in snapshot.jobs])):
        raise ConsentReviewError("accepted_plan_revision_store_mismatch")
    if not snapshot.plan_digest:
        raise ConsentReviewError("accepted_plan_digest_missing")
    if type(snapshot.objective) is not str or not snapshot.objective.strip():
        raise ConsentReviewError("accepted_plan_objective_missing")
    plan_digest = snapshot.plan_digest if snapshot.plan_digest.startswith("sha256:") \
        else "sha256:" + snapshot.plan_digest
    plan_id = "execution-plan-" + hashlib.sha256(
        f"{cycle_id}:{revision[0]}:{plan_digest}".encode("utf-8")).hexdigest()[:32]
    request_expiry = min(
        instant + timedelta(seconds=config.consent_ttl_seconds),
        instant + timedelta(seconds=config.max_grant_ttl_seconds))
    operations = tuple(sorted("job:" + job.job_id for job in snapshot.jobs))
    base = LocalConsentRequest(
        SecurityProfile.LOCAL_SUPERVISED, plan_id, plan_digest, revision[0], workspace_id,
        operations, config.limits,
        request_expiry.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        current_local_operator_principal())
    facts = SimpleNamespace(
        cycle_id=cycle_id, plan_revision_id=snapshot.plan_revision_id,
        plan_content_digest=(revision[2] if revision[2].startswith("sha256:")
                             else "sha256:" + revision[2]),
        workspace_path=path, workspace_identity_digest=identity_digest,
        authorized_scope=snapshot.authorized_scope, target_jobs=snapshot.jobs,
        operations=operations, request=base, approvable_for_review=True,
        objective=snapshot.objective,
        task_summaries=tuple(
            f"{job.job_id}: {job.instruction} [depends={','.join(job.depends_on)}; "
            f"scope={','.join(job.required_scope)}; criteria={','.join(job.acceptance_criteria)}]"
            for job in snapshot.jobs),
        broker_status="accepted APL plan")
    request = prepare_execution_plan_consent_request(
        facts, executor_id=executor_id, executor_version=executor_version,
        execution_limits=execution_limits, task_limits_by_job=task_limits_by_job,
        financial_budget_policy=financial_budget_policy,
        strategies_by_job=strategies_by_job)
    return SimpleNamespace(**{**vars(facts), "request": request})


def review_accepted_execution_plan(*, cycle_store, state_database: Path, broker_store,
                                   config: LocalConsentRuntimeConfiguration,
                                   cycle_id: str, executor_id: str,
                                   executor_version: str, max_attempts_per_job: int = 1,
                                   financial_budget_mode: str = "estimated_cost_accepted",
                                   input_stream: TextIO | None = None,
                                   output_stream: TextIO | None = None,
                                   hidden_prompt: Callable[[str], str] | None = None,
                                   confirm_prompt: Callable[[str], str] | None = None,
                                   now: datetime | None = None):
    """Review and authenticate one grant for the exact accepted full APL plan."""
    if not input_stream and not sys.stdin.isatty() or not output_stream and not sys.stdout.isatty():
        raise ConsentReviewError("execution_consent_requires_interactive_terminal")
    try:
        if (Path(cycle_store.path).resolve(strict=True) != state_database.resolve(strict=True)
                or Path(broker_store.path).resolve(strict=True)
                != config.broker_database.resolve(strict=True)):
            raise ConsentReviewError("execution_consent_store_path_mismatch")
    except (OSError, RuntimeError, AttributeError) as exc:
        raise ConsentReviewError("execution_consent_store_unavailable") from exc
    if type(executor_id) is not str or not executor_id or type(executor_version) is not str or not executor_version:
        raise ConsentReviewError("execution_consent_executor_required")
    if type(max_attempts_per_job) is not int or max_attempts_per_job < 1:
        raise ConsentReviewError("execution_consent_attempt_limit_invalid")
    from agentflow.apl.authorized_execution import (
        ExecutionLimits, FinancialBudgetMode, FinancialBudgetPolicy, TaskExecutionLimits,
    )
    try:
        budget_mode = FinancialBudgetMode(financial_budget_mode)
    except ValueError as exc:
        raise ConsentReviewError("execution_consent_financial_policy_invalid") from exc
    estimate = float(config.limits.estimated_budget)
    budget_policy = (FinancialBudgetPolicy(FinancialBudgetMode.HARD_CAP_REQUIRED)
                     if budget_mode is FinancialBudgetMode.HARD_CAP_REQUIRED else
                     FinancialBudgetPolicy(FinancialBudgetMode.ESTIMATED_COST_ACCEPTED,
                                           estimate, config.limits.budget_currency))
    execution_limits = ExecutionLimits(
        config.limits.max_jobs, max_attempts_per_job, config.limits.timeout_seconds,
        estimate if budget_mode is FinancialBudgetMode.ESTIMATED_COST_ACCEPTED else 0,
        config.limits.max_iterations, 1)
    principal = current_local_operator_principal()
    credentials = SQLiteLocalOperatorCredentialStore(cycle_store)
    active = credentials.active(principal)
    if active is None:
        raise ConsentReviewError("local_operator_credential_not_enrolled")

    context_instant = now or datetime.now(timezone.utc)

    def context_for_current_plan(plan_id: str):
        current = _accepted_execution_context(
            cycle_store=cycle_store, broker_store=broker_store, config=config,
            cycle_id=cycle_id, executor_id=executor_id, executor_version=executor_version,
            execution_limits=execution_limits,
            task_limits_by_job={job.job_id: TaskExecutionLimits(
                max_attempts_per_job, config.limits.timeout_seconds,
                estimate if budget_mode is FinancialBudgetMode.ESTIMATED_COST_ACCEPTED else 0)
                for job in cycle_store.load(cycle_id).jobs},
            financial_budget_policy=budget_policy, now=context_instant)
        return current if current.request.proposal_id == plan_id else None

    facts = context_for_current_plan(_accepted_execution_context(
        cycle_store=cycle_store, broker_store=broker_store, config=config,
        cycle_id=cycle_id, executor_id=executor_id, executor_version=executor_version,
        execution_limits=execution_limits,
        task_limits_by_job={job.job_id: TaskExecutionLimits(
            max_attempts_per_job, config.limits.timeout_seconds,
            estimate if budget_mode is FinancialBudgetMode.ESTIMATED_COST_ACCEPTED else 0)
            for job in cycle_store.load(cycle_id).jobs},
        financial_budget_policy=budget_policy, now=context_instant).request.proposal_id)
    if facts is None:
        raise ConsentReviewError("accepted_plan_binding_unavailable")
    request = facts.request
    key_id = "local-operator-" + active[0].removeprefix("local-credential-")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    verifier = Ed25519LocalGrantVerifier({
        key_id: Ed25519PublicKey.from_public_bytes(active[2]),
    })
    session = LocalPassphraseSigningSession()
    signer = LocalPassphraseGrantSigner(key_id, active[0], session)
    challenge_store = SQLiteLocalAuthenticationChallengeStore(cycle_store)

    class CurrentPlanProposalResolver:
        def resolve(self, proposal_id):
            current = context_for_current_plan(proposal_id)
            if current is None:
                return None
            return ResolvedLocalProposal(
                proposal_id, current.request.proposal_digest, current.request.revision,
                current.request.workspace_id, current.request.allowed_operations,
                config.proposer_principal)

    class CurrentBindingResolver:
        def __call__(self, proposal_id):
            current = context_for_current_plan(proposal_id)
            return current.request.execution_binding if current else None

    policy = LocalAuthorizationPolicy(
        SecurityProfile.LOCAL_SUPERVISED, (principal,),
        config.authorized_consumer_principals, (config.proposer_principal,),
        config.limits.max_jobs, config.limits.max_iterations,
        config.limits.timeout_seconds, config.max_grant_ttl_seconds,
        config.limits.estimated_budget, config.limits.budget_currency)
    authority = LocalAuthorizationAuthority(
        store=broker_store, policy=policy,
        proposal_resolver=CurrentPlanProposalResolver(),
        operator_authenticator=PassphraseLocalOperatorAuthenticator(credentials, principal),
        consumer_authenticator=UnavailableLocalConsumerAuthenticator(),
        signer=signer, verifier=verifier,
        authentication_challenge_store=challenge_store,
        execution_binding_resolver=CurrentBindingResolver())
    proof_provider = LocalPassphraseProofProvider(
        credentials, principal, prompt=hidden_prompt or getpass.getpass,
        confirm=confirm_prompt or input,
        clock=(lambda: now) if now is not None else (lambda: datetime.now(timezone.utc)),
        signing_session=session)
    def resolve_authoritative_context(candidate):
        current = context_for_current_plan(candidate.proposal_id)
        if current is None:
            raise ConsentReviewError("accepted_plan_binding_unavailable")
        if current.request != candidate:
            raise ConsentReviewError("authoritative_request_binding_mismatch")
        return current

    ui = TerminalConsentReview(
        input_stream=input_stream or sys.stdin, output_stream=output_stream or sys.stdout,
        authoritative_context_resolver=resolve_authoritative_context,
        account_label=str(principal), clock=(lambda: now) if now is not None else None)
    return LocalConsentReviewService(ui, authority, proof_provider, challenge_store).review(
        request, now=now)


def review_persisted_handoff(*, cycle_store, state_database: Path, broker_store,
                             config: LocalConsentRuntimeConfiguration,
                             cycle_id: str, proposal_id: str,
                             execution_authorization: dict | None = None,
                             input_stream: TextIO | None = None,
                             output_stream: TextIO | None = None,
                             hidden_prompt: Callable[[str], str] | None = None,
                             confirm_prompt: Callable[[str], str] | None = None,
                             now: datetime | None = None):
    """Resolve APL/Broker records, review explicitly, authenticate and persist a decision."""
    if execution_authorization is not None:
        raise ConsentReviewError("execution_grant_requires_accepted_plan_authority")
    if not cycle_id or not proposal_id:
        raise ConsentReviewError("persisted_cycle_and_proposal_required")
    try:
        if (Path(cycle_store.path).resolve(strict=True) != state_database.resolve(strict=True)
                or Path(broker_store.path).resolve(strict=True)
                != config.broker_database.resolve(strict=True)):
            raise ConsentReviewError("consent_store_path_configuration_mismatch")
    except (AttributeError, OSError, RuntimeError) as exc:
        raise ConsentReviewError("consent_store_path_unavailable") from exc
    principal = current_local_operator_principal()
    credentials = SQLiteLocalOperatorCredentialStore(cycle_store)
    active = credentials.active(principal)

    resolver = AuthoritativeConsentResolver(
        apl_store=cycle_store, broker_store=broker_store,
        project_id=config.project_id, proposer_principal=config.proposer_principal,
        authorized_project_ids=config.authorized_project_ids,
        operator_identity=principal, limits=config.limits,
        workspace_root=config.workspace_root,
        consent_ttl_seconds=config.consent_ttl_seconds,
    )
    # Resolve in historical mode first so an expired consent window does not
    # hide a previously authenticated, durable decision. This mode never makes
    # an expired request approvable; only an existing matching audit record can
    # be returned from it.
    resolved = resolver.resolve(cycle_id, proposal_id, now=now,
                                 include_expired_for_history=True)
    if execution_authorization is not None:
        required = {"executor_id", "executor_version", "execution_limits",
                    "task_limits_by_job", "financial_budget_policy", "strategies_by_job"}
        if type(execution_authorization) is not dict or set(execution_authorization) != required:
            raise ConsentReviewError("execution_authorization_configuration_invalid")
        execution_request = prepare_execution_plan_consent_request(
            resolved, **execution_authorization)
        resolved = replace(resolved, request=execution_request)
    prior = broker_store.get_local_consent_by_request(resolved.request.request_digest)
    if prior is not None:
        record, grant_id = prior
        if (record.request_digest != resolved.request.request_digest
                or record.decision.value in {"approve", "reject"}
                and (not record.explicit_action
                     or record.operator_identity != resolved.request.operator_identity
                     or record.operator_proof_digest is None)):
            raise ConsentReviewError("persisted_consent_decision_binding_invalid")
        request_expiry = datetime.fromisoformat(
            resolved.request.expires_at.replace("Z", "+00:00"))
        decision_time = datetime.fromisoformat(record.occurred_at.replace("Z", "+00:00"))
        if (record.decision.value in {"approve", "reject"}
                and decision_time >= request_expiry):
            raise ConsentReviewError("persisted_consent_decision_was_late")
        with broker_store._mutex:
            decision_count = broker_store.db.execute(
                "SELECT COUNT(*) FROM local_consent_decisions WHERE request_digest=?",
                (resolved.request.request_digest,),
            ).fetchone()[0]
            grants_for_request = tuple(row[0] for row in broker_store.db.execute(
                "SELECT grant_id FROM local_authorization_grants WHERE request_digest=?",
                (resolved.request.request_digest,),
            ).fetchall())
        if decision_count != 1:
            raise ConsentReviewError("persisted_consent_decision_not_unique")
        if grant_id is None:
            if grants_for_request:
                raise ConsentReviewError("persisted_decision_has_unmatched_grant")
            if record.decision.value == "approve":
                raise ConsentReviewError("approved_decision_missing_grant")
            state = "rejected" if record.decision.value == "reject" else "blocked"
            return LocalAuthorizationOutcome(record.decision, record.decision_id,
                                             None, None, state, "persisted_decision_recovered")
        grant, state = broker_store.get_local_grant(grant_id)
        if (len(grants_for_request) != 1 or grants_for_request[0] != grant_id
                or record.decision.value != "approve"
                or grant.consent_decision_id != record.decision_id
                or grant.request_digest != resolved.request.request_digest
                or grant.proposal_id != proposal_id
                or grant.plan_digest != resolved.request.proposal_digest
                or grant.revision != resolved.request.revision
                or grant.workspace_id != resolved.request.workspace_id
                or grant.allowed_operations != resolved.request.allowed_operations
                or grant.limits != resolved.request.limits
                or grant.operator_identity != resolved.request.operator_identity
                or grant.issued_at != record.occurred_at
                or datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
                > request_expiry):
            raise ConsentReviewError("persisted_grant_binding_invalid")
        credential_id = "local-credential-" + grant.key_id.removeprefix("local-operator-")
        public_record = credentials.public_key_record(credential_id)
        if public_record is None or public_record[0] != record.operator_identity:
            raise ConsentReviewError("persisted_grant_verifier_unavailable")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        verifier = Ed25519LocalGrantVerifier({
            grant.key_id: Ed25519PublicKey.from_public_bytes(public_record[1]),
        })
        if not grant.verify_signature(verifier):
            raise ConsentReviewError("persisted_grant_signature_invalid")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise ConsentReviewError("aware_clock_required")
        grant_expiry = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
        if state == "approved" and grant_expiry <= instant.astimezone(timezone.utc):
            # Report effective expiry without mutating historical decision/grant rows.
            state = "expired"
        return LocalAuthorizationOutcome(record.decision, record.decision_id,
                                         grant.grant_id, grant.grant_digest,
                                         state, "persisted_decision_recovered")

    if not resolved.consent_window_open:
        raise AuthoritativeConsentError("proposal_expired")
    if not resolved.approvable_for_review:
        raise AuthoritativeConsentError("proposal_not_pending_or_not_current")
    if active is None:
        raise ConsentReviewError("local_operator_credential_not_enrolled")
    key_id = "local-operator-" + active[0].removeprefix("local-credential-")

    class PersistedProposalResolver:
        def resolve(self, candidate_id: str):
            if candidate_id != proposal_id:
                return None
            current = resolver.resolve(cycle_id, proposal_id, now=now)
            if not current.approvable_for_review:
                return None
            if execution_authorization is not None:
                expected = prepare_execution_plan_consent_request(
                    current, **execution_authorization)
                if expected.execution_binding != resolved.request.execution_binding:
                    return None
                current_request = expected
            else:
                current_request = current.request
            return ResolvedLocalProposal(
                current.apl_proposal_id, current_request.proposal_digest,
                current_request.revision, current.workspace_id,
                current_request.allowed_operations, config.proposer_principal,
            )

    non_operator = tuple(sorted(set((config.proposer_principal,)
                                    + config.authorized_consumer_principals)))
    policy = LocalAuthorizationPolicy(
        SecurityProfile.LOCAL_SUPERVISED, (principal,),
        config.authorized_consumer_principals, non_operator,
        config.limits.max_jobs, config.limits.max_iterations,
        config.limits.timeout_seconds, config.max_grant_ttl_seconds,
        config.limits.estimated_budget, config.limits.budget_currency,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    verifier = Ed25519LocalGrantVerifier({
        key_id: Ed25519PublicKey.from_public_bytes(active[2]),
    })
    session = LocalPassphraseSigningSession()
    signer = LocalPassphraseGrantSigner(key_id, active[0], session)
    challenge_store = SQLiteLocalAuthenticationChallengeStore(cycle_store)
    authority = LocalAuthorizationAuthority(
        store=broker_store, policy=policy, proposal_resolver=PersistedProposalResolver(),
        operator_authenticator=PassphraseLocalOperatorAuthenticator(credentials, principal),
        consumer_authenticator=UnavailableLocalConsumerAuthenticator(),
        signer=signer, verifier=verifier,
        authentication_challenge_store=challenge_store,
        execution_binding_resolver=(make_execution_binding_resolver(
            resolver, cycle_id=cycle_id, **execution_authorization)
            if execution_authorization is not None else None),
    )
    proof_provider = LocalPassphraseProofProvider(
        credentials, principal, prompt=hidden_prompt or getpass.getpass,
        confirm=confirm_prompt or input,
        clock=(lambda: now) if now is not None else (lambda: datetime.now(timezone.utc)),
        signing_session=session,
    )
    def resolve_context(request):
        if execution_authorization is None:
            return resolver.resolve_for_request(request, now=now)
        current = resolver.resolve(cycle_id, proposal_id, now=now)
        expected = prepare_execution_plan_consent_request(
            current, **execution_authorization)
        if expected != request:
            raise AuthoritativeConsentError("execution_consent_request_not_current")
        return replace(current, request=request)

    ui = TerminalConsentReview(
        input_stream=input_stream or sys.stdin, output_stream=output_stream or sys.stdout,
        authoritative_context_resolver=resolve_context,
        account_label=str(principal),
        clock=(lambda: now) if now is not None else (lambda: datetime.now(timezone.utc)),
    )
    return LocalConsentReviewService(ui, authority, proof_provider, challenge_store).review(
        resolved.request, now=now)
