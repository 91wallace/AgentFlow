"""M0 ratchet, using the installed package tree as well as editable checkouts."""

import json
from pathlib import Path

import agentflow

from architecture_audit import audit, baseline_difference, scan_package


def package(tmp_path, sources):
    root = tmp_path / "agentflow"
    for relative, source in sources.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return root


def test_current_architecture_matches_reviewed_debt_baseline():
    # CI runs tests from outside the checkout against a wheel. Don't assume src/.
    root = Path(agentflow.__file__).resolve().parent
    baseline = json.loads(Path(__file__).with_name("architecture_baseline.json").read_text(encoding="utf-8"))
    assert baseline["schema_version"] == 1
    report = audit(root)
    assert not baseline_difference(report, baseline), "\n".join(baseline_difference(report, baseline))


def test_new_edge_inside_existing_cycle_is_rejected(tmp_path):
    root = package(tmp_path, {"apl/first.py": "from agentflow.security.profile import Profile\n",
                              "security/profile.py": "from agentflow.apl.first import First\n"})
    baseline = audit(root)
    (root / "apl" / "second.py").write_text("from agentflow.security.profile import Profile\n")
    assert any("New violations" in message for message in baseline_difference(audit(root), baseline))


def test_new_cycle_is_rejected_and_fixed_debt_must_be_removed(tmp_path):
    root = package(tmp_path, {"state/contracts.py": "", "validation/contracts.py": "",
                              "state/store.py": "from agentflow.validation.contracts import Result\n",
                              "validation/scheduler.py": "from agentflow.state.contracts import Error\n"})
    baseline = audit(root)
    (root / "validation" / "scheduler.py").write_text("")
    reduced = audit(root)
    assert any("Remove stale baseline" in message for message in baseline_difference(reduced, baseline))
    assert not baseline_difference(reduced, reduced)
    (root / "validation" / "scheduler.py").write_text("from agentflow.state.contracts import Error\n")
    assert any("New violations" in message for message in baseline_difference(audit(root), reduced))


def test_type_checking_aliases_are_separate_but_function_imports_count(tmp_path):
    root = package(tmp_path, {"apl/example.py": '''
import typing as t
from typing import TYPE_CHECKING as TC
if TC or t.TYPE_CHECKING:
    from agentflow.security.profile import Profile
if not TC:
    def action():
        from agentflow.approval.persistence import BrokerStore
else:
    from agentflow.state.validation import ValidationStateStore
'''})
    imports, _, _ = scan_package(root)
    security = next(edge for edge in imports if edge.target == "agentflow.security.profile")
    approval = next(edge for edge in imports if edge.target == "agentflow.approval.persistence")
    state = next(edge for edge in imports if edge.target == "agentflow.state.validation")
    assert security.kind == state.kind == "type-only"
    assert approval.kind == "runtime" and approval.deferred
    assert any("local-to-advanced" in value for value in audit(root)["violations"])


def test_relative_import_and_reexport_cannot_hide_native_adapter(tmp_path):
    root = package(tmp_path, {"agents/__init__.py": "from .codex import CodexAdapter as Native\n",
                              "agents/codex.py": "class CodexAdapter: pass\n",
                              "core/service.py": "from ..agents import Native as Selected\n"})
    report = audit(root)
    assert "concrete-agent|agentflow.core.service|agentflow.agents.codex" in report["violations"]


def test_literal_dynamic_import_counts_and_unresolved_import_is_visible(tmp_path):
    root = package(tmp_path, {"apl/example.py": '''
from importlib import import_module as load
def action(name):
    load("agentflow.agents.codex")
    load(name)
''', "agents/codex.py": ""})
    report = audit(root)
    assert "concrete-agent|agentflow.apl.example|agentflow.agents.codex" in report["violations"]
    assert report["unresolved_dynamic_imports"] == ["agentflow.apl.example: load(name)"]


def test_contracts_cannot_import_storage_or_io(tmp_path):
    root = package(tmp_path, {"apl/contracts.py": "import sqlite3\nfrom agentflow.state.store import Store\n",
                              "state/store.py": ""})
    violations = audit(root)["violations"]
    assert "contract-io|agentflow.apl.contracts|sqlite3" in violations
    assert "contract-to-implementation|agentflow.apl.contracts|agentflow.state.store" in violations


def test_wildcards_in_coordinator_are_not_an_escape_hatch(tmp_path):
    root = package(tmp_path, {"apl/example.py": "from agentflow.agents import *\n", "agents/__init__.py": ""})
    assert "wildcard-import|agentflow.apl.example|agentflow.agents" in audit(root)["violations"]


def test_new_module_cannot_hide_crypto_outside_advanced_stack(tmp_path):
    root = package(tmp_path, {"__init__.py": "from agentflow.bootstrap import setup\n",
                              "bootstrap.py": "import cryptography\n"})
    assert "local-crypto|agentflow.bootstrap|cryptography" in audit(root)["violations"]


def test_same_baseline_applies_outside_checkout_layout(tmp_path):
    import shutil
    source = Path(agentflow.__file__).resolve().parent
    installed = tmp_path / "site-packages" / "agentflow"
    shutil.copytree(source, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    report = audit(installed)
    baseline = json.loads(Path(__file__).with_name("architecture_baseline.json").read_text(encoding="utf-8"))
    assert not baseline_difference(report, baseline)
