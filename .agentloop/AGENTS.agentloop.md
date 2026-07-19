# AgentLoop — Agent Operating Rules

AgentLoop develops software **Human on the Loop**: a coding agent performs the work, produces
the deliverables, and self-tests at every phase; **humans only review and approve/decide at
the "gate" on each phase boundary**. The machinery is an installed CLI (`agentloop`, from
`uv tool install git+<the agentloop repo>`); the repository carries only its **state** —
`.agentloop/` (the SSOT, the lock, and the materialized prompts/schema) and `docs/`
(deliverables).

This file holds the **always-true rules** for **every coding agent**; each phase's procedure
lives in `.agentloop/prompts/commands/*.md` — consult those when running a phase. Your
**capability mapping** (`CLAUDE.md` for Claude Code,
`.github/instructions/agentloop.instructions.md` for VS Code Copilot — each present only when
its integration is installed via `agentloop install claude|copilot`) says how to realize the
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
| `role-delegation` | delegate work to a role agent (`requirements-analyst` / `architect` / `implementer` / `adversarial-reviewer`) | adopt the role inline from `.agentloop/prompts/agents/<role>.md`, then return to the lead role; parallel leaves degrade to serial |
| `autonomous-build-iteration` | drive the /build loop without per-iteration human prompts | re-invoke the /build procedure each iteration |
| `command-preauthorization` | pre-authorize known-safe commands so they don't re-prompt | approve each command interactively |

## Language

Write conversation and deliverables (`docs/**`) in **the user's language**. The template files
(this file, the capability mappings, the wrappers, `.agentloop/prompts/**`, the `docs/**`
scaffolds) stay in English as the canonical source; deliverable **headings** may be localized.
Machine-read vocabulary (`pending`/`approved`, task `status` and `kind` values) stays as-is in
every language.

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

Check progress anytime with `/status`; humans also have `agentloop next` (the next command
only) and `agentloop ui` for the same board in a local browser (next command
computed in code; a fixed whitelist of safe operations, never phase execution).
At `done`, `/verify` records `docs/retrospective.md` and closes open logs. An upstream defect
rolls back via `/revise` (see "Roll back").

**Cycles**: an ongoing repo repeats this lifecycle as **delta cycles** — each cycle's docs
describe one change. After `done`, the human runs `agentloop cycle-close --name <slug>`: deliverables
archive to `docs/archive/`, gates/phase reset; `docs/00-product-brief.md` and the baseline
`docs/05-current-state.md` persist (in a brownfield repo the latter is the existing codebase's
baseline — `/req`/`/design` read it first; traceability R-N / NFR-N covers the delta only).

**Mid-cycle scope change / hotfix / abandonment** (each a human decision): a non-defect scope
addition defers to the next cycle or reopens gate ① via `/revise` — never widen approved
requirements silently. An emergency hotfix is a *minimal* delta cycle (gates in order,
one-paragraph deliverables); if even that is too slow the human fixes outside the loop — log
the escalation, fold it into `docs/05-current-state.md` at the next `/verify`. Abandonment is
`agentloop cycle-close --name abandoned-<slug>` (archives partials, resets gates/phase).

## Single Source of Truth (SSOT)

The truth is split across three files with distinct roles — do not conflate them:

- **`.agentloop/state.md`** — phase, gate approvals, and the logs. **Read it when starting
  work; update it after.** `gates.<name>` is `pending` | `approved` — **never set `approved`
  without human approval.** The escalation log's machine truth is `.agentloop/events.ndjson`
  (`agentloop events`; created on first event); state.md embeds only the generated view.
- **`.agentloop/tasks.yaml`** — the machine-readable truth of the task DAG (`/tasks` generates
  it; `build_loop.py`/`dag.py` read it). `req` threads traceability requirements → design →
  tasks (`R-N` / `NFR-N`), cross-checked by `agentloop dag --trace`. Derived values (fan-out,
  frontier, layers, critical path) are **never stored**. GitHub Issues, when enabled, are a
  one-way mirror, never read back.
- **`.agentloop/config.yaml`** — deterministic-execution knobs **and the single DoD definition
  (`quality_gate.steps`)**.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command
   checks its prerequisite up front; if unapproved, stop and tell the human what is needed.
2. **Only humans open a gate.** Go only as far as an `approval-presentation`; record the
   approval only after a human acknowledges it or says an explicit "approve". **Invoking the
   next-phase command is not itself approval.** Recording is an **operation, not a file
   edit**: `agentloop approve <gate> [--by <approver>]` stamps
   the date/approver comment on the gate line (e.g. `tasks: approved   # 2026-07-07 alice`),
   advances `current_phase`, and logs the `gate_approved` event. Never edit a gate line to
   `approved` yourself — the guard denies it (see below); and never pre-authorize
   `agentloop approve` (its permission prompt is the human's confirmation).
3. **Do not silently fix problems in requirements/design.** On an upstream defect, set the
   affected task to `needs-revision`, record it in the escalation log, and raise it to the
   human.

**Enforcement is layered.** Convention: rule 1, checked by each command. Mechanism: the
installed `agentloop guard` denies in code at three checkpoints — **edit-time** (editor
hook on deliverable writes), **commit-stage** (`agentloop guard --check-diff` in pre-commit
/ the quality gate), and **merge-stage** (`agentloop build` re-checks every path a task
changed before it lands; violations escalate as `gate_violation`). Guarded paths:
`gates.guard_paths`; unreadable gates **fail closed**. Rule 2 has its
own mechanism layer: an edit that flips a state.md gate to `approved` is denied edit-time,
and a commit-stage flip without a matching `gate_approved` event fails — `agentloop approve` is
the only sanctioned write path (not relaxed by `template_mode`). Escape hatches:
`gates.enforce_hook: false`; `gates.template_mode: true` while the repo IS the template.
Detail: `gate_guard.py`'s docstring and the config comments.

## Roll back (returning upstream)

When an upstream defect is confirmed — or `/verify` reopens the build (`--to build`) — roll
back at the human's discretion with `/revise` (`agentloop revise`): **gates reset in a chain**
(the target phase's gate and everything downstream return to `pending`; an upstream `pending`
never leaves a downstream gate `approved`). **Rewinding approval is a human privilege** — the
agent never rolls back on its own. **Upstream fixes always entail task impact analysis**:
expand the affected set with `agentloop dag --impacted` and reclassify each task instead of
discarding tasks (procedure: the revise.md and tasks.md procedure files).

## Gate self-assessment (required)

At every gate (①–⑤), present a **self-assessment block** alongside the deliverable — surfacing
the system's own uncertainty is what lightens the human's review: **assumptions made**;
**confidence** (high / medium / low by area, always with a reason for low spots); **open
questions / points for the human to decide** (most important); **anticipated risks and
trade-offs**; and, when relevant, a **context-bloat signal** (propose trimming an outgrowing
deliverable or log). Do not pretend to high confidence to let the human skip verification.
For requirements/design/task tickets, put it in the deliverable itself (each scaffold's
"Self-assessment" section), not just spoken.

Self-assessment alone is not independent verification: gates ① and ② additionally require one
**adversarial-review round** by the `adversarial-reviewer` role (procedure: the req.md and
design.md procedure files) — blockers resolved, findings and dispositions recorded in the
deliverable's "Adversarial review" section. The human may waive it only for a hotfix minimal
cycle, logged in `state.md`.

## Minimizing the approval-wait bottleneck

Do not sit idle while a gate is `pending` — but **never compromise the gate**. Notify the
human immediately (`notify-and-wait`); batch questions into a single `structured-question`.
Pull forward **only outcome-independent work** (scaffolding, dev-env/CI setup, read-only
investigation, fixtures) — never deliverables premised on the pending decision. Speculative
work stays **outside `gates.guard_paths`** (`tests/` is deliberately unguarded for this); a
gate_guard denial marks the boundary — never disable the guard to push through it. It is
throwaway-by-default, recorded in the "speculative work log" of `state.md` (per-phase
specifics: each procedure file's "While waiting for approval" section). **Never set a gate to
`approved` on the grounds of speculative work.**

## Task dependency graph

Tasks form a **DAG**, not a flat list: kind = **foundation** (shared base) / **parallel**
(independent leaves) / **integration** (join); execution layers and the critical path derive
from `blockedBy`. Consumption order, parallelism (max 3, worktree-isolated), deterministic
ascending-id merge, and stopping run **in code** (`build_loop.py` / `dag.py`) — not by LLM
discretion. Procedure detail: the build.md and tasks.md procedure files.

## Principles

- **Reusing existing implementation comes first — and build only the minimum the acceptance
  criteria require (YAGNI).** Speculative generality (config knobs, hooks, abstractions no
  requirement names) is scope creep, not foresight — raise a genuine need instead.
- **Move forward only after passing the quality gate.** The DoD is defined **once**, as
  `quality_gate.steps` in `.agentloop/config.yaml` (default: `test` → `check` → `review` →
  `smoke`); a task is `done` only when every step passes. **For runnable deliverables, fill in
  the `smoke` step's command and set `required: true`.** In interactive `/build` the lead
  **re-runs each `cmd` step itself and reads its exit status** — a delegated agent's textual
  "green" is never evidence. Pipeline detail: the build.md procedure.
- **Durable lessons are promoted into the template, not archived away.** At gate ⑤ lift each
  keeper from `docs/retrospective.md` into the always-loaded files.
- **Small and sure.** One commit = one concern. Get approval before destructive or
  outward-facing operations.
- **Context isolation.** Delegate requirements/design/implementation to their role agents
  (`role-delegation`) so the main context stays clean.
- Write deliverable documents in the user's language (see "Language").

## Context budget (context hygiene)

More context is not better (*Context Rot*, *Lost in the Middle*); every session re-reads the
SSOT and deliverables, so keeping them lean is a first-class quality lever. **Memory lives in
three tiers, each with its own refresh cycle and exit** — no tier grows without bound:

| Tier | Lives in | Refresh cycle | Exit (folds into the next tier) |
|------|----------|---------------|--------------------------------|
| **Short** — session | conversation, open log rows in `state.md`, `in_progress` state | each checkpoint (gate approval / build-layer boundary): flush → compress resolved rows → suggest `session-compaction` | only decisions/outcomes survive, into deliverables and resolved log rows |
| **Mid** — cycle | phase deliverables (`docs/**`), `state.md`, retrospective | written per phase, committed at each gate; logs closed at `/verify` | archived by `agentloop cycle-close`; durable lessons promoted to the long tier |
| **Long** — permanent | `AGENTS.md`, the capability mappings, `.agentloop/prompts/**`, `docs/00-product-brief.md`, `docs/05-current-state.md`, `docs/archive/` | promotions at gate ⑤; `05-current-state.md` updated at `/verify`; archive appended at `cycle-close` | none — always loaded, keep it leanest |

Rules: **keep deliverables lean; push detail out to linked files** (e.g. an `ADR-*.md`).
**Compress and rotate the append-only logs** at each checkpoint — summarize resolved state.md
log rows, keep the decision, drop the transcript (`events.ndjson` rotates itself). **Failures
are summarized, not dumped.** **Prefer fetch-on-demand over holding everything** — read the
slice you need. **A `docs/notes/` memo is a record, not a permanent tier: once its lesson is
promoted (into `AGENTS.md`, an `ADR-*.md`, or the code) the note has served its purpose and is
deleted** — a note that never promotes-then-exits is how records accumulate (a copy that lands
in a product is deletable there; it is outside `upgrade`/`uninstall`).

**Compact the session at clean checkpoints, not mid-flight.** `session-compaction` is
human-run; the agent suggests it — only at a phase or build-layer boundary, and only when the
**pre-compact check** passes in full: (1) the gate decision is recorded and the deliverables
committed; (2) every instruction the human gave this phase is reflected in a deliverable or
the SSOT; (3) no unanswered question or gate presentation is in flight; (4) no task is
`in_progress`, completed tasks merged and `done`; (5) checkpoint GC applied to the resolved
log rows. If any item fails, do not suggest it. Compacting never touches gate truth; `/status`
rehydrates afterwards.

## Quality-check commands

The DoD's commands are the project's own, named once in `quality_gate.steps` of
`.agentloop/config.yaml` (the shipped defaults `make test` / `make check` are placeholders —
`agentloop init` fills detected commands in a brownfield repo; substitute yours otherwise).
`agentloop doctor` is the read-only environment + SSOT diagnosis — run it first when anything
behaves oddly. `agentloop sync --check` verifies the materialized prompts/schema still match
the installed tool's payload.

## Security gate

Three layers: **gitleaks** at commit stage (a pre-commit hook the project installs) / a
**security review** mandatory before gate ④ — mode A auto-runs it headless and binds the
report to the reviewed HEAD in `.agentloop/security-review.md`; otherwise run your agent's
security-review command or an equivalent pass, recorded the same way / a **security review +
a dependency audit** (e.g. `make audit`, pip-audit, npm audit) mandatory in `/verify`,
recorded in `docs/test/test-plan.md`.

## Branch / commit conventions

- Implement **on a work branch** (recorded in `branch` of `state.md`), never directly on main.
  Parallel leaves use worktree-derived branches (`<branch>-T-NNN`) merged back on completion.
- Per-task commits: **`T-NNN: <summary>`**, one commit = one task. **Commit each phase's
  deliverables at its gate approval** with a `docs: gate ③ tasks`-style message.
- **Push / PR creation / merging to main are outward-facing** — only after separate human
  approval. Writing to GitHub Issues likewise (`github.enabled: true` opt-in; one-way mirror).

## Tool-execution permissions (distinct from gate approvals)

**Gate approvals** (①–⑤) are the Human-on-the-Loop essence — never reduce them.
**Tool-execution permission prompts** are separate: `command-preauthorization` of known-safe
commands cuts repeated prompts without touching the gates. The installed settings hold only
**generic AgentLoop commands**; **product-specific** ones go in the product's own committed
settings. Destructive / outward-facing actions (push, PR, merge, `agentloop cycle-close`) stay
human-run — never pre-authorize them, and never pre-authorize `agentloop approve` either: its
permission prompt is the human's approval confirmation (gate rule 2).

## Directories

- `.agentloop/` — SSOT (`state.md`, `tasks.yaml`, `config.yaml`), the event log
  (`events.ndjson`, created on first event), **`agentloop.lock`** (which tool version wrote
  this repo's artifacts, with a hash per installed file), and the **materialized artifacts**
  the installed tool refreshes via `agentloop sync`: `prompts/` (shared phase procedures and
  role definitions), `schema/`, and `AGENTS.agentloop.md` (the rules body)
- `docs/` — phase deliverables; `docs/retrospective.md` holds the retrospective at `done`
- `.claude/commands/`, `.github/prompts/` — per-agent entry points (thin wrappers over
  `.agentloop/prompts/commands/`), present only where `agentloop install <agent>` was run
- `.claude/agents/`, `.github/agents/` — role-agent wrappers (role definitions in
  `.agentloop/prompts/agents/`)
- the orchestration code itself lives in the installed `agentloop` package, not in the repo
