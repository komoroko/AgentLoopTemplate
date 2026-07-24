---
# .agentloop/state.md — this project's "Single Source of Truth"
# Every command/agent reads this file first and updates it after working. Gates are
# pending | approved; the ONLY write path to `approved` is `agentloop approve <gate>
# [--by <name>]` after the human's explicit OK (AGENTS.md "Gate rules" 2 — gate_guard
# denies a hand-edited gate line).
project: "<enter the product name>"
branch: "<enter the work branch name>"  # e.g. build/<product>. Implement on this branch.
current_phase: brief          # brief | requirements | design | tasks | build | verify | done
gates:
  requirements: pending       # gate ① — the human OKs the /req result
  design: pending             # gate ② — the human OKs the /design technical choices
  tasks: pending              # gate ③ — the human OKs the /tasks plan
  build: pending              # gate ④ — the human OKs the /build implementation review
  release: pending            # gate ⑤ — the human OKs the /verify release decision
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
`agentloop dag --render` (deterministic mode A's `build_loop.py`
refreshes the block automatically each iteration; keep the markers). Table, layers, critical path, frontier, and
`fan-out` are all derived — never maintain them by hand. For the `kind`/`status` vocabulary, see the tasks.yaml
schema / AGENTS.md.

<!-- DAG-VIEW:BEGIN -->
_(run /tasks, then paste the `agentloop dag --render` output here)_
<!-- DAG-VIEW:END -->

## Speculative work log (provisional, throwaway-by-default)
Record the "outcome-independent speculative work" done while waiting for approval. Material for the human to decide to discard/adopt.
Do not use it as grounds to set a gate to `approved`.

| Date | Gate awaited | Content | Deliverable/location | Adopt? (human) |
|------|------------------|------|-------------|----------|
| _(append as needed)_ |

## Escalation log (generated view)
The truth of escalations is `.agentloop/events.ndjson` (structured events; see `agentloop events`).
`build_loop.py` appends `blocked` / `merge_conflict` / `integration_red` / `no_runnable` events automatically;
record one by hand (interactive mode, or a `needs-revision`) with
`agentloop events --add blocked --task T-00N --detail "..."`. Everything between the markers below is a
**generated view** — refresh it with `agentloop events --refresh-state` (deterministic mode A refreshes it
automatically). Close an item with `agentloop events --resolve <ID> --note "how it was resolved"` —
/verify closes all open items before gate ⑤.

<!-- ESCALATION-VIEW:BEGIN -->
_(no events yet)_
<!-- ESCALATION-VIEW:END -->

## Roll-back (revision) log
The record of `/revise` (`agentloop revise`) resetting upstream gates to `pending` in a chain. The history of **the human rewinding approval**.
Identify the task ripple with `agentloop dag --impacted` and reconcile (keep/modify/obsolete/new); record the result in the relevant task ticket.

| Date | Target (phase) | Gates reset to pending in chain | Reason |
|------|---------------|-------------------------------|------|
<!-- REVISE-LOG -->
