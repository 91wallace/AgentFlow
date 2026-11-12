from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO

import pytest

from agentflow.apl import (APLState, BrokerHandoffService, CycleSnapshot, JobProposal,
                           PlanProposal, SQLiteCycleStore, SupervisedCycleService, TaskChange)
from agentflow.approval.persistence import BrokerStore
from agentflow.approval.protocol import SnapshotProposal, parse_canonical_json
from agentflow.approval.transport import BrokerEnvelope
from agentflow.state.execution import ExecutionStore
from agentflow.security import (AuthoritativeConsentError, AuthoritativeConsentResolver,
                                ConsentLimits, ConsentReviewError, TerminalConsentReview,
                                LocalConsentRuntimeConfiguration, SQLiteLocalOperatorCredentialStore,
                                current_local_operator_principal, review_persisted_handoff)


class AddTaskPlanner:
    def propose(self, context):
        return PlanProposal("planner-id", context.plan_revision_id, context.cycle_revision,
                            "Add synthetic task", (TaskChange("add", JobProposal(
                                "second", "create second file", (), ("src/second.py",),
                                ("test:second",))),), "criteria pass")


class NoNetworkClient:
    def request(self, _envelope):
        raise AssertionError("resolution must use stores, not transport")


class OwnerCancelClient:
    def __init__(self, broker, principal="apl-local"):
        self.broker = broker
        self.principal = principal

    def request(self, envelope):
        body = parse_canonical_json(envelope.body_json)
        self.broker.cancel_pending_proposal(body["snapshot_id"], principal_id=self.principal,
                                            operation_id=envelope.operation_id)
        record = self.broker.get_proposal_lifecycle_for_identity(
            body["snapshot_id"], principal_id=self.principal,
            authorized_project_ids=("project-local",))
        response = {"action": "snapshot_lifecycle", "operation_id": envelope.operation_id,
                    "protocol_version": 1,
                    "snapshot": {"protocol_version": 1,
                                 "snapshot_id": record.proposal.snapshot_id,
                                 "revision_id": record.proposal.revision_id,
                                 "digest": record.proposal.canonical_digest,
                                 "status": record.state, "principal_id": self.principal,
                                 "journal_sequence": record.journal_sequence,
                                 "authority_status": "unavailable", "dispatch_allowed": False}}
        return BrokerEnvelope.create(message_type="response", operation_id=envelope.operation_id,
                                     nonce="0" * 64, body=response)


def setup_records(tmp_path, *, operator_identity="configured-operator"):
    state_path = tmp_path / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    apl_store = SQLiteCycleStore(state_path)
    apl = SupervisedCycleService(apl_store, state_path)
    cycle = apl_store.create(CycleSnapshot(
        "cycle-authoritative", "Implementar segunda tarefa sintética",
        ("src/first.py", "src/second.py"), APLState.READY,
        (JobProposal("first", "preserve first", (), ("src/first.py",), ("test:first",)),),
        (), 0, 0))
    proposal, status, _ = apl.replan(cycle.cycle_id, AddTaskPlanner(), "request-authoritative")
    assert status == "proposed"
    handoff = BrokerHandoffService(apl_store, state_path, NoNetworkClient(),
                                   project_id="project-local", principal_id="apl-local")
    binding = handoff._prepare(cycle.cycle_id, proposal.proposal_id)
    broker = BrokerStore(tmp_path / "broker.sqlite3")
    broker.propose_snapshot(SnapshotProposal.from_json(binding[2]))
    resolver = AuthoritativeConsentResolver(
        apl_store=apl_store, broker_store=broker, project_id="project-local",
        proposer_principal="apl-local", authorized_project_ids=("project-local",),
        operator_identity=operator_identity,
        limits=ConsentLimits(3, 2, 120, Decimal("2.00"), "USD"),
        workspace_root=tmp_path / "workspace", consent_ttl_seconds=90)
    resolver.workspace_root.mkdir()
    resolver.register_workspace(cycle.cycle_id)
    return state_path, broker, apl_store, cycle, proposal, resolver


def test_persisted_handoff_review_authenticates_and_recovers_without_dispatch(tmp_path):
    from io import StringIO

    now = datetime.now(timezone.utc)
    principal = current_local_operator_principal()
    state_path, broker, apl_store, cycle, proposal, resolver = setup_records(
        tmp_path, operator_identity=principal)
    try:
        credentials = SQLiteLocalOperatorCredentialStore(apl_store)
        credentials._enroll(principal, "synthetic test passphrase", "synthetic test passphrase", now=now)
        configured = LocalConsentRuntimeConfiguration(
            tmp_path / "broker.sqlite3", "project-local", ("project-local",), "apl-local",
            tmp_path / "workspace", ConsentLimits(3, 2, 120, Decimal("2.00"), "USD"),
            ("executor-local",), 90, 90)
        persisted = resolver.resolve(cycle.cycle_id, proposal.proposal_id, now=now)
        digest_suffix = persisted.request.proposal_digest[7:19]
        outcome = review_persisted_handoff(
            cycle_store=apl_store, state_database=state_path, broker_store=broker,
            config=configured, cycle_id=cycle.cycle_id, proposal_id=proposal.proposal_id,
            input_stream=StringIO("approve\n"), output_stream=StringIO(),
            confirm_prompt=lambda _prompt: "APROVAR " + digest_suffix,
            hidden_prompt=lambda _prompt: "synthetic test passphrase", now=now)
        assert outcome.decision.value == "approve" and outcome.state == "approved"
        assert outcome.grant_id is not None
        record = broker.get_local_consent_by_request(persisted.request.request_digest)
        assert record is not None and record[0].operator_identity == principal
        grant, grant_state = broker.get_local_grant(outcome.grant_id)
        assert grant_state == "approved"
        from agentflow.security.authorization import Ed25519LocalGrantVerifier
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        credential = credentials.active(principal)
        verifier = Ed25519LocalGrantVerifier({
            "local-operator-" + credential[0].removeprefix("local-credential-"):
                Ed25519PublicKey.from_public_bytes(credential[2])})
        assert grant.verify_signature(verifier)
        assert broker.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        assert broker.db.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 0
        with apl_store._connect(readonly=True) as db:
            assert db.execute("SELECT event_type FROM apl_local_auth_challenge_events "
                              "ORDER BY event_id DESC LIMIT 1").fetchone()[0] == "consumed"
        recovered_outcome = review_persisted_handoff(
            cycle_store=apl_store, state_database=state_path, broker_store=broker,
            config=configured, cycle_id=cycle.cycle_id, proposal_id=proposal.proposal_id,
            input_stream=StringIO(""), output_stream=StringIO(),
            confirm_prompt=lambda _prompt: pytest.fail("recovered decisions must not prompt again"),
            hidden_prompt=lambda _prompt: pytest.fail("recovered decisions must not authenticate again"),
            now=now)
        assert recovered_outcome.state == "approved"
        assert recovered_outcome.consent_decision_id == outcome.consent_decision_id
        broker.close()
        broker = BrokerStore(tmp_path / "broker.sqlite3")
        recovered = broker.get_local_consent_by_request(persisted.request.request_digest)
        assert recovered is not None and recovered[0].decision.value == "approve"
        recovered_grant, recovered_state = broker.get_local_grant(outcome.grant_id)
        assert recovered_state == "approved" and recovered_grant.grant_digest == grant.grant_digest
    finally:
        broker.close()


def test_persisted_handoff_review_cancel_and_unknown_proposal_fail_closed(tmp_path):
    from io import StringIO

    now = datetime.now(timezone.utc)
    principal = current_local_operator_principal()
    state_path, broker, apl_store, cycle, proposal, resolver = setup_records(
        tmp_path, operator_identity=principal)
    try:
        credentials = SQLiteLocalOperatorCredentialStore(apl_store)
        credentials._enroll(principal, "synthetic test passphrase", "synthetic test passphrase", now=now)
        configured = LocalConsentRuntimeConfiguration(
            tmp_path / "broker.sqlite3", "project-local", ("project-local",), "apl-local",
            tmp_path / "workspace", ConsentLimits(3, 2, 120, Decimal("2.00"), "USD"),
            ("executor-local",), 90, 90)
        with pytest.raises(AuthoritativeConsentError):
            review_persisted_handoff(
                cycle_store=apl_store, state_database=state_path, broker_store=broker,
                config=configured, cycle_id=cycle.cycle_id, proposal_id="missing-proposal",
                input_stream=StringIO("approve\n"), output_stream=StringIO(), now=now)
        outcome = review_persisted_handoff(
            cycle_store=apl_store, state_database=state_path, broker_store=broker,
            config=configured, cycle_id=cycle.cycle_id, proposal_id=proposal.proposal_id,
            input_stream=StringIO("cancel\n"), output_stream=StringIO(), now=now)
        assert outcome.state == "blocked"
        assert outcome.grant_id is None
        assert broker.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        persisted = resolver.resolve(cycle.cycle_id, proposal.proposal_id, now=now)
        decision = broker.get_local_consent_by_request(persisted.request.request_digest)
        assert decision is not None and decision[1] is None
    finally:
        broker.close()


def test_valid_resolution_drives_terminal_review_from_persisted_values(tmp_path):
    now = datetime.now(timezone.utc)
    _state_path, broker, apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        resolved = resolver.resolve(cycle.cycle_id, proposal.proposal_id, now=now)
        assert resolved.approvable_for_review
        assert resolved.objective == cycle.objective
        assert resolved.authorized_scope == cycle.authorized_scope
        assert resolved.plan_digest == "sha256:" + apl_store.proposals(cycle.cycle_id)[0][1]
        assert resolved.operations == ("job:first", "job:second")
        assert resolved.request.operator_identity == "configured-operator"
        assert resolved.workspace_path == str((tmp_path / "workspace").resolve())
        assert resolved.workspace_identity_digest.startswith("sha256:")
        terminal = TerminalConsentReview(
            input_stream=StringIO("approve\n"), output_stream=StringIO(),
            authoritative_context_resolver=resolver.resolve_for_request,
            account_label="account-display-only", clock=lambda: now)
        receipt = terminal.request_consent(resolved.request)
        assert receipt.request_digest == resolved.request.request_digest
        display = terminal._output.getvalue()
        for expected in (cycle.objective, "src/first.py", "src/second.py", "create second file",
                         "pending", resolved.plan_digest, "configured-operator",
                         resolved.workspace_path):
            assert expected in display
        assert broker.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        assert broker.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
    finally:
        broker.close()


def test_handoff_digest_mismatch_is_rejected(tmp_path):
    _state_path, broker, apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        with apl_store._connect() as db:
            db.execute("DROP TRIGGER apl_broker_handoffs_no_update")
            db.execute("UPDATE apl_broker_handoffs SET digest=? WHERE cycle_id=? AND apl_proposal_id=?",
                       ("sha256:" + "0" * 64, cycle.cycle_id, proposal.proposal_id))
        with pytest.raises(AuthoritativeConsentError, match="binding_mismatch"):
            resolver.resolve(cycle.cycle_id, proposal.proposal_id)
    finally:
        broker.close()


def test_terminal_broker_proposal_is_never_presented_as_approvable(tmp_path):
    _state_path, broker, apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        with apl_store._connect(readonly=True) as db:
            snapshot_id = db.execute(
                "SELECT snapshot_id FROM apl_broker_handoffs WHERE cycle_id=? AND apl_proposal_id=?",
                (cycle.cycle_id, proposal.proposal_id)).fetchone()[0]
        broker._transition_snapshot(snapshot_id, "cancelled", "apl-local")
        resolved = resolver.resolve(cycle.cycle_id, proposal.proposal_id)
        assert resolved.broker_status == "cancelled"
        assert not resolved.approvable_for_review
        terminal = TerminalConsentReview(
            input_stream=StringIO("approve\n"), output_stream=StringIO(),
            authoritative_context_resolver=resolver.resolve_for_request,
            account_label="local", clock=lambda: datetime.now(timezone.utc))
        with pytest.raises(ConsentReviewError, match="proposal_not_approvable"):
            terminal.request_consent(resolved.request)
    finally:
        broker.close()


def test_expired_proposal_is_inspectable_but_not_approvable(tmp_path):
    _state_path, broker, _apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        resolved = resolver.resolve(cycle.cycle_id, proposal.proposal_id,
                                    now=datetime.now(timezone.utc) + __import__("datetime").timedelta(days=8))
        assert resolved.broker_status == "expired"
        assert not resolved.approvable_for_review
    finally:
        broker.close()


def test_first_review_starts_one_time_consent_window_for_pending_proposal(tmp_path):
    _state_path, broker, _apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        reviewed_at = datetime.now(timezone.utc) + timedelta(minutes=3)
        first = resolver.resolve(cycle.cycle_id, proposal.proposal_id, now=reviewed_at)
        assert first.broker_status == "pending"
        assert first.expires_at == (reviewed_at + timedelta(seconds=90)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")

        reopened = resolver.resolve(
            cycle.cycle_id, proposal.proposal_id, now=reviewed_at + timedelta(seconds=30))
        assert reopened.request.request_digest == first.request.request_digest
        assert reopened.expires_at == first.expires_at

        with pytest.raises(AuthoritativeConsentError, match="proposal_expired"):
            resolver.resolve(cycle.cycle_id, proposal.proposal_id,
                            now=reviewed_at + timedelta(seconds=91))
    finally:
        broker.close()


def test_handoff_review_recovers_historical_decision_after_window_and_grant_expiry(tmp_path):
    now = datetime.now(timezone.utc)
    principal = current_local_operator_principal()
    state_path, broker, apl_store, cycle, proposal, resolver = setup_records(
        tmp_path, operator_identity=principal)
    try:
        credentials = SQLiteLocalOperatorCredentialStore(apl_store)
        credentials._enroll(principal, "synthetic test passphrase", "synthetic test passphrase", now=now)
        config = LocalConsentRuntimeConfiguration(
            tmp_path / "broker.sqlite3", "project-local", ("project-local",), "apl-local",
            tmp_path / "workspace", ConsentLimits(3, 2, 120, Decimal("2.00"), "USD"),
            ("executor-local",), 90, 90)
        request = resolver.resolve(cycle.cycle_id, proposal.proposal_id, now=now).request
        digest_suffix = request.proposal_digest[7:19]
        approved = review_persisted_handoff(
            cycle_store=apl_store, state_database=state_path, broker_store=broker,
            config=config, cycle_id=cycle.cycle_id, proposal_id=proposal.proposal_id,
            input_stream=StringIO("approve\n"), output_stream=StringIO(),
            confirm_prompt=lambda _prompt: "APROVAR " + digest_suffix,
            hidden_prompt=lambda _prompt: "synthetic test passphrase", now=now)
        assert approved.state == "approved" and approved.grant_id
        grant, persisted_state = broker.get_local_grant(approved.grant_id)
        assert persisted_state == "approved"
        historical_time = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00")) + timedelta(seconds=1)
        decisions_before = broker.db.execute(
            "SELECT COUNT(*) FROM local_consent_decisions WHERE request_digest=?",
            (request.request_digest,)).fetchone()[0]
        grants_before = broker.db.execute(
            "SELECT COUNT(*) FROM local_authorization_grants WHERE request_digest=?",
            (request.request_digest,)).fetchone()[0]
        with apl_store._connect(readonly=True) as db:
            challenge_events_before = db.execute(
                "SELECT COUNT(*) FROM apl_local_auth_challenge_events").fetchone()[0]

        recovered = review_persisted_handoff(
            cycle_store=apl_store, state_database=state_path, broker_store=broker,
            config=config, cycle_id=cycle.cycle_id, proposal_id=proposal.proposal_id,
            input_stream=StringIO(""), output_stream=StringIO(),
            confirm_prompt=lambda _prompt: pytest.fail("historical read must not reconfirm"),
            hidden_prompt=lambda _prompt: pytest.fail("historical read must not authenticate again"),
            now=historical_time)

        assert recovered.decision.value == "approve"
        assert recovered.consent_decision_id == approved.consent_decision_id
        assert recovered.grant_id == approved.grant_id
        assert recovered.state == "expired"
        assert recovered.reason == "persisted_decision_recovered"
        assert broker.db.execute(
            "SELECT COUNT(*) FROM local_consent_decisions WHERE request_digest=?",
            (request.request_digest,)).fetchone()[0] == decisions_before == 1
        assert broker.db.execute(
            "SELECT COUNT(*) FROM local_authorization_grants WHERE request_digest=?",
            (request.request_digest,)).fetchone()[0] == grants_before == 1
        assert broker.get_local_grant(approved.grant_id)[1] == "approved"
        with apl_store._connect(readonly=True) as db:
            assert db.execute("SELECT COUNT(*) FROM apl_local_auth_challenge_events").fetchone()[0] == challenge_events_before
        assert broker.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        assert broker.db.execute("SELECT COUNT(*) FROM execution_receipts").fetchone()[0] == 0
    finally:
        broker.close()


def test_expired_proposal_without_decision_cannot_be_approved(tmp_path):
    state_path, broker, apl_store, cycle, proposal, _resolver = setup_records(tmp_path)
    try:
        future = datetime.now(timezone.utc) + timedelta(days=8)
        config = LocalConsentRuntimeConfiguration(
            tmp_path / "broker.sqlite3", "project-local", ("project-local",), "apl-local",
            tmp_path / "workspace", ConsentLimits(3, 2, 120, Decimal("2.00"), "USD"),
            ("executor-local",), 90, 90)
        with pytest.raises(AuthoritativeConsentError, match="proposal_expired"):
            review_persisted_handoff(
                cycle_store=apl_store, state_database=state_path, broker_store=broker,
                config=config, cycle_id=cycle.cycle_id, proposal_id=proposal.proposal_id,
                input_stream=StringIO("approve\n"), output_stream=StringIO(), now=future)
        assert broker.db.execute("SELECT COUNT(*) FROM local_consent_decisions").fetchone()[0] == 0
        assert broker.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        assert broker.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
    finally:
        broker.close()


def test_missing_handoff_or_inaccessible_sqlite_fails_closed(tmp_path):
    _state_path, broker, apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        with apl_store._connect() as db:
            db.execute("DROP TRIGGER apl_broker_handoffs_no_delete")
            db.execute("DELETE FROM apl_broker_handoffs WHERE cycle_id=? AND apl_proposal_id=?",
                       (cycle.cycle_id, proposal.proposal_id))
        with pytest.raises(AuthoritativeConsentError, match="handoff_not_found"):
            resolver.resolve(cycle.cycle_id, proposal.proposal_id)
        with pytest.raises(AuthoritativeConsentError, match="failed"):
            resolver.resolve("missing-cycle", "missing-proposal")
        broker.close()
        with pytest.raises(AuthoritativeConsentError, match="failed"):
            resolver.resolve(cycle.cycle_id, proposal.proposal_id)
    finally:
        broker.close()


def test_restart_and_concurrent_resolutions_are_stable(tmp_path):
    state_path, broker, _apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: resolver.resolve(cycle.cycle_id, proposal.proposal_id), range(8)))
        assert len({item.request.request_digest for item in results}) == 1
        broker.close()
        reopened = BrokerStore(tmp_path / "broker.sqlite3")
        try:
            apl_reopened = SQLiteCycleStore(state_path, initialize=False)
            recovered_resolver = AuthoritativeConsentResolver(
                apl_store=apl_reopened, broker_store=reopened, project_id="project-local",
                proposer_principal="apl-local", authorized_project_ids=("project-local",),
                operator_identity="configured-operator",
                limits=ConsentLimits(3, 2, 120, Decimal("2.00"), "USD"),
                workspace_root=tmp_path / "workspace", consent_ttl_seconds=90)
            recovered_resolver.register_workspace(cycle.cycle_id)
            recovered = recovered_resolver.resolve(cycle.cycle_id, proposal.proposal_id)
            assert recovered.request.request_digest == results[0].request.request_digest
            with apl_reopened._connect(readonly=True) as db:
                pinned = db.execute(
                    "SELECT canonical_path,filesystem_identity,scope_digest FROM apl_consent_workspace_bindings "
                    "WHERE cycle_id=?", (cycle.cycle_id,)).fetchone()
                request_binding = db.execute(
                    "SELECT proposal_digest,revision,request_digest,operations_json,limits_json "
                    "FROM apl_consent_request_bindings WHERE proposal_id=?",
                    (proposal.proposal_id,)).fetchone()
                assert pinned[0] == str((tmp_path / "workspace").resolve())
                assert pinned[1] and pinned[2].startswith("sha256:")
                assert request_binding[0] == recovered.plan_digest
                assert request_binding[1] == recovered.plan_revision
                assert request_binding[2] == recovered.request.request_digest
            assert reopened.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
            assert reopened.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
        finally:
            reopened.close()
    finally:
        try:
            broker.close()
        except Exception:
            pass


@pytest.mark.parametrize("tamper", ["revision", "workspace"])
def test_broker_revision_or_workspace_divergence_is_rejected(tmp_path, tamper):
    _state_path, broker, apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        with apl_store._connect(readonly=True) as db:
            snapshot_id = db.execute(
                "SELECT snapshot_id FROM apl_broker_handoffs WHERE cycle_id=? AND apl_proposal_id=?",
                (cycle.cycle_id, proposal.proposal_id)).fetchone()[0]
        with broker.transaction() as db:
            db.execute("DROP TRIGGER broker_snapshots_no_update")
            if tamper == "revision":
                db.execute("UPDATE broker_snapshots SET revision_id='tampered-revision' WHERE snapshot_id=?",
                           (snapshot_id,))
            else:
                row = db.execute("SELECT content_json FROM broker_snapshots WHERE snapshot_id=?",
                                 (snapshot_id,)).fetchone()
                from agentflow.approval.protocol import canonical_json, digest_json
                import json
                content = json.loads(row[0])
                content["authorized_scope"] = ["unrelated/path"]
                encoded = canonical_json(content)
                db.execute("UPDATE broker_snapshots SET content_json=?,canonical_digest=? WHERE snapshot_id=?",
                           (encoded, digest_json(content), snapshot_id))
        with pytest.raises(AuthoritativeConsentError):
            resolver.resolve(cycle.cycle_id, proposal.proposal_id)
    finally:
        broker.close()


def test_owner_cancellation_uses_broker_contract_and_blocks_apl_cycle(tmp_path):
    state_path, broker, apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        handoff = BrokerHandoffService(apl_store, state_path,
                                       OwnerCancelClient(broker),
                                       project_id="project-local", principal_id="apl-local")
        first = handoff.cancel(cycle.cycle_id, proposal.proposal_id)
        second = handoff.cancel(cycle.cycle_id, proposal.proposal_id)
        assert first == second
        assert first["status"] == "cancelled"
        assert first["dispatch_allowed"] is False
        assert apl_store.load(cycle.cycle_id).state is APLState.BLOCKED
        resolved = resolver.resolve(cycle.cycle_id, proposal.proposal_id)
        assert resolved.broker_status == "cancelled" and not resolved.approvable_for_review
        assert broker.db.execute("SELECT COUNT(*) FROM local_authorization_grants").fetchone()[0] == 0
        assert broker.db.execute("SELECT COUNT(*) FROM execution_authorizations").fetchone()[0] == 0
    finally:
        broker.close()


def test_workspace_path_change_or_directory_replacement_is_rejected(tmp_path):
    _state_path, broker, _apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        resolved = resolver.resolve(cycle.cycle_id, proposal.proposal_id)
        assert resolved.workspace_identity_digest
        root = tmp_path / "workspace"
        old = tmp_path / "workspace-renamed"
        root.rename(old)
        root.mkdir()
        with pytest.raises(AuthoritativeConsentError, match="workspace_binding_changed"):
            resolver.resolve(cycle.cycle_id, proposal.proposal_id)
    finally:
        broker.close()


def test_changed_limits_conflict_with_persisted_consent_binding(tmp_path):
    _state_path, broker, apl_store, cycle, proposal, resolver = setup_records(tmp_path)
    try:
        first = resolver.resolve(cycle.cycle_id, proposal.proposal_id)
        changed = AuthoritativeConsentResolver(
            apl_store=apl_store, broker_store=broker, project_id="project-local",
            proposer_principal="apl-local", authorized_project_ids=("project-local",),
            operator_identity="configured-operator",
            limits=ConsentLimits(4, 2, 120, Decimal("2.00"), "USD"),
            workspace_root=tmp_path / "workspace", consent_ttl_seconds=90)
        changed.register_workspace(cycle.cycle_id)
        with pytest.raises(AuthoritativeConsentError, match="persisted_consent_binding_conflict"):
            changed.resolve(cycle.cycle_id, proposal.proposal_id)
        assert first.request.limits.max_jobs == 3
    finally:
        broker.close()
