---
description: Roll back. When an upstream (requirements/design) defect is confirmed (e.g. during implementation), reset gates in a chain and analyze task impact.
---

# /revise — Roll back upstream (the going-back loop)

The **first-class operation** for when "go back to the design, even back to the requirements, and reconsider" becomes necessary during implementation/verification.
Symmetric with the human opening a gate, **rewinding approval is also the human's privilege**. This is that procedure.

## When to use it
- When `/build`'s implementer reports `needs-revision` (a requirements/design defect) and the loop has stopped.
- When `/verify` reveals a requirement/design-level problem (a spec error, etc.).
- Small implementation-convenience rework is out of scope (handle that with a fix within the task). Use this **only when an upstream deliverable needs fixing**.

## Steps
1. **Confirm the defect and the human's decision**: present the escalation log / needs-revision points and have the human decide "how far to go back (requirements or design)". Do not roll back on your own.
2. Finalize the target phase (`requirements` | `design` | `tasks` | `build`) and the reason **in a single AskUserQuestion**.
3. **Reset gates in a chain** (deterministic process):
   ```
   make revise ARGS="--to <phase> --reason '<reason>'"
   ```
   `scripts/agentloop/revise.py` resets every gate from the target onward to `pending` **in a chain**, moves `current_phase` back, and records it in the roll-back log. This prevents the stale-approval inconsistency of "upstream pending while downstream approved". The editing order from then on is mechanically enforced by `gate_guard` (e.g. while design is pending, edits to `docs/tasks/**` and implementation code are denied). Use `--dry-run` to check just the plan.
4. **Task impact analysis (reconcile, do not discard)**: before fixing upstream, deterministically enumerate the ripple to existing tasks.
   - Identify the tasks **directly affected** by the upstream change and fully expand their **transitive dependents (downstream)** with `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --impacted T-00x,T-00y`.
   - Classify each task: **keep** (unaffected) / **modify** (needs fixing → `needs-revision`) / **obsolete** (no longer needed → mark, do not delete) / **new** (added).
   - A task that is **`done` but invalidated** reverts to `todo` (needs reimplementation). The implemented code stays on the branch but is back in scope plan-wise.
5. **Guide to rebuilding**: "next is `/<phase>`". Reflect the above reconcile inside the re-run of `/design`/`/tasks`, and present the **impact (the impacted list and classification)** to the human at gate ③ for re-approval.

## Principles
- **Rewinding approval is the human's privilege.** `/revise` is run only under the human's explicit judgment.
- **Do not discard and rebuild tasks.** Reconcile existing tasks against the revised upstream, and pick up the impact exhaustively with deterministic computation (`--impacted`).
- The truth is `.agentloop/state.md` (gates, roll-back log) and `.agentloop/tasks.yaml` (tasks).
