"""Independent structured-result validators for allowlisted gate programs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import RuntimeResult


class StructuredGateValidator:
    """Require one exact JSON report covering every configured criterion.

    A zero exit code is necessary but never sufficient. The process must emit a
    bounded, sanitized report with matching gate/revision/snapshot identity and
    a declared record for each expected criterion. The passing validator is the
    separate verification step; the command's own status is only a declaration.
    """

    version = "1"
    _schema = {"schema_version", "gate_id", "definition_revision",
               "snapshot_digest", "status", "evidence"}
    _evidence = {"criterion_id", "status", "summary"}

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def __init__(self, gate_id: str, definition_revision: int,
                 snapshot_digest: str, criterion_ids: tuple[str, ...],
                 *, max_report_bytes: int = 1024 * 1024):
        if (not gate_id or definition_revision < 1 or not snapshot_digest
                or not criterion_ids or len(set(criterion_ids)) != len(criterion_ids)
                or not 1 <= max_report_bytes <= 1024 * 1024):
            raise ValueError("structured validator identity/limits are invalid")
        self._gate_id = gate_id
        self._revision = definition_revision
        self._snapshot = snapshot_digest
        self._criteria = frozenset(criterion_ids)
        self._limit = max_report_bytes
        self.validator_id = "agentflow-structured-report"
        self.digest = "sha256:" + hashlib.sha256(
            b"agentflow-structured-report:v1:verified-evidence-schema").hexdigest()

    def validate(self, result: RuntimeResult) -> tuple[bool, str]:
        if (result.exit_code != 0 or result.spawn_failed or result.timed_out
                or result.cancelled or result.truncated or not result.process_stopped
                or not result.logs_sanitized):
            return False, "process result is not eligible for structured validation"
        try:
            path = Path(result.stdout_path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > self._limit:
                return False, "structured report missing or exceeds limit"
            raw = path.read_bytes()
            actual = "sha256:" + hashlib.sha256(raw).hexdigest()
            if actual != result.stdout_digest:
                return False, "structured report digest changed after runtime capture"
            report = json.loads(raw.decode("utf-8"), object_pairs_hook=self._unique_object,
                                parse_constant=lambda value: (_ for _ in ()).throw(
                                    ValueError(f"invalid JSON constant: {value}")))
        except (OSError, UnicodeError, ValueError):
            return False, "structured report is unreadable or invalid JSON"
        if not isinstance(report, dict) or set(report) != self._schema:
            return False, "structured report schema differs"
        if (report.get("schema_version") != 1 or report.get("gate_id") != self._gate_id
                or report.get("definition_revision") != self._revision
                or report.get("snapshot_digest") != self._snapshot
                or report.get("status") != "declared"):
            return False, "structured report is stale or does not pass"
        evidence = report.get("evidence")
        if not isinstance(evidence, list):
            return False, "structured report evidence list missing"
        observed: set[str] = set()
        for item in evidence:
            if not isinstance(item, dict) or set(item) != self._evidence:
                return False, "structured evidence schema differs"
            criterion_id = item.get("criterion_id")
            summary = item.get("summary")
            if (not isinstance(criterion_id, str) or criterion_id not in self._criteria
                    or criterion_id in observed or item.get("status") != "declared"
                    or not isinstance(summary, str) or not summary.strip()
                    or len(summary) > 512 or any(ord(char) < 32 for char in summary)):
                return False, "criterion evidence missing, duplicate or invalid"
            observed.add(criterion_id)
        if observed != self._criteria:
            return False, "structured report does not cover every required criterion"
        return True, "structured report verified for current gate/revision/snapshot"


class StructuredGateValidatorFactory:
    """Build a separately bound validator instance for each selected gate."""

    def __init__(self, plan_task):
        self.criteria = {item.id for item in plan_task.acceptance_criteria}

    def for_gate(self, gate, request, snapshot) -> StructuredGateValidator:
        if not set(gate.criterion_ids) <= self.criteria:
            raise ValueError("gate references criteria absent from the current task")
        return StructuredGateValidator(gate.gate_id, request.definition_revision,
                                       snapshot.snapshot_digest, gate.criterion_ids)
