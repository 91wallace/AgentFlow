from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from io import StringIO
import os
from threading import Lock

import pytest

from agentflow.apl.authorized_execution import (
    AuthorizedExecutionController, AuthorizedExecutionError,
    BrokerExecutionGrantResolver, ExecutionLimits, FinancialBudgetMode,
    FinancialBudgetPolicy, SQLiteAuthorizedExecutionStore, TaskExecutionLimits,
)
from agentflow.apl.codex_execution import PinnedWorkspace
from agentflow.apl.contracts import APLState, CycleSnapshot, ExecutionOutcome, JobProposal
from agentflow.apl.persistence import SQLiteCycleStore
from agentflow.approval.persistence import BrokerStore
from agentflow.security.authorization import (
    AuthenticatedConsumer, AuthenticatedOperator, Ed25519LocalGrantSigner,
    Ed25519LocalGrantVerifier, LocalAuthorizationAuthority,
    LocalAuthorizationPolicy, ResolvedLocalProposal,
)
from agentflow.security.local_authentication import SQLiteLocalAuthenticationChallengeStore
from agentflow.security.profile import (
    ConsentDecision, ConsentLimits, ExecutionPlanConsentBinding,
    LocalConsentReceipt, LocalExecutionConsentRequest, SecurityProfile,
)
from agentflow.state.execution import ExecutionStore


NOW = datetime.now(timezone.utc)


class ProposalResolver:
    def __init__(self, request):
        self.proposal = ResolvedLocalProposal(
            request.proposal_id, request.proposal_digest, request.revision,
            request.workspace_id, request.allowed_operations, "apl")

    def resolve(self, proposal_id):
        return self.proposal if proposal_id == self.proposal.proposal_id else None


class OperatorAuth:
    def authenticate(self, _nonce, proof, *, now):
        if proof != "owner-proof":
            return None
        return AuthenticatedOperator("operator-local", "sha256:" + "b" * 64,
                                     "synthetic-test-auth", now.isoformat())


class ConsumerAuth:
    def authenticate_consumer(self, *_args, **_kwargs):
        return AuthenticatedConsumer("executor-local", "sha256:" + "b" * 64,
                                     NOW.isoformat())


class VerifierResolver:
    def __init__(self, key_id, verifier):
        self.key_id, self.verifier = key_id, verifier

    def resolve(self, key_id, operator_identity):
        return self.verifier if key_id == self.key_id and operator_identity == "operator-local" else None


class WorkspaceResolver:
    def __init__(self, pinned):
        self.pinned = pinned

    def resolve(self, cycle_id):
        return self.pinned


class SyntheticExecutor:
    def __init__(self):
        self.calls = []
        self.lock = Lock()

    def execute(self, cycle_id, job, scope, operation_id):
        with self.lock:
            self.calls.append((job.job_id, operation_id))
        return ExecutionOutcome("succeeded", "synthetic task completed",
                                ({"task": job.job_id, "passed": True},),
                                progress_key=operation_id)


class EvidenceValidator:
    def validate(self, _cycle_id, _job, outcome):
        return bool(outcome.evidence) and all(item.get("passed") is True
                                              for item in outcome.evidence)


def _setup(tmp_path, *, executor_version="synthetic-v1", workspace_override=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    cycles = SQLiteCycleStore(state_path)
    jobs = (
        JobProposal("first", "create synthetic record", (), ("workspace",), ("record exists",)),
        JobProposal("second", "verify synthetic record", ("first",), ("workspace",),
                    ("record is valid",)),
    )
    snapshot = cycles.create(CycleSnapshot(
        "cycle-plan-grant", "validate a synthetic two-step plan", ("workspace",),
        APLState.READY, jobs, (), 0, 0, plan_digest="sha256:" + "a" * 64,
        plan_revision_id="revision-initial"))
    workspace = tmp_path / "authorized-workspace"
    workspace.mkdir()
    info = workspace.stat()
    canonical = os.path.normcase(str(workspace.resolve()))
    identity_digest = "sha256:" + hashlib.sha256(
        f"{canonical}\0{info.st_dev}:{info.st_ino}".encode()).hexdigest()
    with cycles._connect(readonly=True) as db:
        plan_content_digest = db.execute(
            "SELECT plan_digest FROM apl_plan_revisions WHERE cycle_id=? AND revision_id=?",
            (snapshot.cycle_id, snapshot.plan_revision_id)).fetchone()[0]
    if not plan_content_digest.startswith("sha256:"):
        plan_content_digest = "sha256:" + plan_content_digest
    workspace_id = "workspace-synthetic"
    pinned = PinnedWorkspace(workspace_override or workspace, "project-synthetic",
                             workspace_id, identity_digest)
    binding = ExecutionPlanConsentBinding(
        cycle_id=snapshot.cycle_id, plan_revision_id=snapshot.plan_revision_id,
        plan_content_digest=plan_content_digest,
        objective_digest="sha256:" + hashlib.sha256(snapshot.objective.encode("utf-8")).hexdigest(),
        workspace_path=str(workspace.resolve()), workspace_identity_digest=identity_digest,
        authorized_scope=snapshot.authorized_scope,
        task_graph=tuple((job.job_id, job.depends_on, job.required_scope) for job in jobs),
        executor_id="synthetic", executor_version=executor_version,
        max_attempts_per_job=1, max_duration_seconds=30, max_concurrency=1,
        financial_budget_mode="estimated_cost_accepted",
        strategies_by_job=(("first", ("default",)), ("second", ("default",))),
        task_limits_by_job=(("first", 1, 30.0, 1.0), ("second", 1, 30.0, 1.0)),
    )
    limits = ConsentLimits(2, 2, 30, Decimal("1.00"), "USD")
    plan_proposal_id = "execution-plan-" + hashlib.sha256(
        f"{snapshot.cycle_id}:1:{snapshot.plan_digest}".encode("utf-8")
    ).hexdigest()[:32]
    request = LocalExecutionConsentRequest(
        SecurityProfile.LOCAL_SUPERVISED, plan_proposal_id, snapshot.plan_digest, 1,
        workspace_id, ("job:first", "job:second"), limits,
        (NOW + timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
        "operator-local", binding)
    broker = BrokerStore(tmp_path / "broker.sqlite3")
    challenge_store = SQLiteLocalAuthenticationChallengeStore(cycles)
    private_key = Ed25519PrivateKey.generate()
    key_id = "test-key-execution"
    signer = Ed25519LocalGrantSigner(key_id, private_key)
    verifier = Ed25519LocalGrantVerifier({key_id: private_key.public_key()})
    policy = LocalAuthorizationPolicy(
        SecurityProfile.LOCAL_SUPERVISED, ("operator-local",), ("executor-local",), ("apl",),
        5, 5, 60, 300, Decimal("5.00"), "USD")
    authority = LocalAuthorizationAuthority(
        store=broker, policy=policy, proposal_resolver=ProposalResolver(request),
        operator_authenticator=OperatorAuth(), consumer_authenticator=ConsumerAuth(),
        signer=signer, verifier=verifier, authentication_challenge_store=challenge_store,
        execution_binding_resolver=lambda proposal_id: binding
        if proposal_id == request.proposal_id else None)
    receipt = LocalConsentReceipt(
        request.request_digest, ConsentDecision.APPROVE, request.operator_identity, True,
        NOW.isoformat().replace("+00:00", "Z"), request.expires_at)
    challenge = challenge_store.issue(request, receipt, now=NOW)
    outcome = authority.authorize_execution_plan(
        request, receipt, "owner-proof", authentication_challenge=challenge, now=NOW)
    grant, grant_state = broker.get_local_grant(outcome.grant_id)
    assert grant_state == "approved" and outcome.state == "approved"
    workspace_resolver = WorkspaceResolver(pinned)
    grant_resolver = BrokerExecutionGrantResolver(
        broker_store=broker, cycle_store=cycles,
        verifier_resolver=VerifierResolver(key_id, verifier),
        workspace_resolver=workspace_resolver, executor_id="synthetic",
        executor_version=executor_version, clock=lambda: NOW)
    return state_path, cycles, broker, grant, grant_resolver, workspace_resolver, snapshot


def test_authenticated_full_plan_grant_runs_two_dependent_synthetic_tasks_once(tmp_path):
    path, cycles, broker, local_grant, resolver, _, snapshot = _setup(tmp_path)
    try:
        assert local_grant.schema_version == 2
        assert local_grant.execution_binding["task_graph"][1][1] == ["first"]
        assert broker.get_local_consent_by_request(local_grant.request_digest)[0].explicit_action
        runs = SQLiteAuthorizedExecutionStore(str(path))
        executor = SyntheticExecutor()
        controller = AuthorizedExecutionController(
            cycle_store=cycles, execution_store=runs, grant_resolver=resolver,
            executor=executor, evidence_validator=EvidenceValidator(),
            clock=lambda: datetime.now(timezone.utc).isoformat())
        result = controller.start(snapshot.cycle_id, local_grant.grant_id)
        assert result["status"] == "completed"
        assert [call[0] for call in executor.calls] == ["first", "second"]
        assert len({call[1] for call in executor.calls}) == 2
        assert len(broker.db.execute("SELECT decision_id FROM local_consent_decisions").fetchall()) == 1
        broker.verify_integrity()
    finally:
        broker.close()


def test_full_plan_grant_resolver_blocks_revoked_or_wrong_executor(tmp_path):
    _path, _cycles, broker, grant, resolver, _workspace, snapshot = _setup(tmp_path)
    try:
        wrong_executor = BrokerExecutionGrantResolver(
            broker_store=broker, cycle_store=_cycles,
            verifier_resolver=resolver.verifiers,
            workspace_resolver=resolver.workspaces, executor_id="codex",
            executor_version="0.166.0", clock=lambda: NOW)
        with pytest.raises(AuthorizedExecutionError, match="executor_binding_changed"):
            wrong_executor.resolve_and_validate(grant.grant_id, snapshot.cycle_id)
        broker._transition_local_grant(grant.grant_id, "revoked", "test_revocation")
        with pytest.raises(AuthorizedExecutionError, match="grant_revoked"):
            resolver.resolve_and_validate(grant.grant_id, snapshot.cycle_id)
    finally:
        broker.close()


def test_full_plan_grant_resolver_rejects_workspace_identity_change(tmp_path):
    other = tmp_path / "different-workspace"
    other.mkdir()
    _path, _cycles, broker, grant, resolver, _, snapshot = _setup(
        tmp_path, workspace_override=other)
    try:
        with pytest.raises(AuthorizedExecutionError, match="workspace_binding_changed"):
            resolver.resolve_and_validate(grant.grant_id, snapshot.cycle_id)
    finally:
        broker.close()


def test_full_plan_grant_resolver_rejects_expiry_and_changed_plan(tmp_path):
    _path, cycles, broker, grant, resolver, _, snapshot = _setup(tmp_path)
    try:
        expired = BrokerExecutionGrantResolver(
            broker_store=broker, cycle_store=cycles,
            verifier_resolver=resolver.verifiers,
            workspace_resolver=resolver.workspaces, executor_id="synthetic",
            executor_version="synthetic-v1", clock=lambda: NOW + timedelta(minutes=10))
        with pytest.raises(AuthorizedExecutionError, match="grant_expired"):
            expired.resolve_and_validate(grant.grant_id, snapshot.cycle_id)
        changed = replace(snapshot, plan_digest="sha256:" + "d" * 64)
        cycles.save(changed, expected_iterations=snapshot.iterations,
                    expected_revision=snapshot.revision)
        with pytest.raises(AuthorizedExecutionError, match="plan_binding_changed"):
            resolver.resolve_and_validate(grant.grant_id, snapshot.cycle_id)
    finally:
        broker.close()


def test_full_plan_grant_resolver_rejects_changed_persisted_objective(tmp_path):
    _path, cycles, broker, grant, resolver, _workspace, snapshot = _setup(tmp_path)
    try:
        cycles.save(replace(snapshot, objective="different persisted objective"),
                    expected_iterations=snapshot.iterations,
                    expected_revision=snapshot.revision)
        with pytest.raises(AuthorizedExecutionError, match="operational_plan_objective_changed"):
            resolver.resolve_and_validate(grant.grant_id, snapshot.cycle_id)
    finally:
        broker.close()


def test_execution_consent_request_rejects_missing_objective_before_authority():
    from types import SimpleNamespace
    from agentflow.apl.authorized_execution import (
        ExecutionLimits, FinancialBudgetMode, FinancialBudgetPolicy,
    )
    from agentflow.security.local_consent_review import (
        ConsentReviewError, prepare_execution_plan_consent_request,
    )

    facts = SimpleNamespace(objective="  ", target_jobs=(object(),))
    with pytest.raises(ConsentReviewError, match="authoritative_objective_required"):
        prepare_execution_plan_consent_request(
            facts, executor_id="synthetic", executor_version="v1",
            execution_limits=ExecutionLimits(1, 1, 10, 0, 1, 1),
            task_limits_by_job={},
            financial_budget_policy=FinancialBudgetPolicy(
                FinancialBudgetMode.ESTIMATED_COST_ACCEPTED, 1, "USD"))


def test_public_consent_api_issues_authenticated_full_plan_grant(tmp_path):
    from agentflow.security.local_consent_review import (
        LocalConsentRuntimeConfiguration, review_accepted_execution_plan,
    )
    from agentflow.security.local_passphrase import (
        SQLiteLocalOperatorCredentialStore, current_local_operator_principal,
    )

    class TTYStream(StringIO):
        def isatty(self):
            return True

    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    cycles = SQLiteCycleStore(state_path)
    jobs = (
        JobProposal("first", "create synthetic record", (), ("workspace",), ("exists",)),
        JobProposal("second", "verify synthetic record", ("first",), ("workspace",), ("valid",)),
    )
    snapshot = cycles.create(CycleSnapshot(
        "accepted-plan", "complete a synthetic two-step plan", ("workspace",),
        APLState.READY, jobs, (), 0, 0,
        plan_digest="sha256:" + "c" * 64))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broker = BrokerStore(tmp_path / "broker.sqlite3")
    now = datetime.now(timezone.utc)
    principal = current_local_operator_principal()
    try:
        credentials = SQLiteLocalOperatorCredentialStore(cycles)
        credentials._enroll(principal, "synthetic bridge passphrase",
                            "synthetic bridge passphrase", now=now)
        policy_limits = ConsentLimits(2, 2, 60, Decimal("2.00"), "USD")
        config = LocalConsentRuntimeConfiguration(
            tmp_path / "broker.sqlite3", "project", ("project",), "apl",
            workspace, policy_limits, ("executor-local",), 60, 120)
        output = StringIO()
        outcome = review_accepted_execution_plan(
            cycle_store=cycles, state_database=state_path, broker_store=broker,
            config=config, cycle_id=snapshot.cycle_id, executor_id="synthetic",
            executor_version="v1", input_stream=TTYStream("approve\n"),
            output_stream=output,
            confirm_prompt=lambda _prompt: "APROVAR " + snapshot.plan_digest[7:19],
            hidden_prompt=lambda _prompt: "synthetic bridge passphrase", now=now)
        assert outcome.state == "approved" and outcome.grant_id
        grant, state = broker.get_local_grant(outcome.grant_id)
        assert state == "approved" and grant.schema_version == 2
        assert grant.execution_binding["task_graph"][1][1] == ["first"]
        assert grant.allowed_operations == ("job:first", "job:second")
        assert "AUTORIZAÇÃO DE EXECUÇÃO DO PLANO COMPLETO" in output.getvalue()
        assert broker.db.execute(
            "SELECT COUNT(*) FROM local_consent_decisions WHERE request_digest=?",
            (grant.request_digest,)).fetchone()[0] == 1
        assert broker.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        credential = credentials.active(principal)
        verifier = Ed25519LocalGrantVerifier({
            grant.key_id: Ed25519PublicKey.from_public_bytes(credential[2])})

        class AuthenticatedOperatorVerifierResolver:
            def resolve(self, key_id, operator_identity):
                return verifier if key_id == grant.key_id and operator_identity == principal else None

        binding = grant.execution_binding
        pinned = PinnedWorkspace(workspace, "project", grant.workspace_id,
                                 binding["workspace_identity_digest"])
        grant_resolver = BrokerExecutionGrantResolver(
            broker_store=broker, cycle_store=cycles,
            verifier_resolver=AuthenticatedOperatorVerifierResolver(),
            workspace_resolver=WorkspaceResolver(pinned), executor_id="synthetic",
            executor_version="v1", clock=lambda: datetime.now(timezone.utc))
        executor = SyntheticExecutor()
        controller = AuthorizedExecutionController(
            cycle_store=cycles, execution_store=SQLiteAuthorizedExecutionStore(str(state_path)),
            grant_resolver=grant_resolver, executor=executor,
            evidence_validator=EvidenceValidator(),
            clock=lambda: datetime.now(timezone.utc).isoformat())
        run = controller.start(snapshot.cycle_id, grant.grant_id)
        assert run["status"] == "completed"
        assert [call[0] for call in executor.calls] == ["first", "second"]
        assert broker.db.execute(
            "SELECT COUNT(*) FROM local_consent_decisions WHERE request_digest=?",
            (grant.request_digest,)).fetchone()[0] == 1
        broker.verify_integrity()
    finally:
        broker.close()
