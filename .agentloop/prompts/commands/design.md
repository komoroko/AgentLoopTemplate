# /design — Design phase

(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Prerequisite gate check (always first)
Read `.agentloop/state.md` and confirm `gates.requirements == approved`.
**If unapproved, do not work**; say "please approve the requirements with `/req` first" and stop.
If `gates.design == approved` already, say "Design is approved; changing it needs re-approval" and wait for the human's instruction (an upstream roll back is done with `/revise`; after `/revise` the design gate is back to `pending`, so proceed normally).

## Steps
1. Read `docs/10-requirements.md` and the existing code/assets.
   - **Brownfield**: if `docs/05-current-state.md` exists, **read it first** — design against its architecture/conventions, reuse the assets in its inventory (state which in the design), and take any existing design docs/ADRs it links as the starting point (the **fast intake** entry described in `/onboard` — gate ② approval adopts them).
2. Delegate to the `architect` role (`role-delegation`) to produce (a) an implementation approach for each requirement, (b) **existing assets that can be reused**, and (c) options for important technical choices (with trade-offs across cost/security/non-functional/effort). **If the product is itself an AI agent app**, also have the architect present the agent-specific choices (Architecture pattern / Context strategy / Tool design — the "AI agent application" lens in `.agentloop/prompts/agents/architect.md`) as options to decide.
3. Present the technical choices as options via a **`structured-question`** and **let the human decide**. Create a `docs/decisions/ADR-NNN.md` for each decision.
4. Write the finalized content into `docs/20-design.md`. **Place a design section (`### R-x → design`) covering every requirement (R-x) in `docs/10-requirements.md`, with none missing.** Do not add design with no backing requirement (out-of-scope build-out) on your own — if needed, return it to the requirements side (`/revise`). **If the product is not an AI agent app, delete the scaffold's "AI agent application design" section** (do not leave the conditional block orphaned).
5. **Forward-coverage check**: cross-check that the design **covers all requirements and adds nothing not in the requirements**. Once `docs/20-design.md` has a design section for each requirement, confirm the requirement IDs in the headings match the requirement IDs in the requirements document (this requirement↔design linkage is mechanically re-checked later by `/tasks`'s `agentloop dag --trace`).
6. **Adversarial review** (required before gate ②): delegate to the `adversarial-reviewer` role (`role-delegation`) in a **fresh context — never the architect that designed** — with `docs/10-requirements.md`, `docs/20-design.md`, and `docs/decisions/ADR-*.md` as its inputs. Record every finding in the `## Adversarial review` section of `20-design.md` with a disposition: `fixed` (the design text was updated), `disputed: <why>` (kept as-is, with the reason), or `accepted-risk`. **Every blocker must be resolved** (fixed or disputed-with-reason) before presenting the gate; re-invoke the reviewer once, on the blocker fixes only — no further rounds. Findings that need the human's judgment fold into the gate's single `structured-question` below. For a hotfix minimal cycle the human may waive this step; record the waiver in the `state.md` log.
7. **Gate ②**: make an **`approval-presentation`** of the design summary + the finalized technical choices + **requirement coverage (every R-x has a corresponding design section / nothing out-of-scope was added)** and confirm "may we proceed with this design?". Ask the technical-choice confirmations **in a single `structured-question`**.
   - **Always present a self-assessment as well** (AGENTS.md "Gate self-assessment"): assumptions made, per-area confidence (architecture/technical choices/non-functional, etc.), open questions, risks/trade-offs, and a context-bloat signal when relevant. Also leave it in the relevant section of `20-design.md`. Make low-confidence design spots explicit.
   - **Also present the adversarial-review summary**: finding counts by severity, the dispositions, and any unresolved dispute (an unresolved dispute is the human's to settle — never loop further with the reviewer).

Write the deliverables (`docs/20-design.md`, `docs/decisions/ADR-*.md`) in the user's language.

## While waiting for approval (minimizing the bottleneck)
After presenting gate ②, while waiting you may proceed with the following (**outcome-independent, throwaway-by-default**). Record in the "speculative work log" of `state.md`.
- `notify-and-wait`: tell the human the approval is pending.
- Setting up the skeleton of the dev environment / test harness / CI, lint/static-analysis config —
  **outside `gates.guard_paths`** (e.g. CI config, `tests/`, tooling); a path the guard denies waits
  for its gate instead.
- **Read-only investigation** of candidate libraries (install only after finalizing).
- **Forbidden**: finalizing tasks or doing real implementation that pre-empts the design/technical choices.

## Once approved
- Record the approval by running `agentloop approve design [BY=<approver>]` — it stamps the gate line, advances `current_phase` to `tasks`, and logs the `gate_approved` event (the permission prompt is the human's confirmation; never edit a gate line yourself — gate_guard denies it).
- After committing the gate's deliverables, suggest `session-compaction` before starting `/tasks` — the next command rehydrates from the SSOT, so nothing is lost (pre-compact check: AGENTS.md "Context budget").
- Point to "next is `/tasks`".

Do not finalize technical choices on your own. Always go through the human's decision.
