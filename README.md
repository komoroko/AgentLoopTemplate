# AgentLoopTemplate

**English** | [日本語](README.ja.md)

A Claude Code template for developing software **Human on the Loop**.
A coding agent performs the work, produces the deliverables, and self-tests from requirements through testing,
while **humans only approve/decide at the "gate" on each phase boundary**.

## Concept

```mermaid
flowchart TD
    brief["brief<br/>(human writes the vision)"]:::human
    req["/req<br/>requirements"]:::agent
    g1{"① freeze requirements"}:::gate
    design["/design<br/>design"]:::agent
    g2{"② technical choices"}:::gate
    tasks["/tasks<br/>task breakdown"]:::agent
    g3{"③ task plan"}:::gate
    build["/build<br/>implementation loop"]:::agent
    g4{"④ implementation done"}:::gate
    verify["/verify<br/>verification"]:::agent
    g5{"⑤ release decision"}:::gate
    done["done"]:::human

    subgraph TASKS["task set (multiple, dependency graph DAG)"]
        direction TD
        T1["foundation T-001"]:::task
        T2["leaf T-002"]:::task
        T3["leaf T-003"]:::task
        Tn["leaf T-00n…"]:::task
        TI["integration T-0xx"]:::task
        T1 --> T2
        T1 --> T3
        T1 --> Tn
        T2 --> TI
        T3 --> TI
        Tn --> TI
    end

    brief --> req --> g1 --> design --> g2 --> tasks
    tasks -->|generates| T1
    TI --> g3
    g3 -->|parallel consumption (max 3)| build
    build --> g4 --> verify --> g5 --> done

    req -. roll back /revise .- build
    design -. roll back /revise .- build
    design -. roll back /revise .- verify

    classDef agent fill:#cfe8ff,stroke:#3b82f6,color:#06325e;
    classDef gate fill:#ffe9c7,stroke:#f59e0b,color:#7a4a00;
    classDef human fill:#d7f5dd,stroke:#22a04b,color:#0b3d1d;
    classDef task fill:#eeeeff,stroke:#8888aa,color:#222255;
    linkStyle 18,19,20 stroke:#ee5544,color:#ee5544,stroke-width:1.5px;
```

Legend: 🟦 blue = phases the agent runs / 🟧 orange = gates ①–⑤ the human approves / 🟩 green = points of human involvement (writing the vision, the completion decision) / 🟪 light purple = tasks (**multiple**, a dependency-graph DAG; foundation → parallel leaves → integration). **Moves top to bottom** (cannot advance while the prerequisite gate is unapproved); `/tasks` generates the task set → gate ③ approval → `/build` consumes in parallel (max 3). The red dotted lines = roll back upstream via `/revise` (from build/verify to design/req; resets the gates from the target onward to `pending` in a chain).

Only the human opens each gate. Rewinding approval (`/revise`) is also at the human's discretion.

## Design principles

This template is itself a multi-agent orchestration, and its own machinery follows three design axes:

- **Architecture** — the simplest structure that meets the need: `build_loop.py` is a **deterministic DAG** (controllable, debuggable), and each phase is delegated to a dedicated subagent to separate concerns.
- **Context** — kept minimal: SSOT files (`state.md` / `tasks.yaml`) hold the truth, subagents read only what they need, failures are **summarized, not dumped**, and oversized logs rotate (see "Context budget" in `CLAUDE.md`).
- **Tools** — minimal, scoped subagent grants; the quality gate has a **retry cap** (`config.yaml`); `summarize_failure()` returns compact, actionable failures.

## Setup

Prerequisites: WSL / Linux / macOS and `make` (not Windows-native).

1. **Copy this template** into a new product repository.
2. `git init` and create a work branch (e.g. `git switch -c build/<product>`). Implement on the work branch, not directly on main.
3. Install tools and sync dependencies:
   ```bash
   make install   # install the uv / pnpm binaries
   make setup     # uv sync (sync dev dependencies, generate uv.lock)
   # if using the frontend: cd frontend && pnpm install
   ```
4. Sanity check: `make check` (lint/format/type)・`make test` (pytest)・`make test-tools` (self-tests of the deterministic orchestrator in `scripts/agentloop/`).
5. Fill in the project name: `name` in `pyproject.toml` (initial `project-name`) and `project`・`branch` in `.agentloop/state.md`.

## Usage

1. Write a few lines on "what you want to build" in `docs/00-product-brief.md` (the only starting point a human writes).
2. Run the following in order. Each command stops at the end to ask for the human's approval.

   | Step | Command | What happens | Your (the human's) role |
   |------|----------|--------------|--------------------|
   | requirements | `/req`    | structure requirements by sounding out | ① freeze requirements |
   | design | `/design` | implementation approach + technical-choice options | ② decide/approve technical choices |
   | breakdown | `/tasks`  | generate task tickets with a test approach | ③ approve the task plan |
   | implementation | `/build`  | autonomous implementation in a loop (test-green condition) | ④ review and approve implementation completion |
   | verification | `/verify` | run functional + non-functional tests | ⑤ decide on release |

3. If an upstream (requirements/design) defect comes to light during implementation, you can roll back with **`/revise <phase>`** (resets the gates from the target onward to `pending` in a chain, and reconciles task impact with `dag.py --impacted`). Rewinding approval is also at the human's discretion.
4. Check the current phase, gate approval status, and task progress anytime with `/status`. Generate the **big picture (dependency diagram)** of tasks with `uv run python scripts/agentloop/dag.py --mermaid`, which renders directly in GitHub/VS Code/Markdown (status color-coding, critical-path emphasis).

> **It does not stall during approval waits**: a notification fires on reaching a gate, and while waiting for approval the agent
> pulls forward outcome-independent work (environment setup, investigation, test-harness setup, etc.).
> Since it does no work that pre-empts the approval outcome, the gate's strictness is preserved. Speculative work is provisional and throwaway-by-default and is
> recorded in the "speculative work log" of `.agentloop/state.md`, so the human can decide to adopt or discard it.

### Running the implementation phase autonomously

The implementation loop has two modes. The behavior (DoD, parallelism/merge rules) is identical. Below is a summary; the canon for operation is `.claude/commands/build.md` (procedure) and `CLAUDE.md` (rules):

**A. Deterministic execution (recommended) — `make build-loop`**
An orchestrator that drives scheduling deterministically in code (`scripts/agentloop/build_loop.py`). It decides **which tasks, at what parallelism, in what merge order, and when to stop** deterministically from `.agentloop/config.yaml` and `tasks.yaml`, not relying on LLM discretion.

```
make build-loop                  # run
make build-loop ARGS=--dry-run   # check just the control flow without calling claude/git
```

**B. Interactive loop — `/loop /build`**
An alternative that runs the loop in conversation without the orchestrator.

- A task is complete only after **passing all quality gates**: automated tests green → `/simplify` (cleanup) → `/code-review` (bug fixes) → `make check` (fix lint/format/typecheck until errors are gone).
- **Parallel tasks run in isolation**: independent leaf tasks are implemented in their own branch/working directory with `git worktree`, **up to 3 in parallel** (`max_parallel` in `config.yaml`), and merged into the work branch sequentially in ascending id order when done. Foundation tasks are finalized first on the work branch.
- An unsolvable task becomes `blocked`; an upstream defect becomes `needs-revision`, **escalated to the human**, and the loop stops.
- **The determinism boundary**: control flow, parallelism, merge, gate decision, and stopping are deterministic in code. Only each task's implementation code content is LLM-derived and non-deterministic, absorbed by "retry until the gate passes, else blocked". **The orchestrator does not touch `gates.build`** (only the human opens a gate).

> **Assumed stack**: the bundled `makefile` provides `make test` (pytest) and `make check` (ruff/format/mypy/tsc together). `make check` bundles `make pre-commit` (commit stage) and `make pre-push` (format/mypy/tsc). If copied into a project without `make`, substitute your own test/check commands.

### Security review

Ensured in three layers: **gitleaks** (mechanically prevents committing secrets at pre-commit; exclude false positives with `.gitleaksignore`) / **`/security-review`** mandatory at implementation completion / **`/security-review` + `make audit`** (dependency vulnerability audit) mandatory in `/verify`.

### GitHub Issues integration (optional)

If you want to make tasks visible to the team/stakeholders, you can **one-way-mirror** `tasks.yaml` to GitHub Issues (`make issue-sync`).

- **Off by default.** Enable with `github.enabled: true` in `.agentloop/config.yaml`. Requires the `gh` CLI and a GitHub remote; auto-skips if absent (does not break offline / right after copying).
- One issue per task T-NNN. The issue number is not written to tasks.yaml; matching is by label + title prefix. `done` is closed.
- **Tell them apart by the labels applied**: `kind:*` (kind) / `status:*` (status) / `phase:*` (phase: requirements/design/build/verify) / `req:*` (covered requirement). The labels used are **auto-created (provisioned)** with `gh label create --force`, so it does not fail on the first run even in a repo with no labels.
- **One-way only**: `tasks.yaml` is always the SSOT. Edits on the Issues side are not read back (preserving deterministic, offline operation). Check just the plan with `make issue-sync ARGS=--dry-run`.
- Writing issues is an outward-facing operation, so the `github.enabled: true` opt-in serves as consent.

## Layout

| Path | Role |
|------|------|
| `.agentloop/state.md` | SSOT for phase, gates, logs |
| `.agentloop/tasks.yaml` | machine-readable SSOT of the task graph (DAG) |
| `.agentloop/config.yaml` | source of knobs for deterministic execution (parallelism, retry, worktree, gate enforcement) |
| `scripts/agentloop/` | deterministic orchestration (`dag.py` / `build_loop.py` / `gate_guard.py`). Product scripts go directly under `scripts/` |
| `CLAUDE.md` | agent operating rules and gate rules |
| `.claude/commands/` | entry points for each phase (`/req` – `/status`) |
| `.claude/agents/` | specialized subagents (requirements/design/implementation) |
| `docs/` | phase deliverables (requirements, design, ADR, task tickets, test plan) |

## Claude Code features used

- **plan mode + ExitPlanMode** — the approval gate for the thinking phase
- **AskUserQuestion** — human decisions such as technical choices
- **/loop** — autonomous consumption of implementation tasks (interactive mode)
- **the deterministic orchestrator (`make build-loop`)** — drives scheduling, parallelism, merge, and gate decisions deterministically in code
- **PreToolUse hook (`gate_guard.py`)** — mechanically denies editing a deliverable while the prerequisite gate is unapproved
- **git worktree** — isolated execution of parallel tasks
- **subagent** — specialization and context isolation per phase
- **slash command** — standardizing each phase
- **/schedule (optional)** — periodic progress checks for long-running loops
