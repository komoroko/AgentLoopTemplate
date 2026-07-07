# Changelog

Template releases, newest first. `agentloop-upgrade` shows the sections between the
installed version (recorded in `.agentloop/adopt-manifest.yaml`) and the new one, so keep
one `## [x.y.z] - YYYY-MM-DD` heading per release. Neither this file nor `VERSION` is
copied by `make adopt` — the manifest's `template.version` is the identity record.

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
