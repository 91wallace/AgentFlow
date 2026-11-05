import hashlib
import json
from pathlib import Path

from agentflow.validation import (CapabilityState, RuntimeCapabilities,
                                  RuntimeProfile, RuntimeResult,
                                  StructuredGateValidator)


def test_structured_validator_checks_declared_claims_and_exact_provenance(tmp_path):
    path = tmp_path / "stdout.log"
    report = {"schema_version": 1, "gate_id": "gate-a", "definition_revision": 2,
              "snapshot_digest": "sha256:" + "a" * 64, "status": "declared",
              "evidence": [{"criterion_id": "criterion-a", "status": "declared",
                            "summary": "Independent test suite reported success"}]}
    raw = json.dumps(report, separators=(",", ":")).encode()
    path.write_bytes(raw)
    result = RuntimeResult(0, False, False, False, "identity", path,
                          tmp_path / "stderr.log",
                          "sha256:" + hashlib.sha256(raw).hexdigest(),
                          "sha256:" + hashlib.sha256(b"").hexdigest(),
                          len(raw), 0, False,
                          RuntimeCapabilities(RuntimeProfile.LOCAL_EXPLICIT, "fake", "1", ()),
                          process_stopped=True, logs_sanitized=True)
    validator = StructuredGateValidator("gate-a", 2, report["snapshot_digest"],
                                        ("criterion-a",))
    assert validator.validate(result)[0]
    stale = StructuredGateValidator("gate-a", 1, report["snapshot_digest"],
                                    ("criterion-a",))
    assert not stale.validate(result)[0]


def test_structured_validator_rejects_duplicate_keys_and_incomplete_criteria(tmp_path):
    path = tmp_path / "stdout.log"
    raw = (b'{"schema_version":1,"schema_version":1,"gate_id":"gate-a",'
           b'"definition_revision":2,"snapshot_digest":"sha256:' + b"a" * 64 +
           b'","status":"declared","evidence":[]}')
    path.write_bytes(raw)
    result = RuntimeResult(0, False, False, False, "id", path,
                          tmp_path / "stderr.log",
                          "sha256:" + hashlib.sha256(raw).hexdigest(),
                          "sha256:" + hashlib.sha256(b"").hexdigest(), len(raw), 0,
                          False, RuntimeCapabilities(RuntimeProfile.LOCAL_EXPLICIT,
                                                     "fake", "1", ()),
                          process_stopped=True, logs_sanitized=True)
    validator = StructuredGateValidator("gate-a", 2, "sha256:" + "a" * 64,
                                        ("criterion-a",))
    assert not validator.validate(result)[0]
    raw = json.dumps({"schema_version": 1, "gate_id": "gate-a",
                      "definition_revision": 2, "snapshot_digest": "sha256:" + "a" * 64,
                      "status": "declared", "evidence": []}).encode()
    path.write_bytes(raw)
    result = RuntimeResult(0, False, False, False, "id", path, tmp_path / "stderr.log",
                          "sha256:" + hashlib.sha256(raw).hexdigest(),
                          "sha256:" + hashlib.sha256(b"").hexdigest(), len(raw), 0,
                          False, RuntimeCapabilities(RuntimeProfile.LOCAL_EXPLICIT,
                                                     "fake", "1", ()),
                          process_stopped=True, logs_sanitized=True)
    assert not validator.validate(result)[0]
