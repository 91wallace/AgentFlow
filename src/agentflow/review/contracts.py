"""Pure policy and response validation for Git-bound reviews."""

from dataclasses import dataclass
import hashlib
import json
import math


STATES = {"APPROVED", "CORRECTIONS_REQUIRED", "BLOCKED", "INCOMPLETE", "FAILED"}


@dataclass(frozen=True)
class ReviewPolicy:
    mode: str = "automatic"
    max_corrections: int = 2
    max_calls: int = 8
    max_seconds: int = 600
    max_payload_bytes: int = 524288
    no_progress_limit: int = 1
    max_context_files: int = 12
    min_confidence: float = 0.8

    def __post_init__(self):
        if self.mode not in {"automatic", "off"}:
            raise ValueError("review mode not implemented")
        for key, low, high in (("max_corrections", 0, 10), ("max_calls", 1, 32),
                ("max_seconds", 1, 7200), ("max_payload_bytes", 1024, 16777216),
                ("no_progress_limit", 1, 10), ("max_context_files", 1, 64)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid review limit: " + key)
        if type(self.min_confidence) not in (int, float) or not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise ValueError("invalid review confidence")


def validate_review(answer, packet, min_confidence=0.8):
    if (type(answer) is not dict or set(answer) != {"schema_version", "state", "findings", "context_requests"}
            or type(answer["schema_version"]) is not int or answer["schema_version"] != 1
            or answer["state"] not in STATES or type(answer["findings"]) is not list
            or type(answer["context_requests"]) is not list
            or any(type(path) is not str for path in answer["context_requests"])):
        raise ValueError("review_response_invalid")
    findings, seen = [], set()
    required = {"id", "path", "line", "end_line", "severity", "evidence", "description",
                "impact", "suggestion", "confidence", "resolution"}
    for value in answer["findings"]:
        if type(value) is not dict or set(value) - {"surface"} != required:
            raise ValueError("review_finding_fields_invalid")
        if any(type(value[key]) is not str or not value[key].strip() for key in (
                "id", "path", "severity", "evidence", "description", "impact", "suggestion", "resolution")):
            raise ValueError("review_finding_text_invalid")
        surface = value.get("surface", "working")
        if surface not in {"working", "index", "before"}:
            raise ValueError("review_finding_surface_invalid")
        source = packet["context"].get(value["path"])
        change = next((item for item in packet["changes"] if item["path"] == value["path"]), None)
        if surface == "index":
            source = {"text": change.get("index_after_text")} if change else None
        elif surface == "before":
            source = change.get("before") if change else None
        if (not source or type(source.get("text")) is not str
                or type(value["line"]) is not int or type(value["end_line"]) is not int
                or not 1 <= value["line"] <= value["end_line"] <= len(source["text"].splitlines())):
            raise ValueError("review_finding_location_invalid")
        anchor = "\n".join(source["text"].splitlines()[value["line"]-1:value["end_line"]])
        if value["evidence"] not in anchor:
            raise ValueError("review_finding_evidence_not_in_snapshot")
        if value["severity"] not in {"critical", "high", "medium", "low"} or value["resolution"] != "open":
            raise ValueError("review_finding_state_invalid")
        confidence = value["confidence"]
        if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("review_finding_confidence_invalid")
        stable = json.dumps([value["path"], value["evidence"], value["description"]], ensure_ascii=False, separators=(",", ":"))
        identifier = "finding-" + hashlib.sha256(stable.encode()).hexdigest()[:24]
        if identifier in seen:
            raise ValueError("duplicate_review_finding")
        seen.add(identifier)
        findings.append({**value, "reported_id": value["id"], "id": identifier,
                         "surface": surface,
                         "grounding": "source_anchor_verified", "actionable": confidence >= min_confidence})
    if answer["state"] == "APPROVED" and (findings or answer["context_requests"]):
        raise ValueError("review_approval_with_pending_items")
    if answer["state"] == "CORRECTIONS_REQUIRED" and (not findings or any(not f["actionable"] for f in findings)):
        raise ValueError("review_unconfirmed_findings")
    return {**answer, "findings": findings}
