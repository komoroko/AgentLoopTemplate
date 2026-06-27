# AgentLoopTemplate

**Human on the Loop** で開発を進めるための Claude Code テンプレート。
コーディングエージェントが要件定義〜テストまでの作業・成果物作成・自己テストを担い、
**人間は各フェーズ境界の「ゲート」で承認・判断するだけ**を担う。

## コンセプト

```
brief → requirements → design → tasks → build → verify → done
        (/req)        (/design) (/tasks) (/build) (/verify)
          ▲ゲート①      ▲ゲート②   ▲ゲート③  ▲ゲート④   ▲ゲート⑤
```

各ゲートは人だけが開ける。前提ゲートが未承認なら次フェーズには進めない。

## セットアップ

前提: WSL / Linux / macOS と `make`（Windows ネイティブ不可）。

1. **このテンプレートをコピー**して新しいプロダクトのリポジトリにする。
2. `git init` し、作業ブランチを作る（例: `git switch -c build/<product>`）。実装は main 直ではなく作業ブランチで行う。
3. ツール導入と依存同期:
   ```bash
   make install   # uv / pnpm のバイナリを導入
   make setup     # uv sync（dev 依存を同期、uv.lock を生成）
   # フロントを使う場合: cd frontend && pnpm install
   ```
4. 動作確認: `make check`（lint/format/type）・`make test`（pytest）・`make test-tools`（`scripts/agentloop/` の確定オーケストレータ自己テスト）。
5. プロジェクト名を記入: `pyproject.toml` の `name`（初期値 `project-name`）と `.agentloop/state.md` の `project`・`branch`。

## 使い方

1. `docs/00-product-brief.md` に「何を作りたいか」を数行書く（人が書く唯一のシード）。
2. 以下を順に実行する。各コマンドの最後に人の承認を求めて止まる。

   | 手順 | コマンド | 何が起きるか | あなた（人）の役割 |
   |------|----------|--------------|--------------------|
   | 要件 | `/req`    | 壁打ちで要件を構造化 | ① 要件を凍結 |
   | 設計 | `/design` | 実装方針＋技術選定の選択肢提示 | ② 技術選定を決定・承認 |
   | 分解 | `/tasks`  | テスト方針付きタスク票を生成 | ③ タスク計画を承認 |
   | 実装 | `/build`  | loop で自律実装（test green 条件） | ④ 実装完了をレビュー承認 |
   | 検証 | `/verify` | 機能＋非機能テストを実行 | ⑤ リリース可否を判断 |

5. いつでも `/status` で現在フェーズ・ゲート承認状況・タスク進捗を確認できる。

> **承認待ち中も止まらない**: ゲート到達時に通知が飛び、承認を待つ間もエージェントは
> 承認結果に依存しない作業（環境構築・調査・テストハーネス整備など）を先回りで進める。
> 承認結果を先取りする作業はしないため、ゲートの厳密さは保たれる。先回り分は暫定・破棄前提で
> `.agentloop/state.md` の「先回り作業ログ」に記録され、人が採否を判断できる。

### 実装フェーズを自律で回す

実装ループには2つのモードがある。挙動（DoD・並列/マージ規則）は同一。以下は要点で、運用の正典は `.claude/commands/build.md`（手順）と `CLAUDE.md`（規約）:

**A. 確定実行（推奨）— `make build-loop`**
スケジューリングをコードで確定駆動するオーケストレータ（`scripts/agentloop/build_loop.py`）。**どのタスクを・何並列で・どの順にマージし・いつ止めるか**を `.agentloop/config.yaml` と `tasks.yaml` から確定的に決め、LLM 裁量に依存しない。

```
make build-loop                  # 実行
make build-loop ARGS=--dry-run   # claude/git を呼ばず制御フローだけ確認
```

**B. 対話ループ — `/loop /build`**
オーケストレータを使わず会話でループを回す代替。

- 各タスクは **品質ゲートを全て通って**初めて完了扱い: 自動テスト green → `/simplify`（整理）→ `/code-review`（バグ修正）→ `make check`（lint/format/typecheck をエラーが消えるまで修正）。
- **並列タスクは隔離実行**: 独立した葉タスクは `git worktree` で各自のブランチ・作業ディレクトリに分離して **最大3並列**（`config.yaml` の `max_parallel`）で実装し、完了後に id 昇順で作業ブランチへ順次マージする。基盤タスクは作業ブランチ上で先に確定する。
- 解決不能なタスクは `blocked`、上流に不備があれば `needs-revision` として **人にエスカレーション**し、ループが止まる。
- **確定化の境界**: 制御フロー・並列・マージ・ゲート判定・停止はコードで確定。各タスクの実装コード内容のみ LLM 由来で非確定で、「ゲートを通るまで retry、駄目なら blocked」で吸収する。**`gates.build` はオーケストレータも触らない**（ゲートは人だけが開ける）。

> **前提スタック**: 同梱の `makefile` で `make test`（pytest）・`make check`（ruff/format/mypy/tsc を一括）を使う。`make check` は `make pre-commit`（commit ステージ）と `make pre-push`（format/mypy/tsc）を束ねたもの。`make` の無いプロジェクトにコピーした場合は、各自のテスト/チェックコマンドに読み替える。

### セキュリティ検査

3層で担保する: **gitleaks**（pre-commit でシークレットのコミットを機構的に防止。誤検知は `.gitleaksignore` で除外）／実装完了時に **`/security-review`** 必須／`/verify` で **`/security-review` + `make audit`**（依存の脆弱性監査）必須。

## 構成

| パス | 役割 |
|------|------|
| `.agentloop/state.md` | フェーズ・ゲート・ログの SSOT |
| `.agentloop/tasks.yaml` | タスクグラフ(DAG)の機械可読 SSOT |
| `.agentloop/config.yaml` | 確定実行のノブ源（並列・retry・worktree・ゲート強制） |
| `scripts/agentloop/` | 確定オーケストレーション（`dag.py`／`build_loop.py`／`gate_guard.py`）。プロダクト用は `scripts/` 直下 |
| `CLAUDE.md` | エージェント運用規約・ゲート規則 |
| `.claude/commands/` | 各工程の入口（`/req` 〜 `/status`） |
| `.claude/agents/` | 専門サブエージェント（要件/設計/実装） |
| `docs/` | 工程成果物（要件・設計・ADR・タスク票・テスト計画） |

## 活用している Claude Code 機能

- **plan mode + ExitPlanMode** — 思考フェーズの承認ゲート
- **AskUserQuestion** — 技術選定など人の意思決定
- **/loop** — 実装タスクの自律消化（対話モード）
- **確定オーケストレータ（`make build-loop`）** — スケジューリング・並列・マージ・ゲート判定をコードで確定駆動
- **PreToolUse フック（`gate_guard.py`）** — 前提ゲート未承認時の成果物編集を機構的に deny
- **git worktree** — 並列タスクの隔離実行
- **subagent** — 工程ごとの専門化・コンテキスト分離
- **slash command** — 各工程の定型化
- **/schedule（任意）** — 長時間ループの定期進捗チェック
