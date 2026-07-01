---
# .agentloop/state.md — this project's "Single Source of Truth"
# Every command/agent reads this file first and updates it after working.
# gates values are one of pending | approved. You cannot advance to the next phase
# unless the prerequisite gate is approved (see CLAUDE.md "Gate rules").
project: "<enter the product name>"
branch: "<enter the work branch name>"  # e.g. build/<product>. Implement on this branch.
current_phase: brief          # brief | requirements | design | tasks | build | verify | done
gates:
  requirements: pending       # set approved once the human approves the /req result
  design: pending             # set approved once the human approves the /design technical choices
  tasks: pending              # set approved once the human approves the /tasks plan
  build: pending              # set approved once the human approves the /build implementation review
  release: pending            # set approved once the human approves the /verify release decision
updated_at: "<YYYY-MM-DD>"
---

# Progress board

## Phase progress
- [ ] brief        — the human fills in `docs/00-product-brief.md`
- [ ] requirements — `/req`    → gate ①
- [ ] design       — `/design` → gate ②
- [ ] tasks        — `/tasks`  → gate ③
- [ ] build        — `/build`  → gate ④
- [ ] verify       — `/verify` → gate ⑤

## Task table (dependency graph)
The truth of tasks is `.agentloop/tasks.yaml` (the machine-readable SSOT of the task graph). This is a **human-facing view**;
update it by pasting the output of `uv run python scripts/agentloop/dag.py --render` (do not hold the truth by hand).
For the vocabulary and meaning of `kind`/`status`, see the tasks.yaml schema / CLAUDE.md. `fan-out` is a derived value.

| ID    | Title | Kind | blockedBy | fan-out | status | Test | Notes |
|-------|----------|------|-----------------|-----------------|--------|--------|------|
| _(generated from dag.py --render after running /tasks)_ |

## Execution plan (dependency chain)
The consumption order derived from the DAG. Initialized by `/tasks`, and **re-derived each time one task completes** in `/build`.

- **Execution layers** (topological order; within a layer, parallel is possible):
  - L0: _(no dependencies; mostly foundation tasks)_
  - L1: _(startable once L0 is done)_
  - L2: …
- **Critical path** (the longest chain = the path that determines the overall duration; fill it first):
  - _(e.g. T-001 → T-004 → T-007)_
- **Current executable frontier** (todo startable right now):
  - _(updated each iteration by /build)_

## Speculative work log (provisional, throwaway-by-default)
Record the "outcome-independent speculative work" done while waiting for approval. Material for the human to decide to discard/adopt.
Do not use it as grounds to set a gate to `approved`.

| Date | Gate awaited | Content | Deliverable/location | Adopt? (human) |
|------|------------------|------|-------------|----------|
| _(append as needed)_ |

## Escalation log
When a `blocked` / `needs-revision` occurs, append one line here and ask for the human's decision.

| Date | Task ID | Kind | Content | Resolution |
|------|----------|------|------|------|
| _(append as needed)_ |

## Roll-back (revision) log
The record of `/revise` (`make revise`) resetting upstream gates to `pending` in a chain. The history of **the human rewinding approval**.
Identify the task ripple with `dag.py --impacted` and reconcile (keep/modify/obsolete/new); record the result in the relevant task ticket.

| Date | Target (phase) | Gates reset to pending in chain | Reason |
|------|---------------|-------------------------------|------|
<!-- REVISE-LOG -->
</content>
