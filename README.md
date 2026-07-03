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
    g3 -->|"parallel consumption (max 3)"| build
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

1. **Copy this template** into a new product repository and `git init`.
2. Install tools and sync dependencies:
   ```bash
   make install   # install the uv / pnpm binaries (runs the official curl|sh installers;
                  # in a locked-down/offline environment, install uv and pnpm manually instead)
   make setup     # uv sync (sync dev dependencies, generate uv.lock)
   # if using the frontend: cd frontend && pnpm install
   ```
3. **Initialize the product** (idempotent):
   ```bash
   make init NAME=<product>   # optionally BRANCH=build/<product>
   ```
   This fills the placeholders (`name` in `pyproject.toml`; `project`/`branch`/`updated_at` in `.agentloop/state.md`), creates and switches to the work branch, and flips `gates.template_mode` off so the gate guard goes live. Implement on the work branch, not directly on main.
4. Sanity check: `make check` (lint/format/type)・`make test` (pytest; passes on the empty template)・`make test-tools` (self-tests of the deterministic orchestrator in `scripts/agentloop/`).

## Adopting into an existing repository (brownfield)

An ongoing repo doesn't get copied over — AgentLoop is **installed into it**, additively and
conflict-aware, from a checkout of this template:

```bash
# run from the template checkout; uv is the only prerequisite in the target
make adopt TARGET=../myrepo NAME=myrepo TEST_CMD="npm test" CHECK_CMD="npm run lint"
# preview first: make adopt TARGET=../myrepo NAME=myrepo ARGS=--dry-run
```

What lands where (idempotent; re-runs skip everything already present):

| Kind | Files | Behavior |
|------|-------|----------|
| copy | `.agentloop/`, `scripts/agentloop/`, `agentloop.mk`, `.claude/commands|agents`, docs scaffolds | **Existing files are never overwritten** (skipped and reported) |
| merge | `CLAUDE.md` | Template rules land in `.agentloop/CLAUDE.agentloop.md`; one `@`-import line is appended to your CLAUDE.md (once) |
| merge | `.claude/settings.json` | Missing permissions/hook entries appended; yours untouched |
| adapt | `.agentloop/config.yaml` | **`guard_paths` scoped to docs deliverables only** — pending gates never freeze your existing code; add code paths (e.g. `src/: tasks`) when ready. Quality-gate commands from `TEST_CMD`/`CHECK_CMD` |
| manual | your `makefile`, `.pre-commit-config.yaml` | Not touched — add one line `include agentloop.mk` (or run `make -f agentloop.mk build-loop`); gitleaks hook recommended |

Then, inside the adopted repo:

1. **`/onboard`** — surveys the existing codebase read-only and fills `docs/05-current-state.md`, the
   **persistent baseline**: architecture, module roles, reusable assets, conventions, links to your
   existing documents (kept in place, never converted), and implementation status including
   in-flight work. Existing behavior is **not** reverse-generated into requirements or done tasks —
   gates stay human-opened, and traceability (R-N) applies to each cycle's delta only. Any starting
   state maps in (the full table lives in `/onboard`):
   - **No documents at all** — the survey is code-driven, so it still succeeds; `/onboard` asks you
     for the few lines of intent code can't reveal (who it's for, non-goals) and writes them into
     the brief. No specification is reverse-written.
   - **Approved-equivalent requirements/design docs exist** — run `/req`/`/design` as a fast intake
     of them and open the gates; that approval *is* the mapping into this system.
   - **Implementation half-done** — the first cycle plans only the remaining delta, anchored by an
     **absorb task** that pins the existing partial code green before new work stacks on it
     (`/tasks`' brownfield note).
2. **Delta cycles** — each pass through `brief → /req → … → /verify` describes **one change**, with
   half-done work resumed as delta requirements. After the release decision, close the cycle:
   ```bash
   make cycle-close NAME=<slug>   # archives the cycle's docs to docs/archive/<date>-<slug>/,
                                  # restores fresh scaffolds, resets gates/phase for the next cycle
   ```
   `docs/00-product-brief.md` and `docs/05-current-state.md` persist across cycles (the baseline is
   updated, not archived). Closing a cycle is a human operation, like opening a gate.
3. **Upgrade / uninstall (any time)** — adoption records `.agentloop/adopt-manifest.yaml`: the
   template source/commit plus a hash of every installed file. Two manifest-driven commands build
   on it (adopt-only; a greenfield `make init` records no manifest). Both are hash-checked —
   **a file you edited since adopt is never overwritten or removed**; it is skipped and listed
   (`FORCE=1` overrides). Review with `git diff` afterwards and commit:
   ```bash
   # inside the adopted repo — refresh the template-owned tooling (scripts/agentloop/,
   # .claude/commands|agents, agentloop.mk, the imported rules). FROM is a git URL or local
   # path; without it the source recorded at adopt time is reused. REF = branch/tag, not a SHA.
   make -f agentloop.mk agentloop-upgrade FROM=https://github.com/you/AgentLoopTemplate.git

   # retract the adoption: everything adopt installed is removed while pristine; the CLAUDE.md
   # @import block and the merged settings.json entries are retracted too
   make -f agentloop.mk agentloop-uninstall ARGS=--dry-run
   ```
   Upgrade never touches repo-owned state (`config.yaml`, `state.md`, `tasks.yaml`, filled docs,
   your CLAUDE.md); uninstall removes it only while still unedited. And when `TEST_CMD`/`CHECK_CMD`
   are omitted at adopt time, commands detected from your build files (package.json,
   pyproject.toml, Cargo.toml, go.mod, makefile) are printed as suggestions — never auto-written.

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
4. Check the current phase, gate approval status, and task progress anytime with `/status`. Generate the **big picture (dependency diagram)** of tasks with `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --mermaid`, which renders directly in GitHub/VS Code/Markdown (status color-coding, critical-path emphasis).

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

- A task is complete only after **passing the quality-gate pipeline** — `quality_gate.steps` in `.agentloop/config.yaml` is the **single definition of the DoD** (default: `make test` green → `make check` clean → a review step applying the `/code-review`+`/simplify` disciplines → a real-launch smoke test for runnable deliverables). Each cmd step has its own retry budget; a failure is sent back to the implementer until the budget runs out (→ `blocked`).
- **Parallel tasks run in isolation**: independent leaf tasks are implemented in their own branch/working directory with `git worktree`, **up to 3 in parallel** (`max_parallel` in `config.yaml`), and merged into the work branch sequentially in ascending id order when done. Foundation tasks are finalized first on the work branch.
- An unsolvable task becomes `blocked`; an upstream defect becomes `needs-revision`, **escalated to the human**, and the loop stops.
- **The determinism boundary**: control flow, parallelism, merge, cmd-step gate decisions, and stopping are deterministic in code. Each task's implementation code and the review step's fixes are LLM-derived and non-deterministic, absorbed by "re-verify the already-passed steps after a review change; retry until green, else blocked". **The orchestrator does not touch `gates.build`** (only the human opens a gate).

> **Assumed stack**: the bundled `makefile` provides `make test` (pytest) and `make check` (ruff/format/mypy/tsc together). `make check` bundles `make pre-commit` (commit stage) and `make pre-push` (format/mypy/tsc). If copied into a project without `make`, substitute your own test/check commands.

### Security review

Ensured in three layers: **gitleaks** (mechanically prevents committing secrets at pre-commit; exclude false positives with `.gitleaksignore`) / **`/security-review`** mandatory at implementation completion / **`/security-review` + `make audit`** (dependency vulnerability audit) mandatory in `/verify`.

### GitHub Issues integration (optional)

If you want to make tasks visible to the team/stakeholders, you can **one-way-mirror** `tasks.yaml` to GitHub Issues (`make issue-sync`).

- **Off by default.** Enable with `github.enabled: true` in `.agentloop/config.yaml`. Requires the `gh` CLI and a GitHub remote; auto-skips if absent (does not break offline / right after copying).
- One issue per task T-NNN. The issue number is not written to tasks.yaml; matching is by label + a hidden `<!-- agentloop:T-NNN -->` body marker (so renaming an issue does not break the link). `done` is closed.
- **Tell them apart by the labels applied**: `kind:*` (kind) / `status:*` (status) / `phase:*` (phase: requirements/design/build/verify) / `req:*` (covered requirement). The labels used are **auto-created (provisioned)** with `gh label create --force`, so it does not fail on the first run even in a repo with no labels.
- **One-way only**: `tasks.yaml` is always the SSOT. Edits on the Issues side are not read back (preserving deterministic, offline operation). Check just the plan with `make issue-sync ARGS=--dry-run`.
- Writing issues is an outward-facing operation, so the `github.enabled: true` opt-in serves as consent.

## Troubleshooting

- **A task went `blocked`** — the quality gate could not be passed within the step's retry budget. Read the summary appended to `.agentloop/build-loop.log` (and the escalation log in `state.md`), fix the cause (or fix the task ticket), set the task's `status` back to `todo` in `.agentloop/tasks.yaml`, and re-run `make build-loop`. If the cause is an upstream (requirements/design) defect, roll back with `/revise <phase>` instead.
- **The loop was interrupted** (Ctrl-C, crash, network) — just re-run `make build-loop`. On startup it resets tasks left `in_progress` back to `todo` and cleans up leftover worktrees/branches before recreating them, so resuming is safe.
- **An edit was denied by the gate guard** ("Blocked: gate not approved…") — you are editing a next-phase deliverable while its prerequisite gate is `pending`. That is the mechanism working; get the gate approved first. If the state is genuinely wrong, fix `gates.*` in `.agentloop/state.md` (approval is the human's call). Emergency escape hatch: `gates.enforce_hook: false` in `.agentloop/config.yaml`.
- **Every guarded edit is denied and the message says state.md is unreadable** — `.agentloop/state.md` is missing or its front-matter is malformed; the guard fails closed. Restore the file (from git history if needed) so the `gates:` block parses again.
- **`make build-loop` refuses to start with "template placeholders"** — run `make init NAME=<product>` first (see Setup).
- **state.md and reality have drifted** (e.g. the task table is stale) — `tasks.yaml` is the truth for tasks; regenerate the human-facing view with `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --render` and paste it into `state.md`. Gates and phase in `state.md` are the truth for the lifecycle; correct them deliberately (only a human opens or rewinds a gate).
- **No usable `make` in an adopted repo** — the AgentLoop targets are self-contained in `agentloop.mk` (they need only the `uv` binary): run them standalone with `make -f agentloop.mk build-loop`, or call the scripts directly, e.g. `uv run --no-project --with pyyaml python scripts/agentloop/build_loop.py`.
- **`agentloop-upgrade`/`agentloop-uninstall` says "no adopt-manifest"** — these two are adopt-only; a greenfield `make init` records no manifest (the whole copied template is yours, so there is nothing to distinguish). In a repo adopted before manifests existed, re-run `make adopt` once (existing files are skipped, the manifest is recorded) and upgrade from there.

## Layout

| Path | Role |
|------|------|
| `.agentloop/state.md` | SSOT for phase, gates, logs |
| `.agentloop/tasks.yaml` | machine-readable SSOT of the task graph (DAG) |
| `.agentloop/config.yaml` | source of knobs for deterministic execution (parallelism, worktree, gate enforcement) and the single definition of the DoD (`quality_gate.steps`) |
| `scripts/agentloop/` | deterministic orchestration (`dag.py` / `build_loop.py` / `gate_guard.py` / `init.py` / `adopt.py` / `cycle.py`). Product scripts go directly under `scripts/` |
| `agentloop.mk` | the AgentLoop make targets, self-contained (uv only) — an existing repo takes just this file |
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
