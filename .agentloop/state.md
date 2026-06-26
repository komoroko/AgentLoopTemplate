---
# .agentloop/state.md — このプロジェクトの「単一情報源（Single Source of Truth）」
# 全コマンド／エージェントはまずこのファイルを読み、作業後に更新する。
# gates の値は pending | approved のいずれか。前提ゲートが approved でない限り
# 次フェーズには進めない（CLAUDE.md「ゲート規則」を参照）。
project: "<プロダクト名を記入>"
branch: "<作業ブランチ名を記入>"  # 例: build/<product>。実装はこのブランチ上で行う
current_phase: brief          # brief | requirements | design | tasks | build | verify | done
gates:
  requirements: pending       # /req の成果を人が承認したら approved
  design: pending             # /design の技術選定を人が承認したら approved
  tasks: pending              # /tasks のタスク計画を人が承認したら approved
  build: pending              # /build 実装完了レビューを人が承認したら approved
  release: pending            # /verify のリリース可否を人が承認したら approved
updated_at: "<YYYY-MM-DD>"
---

# 進捗ボード

## フェーズ進行
- [ ] brief        — `docs/00-product-brief.md` を人が記入
- [ ] requirements — `/req`    → ゲート①
- [ ] design       — `/design` → ゲート②
- [ ] tasks        — `/tasks`  → ゲート③
- [ ] build        — `/build`  → ゲート④
- [ ] verify       — `/verify` → ゲート⑤

## タスク表（依存グラフ）
各タスクの真実はここ。タスクは依存関係で結ばれた **DAG** を成す。

- `種別`: **基盤**（多数が依存する共通土台）/ **並列**（基盤が済めば独立同時進行できる葉）/ **統合**（複数の完了後に合流）
- `依存(blockedBy)`: 先に done が必要なタスクID（DAG の辺）
- `被依存(fan-out)`: このタスクを待っているタスク数（大きいほど早く消化すると並列度が上がる）
- `status`: `todo` / `in_progress` / `blocked`（人の介入要） / `needs-revision`（要件/設計の再検討要） / `done`（テスト green 済み）

| ID    | タイトル | 種別 | 依存(blockedBy) | 被依存(fan-out) | status | テスト | 備考 |
|-------|----------|------|-----------------|-----------------|--------|--------|------|
| _（/tasks 実行時に追記される）_ |

## 実行プラン（依存チェーン）
DAG から導出した消化順。`/tasks` で初期構築し、**`/build` で1タスク完了するたびに再導出**する。

- **実行レイヤ**（トポロジカル順。同一レイヤ内は並列可能）:
  - L0: _（依存なし。多くは基盤タスク）_
  - L1: _（L0 完了で着手可能）_
  - L2: …
- **クリティカルパス**（最長チェーン＝全体所要を決める経路。最優先で詰める）:
  - _（例: T-001 → T-004 → T-007）_
- **現在の実行可能フロンティア**（今すぐ着手できる todo）:
  - _（/build が毎周更新）_

## 先回り作業ログ（暫定・破棄前提）
承認待ち中に進めた「結果非依存の先回り作業」を記録する。人が破棄/採用を判断する材料。
gate を `approved` にする根拠にはしない。

| 日付 | 待っていたゲート | 内容 | 成果物/場所 | 採否(人) |
|------|------------------|------|-------------|----------|
| _（随時追記）_ |

## エスカレーション・ログ
`blocked` / `needs-revision` が発生したらここに1行追記し、人の判断を仰ぐ。

| 日付 | タスクID | 種別 | 内容 | 解決 |
|------|----------|------|------|------|
| _（随時追記）_ |
