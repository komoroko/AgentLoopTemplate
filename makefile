# =========================================================
# Makefile
#
# 注意:
# - 本 Makefile は macOS / Linux (bash, sh) を前提としています
# - Windows (cmd.exe / PowerShell) では動作しません
#   → WSL または Git Bash を利用してください
# =========================================================

.PHONY: install setup pre-commit pre-push check test test-tools audit build-loop issue-sync revise clean

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
	@req="$$(mktemp)"; \
	uv export --format requirements-txt --no-emit-project -o "$$req" && uvx pip-audit -r "$$req"; \
	status=$$?; rm -f "$$req"; exit $$status
	@if [ -f frontend/package.json ]; then cd frontend && pnpm audit; else echo "frontend/package.json なし: フロント監査はスキップ"; fi

# /build の確定的オーケストレータ。.agentloop/{config,tasks}.yaml と state.md を読み、
# フロンティア計算・最大並列・worktree 隔離・マージ・品質ゲート判定・停止を確定駆動する。
# gates.tasks が approved でなければ何もせず停止する。gates.build は人だけが開ける。
# 制御フローだけ確認したい場合は --dry-run（claude/git を呼ばない）:
#   make build-loop ARGS=--dry-run
build-loop:
	uv run python scripts/agentloop/build_loop.py $(ARGS)

# tasks.yaml を GitHub Issues へ一方向ミラー（人向け可視化・opt-in）。
# .agentloop/config.yaml の github.enabled が true のときだけ実働。gh/remote 不在なら自動スキップ。
# 予定だけ見たいときは --dry-run（gh を呼ばない）:
#   make issue-sync ARGS=--dry-run
issue-sync:
	uv run python scripts/agentloop/issue_sync.py $(ARGS)

# 差し戻し（上流への後戻り）。戻し先 phase 以降のゲートを連鎖して pending に戻し、current_phase と
# 差し戻しログを更新する（人が承認を巻き戻す一級操作）。タスクは触らず、影響分析は dag.py --impacted で。
#   make revise ARGS="--to design --reason '認証方式の見直し'"
#   make revise ARGS="--to requirements --dry-run"
revise:
	uv run python scripts/agentloop/revise.py $(ARGS)

# クリーンアップ
clean:
	uv run pre-commit clean ; \
	uv cache clean ; \
	docker image prune -a -f ; \
	docker builder prune --all -f --keep-storage 3GB ; \
	find . \( -type d -name "__pycache__" -o -type d -name ".pytest_cache" -o -type f -name "*.pyc" \) -exec rm -rf {} +
