# AgentLoopTemplate — Agent Operating Rules

This repository is a template for developing software **Human on the Loop**: a coding agent
performs the work, produces the deliverables, and self-tests at every phase; **humans only
review and approve/decide at the "gate" on each phase boundary**.

This file holds the **always-true rules** for **every coding agent**; each phase's procedure
lives in `.agentloop/prompts/commands/*.md` — consult those when running a phase. Your
**capability mapping** (`CLAUDE.md` for Claude Code,
`.github/instructions/agentloop.instructions.md` for VS Code Copilot) says how to realize the
capability vocabulary below; with no mapping file (e.g. Codex, reading this natively), use the
table's degradation column.

## Capability vocabulary (portable verbs)

The rules and procedures name human-interaction points with these neutral capabilities, never
an agent-specific tool name; the degradation column applies when your environment lacks a
mechanism.

| Capability | Meaning | If your environment lacks it |
|---|---|---|
| `phase-invocation` | run a phase procedure (`/req` … `/status`) | when the human names a phase, read `.agentloop/prompts/commands/<name>.md` and execute it |
| `structured-question` | batched multiple-choice questions to the human | ask numbered options in chat, end the turn, and wait |
| `notify-and-wait` | alert the human an approval/decision is pending, then stop | state what is pending and end the turn |
| `approval-presentation` | present a deliverable for explicit human approval | show the summary and ask for an explicit "approve" |
| `session-compaction` | human-run session compaction/reset at a checkpoint | the human starts a fresh session; the SSOT rehydrates |
| `role-delegation` | delegate work to a role agent (`requirements-analyst` / `architect` / `implementer`) | adopt the role inline from `.agentloop/prompts/agents/<role>.md`, then return to the lead role; parallel leaves degrade to serial |
| `autonomous-build-iteration` | drive the /build loop without per-iteration human prompts | re-invoke the /build procedure each iteration |
| `command-preauthorization` | pre-authorize known-safe commands so they don't re-prompt | approve each command interactively |

## Language

Write conversation and deliverables (`docs/**`) in **the user's language — the project's
primary language**. The template files (this file, the capability mappings, the wrappers,
`.agentloop/prompts/**`, the `docs/**` scaffolds) stay in English as the canonical source;
deliverable **headings** may be localized when filling a scaffold in. Machine-read vocabulary
(`pending`/`approved`, task `status` and `kind` values) stays as-is in every language.

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

Check progress anytime with `/status`. At `done`, `/verify` records `docs/retrospective.md`
and closes open logs. An upstream defect rolls back via `/revise` (see "Roll back").

**Cycles**: an ongoing repo repeats this lifecycle as **delta cycles** — each cycle's docs
describe one change. After `done`, the human runs `make cycle-close NAME=<slug>`: deliverables
archive to `docs/archive/`, gates/phase reset; `docs/00-product-brief.md` and the baseline
`docs/05-current-state.md` persist. In an adopted (brownfield) repo the latter is the baseline
of the existing codebase — `/req`/`/design` read it first; traceability (R-N / NFR-N) covers
the cycle's delta only, never reverse-generated from existing behavior.

**Mid-cycle scope change / hotfix / abandonment** (each a human decision):
- **Non-defect scope addition**: defer to the next cycle, or reopen gate ① via `/revise` and
  re-approve — never widen approved requirements silently.
- **Emergency hotfix**: a *minimal* delta cycle (gates in order, one-paragraph deliverables);
  if even that is too slow, the human fixes/approves outside the loop — log the escalation,
  fold it into `docs/05-current-state.md` at the next `/verify`.
- **Abandonment**: `make cycle-close NAME=abandoned-<slug>` archives the partials and resets
  gates/phase; handle the work branch by hand.

## Single Source of Truth (SSOT)

The truth is split across three files with distinct roles — do not conflate them:

- **`.agentloop/state.md`** — phase, gate approvals, and the logs (speculative / escalation /
  roll-back). **Read it when starting work; update it after.** `gates.<name>` is `pending` |
  `approved` — **never set `approved` without human approval.** The escalation log's machine
  truth is **`.agentloop/events.ndjson`** (`make events`; `build_loop.py` appends); state.md
  embeds only the generated view between its `ESCALATION-VIEW` markers.
- **`.agentloop/tasks.yaml`** — the machine-readable truth of the task DAG. `/tasks` generates
  it (schema: the tasks.md procedure); `build_loop.py`/`dag.py` read it. `req` threads
  traceability requirements → design → tasks (`R-N` functional / `NFR-N` non-functional),
  cross-checked by `dag.py --trace` and into the test plan at `/verify`. Derived values
  (fan-out, frontier, layers, critical path) are **not stored** — the state.md task table is a
  generated view. GitHub Issues, when enabled, are a one-way mirror, never read back.
- **`.agentloop/config.yaml`** — deterministic-execution knobs (parallelism, worktree, gate
  enforcement) **and the single DoD definition (`quality_gate.steps`)**.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command
   checks its prerequisite up front (`/design`←requirements, `/tasks`←design, `/build`←tasks,
   `/verify`←build); if unapproved, stop and tell the human what is needed.
2. **Only humans open a gate.** Go only as far as an `approval-presentation`; set the gate to
   `approved` in `state.md` only after a human acknowledges it or says an explicit "approve".
   **Invoking the next-phase command is not itself approval** — stop and ask. Record the date
   (and approver, when several humans share the repo) as a YAML comment on the gate line
   (e.g. `tasks: approved   # 2026-07-07 alice`).
3. **Do not silently fix problems in requirements/design.** On an upstream defect, set the
   affected task to `needs-revision`, record it in the escalation log, and raise it to the
   human.

**Enforcement is two-layered.** Convention: rule 1, checked by each command. Mechanism:
`scripts/agentloop/gate_guard.py` denies in code at two checkpoints — **edit-time**, an editor
hook denying Write/Edit to a next-phase deliverable path while its gate is unapproved
(registration host is per-agent — see your capability mapping), and **commit-stage**,
agent-agnostic: the guard's `--check-diff` mode, run by pre-commit and `make check`, fails on
any pending-gate path in the diff vs HEAD — also catching agents with no edit hook and edits
that bypass one. Guarded paths: `gates.guard_paths`; `scripts/agentloop/**` is always allowed;
unreadable gates **fail closed**. `/build` additionally code-checks `gates.tasks==approved` at
start. Escape hatches: `gates.enforce_hook: false`; `gates.template_mode: true` while the repo
IS the template (`make init` flips it off). Detail: `gate_guard.py`'s docstring and the config
comments.

## Roll back (returning upstream)

When an upstream (requirements/design) defect is confirmed — or `/verify` reopens the build
(`--to build`) — roll back at the human's discretion with `/revise` (`make revise`):

- **Gates reset in a chain** (`revise.py`): the target phase's gate and everything downstream
  return to `pending`. Invariant: an upstream `pending` never leaves a downstream gate
  `approved`; the editing order from then on is enforced by `gate_guard`.
- **Rewinding approval is a human privilege** — the agent never rolls back on its own; the
  trigger is a `needs-revision` raised during implementation (gate rule 3).
- **Upstream fixes always entail task impact analysis**: expand the affected set with
  `dag.py --impacted` and reclassify each task instead of discarding tasks (procedure: the
  revise.md and tasks.md procedure files).

## Gate self-assessment (required)

At every gate (①–⑤), present a **self-assessment block** alongside the deliverable — surfacing
the system's own uncertainty is what lightens the human's review:

- **Assumptions made** (points where, if wrong, the deliverable breaks).
- **Confidence**: high / medium / low, split by area — **always with a reason for low spots**.
- **Open questions / points for the human to decide** (most important).
- **Anticipated risks and trade-offs.**
- **Context-bloat signal** (when relevant): if a deliverable or log risks *Context Rot* /
  *Lost in the Middle*, propose trimming (link detail out to an ADR; archive resolved log
  rows).

Do not pretend to high confidence to let the human skip verification. For
requirements/design/task tickets, put the self-assessment in the deliverable itself (each
scaffold's "Self-assessment" section), not just spoken.

## Minimizing the approval-wait bottleneck

Do not sit idle while a gate is `pending` — but **never compromise the gate**:

- Notify the human immediately (`notify-and-wait`); batch questions into a single
  `structured-question`. Past its practical limits, ask only the blocking decisions and leave
  the rest as "Open questions" in the self-assessment.
- Pull forward **only outcome-independent work** (scaffolding, dev-env/CI setup, read-only
  investigation, fixtures) — **never** deliverables premised on the pending decision.
  Speculative work is throwaway-by-default, recorded in the "speculative work log" of
  `state.md` (per-phase specifics: each procedure file's "While waiting for approval" section).
- **Never set a gate to `approved` on the grounds of speculative work.**

## Task dependency graph

Tasks form a **DAG**, not a flat list: kind = **foundation** (shared base) / **parallel**
(independent leaves) / **integration** (join); execution layers and the critical path derive
from `blockedBy`. Consumption order, parallelism (max 3, worktree-isolated), deterministic
ascending-id merge, and stopping run **in code** (`build_loop.py`, derivation unified in
`dag.py`) — not by LLM discretion — and the chain is reassembled after every completed task.
Procedure detail: the build.md and tasks.md procedure files.

## Principles

- **Reusing existing implementation comes first — and build only the minimum the acceptance
  criteria require (YAGNI).** Look for existing functions, utilities, and patterns before
  writing new code; then implement no more than the ticket asks for. Speculative generality
  (config knobs, hooks, abstractions no requirement names) is scope creep, not foresight —
  raise a genuine need instead of building it unasked.
- **Move forward only after passing the quality gate.** The DoD is defined **once**, as
  `quality_gate.steps` in `.agentloop/config.yaml`; a task is `done` only when every step
  passes. **For runnable deliverables, fill in the `smoke` step's command and set
  `required: true`** — a forgotten launch check must refuse to build, not silently skip. In
  interactive `/build` the lead **re-runs each `cmd` step itself and reads its exit status**
  before marking `done` — a delegated agent's textual "green" is never evidence. Pipeline
  detail: the build.md procedure.
- **Durable lessons are promoted into the template, not archived away.**
  `docs/retrospective.md` is per-cycle; at gate ⑤ lift each keeper into the always-loaded
  files (this file, `.agentloop/prompts/**`, the capability mappings).
- **Small and sure.** One commit = one concern. Get approval before destructive or
  outward-facing operations.
- **Context isolation.** Delegate requirements/design/implementation to their role agents
  (`role-delegation`; definitions in `.agentloop/prompts/agents/`) so the main context stays
  clean.
- Write deliverable documents in the user's language (see "Language").

## Context budget (context hygiene)

More context is not better (*Context Rot*, *Lost in the Middle*); every session re-reads the
SSOT and deliverables, so keeping them lean is a first-class quality lever. **Memory lives in
three tiers, each with its own refresh cycle and exit** — no tier grows without bound:

| Tier | Lives in | Refresh cycle | Exit (folds into the next tier) |
|------|----------|---------------|--------------------------------|
| **Short** — session | conversation, open log rows in `state.md`, `in_progress` state | each checkpoint (gate approval / build-layer boundary): flush → compress resolved rows → suggest `session-compaction` | only decisions/outcomes survive, into deliverables and resolved log rows |
| **Mid** — cycle | phase deliverables (`docs/**`), `state.md`, retrospective | written per phase, committed at each gate; logs closed at `/verify` | archived by `make cycle-close`; durable lessons promoted to the long tier |
| **Long** — permanent | `AGENTS.md`, the capability mappings, `.agentloop/prompts/**`, `docs/00-product-brief.md`, `docs/05-current-state.md`, `docs/archive/` | promotions at gate ⑤; `05-current-state.md` updated at `/verify`; archive appended at `cycle-close` | none — always loaded, keep it leanest |

Rules:

- **Keep deliverables lean; push detail out to linked files** (e.g. an `ADR-*.md`).
- **Compress and rotate the append-only logs**: at the checkpoint below, summarize or archive
  **resolved** state.md log rows — keep the decision, drop the transcript. (`events.ndjson`
  rotates itself, carrying open escalations forward.)
- **Failures are summarized, not dumped** (`summarize_failure()` keeps the salient lines) —
  same discipline when you surface a failure yourself.
- **Prefer fetch-on-demand over holding everything**: read the slice of a file you need;
  consult a doc when the task needs it.
- **Compact the session at clean checkpoints, not mid-flight.** `session-compaction` is
  human-run; the agent suggests it — only at a phase boundary (gate approval recorded,
  deliverables committed) or a build-layer boundary, where the next command rehydrates from
  the SSOT, and only when the **pre-compact check** passes in full:
  1. The gate decision is recorded and the deliverables committed.
  2. Every instruction/decision the human gave this phase is reflected in a deliverable or
     the SSOT — an observation with no home yet goes into a `state.md` log row first.
  3. No unanswered question or gate presentation is in flight.
  4. (Interactive build) no task is `in_progress`; completed tasks are merged and marked
     `done`.
  5. **Checkpoint GC** — apply "Compress and rotate" above to the resolved log rows now.

  If any item fails — or a gate is pending decision — do not suggest it. Compacting never
  touches gate truth (`state.md`); `/status` and each command's SSOT re-read rehydrate
  afterwards.

## Quality-check commands

The bundled `makefile` provides `make test` (pytest), `make check` (lint / format /
type-check, both hook stages — the gate uses this, never `pre-commit run` alone),
`make test-tools` (self-tests of `scripts/agentloop/`), `make audit` (dependency
vulnerabilities), and `make doctor` (read-only environment + SSOT diagnosis — run it first
when anything behaves oddly). In a project without `make`, substitute its commands in
`quality_gate.steps`.

## Security gate

Three layers: **gitleaks** at commit stage (in `make check`; false positives →
`.gitleaksignore`) / a **security review** mandatory before gate ④ — deterministic mode A
auto-runs it headless and binds the report to the reviewed HEAD in
`.agentloop/security-review.md` (config `build.post_build.security_review`); otherwise run
your agent's security-review command (see your capability mapping) or an equivalent
security-focused pass, recorded the same way / a **security review + `make audit`** mandatory
in `/verify`, recorded in `docs/test/test-plan.md`.

## Branch / commit conventions

- Implement **on a work branch** (recorded in `branch` of `state.md`; created by
  `make init`), never directly on main. Parallel leaves use worktree-derived branches
  (`<branch>-T-NNN`) merged back on completion.
- Per-task commits: **`T-NNN: <summary>`**, one commit = one task. Approving `/build` covers
  that loop's local commits.
- **Commit each phase's deliverables at its gate approval** (ADRs, tickets,
  `state.md`/`tasks.yaml` updates) with a `docs: gate ③ tasks`-style message — not left
  uncommitted across the whole build.
- **Push / PR creation / merging to main are outward-facing** — only after separate human
  approval. Writing to GitHub Issues likewise: only with the `github.enabled: true` opt-in;
  `make issue-sync` mirrors one-way and never reads Issues back.

## Tool-execution permissions (distinct from gate approvals)

**Gate approvals** (①–⑤) are the Human-on-the-Loop essence — never reduce them.
**Tool-execution permission prompts** are separate: `command-preauthorization` of known-safe
commands (in your agent's permission settings — see your capability mapping) cuts repeated
prompts without touching the gates. Template-owned settings hold only **generic AgentLoop
commands**; **product-specific** ones (run/smoke, the product's tests) go in the product's own
committed settings, keeping the additive-merge upgrade path clean. Destructive /
outward-facing actions (push, PR, merge, `make cycle-close`) stay human-run — never
pre-authorize them.

## Directories

- `.agentloop/` — SSOT (`state.md`, `tasks.yaml`, `config.yaml`) + the event log
  (`events.ndjson`) + **`prompts/`** (the shared phase procedures and role definitions every
  agent reads)
- `scripts/agentloop/` — deterministic orchestration (`dag.py`, `build_loop.py`,
  `gate_guard.py`, `events.py`, `doctor.py`, `revise.py`, `adopt.py`, …). **Product scripts
  go directly under `scripts/`, not mixed in here.**
- `docs/` — phase deliverables; `docs/retrospective.md` holds the retrospective at `done`
- `.claude/commands/`, `.github/prompts/` — per-agent entry points (thin wrappers over
  `.agentloop/prompts/commands/`)
- `.claude/agents/`, `.github/agents/` — role-agent wrappers (role definitions in
  `.agentloop/prompts/agents/`)
