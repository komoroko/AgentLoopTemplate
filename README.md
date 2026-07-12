# AgentLoopTemplate

**English** | [日本語](README.ja.md)

A coding-agent template for developing software **Human on the Loop**.
A coding agent performs the work, produces the deliverables, and self-tests from requirements through testing,
while **humans only approve/decide at the "gate" on each phase boundary**.
Works with **Claude Code** and **VS Code GitHub Copilot** (full support, including the hook-enforced gates),
and with **Codex** and any other agent that reads `AGENTS.md` (rules + procedures; gates by convention) —
see "Agent support" below.

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

## Where to start

| Your situation | Entry point |
|---|---|
| Building a new product from scratch | "Setup (new repository / greenfield)" → "Usage" |
| Installing into an ongoing repository | "Adopting into an existing repository (brownfield)" → `/onboard` (the full per-starting-state table lives in `/onboard`) |
| Already set up — starting the next change | Write the change into `docs/00-product-brief.md` and run `/req` (if the previous cycle is still open, run `make cycle-close NAME=<slug>` first) |
| The release decision (gate ⑤) is made | `make cycle-close NAME=<slug>` — archive this cycle's docs and reset for the next |
| Refreshing / retracting the template tooling | `make -f agentloop.mk agentloop-upgrade` / `agentloop-uninstall` |
| Lost, or resuming after an interruption | `/status` — it also tells you the next command to run |

## Design principles

This template is itself a multi-agent orchestration, and its own machinery follows three design axes:

- **Architecture** — the simplest structure that meets the need: `build_loop.py` is a **deterministic DAG** (controllable, debuggable), and each phase is delegated to a dedicated role agent to separate concerns.
- **Context** — kept minimal: SSOT files (`state.md` / `tasks.yaml`) hold the truth, role agents read only what they need, failures are **summarized, not dumped**, oversized logs rotate, and the session itself is compacted at phase-boundary checkpoints — memory is tiered (session / cycle / permanent), each tier with its own refresh cycle (see "Context budget" in `AGENTS.md`).
- **Tools** — minimal, scoped role-agent grants; the quality gate has a **retry cap** (`config.yaml`); `summarize_failure()` returns compact, actionable failures.

## Setup (new repository / greenfield)

Prerequisites: WSL / Linux / macOS and `make` (not Windows-native). The deterministic build loop (`make build-loop`, mode A) additionally needs a **headless agent CLI** installed and authenticated — `claude -p` by default; swap it via `build.headless.cmd` in `.agentloop/config.yaml` (e.g. `codex exec`, `gemini -p`; the prompt is appended as the last argument). Any agent (or the human in a terminal) can invoke it; without such a CLI use the interactive mode B — see "Agent support".

1. **Copy this template** into a new product repository:
   ```bash
   git clone --depth 1 https://github.com/you/AgentLoopTemplate.git myproduct
   cd myproduct && rm -rf .git && git init
   # alternative: create the repo with GitHub's "Use this template" button and clone it
   ```
2. Install tools and sync dependencies:
   ```bash
   make install   # install the uv / pnpm binaries (runs the official curl|sh installers;
                  # in a locked-down/offline environment, install uv and pnpm manually instead)
   make setup     # uv sync (sync dev dependencies, generate uv.lock)
   # if using the frontend: scaffold your app into frontend/ first (e.g. `pnpm create vite frontend`),
   # then `cd frontend && pnpm install`. pnpm itself is only needed in that case.
   ```
3. **Initialize the product** (idempotent):
   ```bash
   make init NAME=<product> FROM=https://github.com/you/AgentLoopTemplate.git
   # optionally BRANCH=build/<product>
   ```
   This fills the placeholders (`name` in `pyproject.toml`; `project`/`branch`/`updated_at` in `.agentloop/state.md`), creates and switches to the work branch, and flips `gates.template_mode` off so the gate guard goes live. Implement on the work branch, not directly on main.
   It also records `.agentloop/adopt-manifest.yaml` (provenance + per-file hashes), which is what
   makes `agentloop-upgrade` / `agentloop-uninstall` work for a copied template too. `FROM` is the
   template's git URL (or a local checkout path), remembered as the default upgrade source — omit
   it and upgrades will ask for `FROM=` each time. The root `AGENTS.md`, `CLAUDE.md`, and
   `.claude/settings.json` are yours from day one: upgrades never rewrite them (template rule
   updates reach greenfield repos only through the other tooling files).
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
| copy | `.agentloop/` (incl. `prompts/`, the shared procedures), `scripts/agentloop/`, `agentloop.mk`, `.claude/commands|agents`, `.github/prompts|agents|hooks|instructions`, docs scaffolds | **Existing files are never overwritten** (skipped and reported) |
| merge | `AGENTS.md` / `CLAUDE.md` | Template rules land in `.agentloop/AGENTS.agentloop.md`; your AGENTS.md gets one pointer block, your CLAUDE.md one `@`-import block with the Claude capability mapping (each appended once) |
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
   half-done work resumed as delta requirements (each pass runs exactly the steps in "Usage" below). After the release decision, close the cycle:
   ```bash
   make cycle-close NAME=<slug>   # archives the cycle's docs to docs/archive/<date>-<slug>/,
                                  # restores fresh scaffolds, resets gates/phase for the next cycle
   ```
   `docs/00-product-brief.md` and `docs/05-current-state.md` persist across cycles (the baseline is
   updated, not archived). Closing a cycle is a human operation, like opening a gate.
3. **Upgrade / uninstall (any time)** — both entry paths (`make adopt` here, greenfield
   `make init`) record `.agentloop/adopt-manifest.yaml`: the template source/commit/version plus
   a hash of every installed file. Two manifest-driven commands build on it. Both are
   hash-checked — **a file you edited since install is never overwritten or removed**; it is
   skipped and listed (`FORCE=1` overrides). Review with `git diff` afterwards and commit:
   ```bash
   # inside the repo — refresh the template-owned tooling (scripts/agentloop/, the shared
   # procedures in .agentloop/prompts/, the per-agent wrappers under .claude/ and .github/,
   # agentloop.mk, the imported rules). FROM is a git URL or local
   # path; without it the source recorded at init/adopt time is reused. REF = branch/tag, not a SHA.
   # Prints the installed → new template version with the CHANGELOG entries in between;
   # ARGS=--dry-run previews everything (version, changelog, per-file plan) without applying.
   make -f agentloop.mk agentloop-upgrade FROM=https://github.com/you/AgentLoopTemplate.git

   # retract the installation: everything installed is removed while pristine; the CLAUDE.md
   # @import block, the AGENTS.md pointer block, and the merged settings.json entries are
   # retracted too
   make -f agentloop.mk agentloop-uninstall ARGS=--dry-run
   ```
   Upgrade never touches repo-owned state (`config.yaml`, `state.md`, `tasks.yaml`, filled docs,
   your AGENTS.md/CLAUDE.md); uninstall removes it only while still unedited. The template's identity comes
   from `VERSION`/`CHANGELOG.md` at its root — neither is copied on adopt; the manifest's
   `template.version` is the record. And when `TEST_CMD`/`CHECK_CMD` are omitted at adopt time,
   commands detected from your build files (package.json, pyproject.toml, Cargo.toml, go.mod,
   makefile) are printed as suggestions — never auto-written.

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

3. If an upstream (requirements/design) defect comes to light during implementation, you can roll back with **`/revise <phase>`** (resets the gates from the target onward to `pending` in a chain, and marks task impact deterministically — `make revise ARGS="--impacted T-00x,T-00y"` sets the seeds **and their transitive dependents** to `needs-revision`; `dag.py --impacted` is the read-only enumeration) — or directly via `make revise ARGS="--to <phase> --reason '...'"`. Rewinding approval is also at the human's discretion.
4. Check the current phase, gate approval status, and task progress anytime with `/status`. Generate the **big picture (dependency diagram)** of tasks with `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --mermaid`, which renders directly in GitHub/VS Code/Markdown (status color-coding, critical-path emphasis).
5. Shipping the cycle as a PR? `make pr-draft` assembles the PR body from the SSOT (gate approvals, task table, requirement coverage, security-review binding, commit list) into `.agentloop/pr-draft.md` — read-only; it prints the `gh pr create --body-file` line and creating/pushing the PR stays yours.
6. After the release decision (gate ⑤), close the cycle with `make cycle-close NAME=<slug>`: it archives this cycle's docs to `docs/archive/<date>-<slug>/`, restores fresh scaffolds, and resets gates/phase for the next cycle. This applies to greenfield and brownfield repos alike (`docs/00-product-brief.md` and the `docs/05-current-state.md` baseline persist). Closing a cycle is a human operation, like opening a gate.

> **It does not stall during approval waits**: a notification fires on reaching a gate, and while waiting for approval the agent
> pulls forward outcome-independent work (environment setup, investigation, test-harness setup, etc.).
> Since it does no work that pre-empts the approval outcome, the gate's strictness is preserved. Speculative work is provisional and throwaway-by-default and is
> recorded in the "speculative work log" of `.agentloop/state.md`, so the human can decide to adopt or discard it.

### Running the implementation phase autonomously

The implementation loop has two modes. The behavior (DoD, parallelism/merge rules) is identical. Below is a summary; the canon for operation is `.agentloop/prompts/commands/build.md` (procedure) and `AGENTS.md` (rules):

**A. Deterministic execution (recommended) — `make build-loop`**
An orchestrator that drives scheduling deterministically in code (`scripts/agentloop/build_loop.py`). It decides **which tasks, at what parallelism, in what merge order, and when to stop** deterministically from `.agentloop/config.yaml` and `tasks.yaml`, not relying on LLM discretion.

```
make build-loop                  # run
make build-loop ARGS=--dry-run   # check just the control flow without calling the agent CLI/git
```

**B. Interactive loop — the lead re-enacts mode A in conversation**
An alternative that runs the loop without the orchestrator (and the only mode available without a headless CLI). Claude Code drives it with `/loop /build`; VS Code Copilot re-invokes the `/build` prompt per iteration; Codex re-runs the `/build` procedure.

- A task is complete only after **passing the quality-gate pipeline** — `quality_gate.steps` in `.agentloop/config.yaml` is the **single definition of the DoD** (default: `make test` green → `make check` clean → a review step applying the `/code-review`+`/simplify` disciplines → a real-launch smoke test for runnable deliverables). A task's own `test` command from `tasks.yaml` runs first as a focused step when it differs from the configured ones. Each cmd step has its own retry budget; a failure is sent back to the implementer until the budget runs out (→ `blocked`). Mark the smoke step `required: true` once the deliverable is runnable — an empty command then refuses to build instead of silently skipping the launch check.
- **Parallel tasks run in isolation**: independent leaf tasks are implemented in their own branch/working directory with `git worktree`, **up to 3 in parallel** (`max_parallel` in `config.yaml`), and merged into the work branch sequentially in ascending id order when done. Foundation tasks are finalized first on the work branch. After a batch merges **2 or more** leaves, the cmd steps run once more on the **merged** work branch (the integration gate — each leaf was green only in isolation); a red goes to a headless fixer within the retry budget, else the batch blocks. Uncommitted worktree changes are finalized onto the leaf branch before merge/cleanup, so nothing is lost with the worktree. Before a leaf merges (and before a serial task is marked done), every path the task changed is re-checked against the gate rules — a violation escalates (`gate_violation`), the task blocks, and the branch is kept unmerged for human review instead of landing silently.
- An unsolvable task becomes `blocked`; an upstream defect becomes `needs-revision`, **escalated to the human**, and the loop stops.
- **The determinism boundary**: control flow, parallelism, merge, cmd-step gate decisions, and stopping are deterministic in code. Each task's implementation code and the review step's fixes are LLM-derived and non-deterministic, absorbed by "re-verify the already-passed steps after a review change; retry until green, else blocked". **The orchestrator does not touch `gates.build`** (only the human opens a gate).

> **Assumed stack**: the bundled `makefile` provides `make test` (pytest) and `make check` (ruff/format/mypy/tsc together). `make check` bundles `make pre-commit` (commit stage) and `make pre-push` (format/mypy/tsc). If copied into a project without `make`, substitute your own test/check commands.

### Security review

Ensured in three layers: **gitleaks** (mechanically prevents committing secrets at pre-commit; exclude false positives with `.gitleaksignore`) / a **security review** mandatory at implementation completion — in deterministic mode A, `build_loop.py` auto-runs a headless security review (the `/security-review` discipline, via `build.headless.cmd`) when all tasks are done and binds the report to the reviewed HEAD in `.agentloop/security-review.md` (config `build.post_build.security_review`; re-runs at the same HEAD skip) / a **security review + `make audit`** (dependency vulnerability audit) mandatory in `/verify`. An agent without the `/security-review` command performs an equivalent security-focused review pass and records it the same way.

### GitHub Issues integration (optional)

If you want to make tasks visible to the team/stakeholders, you can **one-way-mirror** `tasks.yaml` to GitHub Issues (`make issue-sync`).

- **Off by default.** Enable with `github.enabled: true` in `.agentloop/config.yaml`. Requires the `gh` CLI and a GitHub remote; auto-skips if absent (does not break offline / right after copying).
- One issue per task T-NNN. The issue number is not written to tasks.yaml; matching is by label + a hidden `<!-- agentloop:T-NNN -->` body marker (so renaming an issue does not break the link). `done` is closed.
- **Tell them apart by the labels applied**: `kind:*` (kind) / `status:*` (status) / `phase:*` (phase: requirements/design/build/verify) / `req:*` (covered requirement). The labels used are **auto-created (provisioned)** with `gh label create --force`, so it does not fail on the first run even in a repo with no labels.
- **One-way only**: `tasks.yaml` is always the SSOT. Edits on the Issues side are not read back (preserving deterministic, offline operation). Check just the plan with `make issue-sync ARGS=--dry-run`.
- Writing issues is an outward-facing operation, so the `github.enabled: true` opt-in serves as consent.

## Troubleshooting

- **First, run `make doctor`** — a read-only diagnosis of the whole setup: binaries on PATH, config/state/tasks consistency (including the gate-chain invariant and `guard_paths` typos), task↔ticket parity, gate-guard hook registration, branch/worktree/leaf-branch/lock leftovers, open escalations and event-log size, the security-review↔HEAD binding, and JSON-Schema validation of `config.yaml`/`tasks.yaml`. Most of the situations below show up there as a FAIL/WARN line.
- **A task went `blocked`** — the quality gate could not be passed within the step's retry budget. Read the open escalation with `make events ARGS=--render` (the failure summary is in the event's detail; state.md's escalation view mirrors it), fix the cause (or fix the task ticket), set the task's `status` back to `todo` in `.agentloop/tasks.yaml`, close the event with `make events ARGS='--resolve <ID> --note "…"'`, and re-run `make build-loop`. If the cause is an upstream (requirements/design) defect, roll back with `/revise <phase>` instead. When the same task keeps blocking, `make events ARGS=--summary` aggregates the history (failures per task / per gate step) to show where the loop is losing time.
- **The loop was interrupted** (Ctrl-C, crash, network) — just re-run `make build-loop`. On startup it resets tasks left `in_progress` back to `todo` and cleans up leftover worktrees/branches before recreating them, so resuming is safe.
- **An edit was denied by the gate guard** ("Blocked: gate not approved…") — you are editing a next-phase deliverable while its prerequisite gate is `pending`. That is the mechanism working; get the gate approved first. If the state is genuinely wrong, fix `gates.*` in `.agentloop/state.md` (approval is the human's call). Emergency escape hatch: `gates.enforce_hook: false` in `.agentloop/config.yaml`.
- **Every guarded edit is denied and the message says state.md is unreadable** — `.agentloop/state.md` is missing or its front-matter is malformed; the guard fails closed. Restore the file (from git history if needed) so the `gates:` block parses again.
- **`make build-loop` refuses to start with "template placeholders"** — run `make init NAME=<product>` first (see Setup).
- **state.md and reality have drifted** (e.g. the task table is stale) — `tasks.yaml` is the truth for tasks; regenerate the human-facing view with `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --render` and paste it into `state.md`. Gates and phase in `state.md` are the truth for the lifecycle; correct them deliberately (only a human opens or rewinds a gate).
- **No usable `make` in an adopted repo** — the AgentLoop targets are self-contained in `agentloop.mk` (they need only the `uv` binary): run them standalone with `make -f agentloop.mk build-loop`, or call the scripts directly, e.g. `uv run --no-project --with pyyaml python scripts/agentloop/build_loop.py`.
- **`agentloop-upgrade`/`agentloop-uninstall` says "no adopt-manifest"** — the repo was set up before manifests existed. Backfill one: in a greenfield copy, re-run `make init NAME=<same-name> FROM=<template-url>`; in an adopted repo, re-run `make adopt` once (existing files are skipped, the manifest is recorded). Note a backfilled manifest records the files **as they are now** as the pristine baseline — a tool you had already edited will look unmodified to the next upgrade.

## Layout

| Path | Role |
|------|------|
| `.agentloop/state.md` | SSOT for phase, gates, logs |
| `.agentloop/tasks.yaml` | machine-readable SSOT of the task graph (DAG) |
| `.agentloop/events.ndjson` | structured orchestration events — the escalation log's machine-readable truth (`make events`); state.md embeds the generated view |
| `.agentloop/config.yaml` | source of knobs for deterministic execution (parallelism, worktree, gate enforcement) and the single definition of the DoD (`quality_gate.steps`) |
| `.agentloop/schema/` | JSON Schemas for `config.yaml` / `tasks.yaml` — editor completion/validation via the `yaml-language-server` modelines; `make doctor` validates against them |
| `scripts/agentloop/` | deterministic orchestration (`dag.py` / `build_loop.py` / `events.py` / `doctor.py` / `gate_guard.py` / `pr_draft.py` / `revise.py` / `issue_sync.py` / `init.py` / `adopt.py` / `cycle.py`). Product scripts go directly under `scripts/` |
| `VERSION` / `CHANGELOG.md` | the template's release identity; `agentloop-upgrade` shows the changelog between the installed and new versions |
| `agentloop.mk` | the AgentLoop make targets, self-contained (uv only) — an existing repo takes just this file |
| `AGENTS.md` | the canonical, agent-neutral operating rules (capability vocabulary + gate rules) |
| `CLAUDE.md` | the Claude Code capability mapping (imports AGENTS.md) |
| `.agentloop/prompts/` | the shared phase procedures and role definitions every agent reads |
| `.claude/commands/`, `.github/prompts/` | per-agent entry points for each phase (`/req`–`/verify`, plus `/onboard`, `/revise`, `/status`) — thin wrappers over `.agentloop/prompts/commands/` |
| `.claude/agents/`, `.github/agents/` | role-agent wrappers (requirements/design/implementation) over `.agentloop/prompts/agents/` |
| `.github/instructions/`, `.github/hooks/` | the VS Code Copilot capability mapping and its gate-guard hook registration |
| `docs/` | phase deliverables (requirements, design, ADR, task tickets, test plan) |

## Agent support

The rules (`AGENTS.md`) and phase procedures (`.agentloop/prompts/`) are agent-neutral: they name
human-interaction points with a **capability vocabulary**, and each agent's mapping file says how
to realize it. What each agent gets:

| Capability | Claude Code | VS Code Copilot | Codex (and other AGENTS.md readers) |
|---|---|---|---|
| phase entry points | slash commands (`.claude/commands/`) | prompt files `/req` … (`.github/prompts/`) | say the phase name — the agent reads `.agentloop/prompts/commands/<name>.md` |
| gate enforcement (mechanism layer) | PreToolUse hook (`gate_guard.py`) + commit-stage check | same hook via `.github/hooks/agentloop.json` (agent hooks, preview) + commit-stage check | commit-stage check (`gate_guard.py --check-diff` in `make check` / `git commit`); edit-time is convention only |
| structured questions to the human | AskUserQuestion | numbered options in chat | numbered options in chat |
| approval presentation | plan mode + ExitPlanMode | Plan mode / explicit "approve" in chat | explicit "approve" in chat |
| role delegation (context isolation) | subagents, worktree-parallel | custom agents `@architect` … (`.github/agents/`) | inline role adoption (serial) |
| autonomous build (mode B) | `/loop /build` | re-invoke `/build` per iteration | re-run the `/build` procedure |
| headless build orchestrator (mode A) | `make build-loop` (default `claude -p`) | `make build-loop` with any headless CLI in `build.headless.cmd` (`claude -p`, `codex exec`, `gemini -p`, …); else mode B | same — swap `build.headless.cmd`; else mode B |
| pending-gate notification | PushNotification | end of turn ("waiting on gate N") | end of turn |

Also used everywhere: **git worktree** (isolated parallel tasks) and the **deterministic
orchestrator** (`make build-loop` — scheduling, parallelism, merge, and gate decisions in code,
including a merge-stage gate check: what a task changed is re-evaluated against the gate rules
before it lands on the work branch).

**VS Code Copilot notes** — open the repo and the pieces load themselves: the prompts appear as
`/req` … in chat, `@requirements-analyst`/`@architect`/`@implementer` resolve as custom agents,
`.github/instructions/agentloop.instructions.md` supplies the capability mapping, and
`.github/hooks/agentloop.json` registers the gate guard + the session-start state echo (agent
hooks are a **preview** feature — if they are off, the gates still hold by convention). VS Code
may also parse `.claude/settings.json` and run the guard twice per tool; that is harmless
(read-only, idempotent deny). Parallel leaf tasks degrade to serial when delegation isn't
available.

**Codex notes** — Codex reads `AGENTS.md` natively; the generic invocation rule there lets you
drive phases by name ("run /req"). Edit-time gate enforcement is convention-only (Codex hooks
intercept Bash, not file edits), but the commit stage is mechanical: `gate_guard.py --check-diff`
runs as a pre-commit hook and inside `make check`, so a gate violation fails the DoD before it
lands. The security review is an equivalent manual pass — `make doctor` reports which hook hosts
are registered.
