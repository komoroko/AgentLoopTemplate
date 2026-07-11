# Changelog

Template releases, newest first. `agentloop-upgrade` shows the sections between the
installed version (recorded in `.agentloop/adopt-manifest.yaml`) and the new one, so keep
one `## [x.y.z] - YYYY-MM-DD` heading per release. Neither this file nor `VERSION` is
copied by `make adopt` — the manifest's `template.version` is the identity record.

## [0.3.0] - 2026-07-11

### Added
- **Per-task test execution**: tasks.yaml's `test` command — documented as the task's
  green decision but never actually run — is now prepended to the quality gate as a
  focused `task-test` step when it differs from the configured cmd steps (dedup keeps
  the default `make test` single), and named in the implementer prompt.
- **`required` step knob**: a quality-gate cmd step marked `required: true` with an
  empty `run` makes `build_loop.py` refuse to start (fail-fast) instead of silently
  skipping — set it on `smoke` once the deliverable is runnable. Gate ④ now prints
  which cmd steps the DoD skipped; doctor FAILs the contradiction and WARNs an
  undecided empty smoke (an explicit `required: false` records the decision).
- **JSON Schemas** (`.agentloop/schema/*.schema.json`) for config.yaml / tasks.yaml:
  editor completion/validation via `yaml-language-server` modelines (the tasks.yaml
  one survives rewrites through `TASKS_HEADER`); `make doctor` validates both files
  (doctor/test-tools now pull in `jsonschema`; the ordinary runtime stays pyyaml-only).
- **`make pr-draft`** (`scripts/agentloop/pr_draft.py`): assemble a PR body from the
  SSOT (gate approvals with date/approver, task table, requirement coverage,
  security-review binding, commit list) into `.agentloop/pr-draft.md`. Read-only and
  never calls gh — PR creation stays human-run.
- **doctor, field-driven checks**: task↔ticket parity (docs/tasks/T-NNN.md), UNMERGED
  vs merged leftover leaf branches, security-review↔HEAD staleness once all tasks are
  done, events.ndjson size vs the rotation threshold, and `guard_paths` gate-name
  typos (which silently disable that path's guard → FAIL).

### Fixed
- **`_finalize_commit` swallowed failures**: a real commit failure (unset git identity,
  index lock) was indistinguishable from the clean-tree no-op, and the forced worktree
  removal right after would drop the very diff the finalize exists to preserve. The
  no-op is now decided by `git status --porcelain` up front, every rc is checked, the
  commit runs `--no-verify` (preservation, not a quality decision), and on failure the
  tree/worktree is kept and the loop escalates instead of continuing.

### Changed / migration notes (for repos upgrading the machinery)
- **The legacy quality-gate config form was removed**: `quality_gate.steps` is now
  required; `quality_gate.test_cmd` / `check_cmd` and `build.retries` are no longer
  read. A config still on the old form fails to load with a migration hint, and
  `make doctor` WARNs about stale legacy keys sitting next to a valid `steps` list.
  Migrate by writing the two commands as steps (see the template config.yaml).
- **Dev dependencies trimmed to what the template exercises**: `mkdocs`,
  `mkdocs-material`, `mkdocstrings`, `filetype`, and `pydantic` (plus the mypy hook's
  pydantic stubs) are no longer preinstalled — nothing in the template imported them.
  Products that use them add them back to their own `dev` group.
- `requires-python` relaxed from `>=3.13,<3.14` to the measured floor `>=3.10`
  (ruff `target-version` / mypy `python_version` follow). Products may re-pin freely.
- `make doctor` / `make test-tools` now launch with `--with jsonschema` in addition to
  pyyaml (first run downloads it once; everything else is unchanged).
- tasks.yaml gets a `yaml-language-server` modeline as its first header line on the
  next rewrite; add `.agentloop/schema/` when upgrading by hand so it resolves.

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

### Fixed
- **Default `worktree.branch_pattern` could never create a leaf branch**: git forbids a
  branch that is a path-prefix of another ref, so the old `{branch}/{task_id}` (e.g.
  `build/demo` + `build/demo/T-003`) always failed with "cannot lock ref". The default is
  now `{branch}-{task_id}`. Repos that copied the old config should change the pattern in
  `.agentloop/config.yaml` before their first parallel batch.

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
