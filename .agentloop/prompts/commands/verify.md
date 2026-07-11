# /verify — Test phase

(Capability terms resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Prerequisite gate check (always first)
Read `.agentloop/state.md` and confirm `gates.build == approved`.
If unapproved, do not work — **invoking `/verify` is not itself the gate-④ approval**: say `build` is still `pending` and needs explicit approval first, and stop.

## Steps
1. Read `docs/test/test-plan.md` and `docs/10-requirements.md` (acceptance points, non-functional requirements). After filling the plan, run `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --trace --test-plan docs/test/test-plan.md` — it mechanically fails (exit 1) any `R-N`/`NFR-N` that never appears in the test plan, so no requirement can silently drop out of verification. Fix the plan until it exits 0 (2 = cannot check: path/notation problem).
2. **Functional tests**: confirm each requirement's acceptance points are satisfied. Run the automated tests and add any missing verification. Record results in the test-plan table. Also fill the test-plan's **Manual verification checklist** (acceptance automated tests can't cover — real-player/device playback, visual/aesthetic review, supported-OS matrix, long-input/end-to-end performance); mark unrun items and surface them as remaining issues at gate ⑤.
   - **Deliverable-inventory check (user-facing docs)**: explicitly confirm every existing user-facing deliverable still describes the *current* behaviour — `README*` (all languages/mirrors), CLI `--help`/`--version` text, and any usage docs. A cycle that changes flags/behaviour easily leaves a stale doc behind (e.g. a translated README that was never updated from a prior cycle, or a removed flag still documented). Treat a stale user-facing doc as a defect.
3. **Non-functional requirement tests**: check the criteria checklist (performance, security, reliability/operations). Security is mandatory — run the following and record results in the test-plan's security column:
   - **`/security-review`** — a vulnerability review of the whole codebase (in an environment without that command, perform an equivalent security-focused review and record it the same way).
   - **`make audit`** — a dependency vulnerability audit (Python/frontend).
4. Record discovered defects/vulnerabilities in the test-plan's defect table. Make serious ones into new tasks, append them to `state.md`, and prompt the human to decide on rolling back to `/build` (those new tasks are `phase: verify`). **Rolling back to `/build` is a `/revise` operation like any other**: at the human's decision run `make revise ARGS="--to build --reason '<defect>'"`, which resets `gates.build` and `gates.release` to `pending` in a chain — do not re-enter `/build` while a stale `gates.build: approved` still stands; gate ④ is re-taken after the fix. For **requirement/design-level problems** (a spec error, etc.), use `/revise` at the human's discretion to roll back to the relevant phase (requirements/design).
5. **Gate ⑤**: present the test result summary (pass/fail, remaining issues, non-functional status) as an **`approval-presentation`** and have the human decide on release.
   - **Always present a self-assessment as well** (AGENTS.md "Gate self-assessment"): release confidence, thinly-verified aspects, residual risks, points for the human to decide.

Write the deliverable (`docs/test/test-plan.md`) in the user's language.

## Once approved
- Set `gates.release` to `approved`, `current_phase` to `done`, and update `updated_at` in `state.md`.
- **Leave a retrospective (recovering the metacognition)**: generate/update `docs/retrospective.md`.
  - Classify needs-revision / blocked into "upstream (requirements/design) defect / implementation convenience / external factor" and summarize the lessons for upstream.
  - **Close the open items**: every open escalation in the event log (`make events ARGS=--render` lists them) gets a `make events ARGS='--resolve <ID> --note "…"'`, and blank adoption columns in `state.md`'s "speculative work log" are filled — do not leave them dangling.
  - **Promote durable lessons into the template before `cycle-close` archives the retrospective.** For each item in the retrospective's "Process / template improvement" and "Lessons for upstream" sections, decide with the human whether to lift it into the always-loaded template files (`AGENTS.md`, the procedure files in `.agentloop/prompts/**`, the per-agent wrappers and capability mappings); apply the agreed promotions and record where each landed (retrospective §5).
- **If `docs/05-current-state.md` exists** (an adopted/ongoing repo), update it with what this cycle changed: new modules, new reusable assets, convention changes, in-flight work that got finished.
- **If the cycle ships as a PR**, offer `make pr-draft`: it assembles the PR body from the SSOT (gate approvals, task table, requirement coverage, security-review binding, commit list) into `.agentloop/pr-draft.md`. Creating/pushing the PR itself stays outward-facing and human-run — the tool only prints the `gh pr create --body-file` line for the human.
- Report completion. **To start the next delta cycle** (ongoing repos run AgentLoop as a series of change-scoped cycles), tell the human to run `make cycle-close NAME=<slug>` — it archives this cycle's deliverables to `docs/archive/`, restores fresh scaffolds, and resets gates/phase. Closing a cycle is the human's operation, like opening a gate; do not run it yourself. Recommend starting the next cycle in a fresh session: the previous cycle's conversation is no longer needed — its baseline lives in `docs/05-current-state.md` and `docs/archive/` (AGENTS.md "Context budget").
