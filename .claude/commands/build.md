---
description: Phase 4 implementation. Autonomously consume tasks with /loop. Each task's condition to advance is green tests.
---

# /build — Implementation phase (autonomous loop consumption)

## Prerequisite gate check (always first)
Read `.agentloop/state.md` and confirm `gates.tasks == approved`.
If unapproved, do not work; say "please approve `/tasks` first" and stop.

## Execution modes

### A. Deterministic execution (recommended) — `make build-loop`
Delegate scheduling to the deterministic orchestrator `scripts/agentloop/build_loop.py`. Code decides **which tasks, at what parallelism, in what merge order, and when to stop** deterministically from `.agentloop/config.yaml` and `tasks.yaml` (not relying on LLM discretion):

```
make build-loop              # run
make build-loop ARGS=--dry-run   # check just the control flow without calling claude/git
```

What the orchestrator does deterministically: compute the frontier → sort the consumption order (foundation/high-fan-out → critical path) → run foundation serially on work, independent leaves isolated with `git worktree` at **up to `max_parallel` (default 3) in parallel** → for each task, **run the quality-gate pipeline `quality_gate.steps` in `.agentloop/config.yaml` — the single definition of the DoD** (default: `test` → `check` → `review` (headless /code-review + /simplify pass) → `smoke`; when the task's own `test` command in tasks.yaml differs from the configured steps it is prepended as a focused `task-test` step). Each `cmd` step is gate-decided by exit code, and a fail is sent back to the implementer up to **that step's own `retries` budget** (over the budget → `blocked`) → merge into work sequentially in done order → **when a batch merged 2+ leaves, re-run the cmd steps once on the merged work branch (the integration gate, `quality_gate.integration_gate`): each leaf was green only in isolation, and the combined file set can still be red — a red goes to a headless fixer within the step's `retries` budget, else the batch's tasks block; a single-leaf join skips this (its tree is identical to the already-verified worktree)** → mark the merged tasks `done` → recompute. At the start it code-checks `gates.tasks == approved` and stops doing nothing if unapproved. **Only the human opens `gates.build`** (the script does not touch it).

The non-deterministic parts are each task's implementation code content and the `review` agent step's fixes. Both are absorbed deterministically: after an agent step changes code, the already-passed cmd steps are re-run; a red cmd step retries until green, else blocked.

### B. Interactive loop — `/loop /build` (fallback: the lead re-enacts mode A by hand)
For when the orchestrator can't run (no `claude` CLI) or the human wants to drive in conversation:

```
/loop /build
```

The lead re-enacts **exactly the mode-A algorithm above**, iteration by iteration: derive the frontier (todo tasks whose `blockedBy` are all done) → order it (foundation/high fan-out first — the sooner done, the more leaves free up; then the critical path) → foundation tasks serial on the work branch (isolating them would strand derivatives on a stale base), independent leaves via the **`implementer` subagent with `isolation: "worktree"`** (git worktree, never subtree) at **most 3 in parallel**, each reporting its branch name for the merge → per-task DoD pipeline → deterministic **ascending-id merges** (conflicts resolved by the implementer at the merge point) → a completed merge frees the frontier for integration tasks; recompute and continue. Empty frontier with unfinished tasks = all blocked/needs-revision → escalate and stop; all done → gate ④ below. Behavior is identical to A — what changes is who runs the machinery, which puts four duties on the lead that mode A does in code:

1. **Run every gate decision yourself and read its exit status.** A subagent's textual "green" report is not evidence, even when it pastes output (summarized/elided pastes have hidden real failures). Run the task's own `test` command first when it differs from the configured steps (the `task-test` step), then the cmd steps; a red goes back to the implementer within that step's `retries` budget, over budget → `blocked`. **Crucially, after the ascending-id merges, re-run the cmd steps on the *merged* work branch before marking the batch `done`**: a leaf can pass in its isolated worktree yet fail on the combined file set (a lint/type error only the whole tree surfaces, a format reflow another task's change triggers). This is mode A's integration gate, done by hand — the effective backstop against green-report inaccuracy.
2. **Check each worktree's branch base.** The Agent's `isolation: "worktree"` may branch from the default branch rather than the work branch; the implementer then lacks the foundation tasks' deliverables and must first pull the work branch in (`git merge`, `--ff-only` if possible) before implementing. (Mode A branches from the work branch, so this never arises there.)
3. **Keep the records by hand.** Statuses (`in_progress` → `done`/`blocked`) in tasks.yaml as you go — never `done` with any DoD step unmet; blocked/needs-revision recorded as events (`make events ARGS='--add blocked --task T-NNN --detail "…"'`); per-task commits **`T-NNN: <summary>`** (one commit = one task — the worktree's commits are exactly that task's diff, which is what scopes the review step); merged worktrees cleaned up; state.md views and `updated_at` refreshed each iteration. A newly discovered dependency or task split updates the DAG in tasks.yaml; an upstream (requirements/design) defect is `needs-revision` + escalation — never fixed on your own; roll back via `/revise` at the human's discretion.
4. **Session hygiene.** At a layer boundary, when the conversation is heavy with re-run output, you may suggest the human run `/compact` — only when no task is `in_progress`, merges are committed and marked `done`, and observations are recorded in tickets / `state.md` (pre-compact check: CLAUDE.md "Context budget"; the SSOT rehydrates the next iteration). Never mid-retry or while a worktree awaits its merge. (Mode A runs in separate processes and needs none of this.)

## Quality-gate step notes (both modes)
The pipeline is `quality_gate.steps` in the config — the single definition of the DoD (see mode A). Operational notes:

- **`check`** = `make pre-commit` + `make pre-push` together (lint / format / type-check, all of it). Auto-fixable hooks (ruff/format) resolve on the re-run; manual fixes (mypy, tsc) are part of the step. In a project without `make`, substitute that project's commands in the config steps.
- **`review`** applies the **`/code-review`** (bugs/correctness) and **`/simplify`** (reuse/simplification/efficiency) disciplines and fixes findings in place; if code changed, the already-passed cmd steps are re-run.
- **`smoke` (runnable deliverables only)** — for CLI, server, etc., minimally confirm it actually launches and the main commands/endpoints work. Tests can be green while the launch path (packaging, entry point, dependency resolution) is broken; this catches that within build. If it cannot launch, set `blocked` or add a task that makes it launchable. **Fill a provisional `smoke.run` as soon as any entry point launches — don't wait for the integration task** — and note it in the foundation task's Notes. Once the deliverable is runnable, also set the step's **`required: true`** (the human decision knob): from then on an empty `run` makes `build_loop.py` refuse to start instead of silently skipping the launch check. Register that command's execution permission in the product's committed `.claude/settings.json` so the smoke step doesn't re-prompt every loop.

## When all tasks complete (gate ④)
1. **The security review is mandatory, bound to this build's deliverable.** In deterministic mode A, `build_loop.py` auto-launches it headless when all tasks are done (config `build.post_build.security_review`, default on): the report lands in `.agentloop/security-review.md` with the reviewed HEAD hash embedded, and the run is recorded as a `security_review` event — **read that report and triage it**; a re-run at the same HEAD skips (already reviewed). In mode B — or when the knob is off / the launch failed — run **`/security-review`** yourself. Either way: return must-fix-equivalent findings to the implementer to fix (a fix moves HEAD, so mode A re-reviews on the next run), and record judgment calls as escalation events (`make events ARGS='--add …'`) for the human. Do not present gate ④ if there is a serious unresolved issue.
2. Notify the human of the pending approval via `PushNotification`.
   - **(Only with GitHub integration)** Run `make issue-sync` to reflect each task's latest status (done → close, etc.) to Issues. Best-effort; do not stop the gate if it fails (auto-skips if `github.enabled: false` / gh/remote absent). Do not put it inside the deterministic orchestration loop (`build_loop.py`) = do not bring networking into the deterministic loop.
3. Present the implementation summary (completed tasks, key additions/changes, test results, **security-review results**, unresolved items) and confirm "may we approve this as implementation-complete?".
   - **Smoke-step check**: if the deliverable is runnable (CLI, server, …) and `quality_gate`'s `smoke.run` is still empty, say so explicitly at the gate — the DoD ran without a launch check — and propose the command to fill in plus `required: true` (mode A prints this nudge mechanically at gate ④; with `required: true` set, an empty run refuses to build at all — an unnoticed empty smoke silently defeats its purpose).
   - **Always present a self-assessment as well** (CLAUDE.md "Gate self-assessment"): implementation confidence (thin test-coverage spots / hard parts avoided), assumptions made, residual risks, points for the human to decide. For spots that produced blocked/needs-revision, add their outcome too.
4. **While waiting for approval**, you may proceed with outcome-independent speculative work (record in the speculative work log of `state.md`): concretizing functional test cases in `docs/test/test-plan.md`, a trial run of `make audit`, and other `/verify` prep pulled forward. Do not make changes that could require redoing the implementation.
5. Once a human approves (plan-mode approval or an explicit "approve") — **running the next command (`/verify`) is not itself approval** — set `gates.build` to `approved`, `current_phase` to `verify`, and point to "next is `/verify`". After committing the gate's deliverables, suggest the human run `/compact` before starting `/verify` (pre-compact check: CLAUDE.md "Context budget").

## Monitoring long-running loops (optional)
When running long in the background, you may operate it to periodically notify the human of progress (equivalent to /status) via `/schedule` or ScheduleWakeup.
