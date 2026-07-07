---
description: Phase 3 task breakdown. Split the design into implementable task tickets and ask for plan approval at gate ③.
---

# /tasks — Task breakdown phase

## Prerequisite gate check (always first)
Read `.agentloop/state.md` and confirm `gates.design == approved`.
If unapproved, do not work; say "please approve `/design` first" and stop.

## Re-run after a roll back (reconcile, when tasks already exist)
On a re-run after rolling back upstream with `/revise`, if `tasks.yaml` already has tasks, **do not rebuild from scratch**. Reconcile the revised design against the existing tasks:
- Identify the tasks **directly affected** by the upstream change and fully expand their **transitive dependents (downstream)** with `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --impacted T-00x,T-00y`.
- Classify each task: **keep** (unaffected, status preserved) / **modify** (needs fixing → `needs-revision`) / **obsolete** (no longer needed → mark in notes, do not delete) / **new** (added).
- A task that is **`done` but invalidated** reverts to `todo` (needs reimplementation). The implemented code stays on the branch but is back in scope plan-wise.
- After reconciling, re-run `dag.py --trace --require-design` and confirm the **task thread is reconnected to the revised requirements/design** (no new requirement left uncovered, no dangling reference to a deleted requirement) (confirm exit 0; 1=missing, 2=cannot check).
- At gate ③, in addition to the usual plan, present the **impact (the impacted list and the keep/modify/obsolete/new classification)** and the **consistency trace** to get re-approval.

On the first run (no tasks generated yet), create them with the steps below.

## Steps
1. Read `docs/20-design.md`.
   - **Brownfield**: if `docs/05-current-state.md` exists (an adopted repo), **read it first**. Cut tasks to **this cycle's remaining delta** — never create a task that re-implements a capability the baseline already lists as implemented. When the cycle completes an in-flight partial implementation, put an **absorb task** first (kind: foundation): write acceptance-level tests around the existing partial code and pin it green, so the remaining-work tasks stack on a verified base; reference the existing code in the ticket's "Notes / design decisions". **Never create a task as `done`** (a new task is always `todo`) — what already works is baseline description, not a task. If a delta requirement names the combined capability (existing part + remaining work), give the absorb task that requirement's `req` and the `dag.py --trace` coverage holds.
2. Split the design into **review-sized task tickets** and create `docs/tasks/T-NNN.md` (following the `T-template.md` scaffold). Each ticket must have:
   - the requirement/design it covers
   - acceptance criteria
   - an **automated-test approach** (kind, target cases, run command) ← do not create a task without this. `/build` uses it for the green decision.
3. **Classify each task by kind**:
   - **foundation**: a shared base many tasks depend on (shared models/schema, shared utilities, auth, config, types/interfaces, etc.).
   - **parallel**: feature tasks (leaves) that can run independently and concurrently once the foundation is in place.
   - **integration**: a task that joins several parallel tasks once they complete (integration, E2E, wrap-up).
   - **Cutover decomposition (replacing shared infrastructure)**: when a cycle *replaces* a shared asset that many tasks touch (a colour LUT, an auth layer, a serialization format, a config field), **do not split the removal of the old asset across the foundation/leaf tasks that build the new one**. Deleting the shared old path early makes those intermediate tasks fail their own per-task DoD (`test`+`smoke` can't stay green while producers and consumers straddle two worlds). Instead: keep every intermediate task **additive** (add the new API/field alongside the old, old path still runs at its defaults), and **concentrate the removal of the old infrastructure in the single integration/cutover task** that wires the new path end to end and can delete the old one in the same commit while keeping the suite green. (This is the generalized "Option A" — see a retrospective if this repo has hit it.)
4. **Assemble the dependency graph (DAG)**: organize each task's `blockedBy` (dependencies) and fan-out (dependents). Confirm there are no cycles (it is a DAG).
5. **Derive the execution plan** from the DAG:
   - **Execution layers**: assign to L0, L1, … in topological order (within a layer, parallel is possible). Foundation should cluster up front.
   - **Critical path**: identify the longest chain (the path that determines the overall duration).
   - **Executable frontier**: todo tasks with no dependencies, ready to start now.
6. **Write the task graph out to `.agentloop/tasks.yaml` as the machine-readable SSOT.** Keep the file **pure data plus its two-line pointer header** (no other comments — `build_loop.py` rewrites the file on every status change and would destroy them). Schema per task: `id` (unique, `T-NNN`) / `title` / `kind` / `blockedBy` (array of task IDs, no cycles) / `status` (`todo | in_progress | blocked | needs-revision | done`; newly created = `todo`) / `test` (the command for the green decision), plus optional `req` (the requirement it covers, from "covers" in `T-NNN.md`; `R-<number>` form, multiple comma/space-separated) / `phase` (`requirements|design|build|verify`, default `build`; a bug-fix task originating from `/verify` is `verify`). Example entry: `{id: T-001, title: "define shared schema", kind: foundation, blockedBy: [], status: todo, test: "make test", req: "R-1"}`. `req`/`phase` become `req:*`/`phase:*` labels when mirrored to GitHub Issues, letting you tell from an issue "which requirement / which phase the work is for". This is the deterministic truth read by `/build` (`scripts/agentloop/build_loop.py`) and `/status` (`scripts/agentloop/dag.py`). fan-out, frontier, layers, and the critical path are derived from `blockedBy`, so they are not stored.
   - After writing, run `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --validate` to confirm no cycles, unknown dependencies, or duplicate IDs (fix if non-zero).
   - **Consistency trace** `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --trace --require-design` mechanically checks whether the **requirements → design → tasks thread is unbroken** (since design is approved at this phase, pass `--require-design` to prevent the design dimension from being silently skipped when the design document is absent). Exit code **0=consistent / 1=missing (fix) / 2=cannot check (0 requirement IDs, document absent, etc.; fix the notation/paths)**.
     - Missing items detected: ① a requirement with no **build** task covering it (verify-phase tasks do not count toward coverage) ② a requirement with no section in the design ③ a design/task referencing an R not in the requirements (dangling reference, any phase) ④ a build task with no `req` set (WARN; does not affect the exit code).
     - Assumption: each task's `req` is in `R-<number>` form (invalid ones are rejected by `--validate`). Requirement IDs are picked from the **heading lines** of the requirements/design documents (any depth H1–H6; multiple IDs per heading allowed; examples inside code fences ignored).
   - The generated view in `state.md` (between the `DAG-VIEW` markers) is the human-facing task table & execution plan. Fill it by pasting the output of `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --render` between the markers (the truth lives in tasks.yaml; deterministic mode A refreshes the block automatically during `/build`).
7. **Gate ③**: in addition to the task list, present the **dependency chain (layer diagram, critical path, foundation tasks)** and the **consistency trace** and confirm "may we proceed to implementation with this split and ordering plan?". Generate and present `dag.py --render` (layers/critical-path text), **`dag.py --mermaid` (the dependency graph as Mermaid `graph TD`)**, and **`dag.py --trace --require-design` (the requirement-coverage table)** (show diagrams/tables, not just words). Make it so the human can see at a glance that **every requirement is linked to a task and there are no dangling references**.
   - **Always present a self-assessment as well** (CLAUDE.md "Gate self-assessment"): assumptions behind the split, confidence in estimates, high-risk tasks (uncertain/external-dependency/coarse-grained), open questions, and a context-bloat signal when relevant. Also leave low-confidence tasks in the ticket's "Notes / design decisions".

Write the deliverables (`docs/tasks/T-NNN.md`) in the user's language.

## While waiting for approval (minimizing the bottleneck)
After presenting gate ③, while waiting you may proceed with the following (**outcome-independent, throwaway-by-default**). Record in the "speculative work log" of `state.md`.
- Notify the human of the pending approval via `PushNotification`.
- Preparing test fixtures/harness/scaffolding that are clearly needed from the approved design.
- **Forbidden**: real implementation of each feature that pre-empts the task plan.

## Once approved
- Set `gates.tasks` to `approved`, `current_phase` to `build`, and update `updated_at` in `state.md`.
- **(Only with GitHub integration)** Run `make issue-sync` to one-way-mirror the approved tasks to Issues. It only acts when `github.enabled: true` in `.agentloop/config.yaml`, and auto-skips if gh/remote is absent (does not fail). **Do not run it before approval** (avoid making issues for unapproved tasks). tasks.yaml is always the SSOT; Issues are not read back.
- After committing the gate's deliverables, suggest the human run `/compact` before starting `/build` — the next command rehydrates from the SSOT, so nothing is lost (pre-compact check: CLAUDE.md "Context budget").
- Point to "next is `/build`".
