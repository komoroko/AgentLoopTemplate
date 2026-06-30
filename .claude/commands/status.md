---
description: 進捗ダッシュボード。現在フェーズ・ゲート承認状況・タスク進捗を一覧表示する。
---

# /status — 進捗ダッシュボード

`.agentloop/state.md`（phase/gates/ログ）と `.agentloop/tasks.yaml`（タスクグラフ）を読み、Human on the Loop の監視ビューとして以下を簡潔に表示する。**state は変更しない（読み取り専用）。**

1. **プロジェクト / 作業ブランチ**（`project`・`branch`）と **現在フェーズ**、次に実行すべきコマンド。
2. **ゲート状況**: requirements / design / tasks / build / release を `approved`/`pending` で一覧。
3. **タスク進捗**: `uv run python scripts/agentloop/dag.py --render` を実行し、その確定出力（件数・実行レイヤ・クリティカルパス・実行可能フロンティア）を表示する。加えて `blocked`・`needs-revision` のタスクは個別に列挙（人の介入が必要なため）。tasks.yaml が未生成（`/tasks` 前）ならスキップ。
   - **依存図**: `uv run python scripts/agentloop/dag.py --mermaid` を実行し、その Mermaid（`graph TD`・status 色分け・クリティカルパス太枠）を併せて提示する。GitHub/VS Code/Markdown でそのまま全体像が描画される。
   - **整合性トレース**: 要件ドキュメント（`docs/10-requirements.md`）があれば `uv run python scripts/agentloop/dag.py --trace` を実行し、要件→設計→タスクの連結に欠落（未カバー要件・宙吊り参照）があれば強調する。非0なら「要対応」に含める。
4. **要対応**: 人の承認待ちのゲート、エスカレーション・ログの未解決項目があれば強調。`done` 到達済みなのに `docs/retrospective.md` 未記入や各ログの未回収（解決欄・採否欄の空白）が残っていれば促す。
5. **先回り作業**: 承認待ち中に進めた暫定作業（先回り作業ログ）があれば、採否未判断のものを示す。
6. **（GitHub 連携時のみ）** `.agentloop/config.yaml` の `github.enabled: true` なら、`make issue-sync` で Issues をこのダッシュボード（tasks.yaml）に一致させられる旨を1行案内する（Issues は SSOT ではなく一方向ミラー）。

最後に「今あなた（人）がすべきこと」を1〜2行で示す。
