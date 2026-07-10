# Changelog

Template releases, newest first. `agentloop-upgrade` shows the sections between the
installed version (recorded in `.agentloop/adopt-manifest.yaml`) and the new one, so keep
one `## [x.y.z] - YYYY-MM-DD` heading per release. Neither this file nor `VERSION` is
copied by `make adopt` — the manifest's `template.version` is the identity record.

## [0.2.0] - 2026-07-10

### Added
- **Structured event log** (`scripts/agentloop/events.py`, `.agentloop/events.ndjson`):
  the escalation log's machine-readable truth. `build_loop.py` emits typed events
  (`blocked` / `merge_conflict` / `integration_red` / `no_runnable` / `step_fail` /
  `task_done` / `security_review`); state.md embeds a generated view between
  `ESCALATION-VIEW` markers; `make events` renders / adds / resolves / aggregates.
  Rotation carries open escalations forward. `build-loop.log` is retired.
- **Post-merge integration gate**: after a parallel batch merges 2+ leaves,
  `build_loop.py` re-runs the cmd steps once on the merged work branch (each leaf was
  green only in isolation); a red goes to a headless fixer within the step's retry
  budget, else the batch blocks (`integration_red`). Single-leaf joins skip the cost.
  Knob: `quality_gate.integration_gate` (default on).
- **Uncommitted-worktree protection**: leaf diffs are finalized onto their branch
  before merge and before blocked/conflict cleanup (`T-NNN: WIP (blocked)`), so an
  implementer's forgotten commit can no longer be lost with the worktree.
- **Bound post-build security review**: when all tasks are done, `build_loop.py`
  auto-runs a headless review and writes `.agentloop/security-review.md` embedding
  `Reviewed-HEAD: <hash>` (idempotent per HEAD; recorded as a `security_review` event).
  Knob: `build.post_build.security_review` (default on).
- **`make doctor`** (`scripts/agentloop/doctor.py`): read-only diagnosis of binaries,
  config/state/tasks consistency (incl. the gate-chain invariant), gate-guard hook
  registration, branch/worktree/lock leftovers, and open escalations.
- **NFR traceability**: non-functional requirements get `NFR-N` IDs; `dag.py --trace`
  follows them with softer rules (missing design/task = WARN, dangling ref = ERROR),
  and the new `--trace --test-plan <path>` fails any R/NFR absent from the test plan
  (run by `/verify`).

### Changed / migration notes (for repos upgrading the machinery)
- `.agentloop/state.md` is not overwritten by upgrades: to adopt the generated
  escalation view, replace your "Escalation log" table with the new scaffold's marker
  block (`<!-- ESCALATION-VIEW:BEGIN/END -->`) by hand — without markers everything
  still works, the view is simply not embedded.
- `.agentloop/build-loop.log` is no longer written; `make cycle-close` still archives
  a leftover one. `.agentloop/events.ndjson` is deliberately tracked in git.
- `dag.py trace()` / `TraceReport` gained NFR and test-plan dimensions (signature
  extended; exit codes unchanged).

## [0.1.0] - 2026-07-08

### Added
- Template version identity: `VERSION` + this changelog; `adopt`/`init` record
  `template.version` in the manifest and `agentloop-upgrade` prints the
  installed → new transition with the changelog sections in between.
- Greenfield provenance: `make init NAME=<product> [FROM=<template-url>]` now writes an
  adopt-manifest (`mode: init`), so copied-template repos can run `agentloop-upgrade` /
  `agentloop-uninstall` too. Pre-0.1.0 greenfield repos can backfill with
  `make init NAME=<same-name> FROM=<url>`.
- `make feedback` (`scripts/agentloop/feedback.py`): optionally file cycle retrospective
  rows marked `Promote? = upstream` as issues on the upstream template repository.
  Opt-in via `github.feedback.enabled`, human-run, idempotent, `--dry-run` support.

### Known limitations
- Upgrading with a pre-0.1.0 `adopt.py` rebuilds the manifest without the new
  `mode`/`template.version` fields; a greenfield repo would then be treated as adopted on
  the next upgrade. Upgrade the machinery once from a >= 0.1.0 template to pick up the fields.
