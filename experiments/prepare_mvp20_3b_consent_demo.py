"""Prepare a disposable accepted APL plan for manual MVP-20.3B consent review.

This helper creates a fresh temporary Git project and SQLite stores. It never
enrolls a credential, asks for a password, starts a provider, or dispatches jobs.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from uuid import uuid4

from agentflow.apl import APLState, CycleSnapshot, JobProposal, SQLiteCycleStore
from agentflow.approval.persistence import BrokerStore
from agentflow.security import SecurityProfile, save_profile_configuration
from agentflow.state.execution import ExecutionStore


def prepare(root: Path) -> tuple[str, str]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    subprocess.run(["git", "-C", str(root), "init", "--quiet"], check=True,
                   capture_output=True, text=True, timeout=15)
    workspace = root / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    control = root / ".agentflow"
    control.mkdir()
    save_profile_configuration(control / "security-profile.toml",
                               SecurityProfile.LOCAL_SUPERVISED)
    (control / "local-consent.toml").write_text(
        'schema_version = 1\n'
        'broker_database = "broker.sqlite3"\n'
        'project_id = "mvp20-3b-consent-demo"\n'
        'authorized_project_ids = ["mvp20-3b-consent-demo"]\n'
        'proposer_principal = "apl-local-synthetic"\n'
        'workspace_root = "workspace"\n'
        'max_jobs = 2\n'
        'max_iterations = 2\n'
        'max_timeout_seconds = 120\n'
        'max_estimated_budget = "1.00"\n'
        'budget_currency = "USD"\n'
        'authorized_consumer_principals = ["synthetic-executor"]\n'
        'consent_ttl_seconds = 120\n'
        'max_grant_ttl_seconds = 120\n', encoding="utf-8", newline="\n")

    jobs = (
        JobProposal(
            "demo-create", "Synthetic only: create src/demo.py with add(a, b)", (),
            ("src/demo.py",), ("file defines add(a, b)",)),
        JobProposal(
            "demo-verify", "Synthetic only: validate the addition function with a unit test",
            ("demo-create",), ("tests/test_demo.py",),
            ("test verifies 2 + 3 equals 5",)),
    )
    canonical_jobs = json.dumps([asdict(job) for job in jobs], sort_keys=True,
                                separators=(",", ":"), ensure_ascii=False)
    plan_digest = "sha256:" + hashlib.sha256(canonical_jobs.encode("utf-8")).hexdigest()
    cycle_id = "cycle-mvp20-3b-" + uuid4().hex[:16]
    state_path = control / "state.sqlite3"
    with ExecutionStore(state_path):
        pass
    cycles = SQLiteCycleStore(state_path)
    cycle = cycles.create(CycleSnapshot(
        cycle_id, "Synthetic only: implement and verify integer addition",
        ("src/demo.py", "tests/test_demo.py"), APLState.READY, jobs, (), 0, 0,
        plan_digest=plan_digest))
    with BrokerStore(root / "broker.sqlite3"):
        pass
    print(json.dumps({
        "project_root": str(root), "workspace": str(workspace.resolve()),
        "cycle_id": cycle.cycle_id, "plan_revision_id": cycle.plan_revision_id,
        "plan_digest": plan_digest, "jobs": [job.job_id for job in jobs],
        "provider_started": False, "jobs_executed": False,
        "dispatch_enabled": False,
    }, sort_keys=True, ensure_ascii=False))
    return cycle.cycle_id, plan_digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path", type=Path,
        default=Path(tempfile.gettempdir()) / f"AgentFlow-MVP20.3B-{uuid4().hex[:12]}",
        help="novo diretório descartável; caminhos existentes nunca são sobrescritos")
    args = parser.parse_args()
    prepare(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
