"""M1 startup and workflow checks run in fresh processes, including wheel CI."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


TESTS = Path(__file__).resolve().parent
BLOCK_CRYPTO = """
import importlib.abc
import sys
attempts = []
assert not any(n == 'cryptography' or n.startswith('cryptography.') for n in sys.modules)
class BlockCrypto(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cryptography' or fullname.startswith('cryptography.'):
            attempts.append(fullname)
            raise ModuleNotFoundError('cryptography blocked by M1 test', name=fullname)
sys.meta_path.insert(0, BlockCrypto())
"""


def fresh_process(tmp_path, code, *, block_crypto=True, timeout=90):
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    result = subprocess.run(
        [sys.executable, "-c", (BLOCK_CRYPTO if block_crypto else "") + textwrap.dedent(code)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("order", [
    ("security", "approval", "apl"),
    ("apl", "approval", "security"),
    ("approval", "security", "apl"),
])
def test_facades_do_not_load_implementations_or_sqlite(tmp_path, order):
    fresh_process(tmp_path, f"""
import importlib
for name in {order!r}:
    module = importlib.import_module('agentflow.' + name)
    assert set(module.__all__) <= set(dir(module))
    assert not hasattr(module, 'missing_export')
assert 'sqlite3' not in sys.modules
assert not attempts
assert not any(n.startswith(('agentflow.security.', 'agentflow.approval.', 'agentflow.apl.'))
               for n in sys.modules)
""")


def test_local_cli_does_not_load_advanced_packages(tmp_path):
    fresh_process(tmp_path, """
from agentflow.cli import main
for arguments in (['--version'], ['--help']):
    try:
        main(arguments)
    except SystemExit as exc:
        assert exc.code == 0
assert not attempts
assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules
               for prefix in ('agentflow.security', 'agentflow.approval', 'agentflow.teg'))
""")


def test_local_workflows_without_cryptography(tmp_path):
    # Reuse behavioral tests: simulated planners/executors, persistence, resume,
    # progressive reads, native CLI defaults, dependency handoffs and real gates.
    cases = [
        "test_foundation.py::test_init_idempotent_and_preserves_existing",
        "test_foundation.py::test_doctor_exit_codes",
        "test_local_entrypoint.py::test_run_reads_backlog_without_authentication_and_preserves_source",
        "test_invoker_agent_selection.py",
        "test_master_plan_context.py",
        "test_handoff.py",
        "test_native_cli_default_selection.py",
        "test_apl_codex_execution.py::test_plan_gate_acceptance_verifier_requires_independent_gate_and_persists_evidence",
    ]
    paths = [str(TESTS / name.split("::")[0]) + ("::" + name.split("::")[1] if "::" in name else "")
             for name in cases]
    fresh_process(tmp_path, f"""
sys.path.insert(0, {str(TESTS)!r})
import pytest
from pathlib import Path
# Do not discover the checkout's pythonpath = ["src"] configuration in wheel CI.
Path('pytest-worker.ini').write_text('[pytest]' + chr(10), encoding='utf-8')
status = pytest.main(['-q', '-c', 'pytest-worker.ini', '--rootdir', '.',
                     '-p', 'no:cacheprovider', '--basetemp', 'workflow-tests', *{paths!r}])
assert status == 0, status
assert not attempts, attempts
assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules
               for prefix in ('agentflow.security', 'agentflow.approval', 'agentflow.teg'))
""", timeout=180)


def test_explicit_advanced_api_reports_missing_dependency_and_local_still_works(tmp_path):
    fresh_process(tmp_path, """
import importlib
module = importlib.import_module('agentflow.approval')
try:
    module.WebAuthnCredential
except ImportError as exc:
    assert 'requires cryptography' in str(exc), str(exc)
else:
    raise AssertionError('advanced dependency must be reported')
assert attempts
attempts.clear()
from agentflow.cli import main
assert main(['profile', 'status']) == 0
assert not attempts
""")


def test_security_contracts_stay_available_until_crypto_is_used(tmp_path):
    fresh_process(tmp_path, """
from agentflow.security import SecurityProfile, Ed25519LocalGrantSigner
assert SecurityProfile.LOCAL_SUPERVISED.value == 'LOCAL_SUPERVISED'
assert not attempts
try:
    Ed25519LocalGrantSigner('test-key', None)
except ModuleNotFoundError as exc:
    assert exc.name == 'cryptography'
else:
    raise AssertionError('signer must not work without cryptography')
assert attempts
""")


def test_public_exports_keep_identity_aliases_and_star_imports(tmp_path):
    fixture = json.loads((TESTS / "facade_exports.json").read_text(encoding="utf-8"))
    fresh_process(tmp_path, f"""
import importlib
for package, exports in {fixture!r}.items():
    module = importlib.import_module('agentflow.' + package)
    assert set(module.__all__) == set(exports)
    namespace = {{}}
    exec('from agentflow.' + package + ' import *', namespace)
    for name, reference in exports.items():
        owner, qualname = reference.split(':')
        expected = importlib.import_module(owner)
        for part in qualname.split('.'):
            expected = getattr(expected, part)
        assert getattr(module, name) is expected, (package, name)
        assert namespace[name] is expected
        assert vars(module)[name] is expected
""", block_crypto=False)
