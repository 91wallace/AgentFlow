"""M2.3 no package cycles, historical exports and independently rejected grants."""

import importlib
from pathlib import Path
import pickle

import agentflow
import pytest

from architecture_audit import audit, scan_package
from test_local_import_isolation import fresh_process


def test_all_package_cycles_are_removed_and_apl_never_imports_security():
    root = Path(agentflow.__file__).resolve().parent
    assert audit(root)["cycles"] == []
    imports, _, _ = scan_package(root)
    assert not [edge for edge in imports if edge.source.startswith("agentflow.apl")
                and (edge.target == "agentflow.security" or edge.target.startswith("agentflow.security."))]


@pytest.mark.parametrize("name", [
    "ExecutionLimits", "TaskExecutionLimits", "FinancialBudgetMode", "FinancialBudgetPolicy",
])
def test_limit_and_budget_types_keep_old_aliases_and_pickle_paths(name):
    current = importlib.import_module("agentflow.apl.execution_contracts")
    original = importlib.import_module("agentflow.apl.authorized_execution")
    facade = importlib.import_module("agentflow.apl")
    expected = getattr(current, name)
    assert getattr(original, name) is expected
    assert getattr(facade, name) is expected
    assert pickle.loads(f"cagentflow.apl.authorized_execution\n{name}\n.".encode()) is expected


def test_public_limits_load_without_sqlite_or_advanced_packages(tmp_path):
    fresh_process(tmp_path, """
import importlib.abc
blocked = []
class NoImplementations(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sqlite3' or fullname.startswith('sqlite3.') or any(
                fullname == p or fullname.startswith(p + '.')
                for p in ('agentflow.security', 'agentflow.approval', 'agentflow.teg')):
            blocked.append(fullname)
            raise ModuleNotFoundError('implementation forbidden', name=fullname)
sys.meta_path.insert(0, NoImplementations())
from agentflow.apl import ExecutionLimits, TaskExecutionLimits, FinancialBudgetMode, FinancialBudgetPolicy
from agentflow.apl import execution_contracts
assert ExecutionLimits is execution_contracts.ExecutionLimits
assert TaskExecutionLimits is execution_contracts.TaskExecutionLimits
assert FinancialBudgetPolicy(FinancialBudgetMode.HARD_CAP_REQUIRED).estimated_amount is None
assert not blocked
assert not attempts
assert 'agentflow.apl.authorized_execution' not in sys.modules
assert 'sqlite3' not in sys.modules
""")


def test_resolver_uses_pure_contracts_and_rejects_invalid_signature_without_security(tmp_path):
    vectors = Path(__file__).with_name("local_grant_vectors.json")
    fresh_process(tmp_path, f"""
import importlib.abc
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
import json
blocked = []
class NoSecurity(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'agentflow.security' or fullname.startswith('agentflow.security.'):
            blocked.append(fullname)
            raise ModuleNotFoundError('security implementation forbidden', name=fullname)
sys.meta_path.insert(0, NoSecurity())
from agentflow.approval.contracts import LocalGrant, LocalConsentAuditRecord
from agentflow.apl.authorized_execution import BrokerExecutionGrantResolver, AuthorizedExecutionError
data = json.loads(Path({str(vectors)!r}).read_text(encoding='utf-8'))['v2']
grant = LocalGrant.from_json(data['grant_json'])
record = LocalConsentAuditRecord.from_json(data['audit_json'])
class Broker:
    def get_local_grant(self, grant_id):
        assert grant_id == grant.grant_id
        return grant, 'approved'
    def get_local_consent_by_request(self, digest):
        assert digest == record.request_digest
        return record, grant.grant_id
checks = []
class InvalidSignatureVerifier:
    def verify_digest(self, key_id, digest, signature):
        assert key_id == grant.key_id and digest == grant.grant_digest
        assert signature == bytes(range(64))
        checks.append(digest)
        return False
def forbidden(*args):
    raise AssertionError('invalid signature must block subsequent workspace/effect access')
resolver = BrokerExecutionGrantResolver(
    broker_store=Broker(), cycle_store=object(),
    verifier_resolver=SimpleNamespace(resolve=lambda *args: InvalidSignatureVerifier()),
    workspace_resolver=SimpleNamespace(resolve=forbidden),
    executor_id='synthetic', executor_version='1.0',
    clock=lambda: datetime(2026, 11, 20, 12, 1, tzinfo=timezone.utc))
try:
    resolver.resolve_and_validate(grant.grant_id, 'cycle-1')
except AuthorizedExecutionError as exc:
    assert str(exc) == 'operational_grant_signature_invalid', exc
else:
    raise AssertionError('invalid signature must never authorize execution')
assert checks == [grant.grant_digest]
assert not attempts
assert not blocked
assert not any(n == 'agentflow.security' or n.startswith('agentflow.security.') for n in sys.modules)
""")
