# AgentLoopTemplate — Agent Operating Rules

This repository is a template for developing software **Human on the Loop**.
A coding agent performs the work, produces the deliverables, and self-tests at every phase,
while **humans only review and approve/decide at the "gate" on each phase boundary**.

## Language

Write conversation and deliverables (`docs/**`) in **the language the user uses — i.e. the project's primary language** (e.g. respond in Japanese to a Japanese user, in English to an English user). The template files themselves (this `CLAUDE.md`, the `.claude/commands/*`, `.claude/agents/*`, and the `docs/**` scaffolds) are written in English as the canonical single source; you may localize the **headings** of a deliverable to match the user's language when you fill a scaffold in. Identifiers and machine-read vocabulary (gate states `pending`/`approved`, task `status` values, `kind` values, etc.) stay as-is in every language.

## Development lifecycle

```
brief → requirements → design → tasks → build → verify → done
        (/req)        (/design) (/tasks) (/build) (/verify)
          ▲gate①        ▲gate②     ▲gate③   ▲gate④    ▲gate⑤
```

| Phase | Command | Deliverable | Gate (what the human approves) |
|----------|----------|--------|------------------------|
| requirements | `/req`    | `docs/10-requirements.md` | ① freeze requirements |
| design       | `/design` | `docs/20-design.md` + `docs/decisions/ADR-*.md` | ② technical decisions |
| tasks        | `/tasks`  | `docs/tasks/T-*.md` | ③ task plan |
| build        | `/build`  | implementation code + tests | ④ implementation review |
| verify       | `/verify` | `docs/test/test-plan.md` execution results | ⑤ release decision |

Check progress and all tasks with `/status` (the text view `dag.py --render` and the dependency graph `dag.py --mermaid`). When `done` is reached, `/verify` records a retrospective in `docs/retrospective.md` (where rework originated, lessons for upstream) and closes any open logs. If an upstream defect is found, you can **roll back** to requirements/design with `/revise` (gates are reset in a chain — see "Roll back" below).

## Single Source of Truth (SSOT)

The truth is split across three files. Their roles differ, so do not conflate them:

- **`.agentloop/state.md`** — the truth for the phase, each gate's approval status, and the various logs (speculative / escalation). **Always read it when starting work.** Update it after work (phase progress, `updated_at`). The front-matter `gates.<name>` is `pending` | `approved`. **Never set this to `approved` without human approval.**
- **`.agentloop/tasks.yaml`** — the **machine-readable truth** of the task graph (DAG). `/tasks` generates it; `/build` (`scripts/agentloop/build_loop.py`) and `/status` (`scripts/agentloop/dag.py`) read it. Each task has `id`/`title`/`kind`/`blockedBy`/`status`/`test` (plus optional display/label metadata `req`/`phase`). `req` (the requirement it covers) is the **traceability thread from requirements → design → tasks**; `dag.py --trace` mechanically cross-checks it against the requirements document and design to deterministically detect uncovered requirements and dangling references (used by the `/tasks` gate and `/status`). fan-out, frontier, execution layers, and the critical path are derived from `blockedBy`, so they are not stored (to prevent drift). The task table in state.md is the human-facing view from `dag.py --render`. **Even with GitHub Issues integration (opt-in) enabled, tasks.yaml remains the SSOT**, and Issues are a **one-way mirror** via `scripts/agentloop/issue_sync.py` (never read back — preserving deterministic, offline-first operation).
- **`.agentloop/config.yaml`** — the source of knobs for deterministic execution (parallelism, retry, worktree, gate enforcement). Read by `build_loop.py`/`gate_guard.py`.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command checks its prerequisite gate up front:
   - `/design` requires `gates.requirements == approved`
   - `/tasks` requires `gates.design == approved`
   - `/build` requires `gates.tasks == approved`
   - `/verify` requires `gates.build == approved`
   If unapproved, stop work and tell the human what is needed.
2. **Only humans open a gate.** The agent goes only as far as presenting the deliverable. Only after a human signals approval (approving plan mode, or an explicit "approve") do you set the corresponding gate in `state.md` to `approved` and move on.
3. **Do not silently fix problems in requirements/design.** If you find an upstream defect (e.g. during implementation), set the affected task to `needs-revision`, record it in the escalation log, and raise it to the human.

**Gates are enforced in two layers** (not relying on the rules alone):
- **Convention layer**: as above, each command checks its prerequisite gate up front.
- **Mechanism layer**: `scripts/agentloop/gate_guard.py` (a PreToolUse hook in `.claude/settings.json`) **denies** in code any Write/Edit to a **next-phase deliverable path** while its prerequisite gate is unapproved (`docs/20-design.md`, `docs/decisions/**` → requires requirements approved; `docs/tasks/**` → design approved; `backend/**`, `frontend/**`, `scripts/**` (product scripts) → tasks approved; `docs/test/**` → build approved). However, `scripts/agentloop/**` (the template's foundational tools) is always allowed regardless of gates (so the hook's own maintenance is not blocked). In addition, `/build` double-checks `gates.tasks==approved` in code at the start of `scripts/agentloop/build_loop.py`. Set `gates.enforce_hook: false` in `.agentloop/config.yaml` to disable the mechanism layer. The hook launches with `uv run --no-project --with pyyaml`, so it works from the very first edit right after copying, **without depending on the project env (`make setup`)**.

> **Note when maintaining the template itself**: the scaffold originals `docs/20-design.md`, `docs/tasks/**`, and the templates under `scripts/**` share the same paths as real product deliverables, so with gates unapproved the mechanism layer will deny edits to them (you get blocked by your own gate during maintenance). This is expected. When maintaining the template, temporarily switch `gates.enforce_hook: false` → edit → **restore it to `true` immediately afterward** (an escape hatch for this purpose; the mechanism does not distinguish originals from real deliverables — we prioritize the gate's simplicity and reliability).

## Roll back (returning upstream)

When an upstream defect (requirements/design) is confirmed during implementation/verification, **roll back upstream** at the human's discretion. `/revise` (`make revise`) is the first-class operation:

- **Gates are reset in a chain**: reset every gate from the target phase onward back to `pending` (`scripts/agentloop/revise.py`). **Invariant: if an upstream gate is `pending`, do not leave a downstream gate `approved`** (preventing the inconsistency of the next phase proceeding on a stale approval). The editing order from then on is mechanically enforced by `gate_guard` (e.g. while design is pending, edits to `docs/tasks/**` and implementation code are denied).
- **Rewinding approval is also a human privilege**: symmetric with opening a gate. `/revise` is run only under the human's explicit judgment; the agent does not roll back on its own.
- **Upstream fixes always entail task impact analysis**: do not throw tasks away and recreate them. Reconcile existing tasks against the revised upstream — **identify the directly affected tasks (seeds) and fully expand their transitive dependents (downstream) with `dag.py --impacted`** — then classify into **keep / modify / obsolete / new**. `modify` becomes `needs-revision`; an invalidated `done` reverts to `todo` (needs reimplementation).
- The trigger for detection is a `needs-revision` raised during implementation (gate rule ③ above). The path to roll back and resume from there is `/revise`.

## Gate self-assessment (required)

When you reach a gate, you must present a **self-assessment block** alongside the deliverable. This is the core of the metacognition that lets the system
recognize its own uncertainty and stated assumptions and lighten the human's review,
common to all gates (①–⑤):

- **Assumptions made**: assumptions taken as given without confirming with the human (points where, if wrong, the deliverable breaks).
- **Confidence**: high / medium / low (may be split by area). **Always attach a reason for low spots** and direct the human's attention there.
- **Open questions / points for the human to decide** (most important).
- **Anticipated risks and trade-offs** (decisions that bite later).

Do not pretend to high confidence and let the human skip verification — **surfacing uncertainty honestly** raises the gate's value.
This is distinct from the "speculative work log" (that one records throwaway-by-default work). For requirements/design/task tickets,
leave this self-assessment in the deliverable itself (not just spoken — the "Self-assessment" section in each scaffold).

## Minimizing the approval-wait bottleneck

Do not leave the agent idle while waiting for human approval. But **never compromise the strictness of the gate**.

### 1) Shorten the wait itself
- On reaching a gate, **notify the human immediately with `PushNotification`** to cut the lag before they notice.
- Ask the human **in a single `AskUserQuestion`** (reduce round-trips; do not dribble out questions).
- The `/build` implementation loop **runs independent tasks in parallel** (worktree-isolated, up to 3 in parallel — see below).

### 2) "Speculative work" while waiting for approval (outcome-independent only)
While a gate is `pending`, you may pull forward **only work that does not depend on the approval outcome**. Criteria:

- **Allowed** (outcome-independent, low-cost, painless to discard):
  repo scaffolding/directory layout, dev environment/dependency setup, the skeleton of CI/test harnesses,
  **read-only investigation** of candidate technologies, lint/static-analysis setup, fixtures and other scaffolding.
- **Not allowed** (pre-empting the approval outcome = breaking the gate's meaning):
  deliverables premised on a pending decision (e.g. writing the design body before requirements are approved / fixing the technical choice /
  finalizing tasks before design is approved). Do these only after human approval.

Speculative work is **provisional and throwaway-by-default**. Record it in the "speculative work log" of `.agentloop/state.md` so the human can decide to discard or adopt it.
**Never set a gate to `approved` on the grounds of speculative work.**

## Task dependency graph and optimal consumption

Treat tasks not as a flat list but as a **dependency graph (DAG)**.

- Each task has a kind: **foundation** (a shared base many depend on) / **parallel** (independent leaves that can run concurrently) / **integration** (a join of several).
- From the graph, derive **execution layers** (same layer can run in parallel) and the **critical path** (the longest path that determines the overall duration).
- **Optimal-consumption policy**: within the executable frontier, prioritize ① foundation / high fan-out (many dependents) → ② on the critical path → ③ everything else. Run independent tasks in parallel.
- **Isolated execution (worktree)**: foundation / high-fan-out tasks are finalized **serially on the work branch**. Independent leaf tasks are run by **launching `implementer` with `isolation: "worktree"`**, each progressing from implementation through the quality gate in isolation in its own worktree (separate branch, separate directory) — **up to 3 in parallel**. Do not use `git subtree` (it is for importing external repos and is unsuitable for separating concurrent work).
- **Join**: after a leaf task's implementation is complete, merge it into the work branch sequentially, **deterministically in ascending id order**. Resolve conflicts at the merge point; completing a merge is the **trigger that frees the frontier for integration tasks**.
- **The chain is dynamic**: assembled ahead of time by `/tasks`, and **reassembled each time one task completes** in `/build`. If a new dependency or split is discovered during implementation, update the DAG and re-derive.
- **Deterministic-driven**: the frontier computation, consumption order, parallelism (max 3), merge, and stop above are run deterministically in code by `scripts/agentloop/build_loop.py` (`make build-loop`). The derivation logic is unified in `scripts/agentloop/dag.py` (shared with `/status`). It is not left to LLM discretion.
- The truth is always **`.agentloop/tasks.yaml`** (the machine-readable SSOT of the graph). The task table / execution plan in state.md is the human-facing view from `dag.py --render`.

## Principles

- **Reusing existing implementation comes first.** Before writing new code, look for existing functions, utilities, and patterns.
- **Move forward only after passing the quality gate.** An implementation task is `done` only when it satisfies "unit/integration tests green **and** passes `/simplify`/`/code-review` **and** `make check` is clean". Do not mark `done` while any of these is unmet. **For runnable deliverables (CLI, server, etc.), the DoD also includes a minimal real-launch smoke test (it actually launches and the main commands work)** in addition to green tests. Tests can pass while the launch path (packaging, entry point) is broken; this catches that within build.
- **Small and sure.** One commit = one concern. Get approval before destructive or outward-facing operations.
- **Context isolation.** Delegate requirements/design/implementation each to their dedicated subagent (`.claude/agents/`) so the main context stays clean. Unify code review on `/code-review` and cleanup on `/simplify`.
- Write deliverable documents in the user's language (see "Language" above).

## Quality-check commands (stack assumptions and substitution)

This template ships a `makefile`; the implementation phase leans on:

- `make test` — run tests (`pytest backend/`)
- `make pre-commit` — commit-stage hooks (ruff lint / eslint, etc.)
- `make pre-push` — pre-push-stage hooks (ruff-format / prettier / mypy / tsc)
- **`make check`** — **runs the above pre-commit + pre-push together** (lint / format / type-check, all of it). Use this at the quality gate.

> `pre-commit run --all-files` runs only the commit-stage hooks (format, mypy, tsc are `stages: [pre-push]`). To include type-checking, the gate uses `make check`.

If copied into a project without `make`, substitute these with that project's test/check commands (the gate's idea is unchanged).

## Security gate

Security is ensured in three layers:

- **Commit stage (mechanism)**: **gitleaks** in `.pre-commit-config.yaml` mechanically blocks committing secrets (included in `make pre-commit` / `make check`). Exclude false positives with `.gitleaksignore`.
- **At implementation completion (before gate ④)**: `/build` mandatorily runs **`/security-review`** and resolves code vulnerabilities before asking the human for approval.
- **Test phase (`/verify`)**: mandatorily runs **`/security-review`** and **`make audit`** (dependency vulnerability audit; Python=pip-audit / frontend=pnpm audit; alternative: `osv-scanner` to scan the lockfile in bulk), and records the results in `docs/test/test-plan.md`.

## Branch / commit conventions

- Implement **on a work branch** (avoid committing directly to main). Record the branch name in `branch` of `.agentloop/state.md`.
- **Parallel leaf tasks are implemented in isolation on a worktree derived branch** (e.g. `<branch>/T-NNN`) and merged into the work branch when done. Worktrees are auto-cleaned if there are no changes.
- `/build`'s per-task local commits use the form **`T-NNN: <summary>`**. **One commit = one task.**
- **Approving `/build` = the local commits within that loop are part of approved work.** No per-commit confirmation needed.
- However, **push / PR creation / merging to main are outward-facing operations.** Do these only after separately getting human approval (do not push or merge on your own).
- **Writing to GitHub Issues (create/update/close / label creation) is also an outward-facing operation.** Treat the explicit opt-in `github.enabled: true` in `.agentloop/config.yaml` as consent; do none of it otherwise. `make issue-sync` is for one-way mirroring only and never reads Issues back as truth. The labels used (`kind:*`/`status:*`/`phase:*`/`req:*`) are provisioned idempotently.

## Directories

- `.agentloop/state.md` — SSOT for phase, gates, logs
- `.agentloop/tasks.yaml` — machine-readable SSOT of the task graph (DAG)
- `.agentloop/config.yaml` — source of knobs for deterministic execution (parallelism, retry, worktree, gate enforcement)
- `scripts/agentloop/` — deterministic orchestration (`dag.py` derivation + consistency trace (`--trace`) / `build_loop.py` implementation loop / `gate_guard.py` gate hook / `issue_sync.py` one-way Issues mirror). **Put product scripts directly under `scripts/` and do not mix them with the foundational tools.**
- `docs/` — phase deliverables (requirements, design, ADR, task tickets, test plan). `docs/retrospective.md` holds the retrospective at `done` (recovering the metacognition).
- `.claude/commands/` — entry points for each phase (slash commands)
- `.claude/agents/` — specialized subagents
</content>
