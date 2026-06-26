# =========================================================
# Makefile
#
# 注意:
# - 本 Makefile は macOS / Linux (bash, sh) を前提としています
# - Windows (cmd.exe / PowerShell) では動作しません
#   → WSL または Git Bash を利用してください
# =========================================================

.PHONY: install setup pre-commit pre-push check test test-tools audit build-loop clean link-claude

# ツール導入（uv / pnpm のバイナリ）
install:
	curl -LsSf https://astral.sh/uv/install.sh | sh
	curl -fsSL https://get.pnpm.io/install.sh | sh -

# 依存同期（dev 群を含む。uv.lock を生成・更新）
# フロントエンドを使う場合は別途 `cd frontend && pnpm install`
setup:
	uv sync

# commit ステージのフック（ruff lint / eslint / 各種チェック）
pre-commit:
	uv run pre-commit run --all-files

# pre-push ステージのフック（ruff-format / prettier / mypy / tsc）
pre-push:
	uv run pre-commit run --all-files --hook-stage pre-push

# 実装の品質ゲート: commit + pre-push の全フック（lint / format / type-check）をまとめて実行
check: pre-commit pre-push

# pytest の実行
test:
	uv run pytest -vv --lf backend/

# テンプレート基盤ツール（scripts/agentloop/）の自己テスト。確定オーケストレータ・DAG・ゲートフックの単体テスト。
test-tools:
	uv run pytest -vv scripts/agentloop/

# 依存の脆弱性監査（サプライチェーン検査）。/verify で必須実行する。
# Python: 解決済み依存を pip-audit で監査。frontend: package.json があれば pnpm audit。
# 代替: lockfile を一括走査する osv-scanner（uv.lock + pnpm-lock.yaml 対応）でも可。
audit:
	uv export --format requirements-txt --no-emit-project | uvx pip-audit -r -
	@if [ -f frontend/package.json ]; then cd frontend && pnpm audit; else echo "frontend/package.json なし: フロント監査はスキップ"; fi

# /build の確定的オーケストレータ。.agentloop/{config,tasks}.yaml と state.md を読み、
# フロンティア計算・最大並列・worktree 隔離・マージ・品質ゲート判定・停止を確定駆動する。
# gates.tasks が approved でなければ何もせず停止する。gates.build は人だけが開ける。
# 制御フローだけ確認したい場合は --dry-run（claude/git を呼ばない）:
#   make build-loop ARGS=--dry-run
build-loop:
	uv run python scripts/agentloop/build_loop.py $(ARGS)

# Create symbolic links for "claude code" files from existing "github copilot" files.
# This scans for files whose names contain both "github" and "copilot", and creates
# a symlink in the same location with 'github'->'claude' and 'copilot'->'code' replacements.
# Example: path/to/github-copilot-config.json -> path/to/claude-code-config.json

link-claude:
	@set -e; \
	# root CLAUDE.md: prefer .github/copilot-instructions.md, then root copilot-instructions.md,
	# then .github/AGENTS.md, then root AGENTS.md
	if [ -f .github/copilot-instructions.md ]; then \
		ln -sf .github/copilot-instructions.md CLAUDE.md; \
	elif [ -f copilot-instructions.md ]; then \
		ln -sf copilot-instructions.md CLAUDE.md; \
	elif [ -f .github/AGENTS.md ]; then \
		ln -sf .github/AGENTS.md CLAUDE.md; \
	elif [ -f AGENTS.md ]; then \
		ln -sf AGENTS.md CLAUDE.md; \
	fi; \
	# ensure target dirs exist once
	mkdir -p ".claude/commands" ".claude/skills" ".claude/agents" ".claude/memory"; \
	# prompts: mirror .github/prompts tree, and map any *.prompt.md files preserving path
	if [ -d .github/prompts ]; then \
		find .github/prompts -mindepth 1 -print0 | \
			while IFS= read -r -d '' src; do \
				rel=$${src#.github/prompts/}; \
				dest=.claude/commands/$$rel; \
				mkdir -p "$$(dirname "$$dest")"; \
				ln -sfn "$$src" "$$dest"; \
			done; \
	fi; \
	find . -type f -name '*.prompt.md' -print0 | \
		while IFS= read -r -d '' src; do \
			rel=$${src#./}; \
			rel=$${rel%.prompt.md}.md; \
			dest=.claude/commands/$$rel; \
			mkdir -p "$$(dirname "$$dest")"; \
			ln -sfn "$$src" "$$dest"; \
		done; \

	# agents: mirror .github/agents tree, and map any *.agent.md files preserving path
	if [ -d .github/agents ]; then \
		find .github/agents -mindepth 1 -print0 | \
			while IFS= read -r -d '' src; do \
				rel=$${src#.github/agents/}; \
				dest=.claude/agents/$$rel; \
				mkdir -p "$$(dirname "$$dest")"; \
				ln -sfn "$$src" "$$dest"; \
			done; \
	fi; \
	find . -type f -name '*.agent.md' -print0 | \
		while IFS= read -r -d '' src; do \
			rel=$${src#./}; \
			rel=$${rel%.agent.md}.md; \
			dest=.claude/agents/$$rel; \
			mkdir -p "$$(dirname "$$dest")"; \
			ln -sfn "$$src" "$$dest"; \
		done; \
	# memory: mirror .github/memory into .claude/memory recursively
	if [ -d .github/memory ]; then \
		find .github/memory -mindepth 1 -print0 | \
			while IFS= read -r -d '' src; do \
				rel=$${src#.github/memory/}; \
				dest=.claude/memory/$$rel; \
				mkdir -p "$$(dirname "$$dest")"; \
				ln -sfn "$$src" "$$dest"; \
			done; \
	fi; \
	# SKILL.md files: place under .claude/skills/<parent>/SKILL.md (mirror parent dir)
	find . -type f -name 'SKILL.md' -print0 | \
		while IFS= read -r -d '' src; do \
			dir=$$(dirname "$$src"); \
			rel=$${dir#./}; \
			dest=.claude/skills/$$rel/SKILL.md; \
			mkdir -p "$$(dirname "$$dest")"; \
			ln -sfn "$$src" "$$dest"; \
		done; \

	# directory-scoped instructions -> <dir>/CLAUDE.md
	find . -type f -name '*.instructions.md' -print0 | \
		while IFS= read -r -d '' src; do \
			dir=$$(dirname "$$src"); \
			ln -sfn "$$src" "$$dir/CLAUDE.md"; \
		done

# クリーンアップ
clean:
	uv run pre-commit clean ; \
	uv cache clean ; \
	docker image prune -a -f ; \
	docker builder prune --all -f --keep-storage 3GB ; \
	find . \( -type d -name "__pycache__" -o -type d -name ".pytest_cache" -o -type f -name "*.pyc" \) -exec rm -rf {} +
