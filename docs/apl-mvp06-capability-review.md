# MVP-06 — capability review

## Verdict

**BLOCKED: real semantic planner integration is not available.** No real planner is registered or selected by the APL. The CLI requires `--planner deterministic`; that adapter emits a no-change baseline and explicitly makes no semantic inference.

## Mechanisms checked

- Local executable found: `C:\Users\criarte\AppData\Local\Programs\OpenAI\Codex\bin\codex.exe`.
- Read-only probes: `codex --version` reported `codex-cli 0.167.0`; `codex exec --help` advertises model-generated shell command execution and sandbox choices. No agent turn was started and no Codex configuration or sandbox setting was changed.
- AgentFlow's `CodexAdapter` only supports version `0.166.0`, and its request contract is an execution adapter with process runtime. It is not a text-only inference interface. The current installed version therefore is not even accepted by that executor adapter.
- The Codex CLI probe exposes no verifiable per-request tool allowlist that proves shell, file writes, subagents, MCP, network effects, or credential access unavailable. A prompt asking the model to avoid tools cannot enforce those properties.
- AgentFlow local subprocesses run with the current user's environment and filesystem access. A read-only workspace option would not prove secrets are inaccessible or that no commands/subagents/network can occur. The task expressly prohibits activating/changing sandbox, so that is not a mitigation used here.

Official references reviewed (2026-11-08):

- [Codex CLI docs](https://developers.openai.com/codex/cli/)
- [Codex CLI command reference](https://developers.openai.com/codex/cli/reference/)
- [Agents API overview](https://developers.openai.com/api/docs/guides/agents-api/overview): managed Codex harness capability summary includes commands, code, MCP and subagent delegation.
- [Agents API multi-agent](https://developers.openai.com/api/docs/guides/agents-api/multi-agent): subagent orchestration capability and configuration.
- [Shell tool](https://developers.openai.com/api/docs/guides/tools-shell): arbitrary shell is a tool capability and execution should rely on sandbox and allow/block controls.

## Implemented safely

- APL remains adapter-based. `UnavailableSemanticPlanner` fails closed.
- `proposal_from_mapping` strictly rejects unknown keys and command/tool-shaped fields in the structured response contract. Every resulting `PlanProposal` must still pass `validate_proposal` before persistence.
- `agentflow apl replan` requires explicit `--planner deterministic`. There is no `semantic` CLI choice; argparse rejects it. The deterministic adapter is not described as AI/semantic and proposes no changes by default.
- Simulated planner exceptions, timeout and cancellation do not persist proposals. Tests do not use credentials or contact external providers.

These checks validate the APL's input contract only. They are not a security boundary against a future adapter process. A future semantic planner needs a separately authorized inference mechanism with an enforceable tool-free request/session, no shell/filesystem/runtime tools, no subagents/MCP, no executor credentials in its environment, bounded network access to inference only, strict output parsing, timeout/cancellation containment, and independent evidence of those controls. Until such a mechanism is selected and verified, real semantic planner remains blocked.
