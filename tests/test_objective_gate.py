import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def run_gate(root, *, checks, artifacts, criteria=("criterion-one",)):
    module_root = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "PYTHONPATH": str(module_root)}
    return subprocess.run([
        sys.executable, "-m", "agentflow.validation.objective_gate",
        "--gate-id", "gate-test", "--definition-revision", "1",
        "--snapshot-digest", "sha256:" + "a" * 64,
        "--criteria-json", json.dumps(list(criteria)),
        "--checks-json", json.dumps(checks),
        "--artifacts-json", json.dumps(artifacts),
    ], cwd=root, env=env, capture_output=True, text=True, timeout=30)


def unittest_check():
    executable = Path(sys.executable).resolve()
    return {"id": "python-unittest", "label": "Python unittest discovery",
            "executable": str(executable),
            "sha256": "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
            "arguments": ["-m", "unittest", "discover", "-v"],
            "version": "stdlib", "package_directory": "",
            "script_name": "", "script_sha256": ""}


def test_gate_executes_real_unittest_and_emits_bound_structured_evidence(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n def test_ok(self):\n  self.assertEqual(2 + 3, 5)\n",
        encoding="utf-8")
    artifacts = [{"criterion_id": "criterion-one", "path": "test_sample.py",
                  "kind": "contains", "expected": "assertEqual(2 + 3, 5)"}]
    result = run_gate(tmp_path, checks=[unittest_check()], artifacts=artifacts)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["gate_id"] == "gate-test"
    assert report["snapshot_digest"] == "sha256:" + "a" * 64
    assert report["evidence"] == [{"criterion_id": "criterion-one", "status": "declared",
                                    "summary": "general project checks passed: python-unittest; "
                                               "objective criterion evidence: artifact contains passed: test_sample.py"}]


def test_gate_failing_project_check_is_not_reported_as_success(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n def test_no(self):\n  self.assertEqual(2, 3)\n",
        encoding="utf-8")
    result = run_gate(tmp_path, checks=[unittest_check()], artifacts=[
        {"criterion_id": "criterion-one", "path": "test_sample.py",
         "kind": "exists", "expected": ""}])
    assert result.returncode != 0
    assert result.stdout == ""


def test_general_project_check_cannot_stand_in_for_objective_criterion(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n def test_ok(self):\n  self.assertEqual(2, 2)\n",
        encoding="utf-8")
    result = run_gate(tmp_path, checks=[unittest_check()], artifacts=[])
    assert result.returncode == 64
    assert "objective_artifact_check_required_for_every_criterion" in result.stderr
    assert result.stdout == ""


def test_artifact_only_gate_checks_output_content_without_executor_claims(tmp_path):
    (tmp_path / "report.json").write_text('{"status":"ready"}', encoding="utf-8")
    checks = [{"criterion_id": "criterion-one", "path": "report.json",
               "kind": "json", "expected": ""}]
    result = run_gate(tmp_path, checks=[], artifacts=checks)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)["evidence"][0]
    assert evidence["status"] == "declared"
    assert "objective criterion evidence: artifact json passed" in evidence["summary"]
    (tmp_path / "report.json").write_text("not-json", encoding="utf-8")
    failed = run_gate(tmp_path, checks=[], artifacts=checks)
    assert failed.returncode != 0
    assert failed.stdout == ""


def test_gate_rejects_inline_shell_or_code_arguments(tmp_path):
    check = unittest_check()
    check["arguments"] = ["-c", "print('should not run')"]
    artifacts = [{"criterion_id": "criterion-one", "path": "script-ran.txt",
                  "kind": "exists", "expected": ""}]
    result = run_gate(tmp_path, checks=[check], artifacts=artifacts)
    assert result.returncode == 64
    assert "project_check_arguments_invalid" in result.stderr


def test_npm_script_must_match_the_planned_definition_before_execution(tmp_path):
    package = {"scripts": {"test": "synthetic test action"}}
    (tmp_path / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (tmp_path / "fake_npm.py").write_text(
        "import json, pathlib, sys\n"
        "assert sys.argv[-2:] == ['run', 'test']\n"
        "scripts=json.loads(pathlib.Path('package.json').read_text())['scripts']\n"
        "assert scripts['test'] == 'synthetic test action'\n"
        "pathlib.Path('script-ran.txt').write_text('ran')\n", encoding="utf-8")
    executable = Path(sys.executable).resolve()
    script_digest = "sha256:" + hashlib.sha256(package["scripts"]["test"].encode()).hexdigest()
    check = {"id": "npm-test", "label": "npm run test", "executable": str(executable),
             "sha256": "sha256:" + hashlib.sha256(executable.read_bytes()).hexdigest(),
             "arguments": ["fake_npm.py", "--ignore-scripts", "run", "test"],
             "version": "synthetic", "package_directory": "",
             "script_name": "test", "script_sha256": script_digest}
    (tmp_path / "package.json").write_text(json.dumps(package), encoding="utf-8")
    artifacts = [{"criterion_id": "criterion-one", "path": "script-ran.txt",
                  "kind": "exists", "expected": ""}]
    result = run_gate(tmp_path, checks=[check], artifacts=artifacts)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "script-ran.txt").read_text(encoding="utf-8") == "ran"

    (tmp_path / "script-ran.txt").unlink()
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "changed after planning"}}), encoding="utf-8")
    changed = run_gate(tmp_path, checks=[check], artifacts=artifacts)
    assert changed.returncode == 65
    assert not (tmp_path / "script-ran.txt").exists()
