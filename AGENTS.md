# AgentLoopTemplate — Agent Operating Rules

This repository is a template for developing software **Human on the Loop**.
A coding agent performs the work, produces the deliverables, and self-tests at every phase,
while **humans only review and approve/decide at the "gate" on each phase boundary**.

This file holds the **always-true rules** and is the canonical rules file for **every coding agent**
(Claude Code, VS Code GitHub Copilot, Codex, …). Each phase's procedure lives in its procedure file
(`.agentloop/prompts/commands/*.md`) — consult those when running a phase, not this file.
How each agent realizes the capability vocabulary below lives in its **capability mapping**:
`CLAUDE.md` for Claude Code, `.github/instructions/agentloop.instructions.md` for VS Code Copilot.
An agent with no mapping file (e.g. Codex, which reads this file natively) uses the degradation
column built into the vocabulary table itself.

## Capability vocabulary (portable verbs)

The rules and procedures name human-interaction points with these neutral capabilities, never with
an agent-specific tool name. Your capability mapping says how to realize each one; the degradation
column applies when your environment has no such mechanism.

| Capability | Meaning | If your environment lacks it |
|---|---|---|
| `phase-invocation` | run a phase procedure (`/req` … `/status`) | when the human names a phase, read `.agentloop/prompts/commands/<name>.md` and execute that procedure |
| `structured-question` | present the human a batched, multiple-choice question set and wait | ask numbered options in chat, end your turn, and wait |
| `notify-and-wait` | alert the human that an approval/decision is pending, then stop | state explicitly what is pending and end your turn |
| `approval-presentation` | present a deliverable for explicit human approval | show the summary in chat and ask for an explicit "approve" |
| `session-compaction` | human-run compaction/reset of the session at a checkpoint | the human starts a fresh session; the SSOT rehydrates |
| `role-delegation` | delegate work to a role agent (`requirements-analyst` / `architect` / `implementer`) | adopt the role inline: read its file in `.agentloop/prompts/agents/`, perform it, then return to the lead role (parallel leaves degrade to serial) |
| `autonomous-build-iteration` | keep driving the /build loop without per-iteration human prompts | re-invoke the /build procedure each iteration |
| `command-preauthorization` | pre-authorize known-safe commands so they don't re-prompt | approve each command interactively |

## Language

Write conversation and deliverables (`docs/**`) in **the language the user uses — i.e. the project's primary language**. The template files themselves (this `AGENTS.md`, the capability mappings, the wrappers under `.claude/**` and `.github/**`, the procedure files in `.agentloop/prompts/**`, the `docs/**` scaffolds) are written in English as the canonical single source; you may localize a deliverable's **headings** when filling a scaffold in. Identifiers and machine-read vocabulary (gate states `pending`/`approved`, task `status` values, `kind` values, etc.) stay as-is in every language.

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

**Cycles**: an ongoing repo runs this lifecycle repeatedly as **delta cycles** — each cycle's docs describe one change, not the whole product. After `done`, the human runs `make cycle-close NAME=<slug>` to archive the filled deliverables to `docs/archive/` and reset gates/phase for the next cycle (`docs/00-product-brief.md` and the baseline `docs/05-current-state.md` persist). In an adopted (brownfield) repo, `docs/05-current-state.md` is the persistent baseline of the existing codebase — `/req`/`/design` read it first, and traceability (R-N / NFR-N) applies to each cycle's delta only, never reverse-generated from existing behavior. (Setup/upgrade/uninstall mechanics — the adopt-manifest — are README material, not agent rules.)

**Mid-cycle scope change / hotfix / abandonment** (each a human decision):
- **A scope addition that is not a defect** never widens approved requirements silently. Default: defer it to the next delta cycle. If it must land now, reopen gate ① (`/revise` to requirements) and re-approve the enlarged scope — traceability stays intact.
- **An emergency hotfix** runs as a *minimal* delta cycle: the gates still open in order, but each deliverable may be a paragraph. If even that is too slow, the human fixes and approves directly outside the loop; record it in the escalation log and fold it into `docs/05-current-state.md` at the next cycle's `/verify`.
- **Abandoning a cycle**: archive it as-is with `make cycle-close NAME=abandoned-<slug>` — the partial deliverables land in `docs/archive/` for the record and gates/phase reset. Keep or delete the work branch by hand.

## Single Source of Truth (SSOT)

The truth is split across three files. Their roles differ, so do not conflate them:

- **`.agentloop/state.md`** — the truth for the phase, each gate's approval status, and the logs (speculative / escalation / roll-back). **Always read it when starting work**; update it after work (phase progress, `updated_at`). `gates.<name>` is `pending` | `approved`. **Never set it to `approved` without human approval.** The escalation log's machine-readable truth is **`.agentloop/events.ndjson`** (structured events, managed via `make events`; `build_loop.py` appends automatically) — state.md embeds only the generated view between its `ESCALATION-VIEW` markers.
- **`.agentloop/tasks.yaml`** — the **machine-readable truth** of the task graph (DAG). `/tasks` generates it (schema: see `.agentloop/prompts/commands/tasks.md`); `build_loop.py` and `dag.py` read it. `req` is the traceability thread requirements → design → tasks (IDs `R-N` functional / `NFR-N` non-functional), mechanically cross-checked by `dag.py --trace` (and into the test plan at `/verify`). Derived values (fan-out, frontier, layers, critical path) are **not stored** (preventing drift); the task table in state.md is the generated human-facing view. Even with GitHub Issues integration enabled, tasks.yaml remains the SSOT and Issues are a one-way mirror (never read back).
- **`.agentloop/config.yaml`** — the source of knobs for deterministic execution (parallelism, worktree, gate enforcement) **and the single definition of the DoD (`quality_gate.steps`)**. Read by `build_loop.py`/`gate_guard.py`.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command checks its prerequisite gate up front (`/design`←requirements, `/tasks`←design, `/build`←tasks, `/verify`←build). If unapproved, stop and tell the human what is needed.
2. **Only humans open a gate.** The agent goes only as far as an `approval-presentation`. Only after a human signals approval (acknowledging that presentation, or an explicit "approve") do you set the gate in `state.md` to `approved` and move on. **Invoking the next-phase command is not itself approval** — running `/verify` while `build` is `pending` does not consent to gate ④; stop and ask for explicit approval. When recording an approval, append the date — and, when several humans share the repo, who approved — as a YAML comment on the gate line (e.g. `tasks: approved   # 2026-07-07 alice`; the tooling preserves comments).
3. **Do not silently fix problems in requirements/design.** On finding an upstream defect, set the affected task to `needs-revision`, record it in the escalation log, and raise it to the human.

**Gates are enforced in two layers**:
- **Convention layer**: rule 1 above, checked by each command.
- **Mechanism layer**: `scripts/agentloop/gate_guard.py` — a PreToolUse hook registered in `.claude/settings.json` (Claude Code) and `.github/hooks/agentloop.json` (VS Code Copilot) — **denies** in code any Write/Edit to a next-phase deliverable path while its prerequisite gate is unapproved. Watched paths and the gate each requires come from `gates.guard_paths` in config; `scripts/agentloop/**` (the template's foundational tools) is always allowed; if state.md's gates are unreadable the guard **fails closed** for guarded paths. In an environment whose hooks cannot intercept file edits (e.g. Codex), only the convention layer applies — the gates still hold by rule, not by code. `/build` additionally code-checks `gates.tasks==approved` at the start of `build_loop.py`. Escape hatches: `gates.enforce_hook: false`, and `gates.template_mode: true` while the repo IS the template itself (scaffold originals share product paths; `make init` flips it off so the guard goes live). Reference detail — default paths, brownfield scoping, how the hook launches — lives in `gate_guard.py`'s docstring and the config comments.

## Roll back (returning upstream)

When an upstream (requirements/design) defect is confirmed — or `/verify` finds an implementation defect serious enough to reopen the build (`--to build`) — roll back at the human's discretion with `/revise` (`make revise`):

- **Gates reset in a chain**: every gate from the target phase onward goes back to `pending` (`revise.py`). Invariant: if an upstream gate is `pending`, no downstream gate stays `approved`. The editing order from then on is enforced by `gate_guard`.
- **Rewinding approval is a human privilege**, symmetric with opening a gate — the agent never rolls back on its own. The detection trigger is a `needs-revision` raised during implementation (gate rule 3).
- **Upstream fixes always entail task impact analysis**: do not throw tasks away — expand the affected set with `dag.py --impacted` and reclassify each task (procedure: `.agentloop/prompts/commands/revise.md` and `tasks.md`).

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

- Notify the human immediately (`notify-and-wait`); batch questions into a single `structured-question`. When they exceed its practical limits (question/option caps), ask only the decisions that block progress and leave the rest as "Open questions" in the deliverable's self-assessment.
- You may pull forward **only outcome-independent work** (scaffolding, dev-env/CI setup, read-only investigation, fixtures). **Never** produce deliverables premised on the pending decision. Speculative work is throwaway-by-default and recorded in the "speculative work log" of `state.md` (per-phase specifics: each procedure file's "While waiting for approval" section).
- **Never set a gate to `approved` on the grounds of speculative work.**

## Task dependency graph

Tasks form a **DAG**, not a flat list: kind = **foundation** (shared base) / **parallel** (independent leaves) / **integration** (join). Execution layers and the critical path are derived from `blockedBy`. Consumption order, parallelism (max 3, worktree-isolated), deterministic ascending-id merge, and stopping are run **deterministically in code** by `build_loop.py` (`make build-loop`), with the derivation logic unified in `dag.py` — not left to LLM discretion. The chain is reassembled every time a task completes. Procedure details: `.agentloop/prompts/commands/build.md` and `tasks.md`.

## Principles

- **Reusing existing implementation comes first.** Before writing new code, look for existing functions, utilities, and patterns.
- **Move forward only after passing the quality gate.** The DoD is defined **once**, as `quality_gate.steps` in `.agentloop/config.yaml`; `build_loop.py` runs exactly that list, and a task is `done` only when every step passes. **For runnable deliverables (CLI, server, etc.), fill in the `smoke` step's command and set its `required: true`** — a forgotten launch check must refuse to build, not silently skip. In interactive mode (`autonomous-build-iteration` of `/build`) the lead **re-runs each `cmd` step itself and reads its exit status** before marking `done` — a delegated agent's textual "green" is never evidence. Pipeline detail (step list, task-test, retry budgets, integration gate): `.agentloop/prompts/commands/build.md`.
- **Durable lessons are promoted into the template, not archived away.** `docs/retrospective.md` is per-cycle and archived at `cycle-close`; at gate ⑤ lift each keeper into the always-loaded files (`AGENTS.md`, the procedure files in `.agentloop/prompts/**`, the capability mappings) rather than leave it only in the retrospective or a product's `state.md`.
- **Small and sure.** One commit = one concern. Get approval before destructive or outward-facing operations.
- **Context isolation.** Delegate requirements/design/implementation to their dedicated role agents (`role-delegation`; role definitions in `.agentloop/prompts/agents/`) so the main context stays clean.
- Write deliverable documents in the user's language (see "Language").

## Context budget (context hygiene)

More context is not better — long inputs suffer *Context Rot* and *Lost in the Middle*. The main session and every delegated agent re-read the SSOT and deliverables, so keeping them lean is a first-class quality lever.

**Memory lives in three tiers, each with its own refresh cycle and exit** — so no tier grows without bound:

| Tier | Lives in | Refresh cycle | Exit (folds into the next tier) |
|------|----------|---------------|--------------------------------|
| **Short** — session | conversation, open log rows in `state.md`, `in_progress` state | every checkpoint (gate approval / build-layer boundary): flush → compress resolved rows → suggest `session-compaction` | only decisions/outcomes survive, into deliverables and resolved log rows |
| **Mid** — cycle | phase deliverables (`docs/**`), `state.md`, retrospective | written per phase, committed at each gate; logs closed at `/verify` | archived by `make cycle-close`; durable lessons promoted to the long tier |
| **Long** — permanent | `AGENTS.md`, the capability mappings, `.agentloop/prompts/**`, `docs/00-product-brief.md`, `docs/05-current-state.md`, `docs/archive/` | lessons promoted at gate ⑤; `05-current-state.md` updated at `/verify`; archive appended at `cycle-close` | none — the always-loaded tier, so keep it leanest |

Rules:

- **Keep deliverables lean; push detail out to linked files** (e.g. an `ADR-*.md`) rather than inlining it.
- **Compress and rotate the append-only logs.** Summarize or archive **resolved** state.md log rows (keep the decision, drop the transcript) at the short-tier checkpoint below — flush and GC are a pair. (`events.ndjson` rotates itself, carrying open escalations forward.)
- **Failures are summarized, not dumped** (`summarize_failure()` keeps only the salient lines). Follow the same discipline when you surface a failure yourself.
- **Prefer fetch-on-demand over holding everything.** Read the slice of a file you need; consult a doc when the task needs it.
- **Compact the session at clean checkpoints, not mid-flight.** `session-compaction` is a human-run operation (the agent cannot execute it); the agent's part is to suggest it at the right moment. That moment is a phase boundary — right after a gate approval is recorded in `state.md` and the deliverables are committed — or a build-layer boundary in interactive mode: the next command rehydrates from the SSOT, so nothing is lost. Before suggesting it, pass every item of the **pre-compact check**:
  1. The gate decision is recorded and the deliverables committed.
  2. Every instruction/decision the human gave in conversation this phase is reflected in a deliverable or the SSOT — an observation with no home yet goes into a `state.md` log row first.
  3. No unanswered question or gate presentation is in flight.
  4. (Interactive build) no task is `in_progress`; completed tasks are merged and marked `done`.
  5. **Checkpoint GC** — apply "Compress and rotate" above to the resolved log rows at the same moment.

  If any item fails — or a gate is pending decision — do not suggest it. Compacting has no bearing on approvals (gate truth stays in `state.md`); afterwards `/status` and each command's SSOT re-read handle rehydration.

## Quality-check commands

The bundled `makefile` provides: `make test` (pytest), `make check` (lint / format / type-check — both hook stages; the gate uses this, never `pre-commit run` alone), `make test-tools` (self-tests of `scripts/agentloop/`), `make audit` (dependency vulnerabilities), `make doctor` (read-only diagnosis of the environment + SSOT consistency — run it first when anything behaves oddly). If copied into a project without `make`, substitute that project's commands in `quality_gate.steps`.

## Security gate

Three layers: **gitleaks** at commit stage (in `make check`; false positives → `.gitleaksignore`) / **a security review** mandatory at implementation completion (before gate ④ — Claude Code's `/security-review`; deterministic mode A auto-runs it headless when all tasks are done and binds the report to the reviewed HEAD in `.agentloop/security-review.md`; config `build.post_build.security_review`; an environment without the command performs an equivalent security-focused review pass and records it the same way) / **a security review + `make audit`** mandatory in `/verify`, recorded in `docs/test/test-plan.md`.

## Branch / commit conventions

- Implement **on a work branch** (recorded in `branch` of `state.md`; created by `make init`), never directly on main. Parallel leaf tasks use worktree-derived branches (`<branch>-T-NNN`) merged back on completion.
- Per-task commits: **`T-NNN: <summary>`**, one commit = one task. Approving `/build` covers that loop's local commits (no per-commit confirmation).
- **Commit each phase's deliverables at its gate approval** (ADRs, task tickets, `state.md`/`tasks.yaml` updates) with a `docs: gate ③ tasks`-style message — not left uncommitted across the whole build.
- **Push / PR creation / merging to main are outward-facing** — only after separate human approval. **Writing to GitHub Issues is also outward-facing**: only with the `github.enabled: true` opt-in; `make issue-sync` mirrors one-way and never reads Issues back.

## Tool-execution permissions (distinct from gate approvals)

**Gate approvals** (①–⑤) are the Human-on-the-Loop essence — never reduce them. **Tool-execution permission prompts** are separate: `command-preauthorization` of known-safe commands in your agent's permission settings cuts repeated prompts without touching the gates (where those settings live is per-agent — see your capability mapping). Keep the shared, template-owned settings to **generic AgentLoop commands**; put **product-specific** ones (a product's run/smoke, its test command) in that product's own committed settings (shared with the team and the build loop), so the template's additive-merge upgrade path stays clean. Destructive / outward-facing actions (push, PR, merge, `make cycle-close`) stay human-run — never pre-authorize them.

## Directories

- `.agentloop/` — SSOT (`state.md`, `tasks.yaml`, `config.yaml`) + the structured event log (`events.ndjson`) + **`prompts/`** (the shared phase procedures and role definitions every agent reads)
- `scripts/agentloop/` — deterministic orchestration (`dag.py`, `build_loop.py`, `events.py`, `doctor.py`, `gate_guard.py`, `issue_sync.py`, `pr_draft.py`, `revise.py`, `init.py`, `adopt.py`, `cycle.py`, `template_lint.py`). **Product scripts go directly under `scripts/`, not mixed in here.**
- `docs/` — phase deliverables; `docs/retrospective.md` holds the retrospective at `done`
- `.claude/commands/`, `.github/prompts/` — per-agent entry points (thin wrappers; the procedure detail lives in `.agentloop/prompts/commands/`)
- `.claude/agents/`, `.github/agents/` — role-agent wrappers (role definitions live in `.agentloop/prompts/agents/`)
