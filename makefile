# =========================================================
# Makefile
#
# Note:
# - This Makefile assumes macOS / Linux (bash, sh)
# - It does not work on Windows (cmd.exe / PowerShell)
#   → use WSL or Git Bash
# =========================================================

.PHONY: install setup init pre-commit pre-push check test test-tools audit build-loop issue-sync revise clean

# Install tools (uv / pnpm binaries)
install:
	curl -LsSf https://astral.sh/uv/install.sh | sh
	curl -fsSL https://get.pnpm.io/install.sh | sh -

# Sync dependencies (including the dev group; generates/updates uv.lock)
# If using the frontend, also run `cd frontend && pnpm install`
setup:
	uv sync

# Turn the copied template into a product (idempotent): fills the pyproject / state.md placeholders,
# creates the work branch, and flips gates.template_mode off so the gate guard goes live.
#   make init NAME=myproduct [BRANCH=build/myproduct]
init:
	uv run python scripts/agentloop/init.py --name "$(NAME)" $(if $(BRANCH),--branch "$(BRANCH)")

# Commit-stage hooks (ruff lint / eslint / various checks)
pre-commit:
	uv run pre-commit run --all-files

# Pre-push-stage hooks (ruff-format / prettier / mypy / tsc)
pre-push:
	uv run pre-commit run --all-files --hook-stage pre-push

# Implementation quality gate: run all commit + pre-push hooks (lint / format / type-check) together
check: pre-commit pre-push

# Run pytest. Exit code 5 = "no tests collected" is tolerated so a freshly
# copied template (empty backend/) passes; the same tolerance is used in CI.
test:
	uv run pytest -vv --lf backend/ || test $$? -eq 5

# Self-tests for the template's foundational tools (scripts/agentloop/). Unit tests of the deterministic orchestrator, DAG, and gate hook.
test-tools:
	uv run pytest -vv scripts/agentloop/

# Dependency vulnerability audit (supply-chain check). Mandatory in /verify.
# Python: audit resolved dependencies with pip-audit. frontend: pnpm audit if package.json exists.
# Alternative: osv-scanner to scan the lockfiles in bulk (supports uv.lock + pnpm-lock.yaml).
audit:
	@req="$$(mktemp)"; \
	uv export --format requirements-txt --no-emit-project -o "$$req" && uvx pip-audit -r "$$req"; \
	status=$$?; rm -f "$$req"; exit $$status
	@if [ -f frontend/package.json ]; then cd frontend && pnpm audit; else echo "no frontend/package.json: skipping frontend audit"; fi

# The deterministic orchestrator for /build. Reads .agentloop/{config,tasks}.yaml and state.md and
# deterministically drives frontier computation, max parallelism, worktree isolation, merge, quality-gate decision, and stopping.
# Does nothing and stops if gates.tasks is not approved. Only the human opens gates.build.
# To check just the control flow, use --dry-run (does not call claude/git):
#   make build-loop ARGS=--dry-run
build-loop:
	uv run python scripts/agentloop/build_loop.py $(ARGS)

# One-way-mirror tasks.yaml to GitHub Issues (human-facing visibility, opt-in).
# Acts only when github.enabled is true in .agentloop/config.yaml. Auto-skips if gh/remote is absent.
# To see just the plan, use --dry-run (does not call gh):
#   make issue-sync ARGS=--dry-run
issue-sync:
	uv run python scripts/agentloop/issue_sync.py $(ARGS)

# Roll back (returning upstream). Resets every gate from the target phase onward to pending in a chain, and updates
# current_phase and the roll-back log (the first-class operation for a human rewinding approval). Does not touch tasks; do impact analysis with dag.py --impacted.
#   make revise ARGS="--to design --reason 'rethink the auth method'"
#   make revise ARGS="--to requirements --dry-run"
revise:
	uv run python scripts/agentloop/revise.py $(ARGS)

# Cleanup
clean:
	uv run pre-commit clean ; \
	uv cache clean ; \
	docker image prune -a -f ; \
	docker builder prune --all -f --keep-storage 3GB ; \
	find . \( -type d -name "__pycache__" -o -type d -name ".pytest_cache" -o -type f -name "*.pyc" \) -exec rm -rf {} +
