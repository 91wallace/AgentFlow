"""M2.2 one-way dependencies, historical wire bytes and storage compatibility."""

import importlib
import json
from pathlib import Path
import pickle

import agentflow
import pytest

from architecture_audit import audit, scan_package
from test_local_import_isolation import fresh_process


VECTORS = Path(__file__).with_name("local_grant_vectors.json")


def test_approval_never_imports_security_or_apl_including_type_only():
    root = Path(agentflow.__file__).resolve().parent
    imports, _, _ = scan_package(root)
    assert not [edge for edge in imports if edge.source.startswith("agentflow.approval")
                and any(edge.target == prefix or edge.target.startswith(prefix + ".")
                        for prefix in ("agentflow.security", "agentflow.apl"))]
    assert not any("approval" in cycle for cycle in audit(root)["cycles"])


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_canonical_bytes_digests_and_execution_binding_match_legacy_vectors(version):
    from agentflow.approval import consent_contracts as c
    from agentflow.approval import local_grants as g
    from agentflow.security.authorization import _build_signed_grant

    # Frozen from pre-extraction source, never regenerated during tests.
    fixture = json.loads(VECTORS.read_text(encoding="utf-8"))
    vector = fixture[version]
    grant = g.LocalGrant.from_json(vector["grant_json"])
    record = g.LocalConsentAuditRecord.from_json(vector["audit_json"])
    assert grant.to_json() == vector["grant_json"]
    assert record.to_json() == vector["audit_json"]
    assert grant.digest_payload_hash() == grant.grant_digest
    args = (grant.profile, grant.proposal_id, grant.plan_digest, grant.revision,
            grant.workspace_id, grant.allowed_operations, grant.limits,
            grant.expires_at, grant.operator_identity)
    if version == "v2":
        binding = c.ExecutionPlanConsentBinding.from_payload(grant.execution_binding)
        request = c.LocalExecutionConsentRequest(*args, binding)
    else:
        request = c.LocalConsentRequest(*args)
    assert request.request_digest == vector["request_digest"] == record.request_digest

    class DeterministicTestSigner:
        key_id = grant.key_id

        def sign_digest(self, digest):
            assert digest == grant.grant_digest
            return bytes(range(64))

    fields = grant.signing_payload()
    fields.pop("profile")
    fields.pop("key_id")
    fields["limits"] = grant.limits
    fields["allowed_operations"] = grant.allowed_operations
    rebuilt = _build_signed_grant(signer=DeterministicTestSigner(), **fields)
    assert rebuilt.to_json() == vector["grant_json"]
    assert g._hash({"message": "ação", "budget": "1.20"}) == fixture["unicode_hash"]


@pytest.mark.parametrize("legacy,owner,names", [
    ("security.profile", "approval.consent_contracts",
     ("SecurityProfile", "ConsentDecision", "ConsentLimits", "LocalConsentRequest",
      "LocalExecutionConsentRequest", "ExecutionPlanConsentBinding", "ProfileConfigurationError")),
    ("security.authorization", "approval.local_grants",
     ("LocalAuthorizationError", "LocalGrant", "LocalConsentAuditRecord", "LocalGrantVerifier")),
])
def test_old_import_and_pickle_paths_keep_identity(legacy, owner, names):
    previous = importlib.import_module("agentflow." + legacy)
    current = importlib.import_module("agentflow." + owner)
    for name in names:
        value = getattr(current, name)
        assert getattr(previous, name) is value
        assert pickle.loads(f"cagentflow.{legacy}\n{name}\n.".encode()) is value


def test_grant_and_profile_validators_keep_distinct_error_semantics():
    from agentflow.approval.consent_contracts import ProfileConfigurationError, _digest as profile_digest
    from agentflow.approval.local_grants import LocalAuthorizationError, validate_grant_digest, validate_grant_id
    from agentflow.security.authorization import _digest, _id
    assert validate_grant_digest is _digest
    assert validate_grant_id is _id
    assert profile_digest is not validate_grant_digest
    with pytest.raises(ProfileConfigurationError, match="^binding_invalid$"):
        profile_digest("invalid", "binding")
    with pytest.raises(LocalAuthorizationError, match="^binding_invalid$"):
        validate_grant_digest("invalid", "binding")
    with pytest.raises(LocalAuthorizationError, match="^binding_invalid$"):
        validate_grant_id("contains spaces", "binding")


def test_contracts_load_without_security_apl_sqlite_or_crypto(tmp_path):
    fresh_process(tmp_path, """
import importlib.abc
blocked = []
class NoServices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sqlite3' or any(fullname == p or fullname.startswith(p + '.')
                for p in ('agentflow.security', 'agentflow.apl')):
            blocked.append(fullname)
            raise ModuleNotFoundError('implementation forbidden', name=fullname)
sys.meta_path.insert(0, NoServices())
from agentflow.approval.contracts import LocalGrant, LocalConsentAuditRecord, ConsentDecision
from agentflow.approval.consent_contracts import ConsentLimits, ExecutionPlanConsentBinding
assert not attempts
assert not blocked
""")


def test_store_writes_reopens_transitions_and_checks_integrity_without_security(tmp_path):
    fresh_process(tmp_path, f"""
import importlib.abc
import sys
import json
from pathlib import Path
blocked = []
class NoSecurity(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + '.') for p in ('agentflow.security', 'agentflow.apl')):
            blocked.append(fullname)
            raise ModuleNotFoundError('security service forbidden', name=fullname)
sys.meta_path.insert(0, NoSecurity())
from agentflow.approval.contracts import LocalConsentAuditRecord, LocalGrant
from agentflow.approval.persistence import BrokerStore
vectors = json.loads(Path({str(VECTORS)!r}).read_text(encoding='utf-8'))
for version, vector in ((key, vectors[key]) for key in ('v1', 'v2')):
    grant = LocalGrant.from_json(vector['grant_json'])
    record = LocalConsentAuditRecord.from_json(vector['audit_json'])
    path = Path(version + '.sqlite3')
    store = BrokerStore(path)
    try:
        assert store._record_local_consent(record, grant) == grant.grant_id
        assert store._record_local_consent(record, grant) == grant.grant_id
        assert store.get_local_consent_by_request(record.request_digest) == (record, grant.grant_id)
        store.verify_integrity()
    finally:
        store.close()
    store = BrokerStore(path)
    try:
        stored, status = store.get_local_grant(grant.grant_id)
        assert stored.to_json() == vector['grant_json'] and status == 'approved'
        store._transition_local_grant(grant.grant_id, 'consumed', 'consumer_consumed',
            operation_id='consume-1', actor_proof_digest='sha256:' + 'e' * 64)
        assert store.get_local_grant(grant.grant_id)[1] == 'consumed'
        store.verify_integrity()
    finally:
        store.close()
assert not blocked
assert not any(n == p or n.startswith(p + '.') for n in sys.modules
               for p in ('agentflow.security', 'agentflow.apl'))
""", block_crypto=False)
