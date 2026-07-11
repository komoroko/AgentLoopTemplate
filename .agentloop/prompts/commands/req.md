# /req — Requirements phase

You drive this project's requirements definition. **Human on the Loop**: you do the work, the human decides.
(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Steps
1. Read `.agentloop/state.md`. Check `current_phase`.
   - If `gates.requirements == approved` already, say "Requirements are approved; changing them needs re-approval" and wait for the human's instruction.
2. Read `docs/00-product-brief.md`. If empty, first prompt the human to fill it in and stop.
   - **Brownfield**: if `docs/05-current-state.md` exists (an adopted repo), **read it first**. Requirements are scoped to **this cycle's delta** (the change), not the whole product. If it links existing requirement documents, take them as the starting point rather than re-deriving from scratch — the human approving gate ① is what adopts them as this cycle's truth (the **fast intake** entry described in `/onboard`). In-flight/unfinished work listed there is candidate delta scope.
3. Delegate to the `requirements-analyst` role (`role-delegation`) to produce a requirements draft, gaps, and open points.
4. Resolve ambiguities and important branches by asking the human via a single **`structured-question`**: rank the analyst's `[NEEDS CLARIFICATION]` markers and open points by **impact × uncertainty** and batch the top ones into one call (~4 questions is the practical cap; prefer multiple-choice with a recommended option). Propagate each answer into the requirement text itself (resolving the marker) and record the Q&A in the `## Clarifications` section of `10-requirements.md` — the audit trail of how ambiguities were closed.
5. Write the agreed content into `docs/10-requirements.md` (in the scaffold structure). Give every functional requirement an `R-N` heading and **every non-functional requirement an `NFR-N` heading with a measurable criterion and how it will be verified** — both ID families are the thread `dag.py --trace` follows through design, tasks, and the test plan (NFR rules are softer: no dedicated design section/task is only a WARN, but presence in the test plan is enforced at `/verify`).
6. **Gate ①**: present the requirements summary as an **`approval-presentation`** and ask "may we freeze the requirements with this content?". Ask any accompanying confirmations **in a single `structured-question`**.
   - Present only when **no unresolved `[NEEDS CLARIFICATION]` marker remains** in `10-requirements.md`: anything deliberately left open is demoted to an explicit "Open questions" entry the human sees at the gate — never left as a stray marker or a silent assumption.
   - **Always present a self-assessment as well** (AGENTS.md "Gate self-assessment"): assumptions made, per-requirement confidence (high/medium/low), open questions, anticipated risks, and a context-bloat signal when relevant. Also leave it in the "Self-assessment" section of `10-requirements.md`. Flag low-confidence requirements for the human's attention.

Write the deliverable (`docs/10-requirements.md`) in the user's language.

## While waiting for approval (minimizing the bottleneck)
After presenting gate ①, while waiting for approval you may proceed with the following (**outcome-independent, throwaway-by-default**; see AGENTS.md "Minimizing the approval-wait bottleneck"). Record what you did in the "speculative work log" of `state.md`.
- `notify-and-wait`: tell the human the approval is pending.
- Repo scaffolding / directory layout / skeleton of the dev environment and CI.
- **Read-only investigation** of candidate technologies surfaced in the brief (do not finalize the design).
- **Forbidden**: writing the design body that pre-empts the requirements.

## Once approved
- Set `gates.requirements` to `approved`, `current_phase` to `design`, and update `updated_at` in `state.md`.
- After committing the gate's deliverables, suggest `session-compaction` before starting `/design` — the next command rehydrates from the SSOT, so nothing is lost (pre-compact check: AGENTS.md "Context budget").
- Point to "next is `/design`".

Until approval is given, the gate stays `pending`. Do not set it to approved on your own.
