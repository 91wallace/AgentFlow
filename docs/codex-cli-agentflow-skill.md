# AgentFlow Skill for Codex CLI

The bundled Skill follows Codex's `SKILL.md` format and project/user locations: `.agents/skills/` for a workspace and `~/.agents/skills/` for an explicit global install. The CLI installer does not edit Codex `config.toml`.

## Install and update

From an environment where AgentFlow is installed:

```powershell
agentflow codex-skill install --path .
```

For all projects, opt in explicitly:

```powershell
agentflow codex-skill install --global
```

Update a managed, unmodified installation with `--update`. Unmanaged destinations and locally edited Skill files are preserved and cause a clear error. The installer records the Python interpreter used to invoke AgentFlow beside the Skill so Codex can call the CLI even when its executable is not on `PATH`. The runtime file is machine-local and ignored by Git.

Remove by deleting only the AgentFlow Skill directory (`.agents/skills/agentflow` in the workspace, or `~/.agents/skills/agentflow` for global installation). Start a new Codex session after install/update so it discovers the Skill.

In Codex CLI, invoke explicitly with `$agentflow` (or select it from `/skills`), for example: `$agentflow use the current workspace to implement <objective>`. The installed `SKILL.md` format and locations match the Codex CLI skill documentation: [installation](https://developers.openai.com/api/reference/cli) and [skill format](https://developers.openai.com/plugins/concepts/skills).

## Invocation and lifecycle

When the operator explicitly asks Codex to use AgentFlow, the Skill calls:

```text
agentflow run "<objective>" --path "<workspace>" --allow-unconfined-network
```

The network flag belongs on the invocation only after the operator explicitly authorizes that scope for this work. Example after that one-time authorization:

```powershell
agentflow run "Add a CSV export option with tests" --path "C:\projects\sample" --allow-unconfined-network
```

`--allow-unconfined-network` is required because the current local validator cannot enforce network isolation. The Skill must explain this scope and obtain the operator's one-time explicit intent before adding the flag. The objective command creates or recovers an initial plan, then creates an idempotently identified APL cycle and delegates execution to the existing `apl run` controller, Codex executor, acceptance verifier, SQLite ledger, and IFR. Planning inspects safe workspace filenames and technology markers, requests a constrained task DAG from the existing Codex planner, and derives the Python pytest gate. Existing `spec.toml` and `tasks.toml` are never overwritten; a different objective is stored under `.agentflow/objective-plans/`. Identical objective/workspace/plan requests reuse the same operation identifier; use an explicit new `--operation-id` only when intentionally starting a distinct run.

The process emits JSON events including `cycle_persisted` and `run_persisted`. Use the run ID with:

```text
agentflow apl run-status --path "<workspace>" --run-id <run-id>
agentflow apl run-cancel --path "<workspace>" --run-id <run-id>
agentflow apl run-resume --path "<workspace>" --run-id <run-id>
```

Codex children inherit `AGENTFLOW_MANAGED_EXECUTION=1`; the public objective command refuses nested AgentFlow orchestration. This is a recursion guard, not an OS security boundary. AgentFlow does not add Codex sandbox settings or claim that the workspace path isolates process filesystem access.

## Current prerequisite and limits

Automatic initial planning discovers Python pytest or unittest, and known npm scripts (`test`, `test:unit`, `lint`, `typecheck`, `type-check`, `build`, `check`) that are actually declared in `package.json` and have an available Node/npm runtime. It does not run project scripts during discovery. Configured scripts run later as pinned general project checks with their package-script definitions checked against the planned digest. React/Next/TypeScript projects use whichever supported scripts they declare; AgentFlow does not assume conventional scripts exist. Separately, every objective-specific acceptance criterion requires its own path-bounded artifact assertion (file exists, nonempty, valid JSON, or contains exact text), even when general project checks are available. This prevents a passing project suite or an executor's success statement from being treated as proof of each requested outcome. Other frameworks without supported project checks can still use these objective artifact assertions; otherwise planning stops before dispatch. Local validation cannot enforce network isolation; the Skill must never add `--allow-unconfined-network` without the operator's explicit one-time intent.

The planner receives the operator's objective plus a bounded inventory of safe filenames and detected technology markers; it does not read project file contents to formulate the initial plan. Its output is strictly decoded, limited to existing workspace scope roots, checked for valid dependencies and cycles, and combined with locally generated general project checks plus per-criterion artifact assertions. The plan is written without replacing existing TOMLs. A persisted planning manifest allows reuse of a completed plan after interruption; plan and cycle digests are checked before reuse.

The default duration, attempt, and estimated-cost values are operational limits; the cost figure is an estimate and not a provider-enforced financial ceiling. Native Codex configuration remains in effect. No real job was started while implementing this integration.
