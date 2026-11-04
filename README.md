# AgentFlow

### Autonomous AI Development Orchestrator

Plan. Build. Validate. Resume.

AgentFlow is an open-source, project-agnostic development orchestration tool designed to help AI coding agents execute structured implementation plans with persistent progress tracking, automated validation, and interruption recovery.

Instead of relying on a single long-running AI conversation, AgentFlow organizes development into small, verifiable tasks and preserves execution state directly in the repository.

The goal is simple: define the plan once, then let the agent work through it systematically.

#### Key Features

- Autonomous Execution — Execute development tasks sequentially without requiring approval after every step.
- Persistent State — Track completed, pending, running, and blocked tasks across sessions.
- Interruption Recovery — Resume development from the last recorded checkpoint.
- Spec-Driven Development — Guide implementation through structured specifications and task definitions.
- Automated Validation — Run configurable tests and verification commands before accepting task completion.
- Dependency Management — Execute tasks according to their dependencies.
- Failure Handling — Limit retries, record errors, and prevent endless execution loops.
- Git Integration — Use version control to track changes and preserve verified progress.
- Project Independence — Apply the same development workflow to different Git repositories.

#### How It Works

AgentFlow follows a continuous development cycle:

        Project Specification
                 |
                 v
          Task Selection
                 |
                 v
         AI Agent Execution
                 |
                 v
        Automated Validation
                 |
          +------+------+
          |             |
        PASS           FAIL
          |             |
          v             v
      Checkpoint     Diagnose
          |          and Retry
          v             |
      Update State <----+
          |
          v
       Next Task
          |
          v
    Project Completion

Each task must satisfy its configured acceptance criteria before being marked as completed.

When an execution is interrupted, AgentFlow uses the persisted task state and repository information to determine how to continue.

### Getting Started

#### Requirements

- Python 3.10 or newer (subject to compatibility validation)
- Git
- Codex CLI installed and authenticated
- A Git repository containing the project

### Installation

#### Clone the repository:

git clone https://github.com/91wallace/AgentFlow.git
cd AgentFlow

Install the package:

python -m pip install .

#### Initialize a Project

Navigate to the Git repository you want to automate:

cd your-project
adr init

AgentFlow initializes the ".adr/" directory containing the development specification, task plan, execution state, and configuration.

#### Configure the Development Plan

Define the project requirements, implementation tasks, dependencies, validation commands, and acceptance criteria.

AgentFlow uses these definitions to determine what to implement and when a task can be considered complete.

#### Run Autonomous Development

adr run --execute

AgentFlow selects eligible tasks, invokes the configured coding agent, performs validation, and records progress.

Check Progress

adr status

Resume Development

adr resume --execute

AgentFlow reloads the persisted execution state and attempts to continue unfinished work.

### Project Structure

your-project/
├── .adr/
│   ├── SPEC.md
│   ├── PLAN.md
│   ├── TASKS.json
│   ├── STATE.json
│   └── LOG.md
├── AGENTS.md
├── src/
└── tests/

SPEC.md — Defines the project requirements, architectural constraints, and expected behavior.

PLAN.md — Documents implementation milestones and execution strategy.

TASKS.json — Stores structured tasks, dependencies, and validation criteria.

STATE.json — Persists task execution status and recovery information.

LOG.md — Records execution history, validation results, and relevant decisions.

AGENTS.md — Provides instructions and constraints for AI coding agents.

Design Principles

1. Specifications First — Implementation follows an explicit, reviewable plan.
2. Persistence Over Conversation Memory — Progress survives agent sessions.
3. Verification Before Completion — Tasks require evidence of successful validation.
4. Incremental Development — Work is divided into manageable units.
5. Controlled Autonomy — Agents operate within predefined permissions and constraints.
6. Recoverability — Interrupted tasks can be inspected and resumed.
7. Project Independence — The orchestration system remains separate from project-specific requirements.

#### Safety and Reliability

Autonomous code execution requires appropriate safeguards.

AgentFlow is designed around bounded retries, task validation, Git checkpoints, and explicit failure states.

For initial adoption, use a dedicated Git branch and a controlled development environment. Avoid exposing production credentials or granting unnecessary system permissions.

A successful task execution does not automatically guarantee that the resulting software is secure or production-ready.

#### Current Status

Early development — v0.1 prototype

The initial implementation targets Python, Git, and Codex CLI.

Basic initialization, task-state management, and orchestration components have been prepared. Full end-to-end Codex integration, interruption recovery, and unattended execution reliability still require further testing.

#### Roadmap

- [ ] Validate end-to-end Codex CLI execution.
- [ ] Strengthen atomic checkpoints and crash recovery.
- [ ] Add independent acceptance-criteria verification.
- [ ] Improve execution logs and diagnostics.
- [ ] Add automated tests and CI.
- [ ] Support configurable agent adapters.
- [ ] Introduce additional coding-agent integrations.
- [ ] Package stable cross-platform releases.

#### License

AgentFlow is intended to be distributed under the Apache License 2.0.

See the "LICENSE" file once added to the repository.

---

AgentFlow — Plan. Build. Validate. Resume.

Autonomous development with persistent progress and verifiable execution.
