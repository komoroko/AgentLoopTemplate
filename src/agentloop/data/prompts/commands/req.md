# /req — Requirements phase

You drive this project's requirements definition. **Human on the Loop**: you do the work, the human decides.
(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.agentloop/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Steps
1. Read `.agentloop/state.yaml`. Check `current_phase`.
   - If `gates.requirements == approved` already, say "Requirements are approved; changing them needs re-approval" and wait for the human's instruction.
2. Read `docs/00-product-brief.md`. If empty, first prompt the human to fill it in and stop.
   - **Brownfield**: if `docs/05-current-state.md` exists (an adopted repo), **read it first**. Requirements are scoped to **this cycle's delta** (the change), not the whole product. If it links existing requirement documents, take them as the starting point rather than re-deriving from scratch — the human approving gate ① is what adopts them as this cycle's truth (the **fast intake** entry described in `/onboard`). In-flight/unfinished work listed there is candidate delta scope.
3. Delegate to the `requirements-analyst` role (`role-delegation`) to produce a requirements draft, gaps, and open points.
4. Resolve ambiguities and important branches by asking the human via a single **`structured-question`**: rank the analyst's `[NEEDS CLARIFICATION]` markers and open points by **impact × uncertainty** and batch the top ones into one call (~4 questions is the practical cap; prefer multiple-choice with a recommended option). Propagate each answer into the requirement text itself (resolving the marker) and record the Q&A in the `## Clarifications` section of `10-requirements.md` — the audit trail of how ambiguities were closed.
5. Write the agreed content into `docs/10-requirements.md` (in the scaffold structure). Give every functional requirement an `R-N` heading and **every non-functional requirement an `NFR-N` heading with a measurable criterion and how it will be verified** — both ID families are the thread `agentloop dag --trace` follows through design, tasks, and the test plan (NFR rules are softer: no dedicated design section/task is only a WARN, but presence in the test plan is enforced at `/verify`).
6. **Adversarial review** (required before gate ①): delegate to the `adversarial-reviewer` role (`role-delegation`) in a **fresh context — never the analyst that drafted** — with `docs/00-product-brief.md` and the written `docs/10-requirements.md` as its inputs. Record every finding in the `## Adversarial review` section of `10-requirements.md` with a disposition: `fixed` (the requirement text was updated), `disputed: <why>` (kept as-is, with the reason), or `accepted-risk`. **Every blocker must be resolved** (fixed or disputed-with-reason) before presenting the gate; re-invoke the reviewer once, on the blocker fixes only — no further rounds. Findings that need the human's judgment fold into the gate's single `structured-question` below. For a hotfix minimal cycle the human may waive this step; record the waiver as an audit event.
7. **Gate ①**: present the requirements summary as an **`approval-presentation`** and ask "may we freeze the requirements with this content?". Ask any accompanying confirmations **in a single `structured-question`**.
   - Present only when **no unresolved `[NEEDS CLARIFICATION]` marker remains** in `10-requirements.md`: anything deliberately left open is demoted to an explicit "Open questions" entry the human sees at the gate — never left as a stray marker or a silent assumption.
   - **Always present a self-assessment as well** (contents: `.agentloop/prompts/rules/gate-workflow.md` "Gate self-assessment"); also leave it in the "Self-assessment" section of `10-requirements.md`, flagging low-confidence requirements for the human's attention.
   - **Also present the adversarial-review summary**: finding counts by severity, the dispositions, and any unresolved dispute (an unresolved dispute is the human's to settle — never loop further with the reviewer).

Write the deliverable (`docs/10-requirements.md`) in the user's language.

## While waiting for approval
`notify-and-wait` first; then only **outcome-independent, throwaway-by-default** work (rules: `.agentloop/prompts/rules/gate-workflow.md` "While a gate is pending"), recorded as speculative-work events:
- Repo scaffolding / directory layout / skeleton of the dev environment and CI.
- **Read-only investigation** of candidate technologies surfaced in the brief (do not finalize the design).
- **Forbidden**: writing the design body that pre-empts the requirements.

## Once approved
Only after an explicit human "approve": run `agentloop approve requirements`, which checks readiness and emits an **attestation request** — it does **not** open the gate. The gate opens when a key the external Trust Manifest names signs the request and `agentloop attestation import <signed>` records it. Never edit a gate line yourself, and **running the next command is not itself approval** (mechanics: AGENTS.md "Gate rules" 2). After committing the gate's deliverables, suggest `session-compaction` (pre-compact check: `.agentloop/prompts/rules/gate-workflow.md` "Context budget") and point to "next is `/design`".
