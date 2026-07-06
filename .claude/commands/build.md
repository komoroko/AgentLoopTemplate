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

What the orchestrator does deterministically: compute the frontier → sort the consumption order (foundation/high-fan-out → critical path) → run foundation serially on work, independent leaves isolated with `git worktree` at **up to `max_parallel` (default 3) in parallel** → for each task, **run the quality-gate pipeline `quality_gate.steps` in `.agentloop/config.yaml` — the single definition of the DoD** (default: `test` → `check` → `review` (headless /code-review + /simplify pass) → `smoke`). Each `cmd` step is gate-decided by exit code, and a fail is sent back to the implementer up to **that step's own `retries` budget** (over the budget → `blocked`) → merge into work sequentially in done order → recompute. At the start it code-checks `gates.tasks == approved` and stops doing nothing if unapproved. **Only the human opens `gates.build`** (the script does not touch it).

The non-deterministic parts are each task's implementation code content and the `review` agent step's fixes. Both are absorbed deterministically: after an agent step changes code, the already-passed cmd steps are re-run; a red cmd step retries until green, else blocked.

### B. Interactive loop — `/loop /build`
Run the "one loop iteration" below in conversation without the orchestrator. Behavior is identical to mode A (same DoD, same parallelism/merge rules). An alternative when A is unavailable.

```
/loop /build
```

## One loop iteration
1. **(Re-)derive the execution plan.** From the task table (DAG) in `state.md`:
   - **Executable frontier** = the set of tasks whose `blockedBy` are all done and whose status is todo.
   - If there are no candidates and all incomplete tasks are blocked/needs-revision, escalate to the human and **stop the loop**.
   - If all tasks are done, go to "When all complete" below.
2. **Optimize the consumption order** within the frontier (optimal consumption). Highest priority first:
   1. **foundation / high fan-out** (tasks with many dependents) — the sooner done, the more parallel tasks are freed.
   2. tasks **on the critical path** — shorten the overall duration.
   3. the rest, in any order.
3. Set the chosen task to `in_progress` (in state.md and the task ticket). **Run in isolation** per kind:
   - **foundation / high-fan-out tasks**: since many depend on them, implement and finalize **serially on the work branch** without worktree isolation (isolating would make derivatives work on top of a stale foundation).
   - **parallel / independent leaf tasks**: per task, **launch the `implementer` subagent with `isolation: "worktree"`** and let it complete implementation + test writing/running through the quality gate in its own dedicated worktree (= separate branch, separate working directory). File edits do not collide. **Launch at most 3 in parallel** (consume more in the next iteration if the frontier has more). Have each implementer report "its own work branch name and the commits on it" (used for the later merge).
     - **Note (branch base)**: in interactive mode B, `isolation: "worktree"` may **branch from the default branch (main, etc.) rather than the work branch**. In that case the worktree **lacks the deliverables of prerequisite (foundation) tasks**, so the implementer first **pulls in the work branch** (`git merge` / `--ff-only` if possible, without changing the work branch) to satisfy dependencies before implementing. Deterministic mode A (`build_loop.py`) branches from `self.branch`, so this handling is unnecessary.
   - Run mutually dependent ones serially. Use **`git worktree` (the Agent's `isolation: "worktree"`)**, not `git subtree`. subtree is for importing external repos and is unsuitable for separating concurrent work.
4. **Confirm tests green.** If still red, have the implementer fix it. If unsolvable within the set number of tries, set `blocked` and record in the log.
5. **Pass the quality gate (definition of done / DoD).** The DoD is defined once, as `quality_gate.steps` in `.agentloop/config.yaml` — run those steps in order (default: `test` → `check` → `review` → `smoke`). Only `done` once every step passes:
   1. **`test`** — automated tests green (`make test`).
   2. **`check`** — **`make check`** (= `make pre-commit` + `make pre-push`; lint / format / type-check, all of it), **fixed and re-run until there are no errors**. Auto-fixable ones (ruff/format, etc.) resolve on re-run; manual fixes for mypy, tsc, etc. are handled here too. If unsolvable within the step's `retries` budget, set `blocked` and go to the human.
   3. **`review`** — apply the **`/code-review`** (bugs/correctness) and **`/simplify`** (reuse/simplification/efficiency) disciplines and fix the findings. If code changed, re-run the earlier steps and keep them green.
   4. **`smoke` (runnable deliverables only)** — for CLI, server, etc., minimally confirm it actually launches and the main commands/endpoints work. Tests can be green while the launch path (packaging, entry point, dependency resolution) is broken; this catches that within build. If it cannot launch, set `blocked` or add a task that makes it launchable. **Fill a provisional `smoke.run` as soon as any entry point launches — don't wait for the integration task** — and note it in the foundation task's Notes. Register that command's execution permission in the product's committed `.claude/settings.json` so the smoke step doesn't re-prompt every loop.
   - **Interactive mode (B): the lead re-runs each `cmd` step (`make test` / `make check`) itself before marking `done`** — a subagent/implementer's textual "green" report is not evidence (mode A already gates on exit code in code).
   - In a project without `make`, substitute that project's commands in the config steps.
6. **Once all the above and tests green are satisfied**, set `status: done`. Do not mark a task done while any is unmet.
   - **Merge (join) an isolated leaf task into the work branch when done.** After completion, merge sequentially **deterministically in ascending id order**, and have the implementer resolve conflicts at this merge point. Completing a merge is the **trigger that frees the frontier for integration tasks** (matching the DAG dependencies). After merging, clean up the now-unneeded worktree/branch (`isolation: "worktree"` auto-cleans if there are no changes).
   - Per-task commits use the `T-NNN: <summary>` form (one commit = one task). The commits inside the worktree become that task's diff exactly, limiting the review scope of `/simplify`/`/code-review` to that task.
7. If the implementer reports a **requirements/design defect**, set `needs-revision`; if a **new dependency or task split is discovered**, update the DAG (dependencies/dependents) in the task table. Log both, and raise upstream defects to the human (do not fix on your own). **If an upstream deliverable needs fixing, use `/revise <phase>` at the human's discretion to roll back to requirements/design** (reset the gates in a chain to `pending` and analyze task impact with `dag.py --impacted`).
8. **Reassemble the chain**: reflect completions/changes, recompute the execution plan (layers / critical path / frontier) in `state.md`, update `updated_at`, and move to the next iteration. Newly freed tasks join the next iteration's frontier.

## When all tasks complete (gate ④)
1. **Mandatorily run `/security-review`.** Review the work-branch diff for vulnerabilities, return must-fix-equivalent findings to the implementer to fix, and record judgment calls in the escalation log of `state.md` for the human. Do not present gate ④ if there is a serious unresolved issue.
2. Notify the human of the pending approval via `PushNotification`.
   - **(Only with GitHub integration)** Run `make issue-sync` to reflect each task's latest status (done → close, etc.) to Issues. Best-effort; do not stop the gate if it fails (auto-skips if `github.enabled: false` / gh/remote absent). Do not put it inside the deterministic orchestration loop (`build_loop.py`) = do not bring networking into the deterministic loop.
3. Present the implementation summary (completed tasks, key additions/changes, test results, **security-review results**, unresolved items) and confirm "may we approve this as implementation-complete?".
   - **Always present a self-assessment as well** (CLAUDE.md "Gate self-assessment"): implementation confidence (thin test-coverage spots / hard parts avoided), assumptions made, residual risks, points for the human to decide. For spots that produced blocked/needs-revision, add their outcome too.
4. **While waiting for approval**, you may proceed with outcome-independent speculative work (record in the speculative work log of `state.md`): concretizing functional test cases in `docs/test/test-plan.md`, a trial run of `make audit`, and other `/verify` prep pulled forward. Do not make changes that could require redoing the implementation.
5. Once a human approves (plan-mode approval or an explicit "approve") — **running the next command (`/verify`) is not itself approval** — set `gates.build` to `approved`, `current_phase` to `verify`, and point to "next is `/verify`".

## Monitoring long-running loops (optional)
When running long in the background, you may operate it to periodically notify the human of progress (equivalent to /status) via `/schedule` or ScheduleWakeup.
