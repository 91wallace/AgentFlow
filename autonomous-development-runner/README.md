# Autonomous Development Runner (ADR) — experimental v0.1

Generic, local task execution with Codex CLI and persistent Git-repository state. **Not a hardened unattended production service.** Requires Python 3.10+, Git and an authenticated Codex CLI installed on PATH. No API keys are bundled.

## Installation

Extract this archive anywhere, then from its extracted directory:

```bash
python -m pip install .
```

Inside **any Git repository**:

```bash
adr init
```

Edit `.adr/SPEC.md`, `.adr/PLAN.md`, `.adr/AGENT.md` and `.adr/tasks.json`. Replace the sample task. For every task, supply explicit `description`, `acceptance`, `depends_on`, and `checks` (each check is an argument array, such as `["python", "-m", "pytest", "-q"]`). **Verification commands are trusted local commands**: review them before execution. Commit the approved plan to Git.

```bash
adr status
adr run --execute
adr resume --execute
```

Run starts only with a clean Git worktree by default. `--allow-dirty` permits continuing with uncommitted changes, including after an interrupted run. `--max-tasks`, `--max-attempts`, `--timeout`, and `--check-timeout` are configurable. Run and resume are equivalent: both recover recorded active work and select the next eligible task. Each run invokes `codex exec --full-auto` once per task and runs the task's verification commands. No automatic Git commits or force operations are performed.

## Safety and limitations

- **Codex may edit source files**. Use a dedicated branch, local sandbox and backups. `--full-auto` grants workspace write access and is not suitable for untrusted repositories without isolation.
- `tasks.json` is protected against modification during each agent invocation by a hash check, but this is not a security sandbox. `SPEC.md`, `PLAN.md`, `AGENT.md` are not cryptographically protected. Human review is needed for high-risk tasks.
- A task is `done` only if Codex exits successfully and all configured checks return success. Checks must meaningfully verify acceptance; this runner cannot prove semantic correctness on its own.
- After a crash, the active task is retried; source edits may already exist. Prefer idempotent tasks and use `--allow-dirty` after inspecting changes. Do not blindly reset the worktree.
- Blocked tasks do not prevent unrelated eligible tasks from proceeding. Dependent tasks remain pending. Retry blocked tasks only after diagnosis and manual reset to `pending` (and reset its attempt counter in state.json).
- No automatic reboot/relaunch, background daemon, remote deployment, cost budget, commit management, or multi-process locking. Only one runner may operate in a repository at a time. Continuous work is limited by CLI process lifetime and configured task count.
- Git-tracked `.adr` state/log files will change while running. Use Git to audit them; do not store secrets in plans.

## Example task

```json
{
  "version": 1,
  "tasks": [
    {
      "id": "T001",
      "title": "Add health endpoint",
      "description": "Implement GET /health returning HTTP 200 and a JSON status",
      "depends_on": [],
      "acceptance": ["GET /health returns 200", "Response JSON has status=ok"],
      "checks": [["python", "-m", "pytest", "tests/test_health.py", "-q"]],
      "status": "pending"
    }
  ]
}
```

## Recommended workflow

1. Create a dedicated branch and write/approve the project specification.
2. Define small, independent tasks and deterministic test commands.
3. Run one task initially: `adr run --execute --max-tasks 1`.
4. Review code, tests and `.adr/events.log`; only then increase task limits.
5. Resume after interruptions with `adr resume --execute --allow-dirty` after checking the worktree.

**Important:** This is a generalized starter implementation, not a fully autonomous production-grade orchestrator. It has not been integration-tested against a live Codex CLI in this environment.
