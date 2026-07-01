---
description: Phase 5 test phase. Run and record functional and non-functional tests, and ask for the release decision at gate ⑤.
---

# /verify — Test phase

## Prerequisite gate check (always first)
Read `.agentloop/state.md` and confirm `gates.build == approved`.
If unapproved, do not work; say "please complete and approve `/build` first" and stop.

## Steps
1. Read `docs/test/test-plan.md` and `docs/10-requirements.md` (acceptance points, non-functional requirements).
2. **Functional tests**: confirm each requirement's acceptance points are satisfied. Run the automated tests and add any missing verification. Record results in the test-plan table.
3. **Non-functional requirement tests**: check the criteria checklist (performance, security, reliability/operations). Security is mandatory — run the following and record results in the test-plan's security column:
   - **`/security-review`** — a vulnerability review of the whole codebase.
   - **`make audit`** — a dependency vulnerability audit (Python/frontend).
4. Record discovered defects/vulnerabilities in the test-plan's defect table. Make serious ones into new tasks, append them to `state.md`, and prompt the human to decide on rolling back to `/build` (those new tasks are `phase: verify`). For **requirement/design-level problems** (a spec error, etc.), use `/revise` at the human's discretion to roll back to the relevant phase (requirements/design).
5. **Gate ⑤**: present the test result summary (pass/fail, remaining issues, non-functional status) and have the human decide on release.
   - **Always present a self-assessment as well** (CLAUDE.md "Gate self-assessment"): release confidence, thinly-verified aspects, residual risks, points for the human to decide.

Write the deliverable (`docs/test/test-plan.md`) in the user's language.

## Once approved
- Set `gates.release` to `approved`, `current_phase` to `done`, and update `updated_at` in `state.md`.
- **Leave a retrospective (recovering the metacognition)**: generate/update `docs/retrospective.md`.
  - Classify needs-revision / blocked into "upstream (requirements/design) defect / implementation convenience / external factor" and summarize the lessons for upstream.
  - **Close the open items** of the "escalation log" and "speculative work log" in `state.md` (blank resolution/adoption columns) — do not leave them dangling.
- Report completion.
