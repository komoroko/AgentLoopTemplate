# AgentLoop — Agent Operating Rules

AgentLoop develops software **Human on the Loop**: a coding agent performs the work and every
phase is judged against **externally-anchored, independent evidence** — not the agent's own
self-consistent explanation; **humans review/decide at the "gate" on each phase boundary, and a
gate opens only by a signed attestation.** The machinery is an installed CLI (`agentloop`); the
repository carries only its state — `.agentloop/` (SSOT, lock, materialized prompts/schema) and
`docs/`.

This file holds the **always-true rules**. Each phase's procedure lives in
`.agentloop/prompts/commands/*.md`; phase-scoped rules live in
`.agentloop/prompts/rules/gate-workflow.md` — the phase commands read both. Your capability
mapping (`CLAUDE.md`, or the Copilot instructions file — present only when installed)
realizes the vocabulary below; with none, use the degradation column.

## Capability vocabulary (portable verbs)

Rules and procedures name human-interaction points with these neutral capabilities, never an
agent-specific tool.

| Capability | Meaning | Lacking it |
|---|---|---|
| `phase-invocation` | run a phase procedure (`/req` … `/status`) | read the command body, execute it |
| `structured-question` | batched multiple-choice questions | numbered chat options, then wait |
| `notify-and-wait` | flag a pending decision, then stop | state it, end the turn |
| `approval-presentation` | present a deliverable for approval | ask for an explicit "approve" |
| `session-compaction` | human-run session reset at a checkpoint | a fresh session; SSOT rehydrates |
| `role-delegation` | delegate to a role agent (analyst/architect/implementer/reviewer) | adopt the role inline, then return; parallel leaves go serial |
| `autonomous-build-iteration` | drive `/build` without per-iteration prompts | re-invoke the procedure each iteration |
| `command-preauthorization` | pre-authorize known-safe commands | approve each interactively |

## Language

Conversation and deliverables (`docs/**`) are written in **the user's language**; template
files stay in English. Machine-read vocabulary (`pending`/`approved`, task `status`/`kind`
values, `epistemic_status`) stays as-is in every language.

## Development lifecycle

```
brief → requirements → design → tasks → build → verify → done
        (/req)        (/design) (/tasks) (/build) (/verify)
          ▲gate①        ▲gate②     ▲gate③   ▲gate④    ▲gate⑤
```

`/req`→`docs/10-requirements.md` (gate① requirements) · `/design`→`docs/20-design.md`+ADRs
(gate② design) · `/tasks`→`docs/tasks/T-*.md`+`plan.yaml` (gate③ tasks) · `/build`→code+tests
then a **grounded review** (gate④ build) · `/verify`→`docs/test/test-plan.md` (gate⑤ release).

`/status` shows progress; `agentloop next`/`ui` show the same board (a fixed safe-operations
whitelist, never phase execution). At `done`, `/verify` records `docs/retrospective.md`. An
ongoing repo repeats the lifecycle as **delta cycles**, closed with `agentloop cycle-close`
(mechanics: the rules module). **A scope change to approved requirements goes through
`/revise` or the next cycle — never widened silently.**

## Single Source of Truth (SSOT)

Four documents, distinct roles — do not conflate them:

- **`.agentloop/plan.yaml`** — the frozen **Expected Model**: claims (`R-N`/`NFR-N`), sources,
  evidence obligations, oracles, and the task DAG. Threads requirements → design → tasks,
  cross-checked by `agentloop dag --trace`. Frozen at gate ③.
- **`.agentloop/state.yaml`** — phase, gate approvals, task status. `gates.<name>` is
  `pending`|`approved` — **the only write path to `approved` is importing a signed attestation.**
- **`.agentloop/review.yaml`** — the **machine review** and the **human review**, digested
  *separately*. Regenerating the machine review resets the human review; a human answer never
  makes the machine review stale.
- **`.agentloop/events.ndjson`** — the hash-chained audit log. Every state change records why;
  a deleted, reordered, or re-hashed line breaks the chain a release attestation pins.

Authority — *who* may approve — lives **outside** the repo, in the Trust Manifest
(`$XDG_CONFIG_HOME/agentloop/trust.yaml`), so a pull request can never widen its own permissions.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command
   checks its prerequisite up front; if unapproved, stop and say what is needed.
2. **Only a signed attestation opens a gate.** A localhost click is not authentication. Go only
   as far as an `approval-presentation`; `agentloop approve <gate>` checks readiness and emits an
   **attestation request** — it does **not** open the gate. The gate opens when a key the external
   Trust Manifest names signs the request and `agentloop attestation import` records it (advancing
   the phase, logging `gate_approved`, machine-checking bound evidence). Never edit a gate line
   yourself, and never pre-authorize `agentloop attestation import`.
3. **Do not silently fix problems in requirements/design.** Set the task `needs-revision`,
   record a `knowledge-gap`/escalation event, and raise it to the human.

Enforcement is layered: `agentloop guard` denies violations in code at edit/commit/merge
stage; unreadable gates **fail closed**. **A guard denial marks a gate boundary — never
disable, relax, or bypass it** (detail: the rules module).

## Roll back (returning upstream)

On a confirmed upstream defect, roll back at the human's discretion with `/revise`: **gates
reset in a chain** — an upstream `pending` never leaves a downstream gate `approved`, and it
invalidates the attestations and the review built on top of it. **Rewinding approval is a human
privilege**, never automatic. Reclassify each task the impact analysis (`agentloop dag
--impacted`) flags, never discard (procedure: revise.md, tasks.md).

## Task dependency graph

Tasks form a **DAG**: kind = **foundation** / **parallel** / **integration**; layers and the
critical path derive from `blockedBy`. Consumption order, parallelism, merge, and stopping
run **in code**, not LLM discretion (detail: build.md, tasks.md).

## Principles

- **Reuse first; build only the minimum acceptance criteria require (YAGNI)** — speculative
  generality no requirement names is scope creep.
- **A fact with no evidence is `unknown`, never prose.** Grounding comes from an external source,
  observed code, or a frozen oracle — reported on three separate axes (integrity / semantic
  support / conformance). There is no single `verified`, and "extra behaviours: 0" shows only
  with the Coverage Manifest that earned it.
- **Pass the quality gate before moving on.** DoD = `quality_gate.steps` in
  `.agentloop/config.yaml` (default `test`→`check`→`review`→`smoke`; runnable deliverables
  set `smoke`'s `required: true`). The lead **re-runs each command step and reads its exit
  status** — a delegated agent's textual "green" is never evidence. Repo code, tests, and
  oracles run in the **OCI sandbox**, never on the host.
- **Small and sure.** One commit, one concern; approval before destructive/outward-facing ops.
- **Context isolation and hygiene.** Delegate phase work to role agents; keep deliverables and
  logs lean (tiers, GC, compaction: the rules module).
- **Promote durable lessons** from `docs/retrospective.md` into the always-loaded files at
  gate ⑤, not archived away.
- If anything behaves oddly, run `agentloop doctor` first.

## Security gate

**gitleaks** at commit stage; a **structured security review** feeds the grounded review before
gate ④ (bound to the reviewed HEAD; a later commit leaves it stale), repeated with a **dependency
audit** at `/verify` (detail: build.md, verify.md).

## Branch / commit / permissions

- Implement **on a work branch** (`work_branch` in `config.yaml`), never on main; parallel leaves
  use worktree branches (`<branch>-T-NNN`) and route every decision through the control plane so a
  worktree's record survives its deletion.
- Per-task commits **`T-NNN: <summary>`**; commit each phase's deliverables at its gate approval.
- **Push / PR / merge to main are outward-facing** — human approval only, same for GitHub Issues.
- `command-preauthorization` of known-safe commands cuts repeated prompts **without touching
  gates** (generic commands in the installed settings; product-specific ones in the product's
  own) — never pre-authorize push/PR/merge/`cycle-close`, nor `agentloop attestation import`
  (gate rule 2).
