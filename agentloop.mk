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

.PHONY: init cycle-close build-loop issue-sync revise test-tools

# Turn the copied template into a product (idempotent): fills the pyproject / state.md placeholders,
# snapshots the pristine docs scaffolds, creates the work branch, and flips gates.template_mode off
# so the gate guard goes live.
#   make init NAME=myproduct [BRANCH=build/myproduct]
init:
	$(AGENTLOOP_PY) scripts/agentloop/init.py --name "$(NAME)" $(if $(BRANCH),--branch "$(BRANCH)")

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

# Roll back (returning upstream). Resets every gate from the target phase onward to pending in a
# chain and updates current_phase and the roll-back log (the first-class operation for a human
# rewinding approval). Does not touch tasks; do impact analysis with dag.py --impacted.
#   make revise ARGS="--to design --reason 'rethink the auth method'"
revise:
	$(AGENTLOOP_PY) scripts/agentloop/revise.py $(ARGS)

# Self-tests for the template's foundational tools (scripts/agentloop/). Unit tests of the
# deterministic orchestrator, DAG, gate hook, and the init/adopt/cycle helpers.
test-tools:
	uv run --no-project --with pyyaml,pytest python -m pytest -vv scripts/agentloop/
