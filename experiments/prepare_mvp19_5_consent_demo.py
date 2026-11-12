"""Create an isolated synthetic APL/Broker proposal for manual consent review.

This helper never enrolls a credential, asks for a password, starts a provider,
or dispatches a job. It creates a fresh temporary Git project and SQLite stores.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from agentflow.apl import (
    APLState,
    BrokerHandoffService,
    CycleSnapshot,
    JobProposal,
    PlanProposal,
    SQLiteCycleStore,
    SupervisedCycleService,
    TaskChange,
)
from agentflow.approval.persistence import BrokerStore
from agentflow.approval.protocol import SnapshotProposal
from agentflow.security import (AuthoritativeConsentResolver, ConsentLimits, SecurityProfile,
                                current_local_operator_principal, save_profile_configuration)
from agentflow.state.execution import ExecutionStore


class _SyntheticPlanner:
    def propose(self, context):
        return PlanProposal(
            "mvp19-5-synthetic-planner", context.plan_revision_id,
            context.cycle_revision, "Synthetic proposal for local consent review",
            (TaskChange("add", JobProposal(
                "demo-second", "Synthetic only: implement a pure addition function",
                (), ("src/demo.py",), ("test:unit",))),),
            "Complete the synthetic unit-test criterion",
        )


class _NoNetworkClient:
    def request(self, _envelope):
        raise AssertionError("demo preparation must not use network transport")


def prepare(root: Path) -> tuple[str, str]:
    if root.exists():
        raise FileExistsError(f"refusing to overwrite existing demo directory: {root}")
    root.mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "init", "--quiet"], check=True,
                   capture_output=True, text=True, timeout=15)
    run_id = uuid4().hex[:16]
    cycle_id = f"cycle-mvp19-5-{run_id}"
    replan_request_id = f"mvp19-5-replan-{run_id}"
    config_dir = root / ".agentflow"
    config_dir.mkdir()
    workspace = root / "workspace"
    (workspace / "src").mkdir(parents=True)
    save_profile_configuration(config_dir / "security-profile.toml",
                               SecurityProfile.LOCAL_SUPERVISED)
    (config_dir / "local-consent.toml").write_text(
        'schema_version = 1\n'
        'broker_database = "broker.sqlite3"\n'
        'project_id = "mvp19-5-consent-demo"\n'
        'authorized_project_ids = ["mvp19-5-consent-demo"]\n'
        'proposer_principal = "apl-local"\n'
        'workspace_root = "workspace"\n'
        'max_jobs = 3\n'
        'max_iterations = 3\n'
        'max_timeout_seconds = 120\n'
        'max_estimated_budget = "1.00"\n'
        'budget_currency = "USD"\n'
        'authorized_consumer_principals = ["local-executor"]\n'
        'consent_ttl_seconds = 120\n'
        'max_grant_ttl_seconds = 90\n', encoding="utf-8", newline="\n")

    state_path = config_dir / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    cycle_store = SQLiteCycleStore(state_path)
    service = SupervisedCycleService(cycle_store, state_path)
    cycle = cycle_store.create(CycleSnapshot(
        cycle_id, "Synthetic consent review: add a pure integer addition function",
        ("src/demo.py", "test:unit"), APLState.READY,
        (JobProposal("demo-first", "Synthetic completed baseline", (),
                     ("src/demo.py",), ("test:unit",)),),
        (), 0, 0,
    ))
    proposal, status, _created = service.replan(
        cycle.cycle_id, _SyntheticPlanner(), replan_request_id)
    if status != "proposed":
        raise RuntimeError(f"synthetic proposal creation failed: {status}")

    broker_path = root / "broker.sqlite3"
    broker = BrokerStore(broker_path)
    try:
        handoff = BrokerHandoffService(
            cycle_store, state_path, _NoNetworkClient(),
            project_id="mvp19-5-consent-demo", principal_id="apl-local")
        binding = handoff._prepare(cycle.cycle_id, proposal.proposal_id)
        broker.propose_snapshot(SnapshotProposal.from_json(binding[2]))
        resolver = AuthoritativeConsentResolver(
            apl_store=cycle_store, broker_store=broker,
            project_id="mvp19-5-consent-demo", proposer_principal="apl-local",
            authorized_project_ids=("mvp19-5-consent-demo",),
            operator_identity=current_local_operator_principal(),
            limits=ConsentLimits(3, 3, 120, Decimal("1.00"), "USD"),
            workspace_root=workspace, consent_ttl_seconds=120)
        resolver.register_workspace(cycle.cycle_id)
    finally:
        broker.close()
    print(json.dumps({
        "project_root": str(root.resolve()),
        "cycle_id": cycle.cycle_id,
        "proposal_id": proposal.proposal_id,
        "synthetic_only": True,
        "provider_started": False,
        "dispatch_enabled": False,
    }, sort_keys=True))
    return cycle.cycle_id, proposal.proposal_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", type=Path,
                        default=Path(tempfile.gettempdir()) / "AgentFlow-MVP19.5-consent-demo",
                        help="destino novo e descartável; o helper nunca sobrescreve diretórios existentes")
    args = parser.parse_args()
    prepare(args.path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
