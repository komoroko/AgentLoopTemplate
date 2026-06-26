# AgentLoopTemplate — エージェント運用規約

このリポジトリは **Human on the Loop** で開発を進めるためのテンプレートである。
コーディングエージェントが各工程の作業・成果物作成・自己テストまでを担い、
**人間は各フェーズ境界の「ゲート」でレビューし承認・判断するだけ**を担う。

## 開発ライフサイクル

```
brief → requirements → design → tasks → build → verify → done
        (/req)        (/design) (/tasks) (/build) (/verify)
          ▲ゲート①      ▲ゲート②   ▲ゲート③  ▲ゲート④   ▲ゲート⑤
```

| フェーズ | コマンド | 成果物 | ゲート（人の承認内容） |
|----------|----------|--------|------------------------|
| requirements | `/req`    | `docs/10-requirements.md` | ① 要件凍結 |
| design       | `/design` | `docs/20-design.md` + `docs/decisions/ADR-*.md` | ② 技術選定 |
| tasks        | `/tasks`  | `docs/tasks/T-*.md` | ③ タスク計画 |
| build        | `/build`  | 実装コード + テスト | ④ 実装完了レビュー |
| verify       | `/verify` | `docs/test/test-plan.md` 実行結果 | ⑤ リリース可否 |

進捗・全タスクは `/status` で確認できる。

## 単一情報源（SSOT）

真実は2つのファイルに分かれる。役割が違うので混同しない:

- **`.agentloop/state.md`** — フェーズ・各ゲートの承認状況・各種ログ（先回り/エスカレーション）の真実。**作業開始時は必ず読む**。作業後に更新する（フェーズ進行、`updated_at`）。フロントマターの `gates.<name>` は `pending` | `approved`。**人の承認以外でこれを `approved` にしてはならない。**
- **`.agentloop/tasks.yaml`** — タスクグラフ(DAG)の**機械可読な真実**。`/tasks` が生成し、`/build`（`scripts/agentloop/build_loop.py`）と `/status`（`scripts/agentloop/dag.py`）が読む。各タスクは `id`/`title`/`kind`/`blockedBy`/`status`/`test`。fan-out・フロンティア・実行レイヤ・クリティカルパスは `blockedBy` から導出するので保存しない（drift 防止）。state.md のタスク表は `dag.py --render` の人間向けビュー。
- **`.agentloop/config.yaml`** — 確定実行のノブ源（並列数・retry・worktree・ゲート強制）。`build_loop.py`/`gate_guard.py` が読む。

## ゲート規則（厳守）

1. **前提ゲート未承認なら次フェーズの作業をしない。** 各コマンドは冒頭で前提ゲートを確認する:
   - `/design` は `gates.requirements == approved` を要求
   - `/tasks` は `gates.design == approved` を要求
   - `/build` は `gates.tasks == approved` を要求
   - `/verify` は `gates.build == approved` を要求
   未承認なら作業を止め、何が必要かを人に伝える。
2. **ゲートは人だけが開ける。** エージェントは成果物を提示するところまで。承認の意思表示（plan mode の承認 or 明示的な「承認」）を受けて初めて `state.md` の該当 gate を `approved` にし、次へ進む。
3. **要件/設計に問題を見つけたら勝手に直さない。** 実装中などに上流の不備を見つけたら、該当タスクを `needs-revision` にし、エスカレーション・ログに記録して人に上げる。

**ゲートは2層で強制する**（規約だけに依存しない）:
- **規約層**: 上記のとおり各コマンドが冒頭で前提ゲートを確認する。
- **機構層**: `scripts/agentloop/gate_guard.py`（`.claude/settings.json` の PreToolUse フック）が、前提ゲート未承認のまま**次フェーズの成果物パス**（`docs/20-design.md`・`docs/decisions/**`→要件承認、`docs/tasks/**`→設計承認、`backend/**`・`frontend/**`・`scripts/**`（プロダクト用スクリプト）→タスク承認、`docs/test/**`→実装承認）を Write/Edit する操作をコードで **deny** する。ただし `scripts/agentloop/**`（テンプレート基盤ツール）はゲートに関わらず常に許可（フック自身の保守を妨げない）。さらに `/build` は `scripts/agentloop/build_loop.py` が冒頭で `gates.tasks==approved` をコード判定して二重化する。`.agentloop/config.yaml` の `gates.enforce_hook: false` で機構層を無効化できる。

## 承認待ち中のボトルネック最小化

人の承認待ちでエージェントを遊ばせない。ただし **ゲートの厳密さは絶対に崩さない**。

### 1) 待ち時間そのものを短くする
- ゲートに到達したら **`PushNotification` で人へ即通知**し、気づくまでのラグを削る。
- 人への確認は **1回の `AskUserQuestion` にまとめて** 聞く（往復を減らす。小出しにしない）。
- `/build` の実装ループは **依存のない独立タスクを並列**で進める（worktree 隔離・最大3並列。後述）。

### 2) 承認待ち中の「先回り作業」（結果非依存に限る）
ゲートが `pending` の間、**承認結果に依存しない作業だけ** を前倒してよい。判定基準:

- **やってよい**（結果非依存・低コスト・破棄しても痛くない）:
  リポジトリ雛形/ディレクトリ構成、開発環境・依存のセットアップ、CI/テストハーネスの骨組み、
  候補技術の**読み取り調査**、Lint/静的解析の整備、フィクスチャ等の足場。
- **やってはいけない**（承認結果を先取りする＝ゲートの意味を壊す）:
  保留中の決定を前提にした成果物（例: 要件未承認なのに設計本体を書く／技術選定を確定する／
  設計未承認なのにタスクを確定する）。これらは人の承認後に行う。

先回り作業は **暫定・破棄前提**。`.agentloop/state.md` の「先回り作業ログ」に記録し、人が破棄・採用を判断できるようにする。
**先回り作業を理由に gate を `approved` にしてはならない。**

## タスク依存グラフと最適消化

タスクはフラットなリストではなく **依存グラフ(DAG)** として扱う。

- 各タスクは種別を持つ: **基盤**（多数が依存する共通土台）/ **並列**（独立同時進行できる葉）/ **統合**（複数の合流）。
- グラフから **実行レイヤ**（同一レイヤは並列可能）と **クリティカルパス**（全体所要を決める最長経路）を導出する。
- **最適消化の方針**: 実行可能フロンティアの中で、①基盤・高 fan-out（被依存が多い）→ ②クリティカルパス上 → ③その他、の順に優先。独立タスクは並列に流す。
- **隔離実行（worktree）**: 基盤・高 fan-out タスクは **work ブランチ上で直列**に確定する。独立な葉タスクは **`implementer` を `isolation: "worktree"` で起動**し、各自の worktree（別ブランチ・別ディレクトリ）で実装〜品質ゲートまで隔離して進める（**最大3並列**）。`git subtree` は使わない（外部リポジトリ取り込み用で並行作業の分離には不適）。
- **合流（join）**: 葉タスクは done になった順に work ブランチへ順次マージする。コンフリクトはマージ点で解消し、マージ完了が **統合タスクのフロンティア解放トリガ** になる。
- **チェーンは動的**: `/tasks` で事前に組み立て、`/build` で **1タスク完了するたびに組み直す**。実装中に新たな依存・分割が判明したら DAG を更新して再導出する。
- **確定駆動**: 上記のフロンティア計算・消化順・並列（最大3）・マージ・停止は `scripts/agentloop/build_loop.py`（`make build-loop`）がコードで確定的に回す。導出ロジックは `scripts/agentloop/dag.py` に一本化（`/status` も共用）。LLM 裁量に委ねない。
- 真実は常に **`.agentloop/tasks.yaml`**（グラフの機械可読 SSOT）。state.md のタスク表/実行プランは `dag.py --render` の人間向けビュー。

## 行動原則

- **既存実装の再利用を最優先**。新規コードを書く前に、既存の関数・ユーティリティ・パターンを探す。
- **品質ゲートを通って初めて前進**。実装タスクは「単体/結合テスト green **かつ** `/simplify`・`/code-review` 通過 **かつ** `make check` クリーン」を満たして初めて `done`。いずれか未達のまま `done` にしない。
- **小さく確実に**。1コミット=1関心事。破壊的・外向きの操作は承認を得てから。
- **コンテキスト分離**。要件/設計/実装はそれぞれ専用サブエージェント（`.claude/agents/`）に委譲し、メインの文脈を汚さない。コードレビューは `/code-review`、整理は `/simplify` スキルに一本化する。
- 成果物ドキュメントは日本語で書く。

## 品質チェックコマンド（スタック前提と読み替え）

このテンプレートは `makefile` を同梱し、実装フェーズは以下に寄せる:

- `make test` — テスト実行（`pytest backend/`）
- `make pre-commit` — commit ステージのフック（ruff lint / eslint 等）
- `make pre-push` — pre-push ステージのフック（ruff-format / prettier / mypy / tsc）
- **`make check`** — 上記 pre-commit + pre-push を**まとめて実行**（lint / format / type-check の全部）。品質ゲートではこれを使う。

> `pre-commit run --all-files` は commit ステージのフックしか走らない（format・mypy・tsc は `stages: [pre-push]`）。型チェックまで含めるため、ゲートでは `make check` を使う。

`make` の無いプロジェクトにコピーした場合は、これらを当該プロジェクトのテスト/チェックコマンドに読み替えること（ゲートの考え方は不変）。

## セキュリティゲート

3層でセキュリティを担保する:

- **コミット段階（機構）**: `.pre-commit-config.yaml` の **gitleaks** が秘匿情報のコミットを機構的にブロック（`make pre-commit`／`make check` に内包）。誤検知は `.gitleaksignore` で除外する。
- **実装完了時（ゲート④前）**: `/build` が **`/security-review`** を必須実行し、コードの脆弱性を解消してから人へ承認を仰ぐ。
- **テスト工程（`/verify`）**: **`/security-review`** と **`make audit`**（依存の脆弱性監査。Python=pip-audit / フロント=pnpm audit。代替: `osv-scanner` で lockfile 一括走査）を必須実行し、結果を `docs/test/test-plan.md` に記録する。

## ブランチ / コミット規約

- 実装は **作業ブランチ上**で行う（main 直は避ける）。ブランチ名は `.agentloop/state.md` の `branch` に記録する。
- **並列葉タスクは worktree 上の派生ブランチ**（例: `<branch>/T-NNN`）で隔離実装し、done 時に work ブランチへマージする。worktree は変更が無ければ自動クリーンアップされる。
- `/build` の per-task ローカルコミットは **`T-NNN: <要約>`** 形式。**1コミット = 1タスク**。
- **`/build` の承認 = そのループ内のローカルコミットは承認済み作業の一部**。逐一の確認は不要。
- ただし **push / PR 作成 / main へのマージは外向き操作**。これらは別途、人の承認を得てから行う（勝手に push・マージしない）。

## ディレクトリ

- `.agentloop/state.md` — フェーズ・ゲート・ログの SSOT
- `.agentloop/tasks.yaml` — タスクグラフ(DAG)の機械可読 SSOT
- `.agentloop/config.yaml` — 確定実行のノブ源（並列・retry・worktree・ゲート強制）
- `scripts/agentloop/` — 確定オーケストレーション（`dag.py` 導出 / `build_loop.py` 実装ループ / `gate_guard.py` ゲートフック）。**プロダクト用スクリプトは `scripts/` 直下に置き、基盤ツールと混在させない**
- `docs/` — 工程成果物（要件・設計・ADR・タスク票・テスト計画）
- `.claude/commands/` — 各工程の入口（slash command）
- `.claude/agents/` — 専門サブエージェント
