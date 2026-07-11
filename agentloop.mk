# =========================================================
# agentloop.mk — AgentLoop's foundational targets (self-contained)
#
# Every target launches via `uv run --no-project --with pyyaml python`, so it
# needs only the uv binary — no project environment (`make setup`) and no
# particular language/stack. That is what lets an existing (brownfield) repo
# take just this file: `include agentloop.mk` in your makefile, or run it
# standalone with `make -f agentloop.mk <target>`.
# =========================================================

AGENTLOOP_PY := uv run --no-project --with pyyaml python

.PHONY: init adopt agentloop-upgrade agentloop-uninstall cycle-close build-loop issue-sync feedback revise events doctor test-tools

# Turn the copied template into a product (idempotent): fills the pyproject / state.md placeholders,
# snapshots the pristine docs scaffolds, records the adopt-manifest (FROM = the template's git URL,
# reused later by agentloop-upgrade), creates the work branch, and flips gates.template_mode off
# so the gate guard goes live.
#   make init NAME=myproduct [BRANCH=build/myproduct] [FROM=https://github.com/you/AgentLoopTemplate.git]
init:
	$(AGENTLOOP_PY) scripts/agentloop/init.py --name "$(NAME)" $(if $(BRANCH),--branch "$(BRANCH)") $(if $(FROM),--source "$(FROM)")

# Install AgentLoop into an EXISTING repository (run from this template checkout). Copies the
# machinery without overwriting anything, merges CLAUDE.md/settings.json additively, and writes
# brownfield defaults (guard_paths = docs only). See scripts/agentloop/adopt.py for details.
#   make adopt TARGET=../myrepo NAME=myrepo [TEST_CMD="npm test"] [CHECK_CMD="npm run lint"] [ARGS=--dry-run]
adopt:
	$(AGENTLOOP_PY) scripts/agentloop/adopt.py --target "$(TARGET)" --name "$(NAME)" $(if $(BRANCH),--branch "$(BRANCH)") $(if $(TEST_CMD),--test-cmd "$(TEST_CMD)") $(if $(CHECK_CMD),--check-cmd "$(CHECK_CMD)") $(ARGS)

# Refresh this repo's template-owned tooling from a newer template (manifest-driven,
# hash-checked: your local edits are never overwritten — they are skipped and listed; FORCE=1
# overrides). Works for adopted AND greenfield (make init) repos. Run inside the repo; FROM is a
# git URL or local path, and without it the source recorded at init/adopt time is reused. Prints
# the installed → new template version with the CHANGELOG sections in between — preview everything
# without applying via ARGS=--dry-run. Review with `git diff`, then commit.
#   make -f agentloop.mk agentloop-upgrade [FROM=https://github.com/you/AgentLoopTemplate.git] [REF=main] [FORCE=1] [ARGS=--dry-run]
agentloop-upgrade:
	$(AGENTLOOP_PY) scripts/agentloop/adopt.py --upgrade --target "$(or $(TARGET),.)" $(if $(FROM),--from-git "$(FROM)") $(if $(REF),--ref "$(REF)") $(if $(FORCE),--force) $(ARGS)

# Remove everything adopt installed from THIS repo (pristine files only: anything you edited is
# left in place and listed for manual review). Retracts the CLAUDE.md @import block and the
# merged settings.json entries too. Manifest-driven — needs no template checkout.
#   make -f agentloop.mk agentloop-uninstall [FORCE=1] [ARGS=--dry-run]
agentloop-uninstall:
	$(AGENTLOOP_PY) scripts/agentloop/adopt.py --uninstall --target "$(or $(TARGET),.)" $(if $(FORCE),--force) $(ARGS)

# Close the current delta cycle (human decision, after /verify's release approval): archive the
# filled deliverables to docs/archive/<date>-<slug>/, restore fresh scaffolds, reset gates/phase.
#   make cycle-close NAME=payment-refactor
cycle-close:
	$(AGENTLOOP_PY) scripts/agentloop/cycle.py --name "$(NAME)" $(ARGS)

# The deterministic orchestrator for /build. Reads .agentloop/{config,tasks}.yaml and state.md and
# deterministically drives frontier computation, max parallelism, worktree isolation, merge,
# quality-gate pipeline decision, and stopping. Does nothing and stops if gates.tasks is not
# approved. Only the human opens gates.build.
#   make build-loop ARGS=--dry-run   # check just the control flow without calling claude/git
build-loop:
	$(AGENTLOOP_PY) scripts/agentloop/build_loop.py $(ARGS)

# One-way-mirror tasks.yaml to GitHub Issues (human-facing visibility, opt-in).
# Acts only when github.enabled is true in .agentloop/config.yaml. Auto-skips if gh/remote is absent.
#   make issue-sync ARGS=--dry-run
issue-sync:
	$(AGENTLOOP_PY) scripts/agentloop/issue_sync.py $(ARGS)

# File cycle feedback (retrospective rows marked `Promote? = upstream`, drafted by /verify into
# .agentloop/feedback.yaml) as issues on the UPSTREAM template repository. Opt-in
# (github.feedback.enabled) and outward-facing: human-run only — never add it to permissions.allow.
#   make feedback ARGS=--dry-run [FILE=.agentloop/feedback.yaml]
feedback:
	$(AGENTLOOP_PY) scripts/agentloop/feedback.py $(if $(FILE),--file "$(FILE)") $(ARGS)

# Structured orchestration events (.agentloop/events.ndjson — the escalation log's machine-readable
# truth; state.md embeds only the generated view between its ESCALATION-VIEW markers).
#   make events ARGS=--render                                        # view + open escalations
#   make events ARGS='--add blocked --task T-003 --detail "..."'     # record one by hand (mode B)
#   make events ARGS='--resolve 3 --note "fixed by abc123"'          # close an open escalation
#   make events ARGS=--summary                                       # aggregates (per task / per step)
events:
	$(AGENTLOOP_PY) scripts/agentloop/events.py $(ARGS)

# Roll back (returning upstream). Resets every gate from the target phase onward to pending in a
# chain and updates current_phase and the roll-back log (the first-class operation for a human
# rewinding approval). Does not touch tasks; do impact analysis with dag.py --impacted.
#   make revise ARGS="--to design --reason 'rethink the auth method'"
revise:
	$(AGENTLOOP_PY) scripts/agentloop/revise.py $(ARGS)

# One-shot read-only diagnosis of the environment and the SSOT's consistency: binaries on PATH,
# config/state/tasks parse + gate-chain invariant, gate-guard hook registration, git branch vs
# state.md, leftover worktrees/lock, open escalations. Exit 1 if anything is FAIL-level.
# (doctor alone also pulls in jsonschema, to validate config/tasks against .agentloop/schema/;
#  the ordinary runtime stays pyyaml-only.)
#   make doctor
doctor:
	uv run --no-project --with pyyaml --with jsonschema python scripts/agentloop/doctor.py $(ARGS)

# Self-tests for the template's foundational tools (scripts/agentloop/). Unit tests of the
# deterministic orchestrator, DAG, gate hook, and the init/adopt/cycle helpers.
test-tools:
	uv run --no-project --with pyyaml,pytest,jsonschema python -m pytest -vv scripts/agentloop/
