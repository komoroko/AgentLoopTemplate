# =========================================================
# agentloop.mk — AgentLoop's foundational targets (self-contained)
#
# Every target launches via `uv run --no-project --with pyyaml python -m`, so it
# needs only the uv binary — no project environment (`make setup`) and no
# particular language/stack. PYTHONPATH=src points at the package source in this
# repo; installed products will call the `agentloop` CLI instead (in progress —
# this file is slated for removal once the CLI covers every verb).
# =========================================================

AGENTLOOP := PYTHONPATH=src uv run --no-project --with pyyaml python -m agentloop.cli

.PHONY: init cycle-close build-loop issue-sync approve revise events doctor pr-draft template-lint test-tools ui

# Seed this repository with AgentLoop state (idempotent; brownfield auto-detected). Adoption
# into an existing repo is the SAME command now — `agentloop init` writes only state, from the
# installed package's payload. Interactive alternative: `agentloop start` (the setup wizard).
#   make init NAME=myproduct [BRANCH=build/myproduct] [FROM=<agentloop source url>]
init:
	$(AGENTLOOP) init --name "$(NAME)" $(if $(BRANCH),--branch "$(BRANCH)") $(if $(FROM),--source "$(FROM)")

# Close the current delta cycle (human decision, after /verify's release approval): archive the
# filled deliverables to docs/archive/<date>-<slug>/, restore fresh scaffolds, reset gates/phase.
#   make cycle-close NAME=payment-refactor
cycle-close:
	$(AGENTLOOP) cycle-close --name "$(NAME)" $(ARGS)

# The deterministic orchestrator for /build. Reads .agentloop/{config,tasks}.yaml and state.md and
# deterministically drives frontier computation, max parallelism, worktree isolation, merge,
# quality-gate pipeline decision, and stopping. Does nothing and stops if gates.tasks is not
# approved. Only the human opens gates.build.
#   make build-loop ARGS=--dry-run   # check just the control flow without calling claude/git
build-loop:
	$(AGENTLOOP) build $(ARGS)

# One-way-mirror tasks.yaml to GitHub Issues (human-facing visibility, opt-in).
# Acts only when github.enabled is true in .agentloop/config.yaml. Auto-skips if gh/remote is absent.
#   make issue-sync ARGS=--dry-run
issue-sync:
	$(AGENTLOOP) issue-sync $(ARGS)

# Structured orchestration events (.agentloop/events.ndjson — the escalation log's machine-readable
# truth; state.md embeds only the generated view between its ESCALATION-VIEW markers).
#   make events ARGS=--render                                        # view + open escalations
#   make events ARGS='--add blocked --task T-003 --detail "..."'     # record one by hand (mode B)
#   make events ARGS='--resolve 3 --note "fixed by abc123"'          # close an open escalation
#   make events ARGS=--summary                                       # aggregates (per task / per step)
events:
	$(AGENTLOOP) events $(ARGS)

# Record a human gate approval (the first-class operation for opening a gate — the forward twin
# of revise). Stamps `gates.<GATE>: approved   # <date> [BY]`, advances current_phase, and appends
# the gate_approved event the commit-stage gate guard cross-checks. The agent may run this after
# an explicit human "approve" — but NEVER pre-authorize it (the permission prompt is the human's
# confirmation); direct state.md gate edits are denied by gate_guard.
#   make approve GATE=design [BY=alice]
approve:
	$(AGENTLOOP) approve "$(GATE)" $(if $(BY),--by "$(BY)")

# Roll back (returning upstream). Resets every gate from the target phase onward to pending in a
# chain and updates current_phase and the roll-back log (the first-class operation for a human
# rewinding approval). Does not touch tasks; do impact analysis with dag.py --impacted.
#   make revise ARGS="--to design --reason 'rethink the auth method'"
revise:
	$(AGENTLOOP) revise $(ARGS)

# One-shot read-only diagnosis of the environment and the SSOT's consistency: binaries on PATH,
# config/state/tasks parse + gate-chain invariant, guard_paths typos, task↔ticket parity,
# gate-guard hook registration, git branch vs state.md, leftover worktrees/leaf-branches/lock,
# open escalations + event-log size, security-review↔HEAD binding, JSON-Schema validation.
# Exit 1 if anything is FAIL-level.
# (doctor alone also pulls in jsonschema, to validate config/tasks against .agentloop/schema/;
#  the ordinary runtime stays pyyaml-only.)
#   make doctor
doctor:
	PYTHONPATH=src uv run --no-project --with pyyaml --with jsonschema python -m agentloop.cli doctor $(ARGS)

# Assemble a PR body from the SSOT (gates, tasks, requirement coverage, security-review binding,
# commit list) into .agentloop/pr-draft.md. Read-only and never calls gh — creating the PR stays
# a human action; the tool prints the `gh pr create --body-file` line to run after review.
#   make pr-draft [ARGS='--base develop' | ARGS=--stdout]
pr-draft:
	$(AGENTLOOP) pr-draft $(ARGS)

# Local browser dashboard over the SSOT: current phase/gates, the task DAG, open escalations, and
# the deterministically computed next command (the same "what next" logic /status describes, in code).
# Guidance-first and read-only by default for reads; a fixed whitelist of safe operations (gate-approval
# recording, doctor, events --resolve, revise, cycle-close) can also be run from the page — the client
# sends an action id, never a command string. Binds 127.0.0.1 with a per-start token. Ctrl+C to stop.
#   make ui                    # serve on 127.0.0.1:8765 and open the browser
#   make ui PORT=9000 ARGS=--no-open
#   make ui ARGS=--read-only   # disable the action endpoints (view only)
ui:
	$(AGENTLOOP) ui $(if $(PORT),--port $(PORT)) $(ARGS)

# Drift canaries across the hand-maintained template files: wrapper parity, capability-mapping
# set-equality, machine-read vocabulary echoes, README EN↔JA structure, VERSION↔CHANGELOG.
# Part of `make check`; exits 0 in a product repo (gates.template_mode false) — the canaries
# guard the template itself, not products built from it.
template-lint:
	$(AGENTLOOP) template-lint

# Self-tests for the template's foundational tools (src/agentloop/). Unit tests of the
# deterministic orchestrator, DAG, gate hook, and the init/adopt/cycle helpers.
test-tools:
	PYTHONPATH=src uv run --no-project --with pyyaml,pytest,jsonschema python -m pytest -vv tests/
