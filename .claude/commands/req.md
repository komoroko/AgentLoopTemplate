---
description: Phase 1 requirements. Sound out from the brief, firm up the requirements, and ask for approval at gate ①.
---

# /req — Requirements phase

You drive this project's requirements definition. **Human on the Loop**: you do the work, the human decides.

## Steps
1. Read `.agentloop/state.md`. Check `current_phase`.
   - If `gates.requirements == approved` already, say "Requirements are approved; changing them needs re-approval" and wait for the human's instruction.
2. Read `docs/00-product-brief.md`. If empty, first prompt the human to fill it in and stop.
   - **Brownfield**: if `docs/05-current-state.md` exists (an adopted repo), **read it first**. Requirements are scoped to **this cycle's delta** (the change), not the whole product. If it links existing requirement documents, take them as the starting point rather than re-deriving from scratch — the human approving gate ① is what adopts them as this cycle's truth. In-flight/unfinished work listed there is candidate delta scope.
3. Delegate to the `requirements-analyst` subagent to produce a requirements draft, gaps, and open points.
4. Fill ambiguities and important branches by asking the human via **AskUserQuestion**.
5. Write the agreed content into `docs/10-requirements.md` (in the scaffold structure).
6. **Gate ①**: in plan mode, present the requirements summary via ExitPlanMode and ask for approval. If not in plan mode, present the summary and explicitly confirm "may we freeze the requirements with this content?". Ask any confirmations **in a single AskUserQuestion**.
   - **Always present a self-assessment as well** (CLAUDE.md "Gate self-assessment"): assumptions made, per-requirement confidence (high/medium/low), open questions, anticipated risks. Also leave it in the "Self-assessment" section of `10-requirements.md`. Flag low-confidence requirements for the human's attention.

Write the deliverable (`docs/10-requirements.md`) in the user's language.

## While waiting for approval (minimizing the bottleneck)
After presenting gate ①, while waiting for approval you may proceed with the following (**outcome-independent, throwaway-by-default**; see CLAUDE.md "Minimizing the approval-wait bottleneck"). Record what you did in the "speculative work log" of `state.md`.
- Notify the human of the pending approval via `PushNotification`.
- Repo scaffolding / directory layout / skeleton of the dev environment and CI.
- **Read-only investigation** of candidate technologies surfaced in the brief (do not finalize the design).
- **Forbidden**: writing the design body that pre-empts the requirements.

## Once approved
- Set `gates.requirements` to `approved`, `current_phase` to `design`, and update `updated_at` in `state.md`.
- After committing the gate's deliverables, suggest the human run `/compact` before starting `/design` — the next command rehydrates from the SSOT, so nothing is lost (pre-compact check: CLAUDE.md "Context budget").
- Point to "next is `/design`".

Until approval is given, the gate stays `pending`. Do not set it to approved on your own.
