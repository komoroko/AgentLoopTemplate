---
description: Phase 2 design. Design from approved requirements, let the human decide technical choices, and ask for approval at gate ②.
---

# /design — Design phase

## Prerequisite gate check (always first)
Read `.agentloop/state.md` and confirm `gates.requirements == approved`.
**If unapproved, do not work**; say "please approve the requirements with `/req` first" and stop.
If `gates.design == approved` already, say "Design is approved; changing it needs re-approval" and wait for the human's instruction (an upstream roll back is done with `/revise`; after `/revise` the design gate is back to `pending`, so proceed normally).

## Steps
1. Read `docs/10-requirements.md` and the existing code/assets.
   - **Brownfield**: if `docs/05-current-state.md` exists, **read it first** — design against its architecture/conventions, reuse the assets in its inventory (state which in the design), and take any existing design docs/ADRs it links as the starting point.
2. Delegate to the `architect` subagent to produce (a) an implementation approach for each requirement, (b) **existing assets that can be reused**, and (c) options for important technical choices (with trade-offs across cost/security/non-functional/effort). **If the product is itself an AI agent app**, also have the architect present the agent-specific choices (Architecture pattern / Context strategy / Tool design — the "AI agent application" lens in `.claude/agents/architect.md`) as options to decide.
3. Present the technical choices as options via **AskUserQuestion** and **let the human decide**. Create a `docs/decisions/ADR-NNN.md` for each decision.
4. Write the finalized content into `docs/20-design.md`. **Place a design section (`### R-x → design`) covering every requirement (R-x) in `docs/10-requirements.md`, with none missing.** Do not add design with no backing requirement (out-of-scope build-out) on your own — if needed, return it to the requirements side (`/revise`). **If the product is not an AI agent app, delete the scaffold's "AI agent application design" section** (do not leave the conditional block orphaned).
5. **Forward-coverage check**: cross-check that the design **covers all requirements and adds nothing not in the requirements**. Once `docs/20-design.md` has a design section for each requirement, confirm the requirement IDs in the headings match the requirement IDs in the requirements document (this requirement↔design linkage is mechanically re-checked later by `/tasks`'s `dag.py --trace`).
6. **Gate ②**: present via ExitPlanMode in plan mode; otherwise present the design summary + the finalized technical choices + **requirement coverage (every R-x has a corresponding design section / nothing out-of-scope was added)** and confirm "may we proceed with this design?". Ask the technical-choice confirmations **in a single AskUserQuestion**.
   - **Always present a self-assessment as well** (CLAUDE.md "Gate self-assessment"): assumptions made, per-area confidence (architecture/technical choices/non-functional, etc.), open questions, risks/trade-offs. Also leave it in the relevant section of `20-design.md`. Make low-confidence design spots explicit.

Write the deliverables (`docs/20-design.md`, `docs/decisions/ADR-*.md`) in the user's language.

## While waiting for approval (minimizing the bottleneck)
After presenting gate ②, while waiting you may proceed with the following (**outcome-independent, throwaway-by-default**). Record in the "speculative work log" of `state.md`.
- Notify the human of the pending approval via `PushNotification`.
- Setting up the skeleton of the dev environment / test harness / CI, lint/static-analysis config.
- **Read-only investigation** of candidate libraries (install only after finalizing).
- **Forbidden**: finalizing tasks or doing real implementation that pre-empts the design/technical choices.

## Once approved
- Set `gates.design` to `approved`, `current_phase` to `tasks`, and update `updated_at` in `state.md`.
- After committing the gate's deliverables, suggest the human run `/compact` before starting `/tasks` — the next command rehydrates from the SSOT, so nothing is lost (pre-compact check: CLAUDE.md "Context budget").
- Point to "next is `/tasks`".

Do not finalize technical choices on your own. Always go through the human's decision.
