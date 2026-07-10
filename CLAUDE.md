# AgentLoopTemplate — Agent Operating Rules

This repository is a template for developing software **Human on the Loop**.
A coding agent performs the work, produces the deliverables, and self-tests at every phase,
while **humans only review and approve/decide at the "gate" on each phase boundary**.

This file holds the **always-true rules**. Each phase's procedure lives in its command
(`.claude/commands/*.md`) — consult those when running a phase, not this file.

## Language

Write conversation and deliverables (`docs/**`) in **the language the user uses — i.e. the project's primary language**. The template files themselves (this `CLAUDE.md`, `.claude/commands/*`, `.claude/agents/*`, the `docs/**` scaffolds) are written in English as the canonical single source; you may localize a deliverable's **headings** when filling a scaffold in. Identifiers and machine-read vocabulary (gate states `pending`/`approved`, task `status` values, `kind` values, etc.) stay as-is in every language.

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

Check progress anytime with `/status`. At `done`, `/verify` records a retrospective in `docs/retrospective.md` and closes any open logs. An upstream defect rolls back via `/revise` (see "Roll back").

**Cycles**: an ongoing repo runs this lifecycle repeatedly as **delta cycles** — each cycle's docs describe one change, not the whole product. After `done`, the human runs `make cycle-close NAME=<slug>` to archive the filled deliverables to `docs/archive/` and reset gates/phase for the next cycle (`docs/00-product-brief.md` and the baseline `docs/05-current-state.md` persist). In an adopted (brownfield) repo, `docs/05-current-state.md` is the persistent baseline of the existing codebase — `/req`/`/design` read it first, and traceability (R-N / NFR-N) applies to each cycle's delta only, never reverse-generated from existing behavior. Both entry paths (`make init` greenfield copy and `make adopt` brownfield) record `.agentloop/adopt-manifest.yaml`; `make -f agentloop.mk agentloop-upgrade` / `agentloop-uninstall` are manifest-driven and only ever touch pristine template-owned files.

**Mid-cycle scope change / hotfix / abandonment** (each a human decision):
- **A scope addition that is not a defect** never widens approved requirements silently. Default: defer it to the next delta cycle. If it must land now, reopen gate ① (`/revise` to requirements) and re-approve the enlarged scope — traceability stays intact.
- **An emergency hotfix** runs as a *minimal* delta cycle: the gates still open in order, but each deliverable may be a paragraph. If even that is too slow, the human fixes and approves directly outside the loop; record it in the escalation log and fold it into `docs/05-current-state.md` at the next cycle's `/verify`.
- **Abandoning a cycle**: archive it as-is with `make cycle-close NAME=abandoned-<slug>` — the partial deliverables land in `docs/archive/` for the record and gates/phase reset. Keep or delete the work branch by hand.

## Single Source of Truth (SSOT)

The truth is split across three files. Their roles differ, so do not conflate them:

- **`.agentloop/state.md`** — the truth for the phase, each gate's approval status, and the logs (speculative / escalation / roll-back). **Always read it when starting work**; update it after work (phase progress, `updated_at`). `gates.<name>` is `pending` | `approved`. **Never set it to `approved` without human approval.** The escalation log's machine-readable truth is **`.agentloop/events.ndjson`** (structured events; `build_loop.py` appends automatically, `make events ARGS='--add …'` by hand) — state.md embeds only the generated view between its `ESCALATION-VIEW` markers, and items are closed with `make events ARGS='--resolve <id> --note "…"'`.
- **`.agentloop/tasks.yaml`** — the **machine-readable truth** of the task graph (DAG). `/tasks` generates it (schema: see `.claude/commands/tasks.md`); `build_loop.py` and `dag.py` read it. `req` is the traceability thread requirements → design → tasks (IDs `R-N` for functional, `NFR-N` for non-functional requirements), mechanically cross-checked by `dag.py --trace`; `/verify` extends the same check into the test plan with `--test-plan`. Derived values (fan-out, frontier, layers, critical path) are **not stored** (preventing drift); the task table in state.md is the human-facing view from `dag.py --render`. Even with GitHub Issues integration enabled, tasks.yaml remains the SSOT and Issues are a one-way mirror (never read back).
- **`.agentloop/config.yaml`** — the source of knobs for deterministic execution (parallelism, worktree, gate enforcement) **and the single definition of the DoD (`quality_gate.steps`)**. Read by `build_loop.py`/`gate_guard.py`.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command checks its prerequisite gate up front (`/design`←requirements, `/tasks`←design, `/build`←tasks, `/verify`←build). If unapproved, stop and tell the human what is needed.
2. **Only humans open a gate.** The agent goes only as far as presenting the deliverable. Only after a human signals approval (approving plan mode, or an explicit "approve") do you set the gate in `state.md` to `approved` and move on. **Invoking the next-phase command is not itself approval** — running `/verify` while `build` is `pending` does not consent to gate ④; stop and ask for explicit approval. When recording an approval, append the date — and, when several humans share the repo, who approved — as a YAML comment on the gate line (e.g. `tasks: approved   # 2026-07-07 alice`): machine parsing reads only the value, and `revise.py`/`cycle.py` preserve comments.
3. **Do not silently fix problems in requirements/design.** On finding an upstream defect, set the affected task to `needs-revision`, record it in the escalation log, and raise it to the human.

**Gates are enforced in two layers**:
- **Convention layer**: rule 1 above, checked by each command.
- **Mechanism layer**: `scripts/agentloop/gate_guard.py` (a PreToolUse hook in `.claude/settings.json`) **denies** in code any Write/Edit to a next-phase deliverable path while its prerequisite gate is unapproved (defaults: `docs/20-design.md`, `docs/decisions/**` ← requirements; `docs/tasks/**` ← design; `backend/**`, `frontend/**`, `scripts/**` (product scripts) ← tasks; `docs/test/**` ← build). The watched paths are configurable via `gates.guard_paths` in config — an adopted (brownfield) repo scopes them to the docs deliverables so pending gates never freeze existing code, then maps its own layout (e.g. `src/`) when ready. `scripts/agentloop/**` (the template's foundational tools) is always allowed. If state.md's gates are unreadable, the guard **fails closed** for guarded paths. `/build` additionally code-checks `gates.tasks==approved` at the start of `build_loop.py`. Escape hatch: `gates.enforce_hook: false` in config. The hook launches with `uv run --no-project --with pyyaml`, so it works right after copying without `make setup`.

> **Note when maintaining the template itself**: the scaffold originals (`docs/20-design.md`, `docs/tasks/**`, `scripts/**`) share paths with real product deliverables — the mechanism does not distinguish them (we prioritize the gate's simplicity). The template repo therefore keeps `gates.template_mode: true` in config, which makes the hook allow everything; `make init` flips it to `false` when the template becomes a product, so the guard goes live without a manual toggle to forget.

## Roll back (returning upstream)

When an upstream (requirements/design) defect is confirmed — or `/verify` finds an implementation defect serious enough to reopen the build (`--to build`) — roll back at the human's discretion with `/revise` (`make revise`):

- **Gates reset in a chain**: every gate from the target phase onward goes back to `pending` (`revise.py`). Invariant: if an upstream gate is `pending`, no downstream gate stays `approved`. The editing order from then on is enforced by `gate_guard`.
- **Rewinding approval is a human privilege**, symmetric with opening a gate — the agent never rolls back on its own. The detection trigger is a `needs-revision` raised during implementation (gate rule 3).
- **Upstream fixes always entail task impact analysis**: do not throw tasks away. Expand the affected set with `dag.py --impacted`, then classify keep / modify / obsolete / new (`modify` → `needs-revision`; an invalidated `done` → `todo`). Details: `.claude/commands/revise.md` and `tasks.md`.

## Gate self-assessment (required)

At every gate (①–⑤), present a **self-assessment block** alongside the deliverable — the metacognition that surfaces the system's own uncertainty and lightens the human's review:

- **Assumptions made** (points where, if wrong, the deliverable breaks).
- **Confidence**: high / medium / low, split by area. **Always attach a reason for low spots.**
- **Open questions / points for the human to decide** (most important).
- **Anticipated risks and trade-offs.**
- **Context-bloat signal** (when relevant): if a deliverable or log has grown enough to risk *Context Rot* / *Lost in the Middle*, propose trimming (link detail out to an ADR; summarize/archive resolved log rows).

Do not pretend to high confidence and let the human skip verification — surfacing uncertainty honestly is what makes the gate valuable. For requirements/design/task tickets, leave the self-assessment in the deliverable itself (the "Self-assessment" section in each scaffold), not just spoken.

## Minimizing the approval-wait bottleneck

Do not sit idle while a gate is `pending` — but **never compromise the gate**:

- Notify the human immediately with `PushNotification`; batch questions into a single `AskUserQuestion`. When they exceed its practical limits (question/option caps), ask only the decisions that block progress and leave the rest as "Open questions" in the deliverable's self-assessment.
- You may pull forward **only outcome-independent work** (scaffolding, dev-env/CI setup, read-only investigation, fixtures). **Never** produce deliverables premised on the pending decision. Speculative work is throwaway-by-default and recorded in the "speculative work log" of `state.md` (per-phase specifics: each command's "While waiting for approval" section).
- **Never set a gate to `approved` on the grounds of speculative work.**

## Task dependency graph

Tasks form a **DAG**, not a flat list: kind = **foundation** (shared base) / **parallel** (independent leaves) / **integration** (join). Execution layers and the critical path are derived from `blockedBy`. Consumption order, parallelism (max 3, worktree-isolated), deterministic ascending-id merge, and stopping are run **deterministically in code** by `build_loop.py` (`make build-loop`), with the derivation logic unified in `dag.py` — not left to LLM discretion. The chain is reassembled every time a task completes. Procedure details: `.claude/commands/build.md` and `tasks.md`.

## Principles

- **Reusing existing implementation comes first.** Before writing new code, look for existing functions, utilities, and patterns.
- **Move forward only after passing the quality gate.** The DoD is defined **once**, as `quality_gate.steps` in `.agentloop/config.yaml` (default: `test` → `check` → `review` (= the `/code-review`+`/simplify` disciplines) → `smoke`); `build_loop.py` runs exactly that list. A task is `done` only when every step passes. **For runnable deliverables (CLI, server, etc.), fill in the `smoke` step's command** — tests can pass while the launch path is broken; the smoke step catches that within build. In interactive mode (`/loop /build`), the lead **re-runs each `cmd` step (`make test`, `make check`) itself and reads its exit status before marking a task `done`** — a subagent's textual "green" report is never sufficient evidence (deterministic mode A already gates on exit code in `build_loop.py`).
- **Durable lessons are promoted into the template, not archived away.** `docs/retrospective.md` is per-cycle and archived at `cycle-close`; at gate ⑤ lift each keeper into the always-loaded files (`CLAUDE.md`, `.claude/commands/*`, `.claude/agents/*`) rather than leave it only in the retrospective or a product's `state.md`.
- **Small and sure.** One commit = one concern. Get approval before destructive or outward-facing operations.
- **Context isolation.** Delegate requirements/design/implementation to their dedicated subagents (`.claude/agents/`) so the main context stays clean.
- Write deliverable documents in the user's language (see "Language").

## Context budget (context hygiene)

More context is not better — long inputs suffer *Context Rot* and *Lost in the Middle*. The main session and every subagent re-read the SSOT and deliverables, so keeping them lean is a first-class quality lever.

**Memory lives in three tiers, each with its own refresh cycle and exit** — so no tier grows without bound:

| Tier | Lives in | Refresh cycle | Exit (folds into the next tier) |
|------|----------|---------------|--------------------------------|
| **Short** — session | conversation, open log rows in `state.md`, `in_progress` state | every checkpoint (gate approval / build-layer boundary): flush → compress resolved rows → suggest `/compact` | only decisions/outcomes survive, into deliverables and resolved log rows |
| **Mid** — cycle | phase deliverables (`docs/**`), `state.md`, retrospective | written per phase, committed at each gate; logs closed at `/verify` | archived by `make cycle-close`; durable lessons promoted to the long tier |
| **Long** — permanent | `CLAUDE.md`, `.claude/commands/*`, `.claude/agents/*`, `docs/00-product-brief.md`, `docs/05-current-state.md`, `docs/archive/` | lessons promoted at gate ⑤; `05-current-state.md` updated at `/verify`; archive appended at `cycle-close` | none — the always-loaded tier, so keep it leanest |

Rules:

- **Keep deliverables lean; push detail out to linked files** (e.g. an `ADR-*.md`) rather than inlining it.
- **Compress and rotate the append-only logs.** Summarize or archive **resolved** rows of the state.md logs (keep the decision, drop the transcript); `build_loop.py` rotates `.agentloop/events.ndjson` past a size threshold (carrying open escalations forward, so pending items never rotate away) — do the equivalent by hand for the hand-maintained state.md tables. The defined moment for this hand-pruning is the short-tier checkpoint below (flush and GC are a pair).
- **Failures are summarized, not dumped** (`summarize_failure()` keeps only the salient lines). Follow the same discipline when you surface a failure yourself.
- **Prefer fetch-on-demand over holding everything.** Read the slice of a file you need; consult a doc when the task needs it.
- **Compact the session at clean checkpoints, not mid-flight.** `/compact` is a human-run command (the agent cannot execute it); the agent's part is to suggest it at the right moment. That moment is a phase boundary — right after a gate approval is recorded in `state.md` and the deliverables are committed — or a build-layer boundary in interactive mode: the next command rehydrates from the SSOT, so nothing is lost. Before suggesting it, pass every item of the **pre-compact check**:
  1. The gate decision is recorded and the deliverables committed.
  2. Every instruction/decision the human gave in conversation this phase is reflected in a deliverable or the SSOT — an observation with no home yet goes into a `state.md` log row first.
  3. No unanswered question or gate presentation is in flight.
  4. (Interactive build) no task is `in_progress`; completed tasks are merged and marked `done`.
  5. **Checkpoint GC** — apply "Compress and rotate" above to the resolved log rows at the same moment.

  If any item fails, do not suggest compacting. Never suggest it while a gate is pending decision, and suggesting/running `/compact` has no bearing on approvals — gate truth stays in `state.md`. After a compact, `/status` and each command's `state.md` re-read handle rehydration.

## Quality-check commands

The bundled `makefile` provides: `make test` (pytest), `make check` (= `make pre-commit` + `make pre-push`: lint / format / type-check, all of it — the gate uses this, since `pre-commit run --all-files` alone skips the pre-push-stage format/mypy/tsc hooks), `make test-tools` (self-tests of `scripts/agentloop/`), `make audit` (dependency vulnerabilities). If copied into a project without `make`, substitute that project's commands in `quality_gate.steps`.

## Security gate

Three layers: **gitleaks** at commit stage (in `make check`; false positives → `.gitleaksignore`) / **`/security-review`** mandatory at implementation completion (before gate ④ — deterministic mode A auto-runs it headless when all tasks are done and binds the report to the reviewed HEAD in `.agentloop/security-review.md`; config `build.post_build.security_review`) / **`/security-review` + `make audit`** mandatory in `/verify`, recorded in `docs/test/test-plan.md`.

## Branch / commit conventions

- Implement **on a work branch** (recorded in `branch` of `state.md`; created by `make init`), never directly on main. Parallel leaf tasks use worktree-derived branches (`<branch>/T-NNN`) merged back on completion.
- Per-task commits: **`T-NNN: <summary>`**, one commit = one task. Approving `/build` covers that loop's local commits (no per-commit confirmation).
- **Commit each phase's deliverables at its gate approval** (ADRs, task tickets, `state.md`/`tasks.yaml` updates) with a `docs: gate ③ tasks`-style message — not left uncommitted across the whole build.
- **Push / PR creation / merging to main are outward-facing** — only after separate human approval. **Writing to GitHub Issues is also outward-facing**: only with the `github.enabled: true` opt-in; `make issue-sync` mirrors one-way and never reads Issues back. Likewise `make feedback` (filing retrospective rows marked `upstream` as issues on the template repository): `github.feedback.enabled` opt-in and human-run.

## Tool-execution permissions (distinct from gate approvals)

**Gate approvals** (①–⑤) are the Human-on-the-Loop essence — never reduce them. **Tool-execution permission prompts** are separate: pre-authorizing known-safe commands in `.claude/settings.json`'s `permissions.allow` cuts repeated prompts without touching the gates. Keep the shared, template-owned `settings.json` to **generic AgentLoop commands**; put **product-specific** ones (a product's run/smoke, its test command) in that product's own committed `settings.json` (shared with the team and the build loop), so the template's additive-merge upgrade path stays clean. Destructive / outward-facing actions (push, PR, merge, `make cycle-close`) stay human-run — never add them to `allow`.

## Directories

- `.agentloop/` — SSOT (`state.md`, `tasks.yaml`, `config.yaml`) + the structured event log (`events.ndjson`)
- `scripts/agentloop/` — deterministic orchestration (`dag.py`, `build_loop.py`, `events.py`, `gate_guard.py`, `issue_sync.py`, `revise.py`, `init.py`, `adopt.py`, `cycle.py`, `feedback.py`, `template_lint.py`). **Product scripts go directly under `scripts/`, not mixed in here.**
- `docs/` — phase deliverables; `docs/retrospective.md` holds the retrospective at `done`
- `.claude/commands/` — per-phase entry points (the procedure detail lives here)
- `.claude/agents/` — specialized subagents
