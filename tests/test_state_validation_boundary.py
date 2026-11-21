"""M2.1 dependency direction and compatibility at the persistence boundary."""

import importlib
from pathlib import Path
import pickle

import agentflow
import pytest

from architecture_audit import audit, scan_package
from test_local_import_isolation import fresh_process


def test_validation_has_no_state_imports_including_type_only():
    root = Path(agentflow.__file__).resolve().parent
    imports, unresolved, _ = scan_package(root)
    assert not unresolved
    assert not [edge for edge in imports if edge.source.startswith("agentflow.validation")
                and (edge.target == "agentflow.state" or edge.target.startswith("agentflow.state."))]
    assert not any("state" in cycle and "validation" in cycle for cycle in audit(root)["cycles"])


@pytest.mark.parametrize("name", ["StateError", "IntegrityError", "ConflictError", "TransitionError"])
def test_error_identity_inheritance_and_legacy_pickle_paths(name):
    shared = importlib.import_module("agentflow.contracts.state_errors")
    expected = getattr(shared, name)
    for path in ("agentflow.state", "agentflow.state.contracts"):
        assert getattr(importlib.import_module(path), name) is expected
        # GLOBAL opcodes in pickles written before extraction retain these paths.
        assert pickle.loads(f"c{path}\n{name}\n.".encode()) is expected
    instance = expected("preserve message")
    assert isinstance(instance, shared.StateError)
    assert isinstance(instance, ValueError)
    assert pickle.loads(pickle.dumps(instance)).args == ("preserve message",)


def test_reservation_reexport_preserves_legacy_serialization():
    from agentflow.state.validation import ValidationReservation as legacy
    from agentflow.validation.contracts import ValidationReservation
    assert legacy is ValidationReservation
    assert pickle.loads(b"cagentflow.state.validation\nValidationReservation\n.") is legacy
    value = legacy("run", "operation", "passed", "digest")
    assert pickle.loads(pickle.dumps(value)) == value


def test_validation_can_replay_and_recover_without_state_or_sqlite(tmp_path):
    fresh_process(tmp_path, """
import importlib.abc
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
blocked = []
class NoPersistence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sqlite3' or fullname == 'agentflow.state' or fullname.startswith('agentflow.state.'):
            blocked.append(fullname)
            raise ModuleNotFoundError('persistence implementation forbidden', name=fullname)
sys.meta_path.insert(0, NoPersistence())
from agentflow.validation.contracts import ValidationReservation, ValidationResult, RunStatus, ValidationStore
from agentflow.validation.scheduler import ValidationScheduler
assert get_type_hints(ValidationScheduler.__init__)['store'] is ValidationStore
result = ValidationResult('run', 'operation', RunStatus.PASSED, (), 'request', 'snapshot')
class MemoryStore:
    def begin_validation(self, request, workspace, checkpoint, plan):
        return ValidationReservation('run', 'operation', 'passed', 'request')
    def validation_result(self, run_id):
        assert run_id == 'run'
        return result
    def recover_interrupted_validation(self, run_id, *, process_stopped, reason):
        assert (run_id, process_stopped, reason) == ('run', True, 'stopped')
        return 'inconclusive'
store = MemoryStore()
scheduler = ValidationScheduler(store, None, None, None, None, Path('snapshots'), Path('outputs'))
assert scheduler.run(SimpleNamespace(validation_run_id='run'), None, None, None) is result
assert scheduler.recover_interrupted('run', process_stopped=True, reason='stopped') == 'inconclusive'
assert not blocked
assert not attempts
assert 'sqlite3' not in sys.modules
assert not any(name == 'agentflow.state' or name.startswith('agentflow.state.') for name in sys.modules)
""")
