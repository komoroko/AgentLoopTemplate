# Changelog

Releases, newest first — one `## [x.y.z] - YYYY-MM-DD` heading per release (`agentloop
upgrade` shows the sections between the installed version, recorded in
`.agentloop/agentloop.lock`, and the new one). The version's single source is
`pyproject.toml [project] version`.

## [0.7.3] - 2026-07-18

### Added
- **The dashboard can switch between projects.** A new user-global project registry
  (`agentloop project add|list|remove|use`, stored at `$XDG_CONFIG_HOME/agentloop/projects.yaml`)
  names the repos you work across, and `agentloop ui` grows a **project switcher** dropdown in its
  header that retargets the whole board — status, gate approvals, and actions — without restarting
  the server. `agentloop ui` auto-registers the repo it was launched from (most-recently-used), and
  can now start from any directory once projects are registered. The browser only ever sends a
  registered *name*; the server resolves it to a path through the registry, so switching never
  widens what the dashboard can reach (arbitrary command execution stays structurally impossible).
  The existing per-command `--repo PATH` / `AGENTLOOP_ROOT` targeting is unchanged and now
  documented in the READMEs.

### Changed
- **Diagnostics now go through `logging`, not bare `print`.** Every error/warning/progress
  message the CLI wrote to stderr is now emitted through a per-module `logging.getLogger`
  (all children of the `agentloop` logger, configured once at each entry point), while genuine
  command *results* stay on stdout via `print`. The stderr text is byte-identical to before, so
  scripts and the existing output are unaffected; the split just makes the two channels — data
  vs. diagnostics — explicit and independently controllable.
- **Version-skew detection uses `packaging.version` instead of a hand-rolled parser.** The
  per-invocation lock check (`lock.startup_warning`) compared versions by stripping non-digits
  into an int tuple, which mis-ordered pre-releases and normalized spellings. It now parses both
  sides with `packaging.version.Version` (a new, minimal runtime dependency) for correct PEP 440
  ordering, and stays silent on a non-PEP 440 (hand-corrupted) string — `doctor` surfaces that in
  its deeper pass.

## [0.7.2] - 2026-07-16

### Added
- **Release notes now carry install instructions.** `release.yml` appends a generated
  Install footer to the release body after extracting the CHANGELOG section: the pinned
  `uv tool install git+<repo>@vX.Y.Z` line plus the upgrade one-liner (`uv tool upgrade
  agentloop`, then `agentloop upgrade` in each adopted repo). The repo URL is derived from
  the workflow context, not hardcoded.

### Changed
- **README.ja.md rewritten in natural Japanese.** The Japanese README was a near-literal
  translation of README.md (long sentences, English dash insertions carried over verbatim).
  Rewritten for clarity and concision with the same information and structure; the
  template-lint EN↔JA parity canaries are unchanged and stay green.

## [0.7.1] - 2026-07-16

### Added
- **CI can now cut GitHub Releases.** A new `.github/workflows/release.yml` runs on manual
  dispatch (Actions tab): it derives the tag `vX.Y.Z` from the `pyproject.toml` version (the
  single version source), refuses to re-release an existing tag, extracts the matching
  `CHANGELOG.md` section as the release body, then creates and pushes the tag and publishes a
  notes-only GitHub Release. Distribution stays git-tag + `uv tool install git+<repo>@vX.Y.Z`,
  so no build artifacts are attached.

## [0.7.0] - 2026-07-14

### Changed
- **The harness is now an installable package, not copied files.** `scripts/agentloop/`
  became the `agentloop` Python package (`src/agentloop/`, console script `agentloop`,
  installed with `uv tool install git+<this repo>`); product repositories keep only their
  state — `.agentloop/` (SSOT + lock + materialized prompts/schema) and `docs/`
  (deliverables). Prompt bodies, JSON schemas, the rules `AGENTS.md`, the repo scaffolds,
  and the per-agent integration surfaces ship inside the wheel (`src/agentloop/data/`).
- **Repo-root discovery replaces the cwd assumption.** Every command resolves the
  repository once per invocation — `--repo` flag > `AGENTLOOP_ROOT` > walking up from cwd
  for `.agentloop/` — into resolved absolute paths (`repo.py`), so the tools behave
  identically from any launch directory. The gate guard resolves the root from the hook
  payload's `cwd`, fixing evaluation for hooks fired in subdirectories and worktrees.
- **Versioning is consolidated.** The `VERSION` file is gone: `pyproject.toml` is the
  single version source (`agentloop.__version__`). `.agentloop/agentloop.lock` records the
  tool version/source, the SSOT `schema_version`s, and a content hash per installed file
  (replacing `.agentloop/adopt-manifest.yaml`); the tool refuses lock formats and
  `schema_version`s newer than it knows, and warns on tool↔lock version skew.

- **Agent integrations are opt-in.** `.claude/` and `.github/` surfaces no longer land
  unconditionally: `agentloop install claude|copilot` writes them (settings.json merged
  marker-guarded, hooks invoking `agentloop guard`), `agentloop uninstall <agent>|--all`
  retracts them (pristine files only), and the lock records what is installed with a hash
  per file so a locally modified wrapper is never overwritten or removed silently.
- **`agentloop init` replaces the copy-the-template + `make adopt` split.** One command
  seeds a repo (greenfield or brownfield, auto-detected) purely from the packaged payload —
  SSOT trio, docs scaffolds, materialized artifacts, scaffold snapshot, AGENTS.md pointer,
  lock — and never touches product build files. Brownfield detection scopes `guard_paths`
  to docs and fills the quality-gate commands from the repo's tooling.
- **The daily verbs and every make target are now `agentloop <verb>`.** Gate approval moves
  to `agentloop approve <gate> [--by <name>]` (still never pre-authorized). Products need no
  `make`; the template repo keeps a makefile only for its own dev workflow.

### Added
- `agentloop sync [--check|--force]` rematerializes `.agentloop/prompts|schema|rules` from
  the installed package; `agentloop upgrade` shows the changelog transition and refreshes
  everything materialized (upgrading the code itself is `uv tool upgrade agentloop`).
- `schema_version: 1` in `config.yaml`/`tasks.yaml` (validated by the bundled JSON
  schemas; `dag.load`/`Config.load` refuse newer versions with an upgrade hint).
- `doctor` checks for the lock (readability, format, writer-version skew) and for the hook
  command's binary being resolvable on PATH.

### Fixed
- **Swept the prompts, rules, scaffolds, and config comments onto the installed CLI.**
  The migration had left invocations that no longer run: `agentloop <verb> ARGS="…"`
  (the old make passthrough — now a literal argv token that fails to parse), `dag.py
  --trace|--impacted|--render|--mermaid` and `uv run … scripts/agentloop/*.py …` (the
  retired in-repo script paths), and a few residual `make init|cycle-close|events`
  references. They are now `agentloop dag …` / `agentloop events …` / `agentloop init` etc.
  `config.yaml` comments and the `scripts/README.md` no longer point at the removed
  `scripts/agentloop/` tree or its retired always-allowed guard carve-out.
- **`agentloop dag` is listed in `--help`.** The verb was dispatched but undocumented, so
  the prompts that reference `agentloop dag --render|--trace|--mermaid|--impacted` pointed
  at a command the help never mentioned.
- **`data.py` no longer imports the deprecated `importlib.abc.Traversable`** (removed in
  Python 3.14): a `sys.version_info` guard uses `importlib.resources.abc` on 3.11+ while
  keeping the 3.10 floor working.

### Removed
- `VERSION`, `agentloop.mk`, the `./agentloop` shell wrapper, the `scripts/agentloop/`
  location, and the empty `backend/`/`frontend/` scaffolds (products bring their own stack).

### Migration (from a repo adopted by ≤ 0.6.0)
A clean break — no automated migration. In the old repo, remove the previous machinery
(`make -f agentloop.mk agentloop-uninstall` if it is still present, or delete the copied
`scripts/agentloop/`, `agentloop.mk`, `agentloop`, and the `.claude`/`.github` wrappers by
hand). Then, with the CLI installed (`uv tool install git+<this repo>`), run `agentloop
init --name <same-product>` followed by `agentloop install claude` (and/or `copilot`). Your
`.agentloop/state.md`, `config.yaml`, `tasks.yaml`, and filled `docs/**` survive — `init`
never overwrites an existing file — so the lifecycle continues where it left off.

## [0.6.0] - 2026-07-14

### Added
- **Gate approval is an operation, not a state-file edit**: `make approve GATE=<gate>
  [BY=<name>]` (`scripts/agentloop/approve.py`, the forward twin of `revise.py`) stamps the
  date/approver on the gate line, advances `current_phase`, and appends a `gate_approved`
  event — the single sanctioned `pending → approved` write path. Gate rule 2 gains its own
  mechanism layer in `gate_guard.py`: **edit-time**, a Write/Edit/MultiEdit whose result
  flips a state.md gate to `approved` is denied (deliberately **not** relaxed by
  `template_mode`; `enforce_hook: false` stays the escape hatch); **commit-stage**,
  `--check-diff` fails a flip vs HEAD that has no matching `gate_approved` event, catching
  shell-redirect/sed bypasses. The `make ui` approval endpoint delegates to the same
  operation. `make approve` must never be pre-authorized — its permission prompt is the
  human's confirmation (AGENTS.md "Tool-execution permissions").
- **Crash recovery salvages a previous run's work instead of destroying it**: on re-run,
  `build_loop.py` no longer unconditionally force-removes a leftover leaf worktree and
  `branch -D`s its branch. Uncommitted worktree changes are finalized onto the leaf branch
  first (if that fails, the loop stops and keeps the tree — nothing unmerged may be the only
  copy); a branch holding commits the work branch does not have is renamed to
  `<leaf>-salvage-<UTC stamp>` and recorded as a `branch_salvaged` event. Fully-merged
  leftovers are deleted as before. This also protects the branches `_cleanup_worktree`
  deliberately keeps for human inspection.
- **One human entry point, four verbs**: `./agentloop start | next | ui | agent` (root
  wrapper + `scripts/agentloop/cli.py`, a thin dispatcher over the owning modules). The
  human-facing command surface is deliberately compressed to these; the operational make
  targets (approve / revise / doctor / events / cycle-close / build-loop / agentloop-upgrade)
  are unchanged and listed in `./agentloop --help`. `approve` is deliberately not a verb —
  the "never pre-authorize `make approve`" rule keeps a single spelling. `make adopt`
  installs the wrapper too (template-owned: upgrade refreshes it while pristine).
- **`agentloop start`** — on a fresh copy: an interactive setup wizard (product name, work
  branch, upgrade source, headless agent CLI, and an optional 1–3-line product-brief seed
  written after the pristine scaffold snapshot). Answers are collected before anything is
  written; non-TTY falls back to the `make init NAME=` hint. On an initialized repo: a
  one-line "where you are" plus the next recommendation (read-only).
- **`agentloop next`** — `status_api.py --next` prints only the deterministic next-command
  recommendation (`--json` emits the recommendation object alone, for integrations).
- **`agentloop agent <claude|codex|gemini|"custom cmd">`** (`scripts/agentloop/agent_cli.py`)
  — rewrites exactly the `build.headless.cmd` line (surgical line surgery; comments survive).

### Changed
- `events.py`: new event kinds `gate_approved` / `branch_salvaged`; `Event` gains a
  first-class `gate` field (what the commit-stage flip check matches on).
- `common.py` now hosts `set_current_phase` / `set_updated_at` (moved from `revise.py`;
  shared by `revise.py`, `cycle.py`, and the new `approve.py`).
- CLI errors now name the broken input **and** the next command (`make doctor` for SSOT
  parse failures) — `revise.py` / `build_loop.py` / `events.py` / `dag.py` / `init.py`.
- Decisions recorded (no code change): **no per-agent adapter layer for the headless CLI**
  (`build.headless.cmd` + prompt-as-last-argument covers every known CLI; the extension
  point, should one ever not, is `_parse_headless`); the `make ui` dashboard already is the
  "driver's seat" (gate approval + a fixed whitelist of safe operations; phase execution and
  outward-facing operations stay excluded by design); and **no config.yaml profile/preset
  layer** — the frequently-switched knob is covered by `agentloop agent`, the quality-gate
  commands are product-specific (adopt already injects them), and a preset layer would add
  indirection to the single source of knobs (YAGNI).

## [0.5.0] - 2026-07-12

### Added
- **Local dashboard UI (`make ui`)**: a stdlib-only (`http.server`, no new dependency) web
  page that visualizes the SSOT — phase stepper with gate marks, the task DAG as
  status-colored layer chips, open escalations — and shows the **next recommended command**,
  computed deterministically in `status_api.py` (the "what next" logic that previously lived
  only as prose in the `/status` prompt). Guidance-first and read-only for reads; a fixed
  whitelist of safe operations (gate-approval recording, `make doctor`, `events --resolve`,
  `revise`, `cycle-close`) can be run from the page — the client sends an action id, never a
  command string, so command lines are built server-side. Binds `127.0.0.1` with a per-start
  token; `make ui ARGS=--read-only` disables the action endpoints. Phase execution (`/req`…
  `/verify`) stays in the agent chat. Opens inside VS Code too (Simple Browser / PORTS
  preview): `make ui` detects a VS Code terminal (`TERM_PROGRAM=vscode`) and prints the
  Simple Browser hint instead of launching an external browser. The page renders the task
  graph as an inline **dependency-graph SVG** (offline-safe, no CDN), a **traceability**
  panel (requirement → design → task coverage, reusing `dag.trace`), the **speculative-work
  and roll-back logs** parsed from state.md, per-task detail on click, and is **theme-aware**
  (auto dark/light with a toggle) with a live "updated Ns ago" / connection indicator, manual
  refresh, and action toasts. Visual identity is a "control console": a teal/amber signal
  palette, machine-computed values in monospace, and the lifecycle rendered as an illuminated
  **loop rail with gate locks** (the live phase glows amber at the gate awaiting the human).
- **Commit-stage gate enforcement (agent-agnostic)**: `gate_guard.py --check-diff`
  fails when the diff vs HEAD (worktree + index + untracked) touches a gate-guarded
  path whose prerequisite gate is unapproved. Registered as a local pre-commit hook,
  so it runs inside `make check` (every agent's DoD) and on `git commit`; `make setup`
  now runs `pre-commit install` so the commit-stage layer actually fires. This gives
  hook-less agents (e.g. Codex) a mechanism layer at the commit/DoD boundary and also
  catches edits that bypass the tool hooks (e.g. shell redirects). `template_mode` /
  `enforce_hook: false` short-circuit it as before.
- **Minimal-implementation (YAGNI) discipline standardized** (adopted from the
  ponytail comparison, `docs/notes/ponytail-comparison.md`): AGENTS.md Principles,
  the implementer protocol, the quality-gate `review` step prompt, and the /build
  procedure now state explicitly that implementation stays at the minimum the
  ticket's acceptance criteria require — no speculative generality. No new gate step.
- **Merge-stage gate enforcement (mode A)**: `build_loop.py` re-evaluates every path a
  task changed against the gate rules before a leaf branch merges into work (and before
  a serial task is marked done). Preservation commits run `--no-verify` and a commit
  already in the work branch's HEAD escapes the commit-stage diff check, so a stray
  next-phase edit used to be able to land silently; it now escalates as a
  `gate_violation` event, blocks the task, and keeps the branch unmerged for human
  review. `template_mode` / `enforce_hook: false` short-circuit it as everywhere else.
- **Pluggable headless CLI for mode A** (`build.headless.cmd` in `.agentloop/config.yaml`):
  the deterministic build loop launches its implementer / review step / integration fixer /
  security review through a configurable command — default `["claude", "-p"]`; `codex exec`,
  `gemini -p`, etc. also work (the prompt is appended as the last argument). `make doctor`'s
  binary probe follows the knob. **Breaking**: `build_loop.py --claude-bin` is removed —
  set `build.headless.cmd` instead.
- **Deterministic roll-back impact marking**: `revise.py --impacted T-00x,T-00y` marks the
  directly-affected seeds **and their transitive dependents** `needs-revision` in tasks.yaml
  in code (combinable with `--to`; `--dry-run` previews; former statuses are printed for the
  /tasks reconcile). Missing an impacted task was previously prompt-discipline only; now the
  whole closure is parked mechanically and "keep" is a deliberate reclassification at gate ③.
- **template_lint canary for the guard_paths pair**: the shipped config.yaml's
  `gates.guard_paths` block must mirror `gate_guard._DEFAULT_GUARD_PATHS` exactly, so the
  hand-maintained duplicate can no longer drift from the code default.

### Changed
- **Default guard paths widened to the common code layouts**: `src/`, `lib/`, and `app/`
  now require the tasks gate (alongside `backend/`, `frontend/`, `scripts/`); `tests/`
  stays deliberately unguarded so approval-wait speculative work (fixtures, harness prep)
  keeps flowing — AGENTS.md and the /design·/tasks "While waiting" sections now state that
  speculative work must stay outside `gates.guard_paths`.
- **Agent-specific text pruned from the neutral files; AGENTS.md compacted ~15%**: the
  `AskUserQuestion` dialect leak in the docs scaffolds is fixed and template_lint's dialect
  canary now scans the docs scaffolds too (`docs/notes/` and `docs/archive/` are records and
  stay exempt). AGENTS.md no longer names per-agent hook hosts or "Claude Code's
  `/security-review`" — that detail lives in the capability mappings — and its prose is
  tightened throughout (every rule, table, and machine-checked token survives verbatim).
- **Mode A's requirement stated accurately**: the deterministic build loop needs **the
  `claude` CLI installed and authenticated** (the orchestrator launches `claude -p` itself),
  not "Claude Code only" — any agent, or the human in a terminal, can invoke
  `make build-loop`; without the CLI, use mode B. build.md, both mappings, and both READMEs'
  agent-support matrices updated.

### Fixed
- **`build_loop.py --dry-run` is now strictly read-only**: it used to write task
  statuses through to `tasks.yaml` (one dry run marked every task `done`) and could
  append escalation events. Statuses now advance in an in-memory overlay only; the
  event log, state.md, and the run lock are untouched, so a dry run can no longer
  corrupt the starting state of the next real run.

## [0.4.0] - 2026-07-12

### Added
- **Multi-agent support**: the operating rules move to a canonical, agent-neutral
  `AGENTS.md` (capability vocabulary + degradation rules; Codex and other AGENTS.md
  readers get rules+procedures support natively), the eight phase procedures and three
  role definitions move to shared bodies in `.agentloop/prompts/`, and
  `.claude/commands|agents/*` become thin wrappers over them. `CLAUDE.md` shrinks to
  the Claude Code capability mapping (+ `@AGENTS.md` import).
- **VS Code GitHub Copilot surfaces**: `.github/prompts/*.prompt.md` (the `/req` …
  entry points), `.github/agents/*.agent.md` (`@architect` etc.),
  `.github/instructions/agentloop.instructions.md` (the Copilot capability mapping),
  and `.github/hooks/agentloop.json` — the same `gate_guard.py` deny contract runs
  under VS Code agent hooks (preview), so the gates' mechanism layer works from
  Copilot too. `gate_guard.py` accepts the camelCase `filePath` VS Code sends.
- **template_lint drift checks** for the new layout: wrapper parity (both dialects
  wrap every shared body, descriptions byte-identical), capability-mapping set
  equality across the two mapping files (every token defined in AGENTS.md), and a
  dialect canary (Claude-only mechanism names must not leak into neutral files).

### Changed
- **adopt/upgrade/uninstall**: the rules body installs as
  `.agentloop/AGENTS.agentloop.md` (was `CLAUDE.agentloop.md`; `--upgrade` migrates
  the CLAUDE.md import line and retires the pristine legacy file). The target
  AGENTS.md gets a marker-guarded pointer block (recorded as `agents_md` in the
  manifest, retracted on uninstall); the CLAUDE.md import block now carries the
  Claude capability mapping. The `.github` surfaces and `.agentloop/prompts/` are
  copied template-owned.
- **doctor**: `check_hook` passes on either hook host (`.claude/settings.json` or
  `.github/hooks/*.json`), reports which are registered, and flags single-host
  registration as INFO (the other host runs convention-layer only).

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

### Removed
- **`make feedback`** (`feedback.py`, `github.feedback.*`): filing retrospective rows as
  issues on the upstream template repository was elaborate machinery for a flow that a
  hand-written issue serves just as well — retrospective §5 now simply says to file
  `Promote? = upstream` rows by hand and record the URL. Repos that upgraded earlier can
  delete `scripts/agentloop/feedback.py` and the `github.feedback` config block.

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
