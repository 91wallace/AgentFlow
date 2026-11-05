"""Read-only dependency checks with injectable process probe."""

from dataclasses import dataclass
import shutil
import subprocess
import sys
from typing import Protocol

from agentflow.config import Config


class Probe(Protocol):
    def __call__(self, executable: str, arguments: tuple[str, ...]) -> tuple[str, str]: ...


@dataclass(frozen=True)
class Check:
    name: str
    required: bool
    status: str
    detail: str


def subprocess_probe(executable: str, arguments: tuple[str, ...]) -> tuple[str, str]:
    found = shutil.which(executable)
    if found is None:
        return "missing", "não encontrado no PATH"
    try:
        result = subprocess.run([found, *arguments], capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", str(exc)
    detail = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode:
        return "error", detail[0] if detail else f"código {result.returncode}"
    return "ok", detail[0] if detail else "disponível"


def diagnose(config: Config, probe: Probe = subprocess_probe,
             python_version: tuple[int, int] | None = None) -> list[Check]:
    version = python_version or sys.version_info[:2]
    checks = [Check("Python", True, "ok" if version >= (3, 11) else "error",
                    f"{version[0]}.{version[1]} (requer >=3.11)")]
    for name, executable, args, required in (
        ("Git", "git", ("--version",), True),
        ("Codex CLI", "codex", ("--version",), config.agent == "codex"),
        ("Docker", "docker", ("--version",), config.runtime == "docker"),
    ):
        status, detail = probe(executable, args)
        checks.append(Check(name, required, status, detail))
    return checks
