"""Resolve consent-review facts from persisted APL and Broker records."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import re
import sqlite3

from agentflow.approval.persistence import PENDING_PROPOSAL_TTL_SECONDS
from agentflow.approval.protocol import SnapshotProposal, canonical_json, digest_json
from agentflow.apl.replanning import validate_proposal
from agentflow.apl.supervised import SupervisedCycleService
from .profile import ConsentLimits, LocalConsentRequest, SecurityProfile


class AuthoritativeConsentError(RuntimeError):
    """Persisted consent facts are absent, inconsistent, stale, or inaccessible."""


@dataclass(frozen=True)
class AuthoritativeConsentProposal:
    cycle_id: str
    apl_proposal_id: str
    broker_snapshot_id: str
    broker_journal_sequence: int
    broker_status: str
    apl_status: str
    objective: str
    authorized_scope: tuple[str, ...]
    plan_digest: str
    plan_revision: int
    workspace_id: str
    workspace_path: str
    workspace_identity_digest: str
    operations: tuple[str, ...]
    task_summaries: tuple[str, ...]
    limits: ConsentLimits
    expires_at: str
    request: LocalConsentRequest
    consent_window_open: bool = True

    @property
    def approvable_for_review(self) -> bool:
        return (self.broker_status == "pending" and self.apl_status == "proposed"
                and self.consent_window_open)


class AuthoritativeConsentResolver:
    """Cross-check immutable APL proposal, handoff binding, Broker snapshot and journal.

    ``operator_identity`` and ``limits`` are local authority configuration; they
    must not come from the planner, request file, or CLI arguments. Resolution is
    read-only except that Broker's existing TTL sweeper may durably expire old
    pending snapshots before their state is returned.
    """

    def __init__(self, *, apl_store, broker_store, project_id: str,
                 proposer_principal: str, authorized_project_ids: tuple[str, ...],
                 operator_identity: str, limits: ConsentLimits,
                 workspace_root: str | Path,
                 consent_ttl_seconds: int = 120):
        if not authorized_project_ids or project_id not in authorized_project_ids:
            raise AuthoritativeConsentError("broker_project_scope_configuration_invalid")
        if type(consent_ttl_seconds) is not int or not 1 <= consent_ttl_seconds <= 120:
            raise AuthoritativeConsentError("consent_ttl_out_of_range")
        if not isinstance(limits, ConsentLimits):
            raise AuthoritativeConsentError("configured_consent_limits_required")
        self.apl_store = apl_store
        self.broker_store = broker_store
        self.project_id = project_id
        self.proposer_principal = proposer_principal
        self.authorized_project_ids = tuple(sorted(set(authorized_project_ids)))
        self.operator_identity = operator_identity
        self.limits = limits
        self.workspace_root = Path(workspace_root)
        self.consent_ttl_seconds = consent_ttl_seconds

    @staticmethod
    def _workspace_record(root: Path) -> tuple[str, str, str, str]:
        """Return canonical path, filesystem identity and identity digest.

        ``st_dev``/``st_ino`` map to the volume and file ID on supported Windows
        filesystems and to device/inode on Linux. Both are required; a path string
        or derived workspace ID alone is not accepted as location evidence.
        """
        try:
            canonical = root.resolve(strict=True)
            info = canonical.stat()
        except (OSError, RuntimeError) as exc:
            raise AuthoritativeConsentError("workspace_unavailable") from exc
        if not canonical.is_dir() or not info.st_dev or not info.st_ino:
            raise AuthoritativeConsentError("workspace_identity_unavailable")
        canonical_text = os.path.normcase(str(canonical))
        identity = f"{info.st_dev}:{info.st_ino}"
        digest = "sha256:" + hashlib.sha256(
            f"{canonical_text}\0{identity}".encode("utf-8")
        ).hexdigest()
        return str(canonical), identity, digest, canonical_text

    def _ensure_binding_schema(self, db) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS apl_consent_workspace_bindings(
            cycle_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, canonical_path TEXT NOT NULL,
            filesystem_identity TEXT NOT NULL, workspace_identity_digest TEXT NOT NULL,
            scope_digest TEXT NOT NULL, workspace_id TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS apl_consent_request_bindings(
            proposal_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, proposal_digest TEXT NOT NULL,
            revision INTEGER NOT NULL, workspace_identity_digest TEXT NOT NULL,
            request_digest TEXT NOT NULL, operations_json TEXT NOT NULL, limits_json TEXT NOT NULL,
            expires_at TEXT NOT NULL, operator_identity TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(cycle_id) REFERENCES apl_consent_workspace_bindings(cycle_id))""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_consent_workspace_no_update
            BEFORE UPDATE ON apl_consent_workspace_bindings BEGIN
            SELECT RAISE(ABORT,'consent workspace bindings are immutable'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_consent_workspace_no_delete
            BEFORE DELETE ON apl_consent_workspace_bindings BEGIN
            SELECT RAISE(ABORT,'consent workspace bindings are immutable'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_consent_request_no_update
            BEFORE UPDATE ON apl_consent_request_bindings BEGIN
            SELECT RAISE(ABORT,'consent request bindings are immutable'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS apl_consent_request_no_delete
            BEFORE DELETE ON apl_consent_request_bindings BEGIN
            SELECT RAISE(ABORT,'consent request bindings are immutable'); END""")

    @staticmethod
    def _validate_scope_paths(root: Path, scope: tuple[str, ...]) -> None:
        canonical_root = os.path.normcase(str(root.resolve(strict=True)))
        for item in scope:
            if type(item) is not str or not item or "\x00" in item:
                raise AuthoritativeConsentError("workspace_scope_path_invalid")
            normalized = item.replace("\\", "/")
            if (normalized.startswith("/") or ".." in PurePosixPath(normalized).parts
                    or PureWindowsPath(item).is_absolute()
                    or re.match(r"^[A-Za-z]:", item)
                    or ("/" in normalized and PureWindowsPath(item).drive)):
                raise AuthoritativeConsentError("workspace_scope_path_invalid")
            # Scope labels without separators can represent capabilities such as
            # ``test:unit``. Path-like entries must resolve beneath the pinned root.
            if "/" in normalized:
                candidate = (root / Path(*PurePosixPath(normalized).parts)).resolve(strict=False)
                try:
                    common = os.path.normcase(os.path.commonpath((canonical_root, str(candidate))))
                except ValueError as exc:
                    raise AuthoritativeConsentError("workspace_scope_path_invalid") from exc
                if common != canonical_root:
                    raise AuthoritativeConsentError("workspace_scope_path_outside_root")

    def register_workspace(self, cycle_id: str) -> str:
        """Pin the configured real workspace to a cycle before consent review.

        Repeated registration is idempotent only while path, filesystem identity,
        project and authorized scope remain identical.
        """
        from datetime import datetime, timezone
        try:
            cycle = self.apl_store.load(cycle_id)
            path, fs_identity, identity_digest, canonical_text = self._workspace_record(
                self.workspace_root)
            self._validate_scope_paths(self.workspace_root, cycle.authorized_scope)
            scope_digest = "sha256:" + hashlib.sha256(json.dumps(
                list(cycle.authorized_scope), separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")).hexdigest()
            workspace_id = "workspace-" + hashlib.sha256(json.dumps({
                "project": self.project_id, "path": canonical_text,
                "filesystem_identity": fs_identity, "scope_digest": scope_digest,
            }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
            with self.apl_store._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._ensure_binding_schema(db)
                prior = db.execute("""SELECT project_id,canonical_path,filesystem_identity,
                    workspace_identity_digest,scope_digest,workspace_id FROM
                    apl_consent_workspace_bindings WHERE cycle_id=?""", (cycle_id,)).fetchone()
                current = (self.project_id, path, fs_identity, identity_digest,
                           scope_digest, workspace_id)
                if prior is not None and tuple(prior) != current:
                    raise AuthoritativeConsentError("workspace_binding_conflict")
                if prior is None:
                    db.execute("INSERT INTO apl_consent_workspace_bindings VALUES(?,?,?,?,?,?,?,?)",
                               (cycle_id, *current,
                                datetime.now(timezone.utc).isoformat(timespec="microseconds")))
                db.commit()
            return workspace_id
        except AuthoritativeConsentError:
            raise
        except Exception as exc:
            raise AuthoritativeConsentError("workspace_binding_persistence_failed") from exc

    @staticmethod
    def _limits_json(limits: ConsentLimits) -> str:
        return json.dumps({"max_jobs": limits.max_jobs,
            "max_iterations": limits.max_iterations, "timeout_seconds": limits.timeout_seconds,
            "estimated_budget": str(limits.estimated_budget),
            "budget_currency": limits.budget_currency}, sort_keys=True, separators=(",", ":"))

    def _validate_workspace_binding(self, cycle_id: str, scope: tuple[str, ...]):
        try:
            path, fs_identity, identity_digest, canonical_text = self._workspace_record(
                self.workspace_root)
            self._validate_scope_paths(self.workspace_root, scope)
            with self.apl_store._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._ensure_binding_schema(db)
                row = db.execute("""SELECT project_id,canonical_path,filesystem_identity,
                    workspace_identity_digest,scope_digest,workspace_id FROM
                    apl_consent_workspace_bindings WHERE cycle_id=?""", (cycle_id,)).fetchone()
                if row is None:
                    raise AuthoritativeConsentError("workspace_binding_not_registered")
                scope_digest = "sha256:" + hashlib.sha256(json.dumps(
                    list(scope), separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")).hexdigest()
                expected_id = "workspace-" + hashlib.sha256(json.dumps({
                    "project": self.project_id, "path": canonical_text,
                    "filesystem_identity": fs_identity, "scope_digest": scope_digest,
                }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
                if tuple(row) != (self.project_id, path, fs_identity, identity_digest,
                                  scope_digest, expected_id):
                    raise AuthoritativeConsentError("workspace_binding_changed_or_scope_mismatch")
                db.commit()
            return path, identity_digest, expected_id
        except AuthoritativeConsentError:
            raise
        except Exception as exc:
            raise AuthoritativeConsentError("workspace_binding_validation_failed") from exc

    def _persist_request_binding(self, cycle_id: str, proposal_id: str, request: LocalConsentRequest,
                                 workspace_identity_digest: str) -> str:
        from datetime import datetime, timezone
        limits_json = self._limits_json(request.limits)
        operations_json = json.dumps(list(request.allowed_operations), separators=(",", ":"))
        immutable = (cycle_id, request.proposal_digest, request.revision,
                     workspace_identity_digest, operations_json, limits_json,
                     request.operator_identity)
        with self.apl_store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_binding_schema(db)
            row = db.execute("""SELECT cycle_id,proposal_digest,revision,workspace_identity_digest,
                request_digest,operations_json,limits_json,expires_at,operator_identity
                FROM apl_consent_request_bindings WHERE proposal_id=?""", (proposal_id,)).fetchone()
            if row is not None:
                prior_immutable = (row[0], row[1], row[2], row[3], row[5], row[6], row[8])
                if prior_immutable != immutable:
                    raise AuthoritativeConsentError("persisted_consent_binding_conflict")
                persisted_expiry = row[7]
            else:
                db.execute("INSERT INTO apl_consent_request_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (proposal_id, cycle_id, request.proposal_digest, request.revision,
                     workspace_identity_digest, request.request_digest, operations_json,
                     limits_json, request.expires_at, request.operator_identity,
                     datetime.now(timezone.utc).isoformat(timespec="microseconds")))
                persisted_expiry = request.expires_at
            db.commit()
        return persisted_expiry

    def resolve(self, cycle_id: str, apl_proposal_id: str, *,
                now: datetime | None = None,
                include_expired_for_history: bool = False) -> AuthoritativeConsentProposal:
        # BrokerStore shares one SQLite connection; serialize state and journal reads.
        with self.broker_store._mutex:
            return self._resolve_locked(cycle_id, apl_proposal_id, now=now,
                                        include_expired_for_history=include_expired_for_history)

    def _resolve_locked(self, cycle_id: str, apl_proposal_id: str, *,
                        now: datetime | None = None,
                        include_expired_for_history: bool = False) -> AuthoritativeConsentProposal:
        from agentflow.apl.contracts import APLState
        from agentflow.apl.replanning import proposal_digest

        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            raise AuthoritativeConsentError("aware_clock_required")
        instant = instant.astimezone(timezone.utc)
        try:
            self.broker_store.expire_pending_proposals(as_of=instant)
            cycle = self.apl_store.load(cycle_id)
            rows = {item[0].proposal_id: item for item in self.apl_store.proposals(cycle_id)}
            row = rows.get(apl_proposal_id)
            if row is None:
                raise AuthoritativeConsentError("apl_proposal_not_found")
            proposal, stored_digest, apl_status = row
            if proposal_digest(proposal) != stored_digest:
                raise AuthoritativeConsentError("apl_proposal_digest_mismatch")
            with self.apl_store._connect(readonly=True) as db:
                revision_row = db.execute(
                    "SELECT sequence FROM apl_plan_revisions WHERE cycle_id=? AND revision_id=?",
                    (cycle_id, cycle.plan_revision_id),
                ).fetchone()
                latest_sequence = db.execute(
                    "SELECT MAX(sequence) FROM apl_plan_revisions WHERE cycle_id=?", (cycle_id,)
                ).fetchone()[0]
            if revision_row is None or revision_row[0] != latest_sequence:
                raise AuthoritativeConsentError("current_apl_plan_revision_missing_or_stale")
            proposed_plan_revision = revision_row[0] + 1

            uri = Path(self.apl_store.path).resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=10) as db:
                db.execute("PRAGMA query_only=ON")
                handoff = db.execute(
                    "SELECT snapshot_id,operation_id,proposal_json,digest FROM apl_broker_handoffs "
                    "WHERE cycle_id=? AND apl_proposal_id=?", (cycle_id, apl_proposal_id),
                ).fetchone()
            if handoff is None:
                raise AuthoritativeConsentError("persisted_broker_handoff_not_found")
            snapshot_id, operation_id, handoff_json, handoff_digest = handoff
            handoff_proposal = SnapshotProposal.from_json(handoff_json)
            provenance = json.loads(handoff_proposal.provenance_json)
            expected_provenance = {"source": "agentflow.apl", "cycle_id": cycle_id,
                                   "apl_proposal_id": apl_proposal_id,
                                   "apl_proposal_digest": stored_digest}
            expected_content = {
                "authorized_scope": list(cycle.authorized_scope),
                "changes": [{"action": change.action, "task": {
                    "job_id": change.task.job_id, "instruction": change.task.instruction,
                    "depends_on": list(change.task.depends_on),
                    "required_scope": list(change.task.required_scope),
                    "acceptance_criteria": list(change.task.acceptance_criteria),
                }} for change in proposal.changes],
                "completion_condition": proposal.completion_condition,
                "evidence_used": list(proposal.evidence_used),
                "objective": cycle.objective,
                "rationale": proposal.rationale,
                "source_cycle_revision": proposal.source_cycle_revision,
                "source_plan_revision_id": proposal.source_revision_id,
            }
            expected_content_json = canonical_json(expected_content)
            if (handoff_proposal.snapshot_id != snapshot_id
                    or handoff_proposal.operation_id != operation_id
                    or handoff_proposal.project_id != self.project_id
                    or handoff_proposal.proposed_by != self.proposer_principal
                    or handoff_proposal.revision_id != apl_proposal_id
                    or handoff_proposal.canonical_digest != handoff_digest
                    or digest_json(json.loads(handoff_proposal.content_json)) != handoff_digest
                    or handoff_proposal.content_json != expected_content_json
                    or provenance != expected_provenance):
                raise AuthoritativeConsentError("apl_broker_persisted_binding_mismatch")

            lifecycle = self.broker_store.get_proposal_lifecycle_for_identity(
                snapshot_id, principal_id=self.proposer_principal,
                authorized_project_ids=self.authorized_project_ids,
            )
            if lifecycle is None:
                raise AuthoritativeConsentError("broker_snapshot_missing_or_out_of_scope")
            if (lifecycle.proposal.to_json() != handoff_proposal.to_json()
                    or lifecycle.proposal.canonical_digest != handoff_digest):
                raise AuthoritativeConsentError("broker_snapshot_differs_from_apl_handoff")

            # Broker expiry is measured from the durable proposal event, not sender time.
            event = self.broker_store.db.execute(
                "SELECT occurred_at FROM broker_events WHERE aggregate_type='snapshot' "
                "AND aggregate_id=? AND event_type='snapshot.proposed' ORDER BY sequence LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if event is None:
                raise AuthoritativeConsentError("broker_proposal_event_missing")
            proposal_received = datetime.fromisoformat(event[0].replace("Z", "+00:00"))
            broker_expires = proposal_received + timedelta(seconds=PENDING_PROPOSAL_TTL_SECONDS)
            # The short consent window begins at the first authoritative review,
            # not at handoff submission. Persist it once below; retries never extend it.
            consent_expires = min(broker_expires,
                                  instant + timedelta(seconds=self.consent_ttl_seconds))

            content = json.loads(lifecycle.proposal.content_json)
            scope = tuple(content["authorized_scope"])
            changes = content["changes"]
            if (not isinstance(cycle.objective, str) or not cycle.objective.strip()
                    or not scope or tuple(sorted(set(scope))) != scope
                    or any(not set(change["task"]["required_scope"]).issubset(scope)
                           for change in changes)):
                raise AuthoritativeConsentError("persisted_objective_or_scope_invalid")
            if (cycle.state is not APLState.WAITING_APPROVAL
                    or apl_status != "proposed"
                    or proposal.source_revision_id != cycle.plan_revision_id
                    or proposal.source_cycle_revision + 1 != cycle.revision):
                # Terminal broker state is returned for diagnosis, but cannot be reviewed as approvable.
                if lifecycle.state == "pending":
                    raise AuthoritativeConsentError("apl_proposal_is_stale_or_not_waiting_approval")

            # Consent is for the resulting immutable plan, not only its delta.
            # This is what makes one approval sufficient for the whole plan.
            context = SupervisedCycleService(self.apl_store, self.apl_store.path)._replan_context(cycle)
            try:
                target_jobs = validate_proposal(context, proposal, max_jobs=self.limits.max_jobs)
            except ValueError as exc:
                if lifecycle.state == "pending":
                    raise AuthoritativeConsentError("persisted_plan_proposal_invalid") from exc
                # Keep terminal history/cancellation inspectable; terminal Broker
                # states are never approvable and cannot produce a grant.
                merged = {job.job_id: job for job in cycle.jobs}
                for change in proposal.changes:
                    merged[change.task.job_id] = change.task
                target_jobs = tuple(merged.values())
            operations = tuple(sorted("job:" + job.job_id for job in target_jobs))
            if not operations or len(set(operations)) != len(operations):
                raise AuthoritativeConsentError("persisted_proposal_operations_invalid")
            workspace_path, workspace_identity_digest, workspace_id = \
                self._validate_workspace_binding(cycle_id, scope)
            expires_text = consent_expires.astimezone(timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z")
            request = LocalConsentRequest(
                profile=SecurityProfile.LOCAL_SUPERVISED,
                proposal_id=apl_proposal_id,
                # APL's digest helper stores bare hex; security contracts use a typed prefix.
                proposal_digest=stored_digest if stored_digest.startswith("sha256:")
                else "sha256:" + stored_digest,
                revision=proposed_plan_revision,
                workspace_id=workspace_id,
                allowed_operations=operations,
                limits=self.limits,
                expires_at=expires_text,
                operator_identity=self.operator_identity,
            )
            persisted_expiry = self._persist_request_binding(
                cycle_id, apl_proposal_id, request, workspace_identity_digest)
            if persisted_expiry != request.expires_at:
                request = replace(request, expires_at=persisted_expiry)
                self._persist_request_binding(cycle_id, apl_proposal_id, request,
                                              workspace_identity_digest)
            consent_window_open = datetime.fromisoformat(
                request.expires_at.replace("Z", "+00:00")) > instant
            if (lifecycle.state == "pending" and not consent_window_open
                    and not include_expired_for_history):
                raise AuthoritativeConsentError("proposal_expired")
            changed = {change.task.job_id: change.action for change in proposal.changes}
            summaries = tuple(
                f"{changed.get(job.job_id, 'keep')} {job.job_id}: {job.instruction} "
                f"[depends={','.join(job.depends_on)}; scope={','.join(job.required_scope)}; "
                f"criteria={','.join(job.acceptance_criteria)}]"
                for job in target_jobs
            )
            return AuthoritativeConsentProposal(
                cycle_id=cycle_id, apl_proposal_id=apl_proposal_id,
                broker_snapshot_id=snapshot_id,
                broker_journal_sequence=lifecycle.journal_sequence,
                broker_status=lifecycle.state, apl_status=apl_status,
                objective=cycle.objective, authorized_scope=scope,
                plan_digest=request.proposal_digest, plan_revision=proposed_plan_revision,
                workspace_id=workspace_id, workspace_path=workspace_path,
                workspace_identity_digest=workspace_identity_digest,
                operations=operations, task_summaries=summaries,
                limits=self.limits, expires_at=request.expires_at, request=request,
                consent_window_open=consent_window_open,
            )
        except AuthoritativeConsentError:
            raise
        except Exception as exc:
            raise AuthoritativeConsentError("authoritative_consent_resolution_failed") from exc

    def resolve_for_request(self, request: LocalConsentRequest, *, now: datetime | None = None):
        """Re-resolve by stable proposal ID and require exact request equality."""
        if not isinstance(request, LocalConsentRequest):
            raise AuthoritativeConsentError("typed_local_consent_request_required")
        with self.apl_store._connect(readonly=True) as db:
            row = db.execute("SELECT cycle_id FROM apl_plan_proposals WHERE proposal_id=?",
                             (request.proposal_id,)).fetchone()
        if row is None:
            raise AuthoritativeConsentError("apl_proposal_not_found")
        resolved = self.resolve(row[0], request.proposal_id, now=now)
        if resolved.request != request:
            raise AuthoritativeConsentError("consent_request_differs_from_persisted_proposal")
        return resolved
