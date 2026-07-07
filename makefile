# =========================================================
# Makefile
#
# Note:
# - This Makefile assumes macOS / Linux (bash, sh)
# - It does not work on Windows (cmd.exe / PowerShell)
#   → use WSL or Git Bash
#
# The AgentLoop foundational targets (init / cycle-close / build-loop /
# issue-sync / revise / test-tools) live in agentloop.mk — self-contained,
# so an existing repo can take just that file. This makefile keeps the
# stack-specific targets (install / setup / test / check / audit / clean).
# =========================================================

include agentloop.mk

.PHONY: install setup pre-commit pre-push check test audit clean

# Install tools (uv / pnpm binaries)
install:
	curl -LsSf https://astral.sh/uv/install.sh | sh
	curl -fsSL https://get.pnpm.io/install.sh | sh -

# Sync dependencies (including the dev group; generates/updates uv.lock)
# If using the frontend, also run `cd frontend && pnpm install`
setup:
	uv sync

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
# No --lf here: this target is the quality gate's `test` step (the DoD), and
# "last failed only" would let a fix regress the rest of the suite unseen.
test:
	uv run pytest -vv backend/ || test $$? -eq 5

# Dependency vulnerability audit (supply-chain check). Mandatory in /verify.
# Python: audit resolved dependencies with pip-audit. frontend: pnpm audit if package.json exists.
# Alternative: osv-scanner to scan the lockfiles in bulk (supports uv.lock + pnpm-lock.yaml).
audit:
	@req="$$(mktemp)"; \
	uv export --format requirements-txt --no-emit-project -o "$$req" && uvx pip-audit -r "$$req"; \
	status=$$?; rm -f "$$req"; exit $$status
	@if [ -f frontend/package.json ]; then cd frontend && pnpm audit; else echo "no frontend/package.json: skipping frontend audit"; fi

# Cleanup
clean:
	uv run pre-commit clean ; \
	uv cache clean ; \
	docker image prune -a -f ; \
	docker builder prune --all -f --keep-storage 3GB ; \
	find . \( -type d -name "__pycache__" -o -type d -name ".pytest_cache" -o -type f -name "*.pyc" \) -exec rm -rf {} +
