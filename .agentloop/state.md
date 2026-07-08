---
# .agentloop/state.md — this project's "Single Source of Truth"
# Every command/agent reads this file first and updates it after working.
# gates values are one of pending | approved. You cannot advance to the next phase
# unless the prerequisite gate is approved (see CLAUDE.md "Gate rules").
# When recording an approval, append the date (and approver, if several humans share the repo)
# as a trailing comment on the gate line, e.g. `tasks: approved   # 2026-07-07 alice`.
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

## Task table & execution plan (generated view)
The truth of tasks is `.agentloop/tasks.yaml` (the machine-readable SSOT of the task graph). Everything between the
markers below is a **derived, human-facing view** — refresh it by pasting the output of
`uv run --no-project --with pyyaml python scripts/agentloop/dag.py --render` (deterministic mode A's `build_loop.py`
refreshes the block automatically each iteration; keep the markers). Table, layers, critical path, frontier, and
`fan-out` are all derived — never maintain them by hand. For the `kind`/`status` vocabulary, see the tasks.yaml
schema / CLAUDE.md.

<!-- DAG-VIEW:BEGIN -->
_(run /tasks, then paste the `dag.py --render` output here)_
<!-- DAG-VIEW:END -->

## Speculative work log (provisional, throwaway-by-default)
Record the "outcome-independent speculative work" done while waiting for approval. Material for the human to decide to discard/adopt.
Do not use it as grounds to set a gate to `approved`.

| Date | Gate awaited | Content | Deliverable/location | Adopt? (human) |
|------|------------------|------|-------------|----------|
| _(append as needed)_ |

## Escalation log (generated view)
The truth of escalations is `.agentloop/events.ndjson` (structured events; see `scripts/agentloop/events.py`).
`build_loop.py` appends `blocked` / `merge_conflict` / `integration_red` / `no_runnable` events automatically;
record one by hand (interactive mode, or a `needs-revision`) with
`make events ARGS='--add blocked --task T-00N --detail "..."'`. Everything between the markers below is a
**generated view** — refresh it with `make events ARGS=--refresh-state` (deterministic mode A refreshes it
automatically). Close an item with `make events ARGS='--resolve <ID> --note "how it was resolved"'` —
/verify closes all open items before gate ⑤.

<!-- ESCALATION-VIEW:BEGIN -->
_(no events yet)_
<!-- ESCALATION-VIEW:END -->

## Roll-back (revision) log
The record of `/revise` (`make revise`) resetting upstream gates to `pending` in a chain. The history of **the human rewinding approval**.
Identify the task ripple with `dag.py --impacted` and reconcile (keep/modify/obsolete/new); record the result in the relevant task ticket.

| Date | Target (phase) | Gates reset to pending in chain | Reason |
|------|---------------|-------------------------------|------|
<!-- REVISE-LOG -->
