# Phase-scoped operating rules (gate workflow)

Read by every phase command (`/req` `/design` `/tasks` `/build` `/verify` `/onboard`) on top
of the always-loaded core rules (AGENTS.md). Everything here applies while a phase procedure
is running; the core's gate rules apply at all times regardless.

## Gate self-assessment (required at every gate)

At every gate (①–⑤), present a **self-assessment block** alongside the deliverable — surfacing
the system's own uncertainty is what lightens the human's review: **assumptions made**;
**confidence** (high / medium / low by area, always with a reason for low spots); **open
questions / points for the human to decide** (most important); **anticipated risks and
trade-offs**; and, when relevant, a **context-bloat signal** (propose trimming an outgrowing
deliverable or log). Do not pretend to high confidence to let the human skip verification.
For requirements/design/task tickets, put it in the deliverable itself (each scaffold's
"Self-assessment" section), not just spoken.

Self-assessment alone is not independent verification: gates ①–③ additionally require one
**adversarial-review round** by the `adversarial-reviewer` role — procedure and recording:
the req.md, design.md, and tasks.md procedure files. The human may waive it only for a
hotfix minimal cycle, logged in `state.md`.

## While a gate is pending

Do not sit idle — but **never compromise the gate**. Notify the human immediately
(`notify-and-wait`); batch questions into a single `structured-question`. Pull forward **only
outcome-independent work** (scaffolding, dev-env/CI setup, read-only investigation, fixtures)
— never deliverables premised on the pending decision. Speculative work stays **outside
`gates.guard_paths`** (`tests/` is deliberately unguarded for this); a gate_guard denial marks
the boundary. It is throwaway-by-default, recorded in the "speculative work log" of `state.md`
(per-phase specifics: each procedure file's "While waiting for approval" section).

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

## Cycles, scope changes, hotfixes

An ongoing repo repeats the lifecycle as **delta cycles** — each cycle's docs describe one
change. After `done`, the human runs `agentloop cycle-close --name <slug>`: deliverables
archive to `docs/archive/`, gates/phase reset; `docs/00-product-brief.md` and the baseline
`docs/05-current-state.md` persist (in a brownfield repo the latter is the existing codebase's
baseline — `/req`/`/design` read it first; traceability R-N / NFR-N covers the delta only).

**Mid-cycle scope change / hotfix / abandonment** (each a human decision): a non-defect scope
addition defers to the next cycle or reopens gate ① via `/revise`. An emergency hotfix is a
*minimal* delta cycle (gates in order, one-paragraph deliverables); if even that is too slow
the human fixes outside the loop — log the escalation, fold it into `docs/05-current-state.md`
at the next `/verify`. Abandonment is `agentloop cycle-close --name abandoned-<slug>`
(archives partials, resets gates/phase).

## Enforcement detail (the gate rules' mechanism layer)

The installed `agentloop guard` denies in code at three checkpoints — **edit-time** (editor
hook on deliverable writes), **commit-stage** (`agentloop guard --check-diff` in pre-commit /
the quality gate), and **merge-stage** (`agentloop build` re-checks every path a task changed
before it lands; violations escalate as `gate_violation`). Guarded paths: `gates.guard_paths`.
A `state.yaml` gate flip to `approved` written by hand is denied — the only write path is
`agentloop attestation import` of a request signed by a Trust-Manifest key. `agentloop approve`
machine-checks the gate's bound evidence (unresolved `[NEEDS CLARIFICATION]` markers, the
review's `subject_head_sha` freshness, coverage sufficiency, blocking findings, a frozen human
review, open escalations) and refuses to emit a request when it is missing — there is **no
`--force`**. The only standing exception is `gates.template_mode: true` while the repo IS the
template. Detail: `gate_guard.py`'s docstring and the config comments.

## Repo map

- `.agentloop/` — SSOT (`state.md`, `tasks.yaml`, `config.yaml`), the event log
  (`events.ndjson`, created on first event), `agentloop.lock` (tool version + a hash per
  installed file), and the materialized artifacts `agentloop sync` refreshes: `prompts/`
  (phase procedures, role definitions, these rules modules), `schema/`, and
  `AGENTS.agentloop.md` (the core rules body)
- `docs/` — phase deliverables; `docs/retrospective.md` holds the retrospective at `done`
- `.claude/commands/`, `.github/prompts/` — per-agent entry points (thin wrappers over
  `.agentloop/prompts/commands/`), present only where `agentloop install <agent>` was run;
  `.claude/agents/`, `.github/agents/` — role-agent wrappers
