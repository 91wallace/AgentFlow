"""Canonical identities and digests for persisted APL executions."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from .contracts import CycleSnapshot


def canonical_json(value) -> str:
    """Serialize values using the stable encoding used by APL persistence."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def local_plan_digest(snapshot: CycleSnapshot) -> str:
    """Digest the complete local plan binding, excluding mutable run state."""
    return sha256_digest(canonical_json({
        "cycle_id": snapshot.cycle_id,
        "objective": snapshot.objective,
        "revision": snapshot.plan_revision_id,
        "jobs": [asdict(job) for job in snapshot.jobs],
        "authorized_scope": list(snapshot.authorized_scope),
    }))


def operation_id(run_id: str, job_id: str, plan_digest: str) -> str:
    raw = sha256_digest(f"{run_id}:{job_id}:{plan_digest}")[-32:]
    return "apl-op-" + raw


def attempt_id(run_id: str, job_id: str, attempt: int) -> str:
    raw = sha256_digest(f"{run_id}:{job_id}:{attempt}")[-32:]
    return "apl-attempt-" + raw
