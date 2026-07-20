# AGENTS.md core/module split (R3) — next-cycle design memo

*A docs/notes memo is promote-then-delete: this exits when the split lands (or is rejected).*

## Why

The always-on floor is ~5.2k tok/session (AGENTS.md ~4.6k + CLAUDE.md + the state.md header),
paid even by a bare `/status`. The 2026-07 dedup cycle cut the on-demand tier (config −34%,
command bodies deduped) but deliberately reinvested part of the saving into process strength
(gate-③ adversarial review, approve preconditions); the floor itself is untouched. Splitting
AGENTS.md into a small always-on core + phase-scoped modules is the only remaining lever with
a large permanent yield: **~2.5–3k tok off every session**.

## Partition rule (found adversarially — keep it)

A rule stays in the core **iff its violation window is "any time"**: gate edits / approval
rules, branch discipline, language, the SSOT map, the capability vocabulary, the never-list.
Phase-scoped guidance (the lifecycle narrative detail, context-budget tiers table, speculative
work specifics, quality-command notes, cycle mechanics) moves to
`.agentloop/prompts/rules/*.md`, loaded by the commands that need it (same wrapper pattern as
`prompts/commands/`).

Target core: ~1.2–1.5k tok — lifecycle table, Gate rules, SSOT map, capability vocabulary
table, roll-back one-paragraph, the hard prohibitions.

## Follow-up changes the split forces (checked 2026-07-20)

- `template_lint.py`: anchors that must stay at path `AGENTS.md` — gate names, `dag.KIND_VALUES`,
  quality-gate step names (`check_vocabulary`), every capability token (`check_capability_mapping`),
  no Claude-only terms (`check_neutral_vocabulary` also scans `.agentloop/prompts/**`, which will
  cover the new rules/ modules automatically). Re-point or extend `_require` targets for anything
  that moves.
- `install.py` `MATERIALIZED` map + `sync`: add `prompts/rules/**` to the payload set; lock hashes
  follow automatically.
- Command bodies: each `prompts/commands/*.md` gains an explicit "read
  `.agentloop/prompts/rules/<x>.md`" line for its module (Copilot/native agents don't @-import).
- Both READMEs' directory tables; `CLAUDE.md`/copilot instructions stay pointed at AGENTS.md.
- Risk to manage: native agents (Codex) auto-load only AGENTS.md — anything moved out is invisible
  outside phase invocations, which is exactly why the partition rule above is the safety line.

## Verification

`make check` (template-lint incl. data parity, `sync --check`) + full pytest; then measure
`wc -w AGENTS.md` (target ≤ ~850 words) and confirm a `/status`-only session loads the core alone.
