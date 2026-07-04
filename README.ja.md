# AgentLoopTemplate

[English](README.md) | **日本語**

**Human on the Loop** で開発を進めるための Claude Code テンプレート。
コーディングエージェントが要件定義〜テストまでの作業・成果物作成・自己テストを担い、
**人間は各フェーズ境界の「ゲート」で承認・判断するだけ**でよい。

## コンセプト

```mermaid
flowchart TD
    brief["brief<br/>(人が構想を記入)"]:::human
    req["/req<br/>要件定義"]:::agent
    g1{"①要件凍結"}:::gate
    design["/design<br/>設計"]:::agent
    g2{"②技術選定"}:::gate
    tasks["/tasks<br/>タスク分解"]:::agent
    g3{"③タスク計画"}:::gate
    build["/build<br/>実装ループ"]:::agent
    g4{"④実装完了"}:::gate
    verify["/verify<br/>検証"]:::agent
    g5{"⑤リリース可否"}:::gate
    done["done"]:::human

    subgraph TASKS["タスク群（複数・依存グラフ DAG）"]
        direction TD
        T1["基盤 T-001"]:::task
        T2["葉 T-002"]:::task
        T3["葉 T-003"]:::task
        Tn["葉 T-00n…"]:::task
        TI["統合 T-0xx"]:::task
        T1 --> T2
        T1 --> T3
        T1 --> Tn
        T2 --> TI
        T3 --> TI
        Tn --> TI
    end

    brief --> req --> g1 --> design --> g2 --> tasks
    tasks -->|生成| T1
    TI --> g3
    g3 -->|"並列消化（最大3）"| build
    build --> g4 --> verify --> g5 --> done

    req -. 上流へ /revise .- build
    design -. 上流へ /revise .- build
    design -. 上流へ /revise .- verify

    classDef agent fill:#cfe8ff,stroke:#3b82f6,color:#06325e;
    classDef gate fill:#ffe9c7,stroke:#f59e0b,color:#7a4a00;
    classDef human fill:#d7f5dd,stroke:#22a04b,color:#0b3d1d;
    classDef task fill:#eeeeff,stroke:#8888aa,color:#222255;
    linkStyle 18,19,20 stroke:#ee5544,color:#ee5544,stroke-width:1.5px;
```

凡例: 🟦 青=エージェントが実施するフェーズ ／ 🟧 橙=人が承認するゲート①〜⑤ ／ 🟩 緑=人の関与点（構想の記入・完了判断）／ 🟪 薄紫=タスク（**複数**・依存グラフ DAG。基盤→並列葉→統合）。**上から下へ前進**し（前提ゲート未承認なら次へ進めない）、`/tasks` がタスク群を生成→ゲート③承認→`/build` が並列消化（最大3）。赤い点線＝`/revise` による上流への差し戻し（build/verify から design/req へ。戻し先以降のゲートを連鎖して `pending` に戻す）。

各ゲートは人だけが開ける。承認の巻き戻し（`/revise`）も人の判断で行う。

## どこから始めるか

| あなたの状態 | 入口 |
|---|---|
| ゼロから新プロダクトを作る | 「セットアップ（新規リポジトリ / greenfield）」→「使い方」 |
| 進行中の既存リポジトリに導入する | 「既存リポジトリへの導入（brownfield）」→ `/onboard`（開始状態別の対応表の全体は `/onboard` 内） |
| 導入・初期化済みで、次の変更を始める | `docs/00-product-brief.md` に変更を書いて `/req`（前サイクルが未クローズなら先に `make cycle-close`） |
| リリース判断（ゲート⑤）が出た | `make cycle-close NAME=<slug>` — このサイクルの docs を退避し、次サイクル用にリセット |
| テンプレートのツール群を更新/撤去したい（adopt 済み） | `make -f agentloop.mk agentloop-upgrade` / `agentloop-uninstall` |
| 現在地が分からない・中断から再開する | `/status` — 次に打つコマンドまで表示される |

## 設計原則

本テンプレートは **それ自体が複数エージェントのオーケストレーション**であり、その仕組みは3つの設計軸に沿う。

- **Architecture** — 要件を満たす最もシンプルな構成: `build_loop.py` は**決定論的な DAG**（制御/デバッグ容易）、各フェーズは専用サブエージェントへ委譲し関心を分離。
- **Context** — 必要最小限に保つ: SSOT（`state.md` / `tasks.yaml`）が真実を保持、サブエージェントは必要分のみ読む、失敗は**ダンプせず要約**、肥大ログは自動ローテーション（`CLAUDE.md`「Context budget」参照）。
- **Tools** — サブエージェントの tool 付与は最小・用途限定、品質ゲートに**再試行上限**（`config.yaml`）、`summarize_failure()` が簡潔で要点のある失敗を返す。

## セットアップ（新規リポジトリ / greenfield）

前提: WSL / Linux / macOS と `make`（Windows ネイティブ不可）。

1. **このテンプレートをコピー**して新しいプロダクトのリポジトリにし、`git init` する。
2. ツール導入と依存同期:
   ```bash
   make install   # uv / pnpm のバイナリを導入（公式の curl|sh インストーラを実行。
                  # ロックダウン/オフライン環境では uv・pnpm を手動で導入する）
   make setup     # uv sync（dev 依存を同期、uv.lock を生成）
   # フロントを使う場合: cd frontend && pnpm install
   ```
3. **プロダクトとして初期化**（冪等）:
   ```bash
   make init NAME=<product>   # 必要なら BRANCH=build/<product>
   ```
   プレースホルダ（`pyproject.toml` の `name`、`.agentloop/state.md` の `project`/`branch`/`updated_at`）を埋め、作業ブランチを作成・切替し、`gates.template_mode` をオフにしてゲートガードを本稼働させる。実装は main 直ではなく作業ブランチで行う。
4. 動作確認: `make check`（lint/format/type）・`make test`（pytest。空のテンプレートでも成功する）・`make test-tools`（`scripts/agentloop/` の確定オーケストレータ自己テスト）。

## 既存リポジトリへの導入（brownfield）

進行中のリポジトリにはコピーで上書きするのではなく、このテンプレートの checkout から AgentLoop を**追加インストール**する（衝突検知つき・追記のみ）:

```bash
# テンプレートの checkout から実行。導入先に必要なのは uv バイナリだけ
make adopt TARGET=../myrepo NAME=myrepo TEST_CMD="npm test" CHECK_CMD="npm run lint"
# まず計画だけ確認: make adopt TARGET=../myrepo NAME=myrepo ARGS=--dry-run
```

何がどう入るか（冪等。再実行時は既存分をすべてスキップ）:

| 種別 | 対象 | 挙動 |
|------|------|------|
| copy | `.agentloop/`、`scripts/agentloop/`、`agentloop.mk`、`.claude/commands|agents`、docs スキャフォールド | **既存ファイルは絶対に上書きしない**（スキップして報告） |
| merge | `CLAUDE.md` | テンプレの規約は `.agentloop/CLAUDE.agentloop.md` に置かれ、既存 CLAUDE.md には `@`-import 行を1回だけ追記 |
| merge | `.claude/settings.json` | 不足している permissions / フックだけ追記。既存分は触らない |
| adapt | `.agentloop/config.yaml` | **`guard_paths` を docs 成果物のみに限定** — ゲート未承認でも既存コードの開発は止まらない。準備ができたらコードパス（例 `src/: tasks`）を追加。品質ゲートのコマンドは `TEST_CMD`/`CHECK_CMD` から設定 |
| manual | あなたの `makefile`、`.pre-commit-config.yaml` | 触らない — `include agentloop.mk` を1行追加（または `make -f agentloop.mk build-loop`）。gitleaks フック追加を推奨 |

導入したリポジトリでの流れ:

1. **`/onboard`** — 既存コードベースを読み取り専用で調査し、**永続ベースライン** `docs/05-current-state.md` を生成する: アーキテクチャ・モジュールの役割・再利用可能な資産・規約・既存ドキュメントへのリンク（移動・変換はせず現在地のまま）・実装状況（仕掛かり作業を含む）。既存の動作を要件や done タスクへ**逆生成はしない** — ゲートを開くのは常に人間で、トレーサビリティ（R-N）は各サイクルのデルタにだけ適用される。どんな初期状態からでも入れる（対応表の全体は `/onboard` 内）:
   - **ドキュメントが一切無い** — 調査はコード駆動なのでそのまま成立する。コードから読み取れない意図（誰のための何か・非目標）だけを `/onboard` が質問で回収し、brief に書き戻す。仕様書の逆生成はしない。
   - **承認済み相当の要件書・設計書が既にある** — `/req`・`/design` をその取り込みとして高速に走らせてゲートを開く。その承認こそが本機構への対応付けになる。
   - **実装が半分できている** — 最初のサイクルは**残作業のデルタ**だけを計画し、先頭の**吸収タスク**が既存の部分実装をテストで green に固定してから新しい作業を積む（`/tasks` のブラウンフィールド注記）。
2. **デルタサイクル** — `brief → /req → … → /verify` の1周は**1つの変更**を扱い、仕掛かりの残作業はデルタ要件として再開する（1周の回し方自体は後述の「使い方」と同じ）。リリース判断のあと、サイクルを閉じる:
   ```bash
   make cycle-close NAME=<slug>   # このサイクルの docs を docs/archive/<日付>-<slug>/ へ退避し、
                                  # 新しいスキャフォールドを復元、ゲート/フェーズを次サイクル用にリセット
   ```
   `docs/00-product-brief.md` と `docs/05-current-state.md` はサイクルをまたいで残る（ベースラインはアーカイブせず更新する）。サイクルを閉じるのはゲートを開くのと同じく人間の操作。
3. **アップグレード / アンインストール（いつでも）** — 導入時に `.agentloop/adopt-manifest.yaml`（テンプレートの出所・コミットと、インストールした全ファイルのハッシュ）が記録される。これを駆動源にした2つのコマンドが使える（adopt 専用。greenfield の `make init` はマニフェストを記録しない）。どちらもハッシュ検査つきで、**導入後にあなたが編集したファイルは絶対に上書き・削除されない**（スキップして列挙。`FORCE=1` で強制）。実行後は `git diff` でレビューしてコミットする:
   ```bash
   # 導入済みリポジトリの中で実行 — テンプレート所有のツール群（scripts/agentloop/、
   # .claude/commands|agents、agentloop.mk、取り込まれた規約）を更新する。FROM は git URL か
   # ローカルパス。省略時は導入時に記録された出所を再利用。REF はブランチ/タグ（SHA 不可）
   make -f agentloop.mk agentloop-upgrade FROM=https://github.com/you/AgentLoopTemplate.git

   # 導入の撤去: adopt が入れたものを pristine な範囲で除去。CLAUDE.md の @import ブロックと
   # settings.json へのマージ分も取り消す
   make -f agentloop.mk agentloop-uninstall ARGS=--dry-run
   ```
   アップグレードはリポジトリ所有の状態（`config.yaml`・`state.md`・`tasks.yaml`・記入済み docs・あなたの CLAUDE.md）に絶対に触れない。アンインストールは未編集のものだけ削除する。また導入時に `TEST_CMD`/`CHECK_CMD` を省略すると、ビルドファイル（package.json・pyproject.toml・Cargo.toml・go.mod・makefile）から検出したコマンドを**提案として表示**する（自動書き込みはしない）。

## 使い方

1. `docs/00-product-brief.md` に「何を作りたいか」を数行書く（人が書く唯一の出発点）。
2. 以下を順に実行する。各コマンドの最後に人の承認を求めて止まる。

   | 手順 | コマンド | 何が起きるか | あなた（人）の役割 |
   |------|----------|--------------|--------------------|
   | 要件 | `/req`    | 壁打ちで要件を構造化 | ① 要件を凍結 |
   | 設計 | `/design` | 実装方針＋技術選定の選択肢提示 | ② 技術選定を決定・承認 |
   | 分解 | `/tasks`  | テスト方針付きタスク票を生成 | ③ タスク計画を承認 |
   | 実装 | `/build`  | loop で自律実装（test green 条件） | ④ 実装完了をレビュー承認 |
   | 検証 | `/verify` | 機能＋非機能テストを実行 | ⑤ リリース可否を判断 |

3. 実装中に上流（要件/設計）の不備が判明したら **`/revise <phase>`** で差し戻せる（戻し先以降のゲートを連鎖して `pending` に戻し、`dag.py --impacted` で影響タスクを reconcile）。承認の巻き戻しも人の判断で行う。
4. いつでも `/status` で現在フェーズ・ゲート承認状況・タスク進捗を確認できる。タスクの**全体像（依存図）**は `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --mermaid` で Mermaid を生成でき、GitHub/VS Code/Markdown にそのまま描画される（status 色分け・クリティカルパス強調）。
5. リリース判断（ゲート⑤）のあとは `make cycle-close NAME=<slug>` でサイクルを閉じる: このサイクルの docs を `docs/archive/<日付>-<slug>/` へ退避し、新しいスキャフォールドを復元、ゲート/フェーズを次サイクル用にリセットする。greenfield / brownfield 共通の操作（`docs/00-product-brief.md` とベースライン `docs/05-current-state.md` は残る）。サイクルを閉じるのはゲートを開くのと同じく人間の操作。

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

- 各タスクは **品質ゲートのパイプラインを全て通って**初めて完了扱い — `.agentloop/config.yaml` の `quality_gate.steps` が **DoD の唯一の定義**（既定: `make test` green → `make check` clean → `/code-review`+`/simplify` の規律を適用する review ステップ → 起動可能な成果物では実起動スモーク）。各 cmd ステップは自分のリトライ予算を持ち、失敗は予算が尽きるまで implementer に差し戻される（尽きたら `blocked`）。
- **並列タスクは隔離実行**: 独立した葉タスクは `git worktree` で各自のブランチ・作業ディレクトリに分離して **最大3並列**（`config.yaml` の `max_parallel`）で実装し、完了後に id 昇順で作業ブランチへ順次マージする。基盤タスクは作業ブランチ上で先に確定する。
- 解決不能なタスクは `blocked`、上流に不備があれば `needs-revision` として **人にエスカレーション**し、ループが止まる。
- **確定化の境界**: 制御フロー・並列・マージ・cmd ステップのゲート判定・停止はコードで確定。各タスクの実装コードと review ステップの修正内容は LLM 由来で非確定で、「review が変更したら通過済みステップを再検証、green になるまで retry、駄目なら blocked」で吸収する。**`gates.build` はオーケストレータも触らない**（ゲートは人だけが開ける）。

> **前提スタック**: 同梱の `makefile` で `make test`（pytest）・`make check`（ruff/format/mypy/tsc を一括）を使う。`make check` は `make pre-commit`（commit ステージ）と `make pre-push`（format/mypy/tsc）を束ねたもの。`make` の無いプロジェクトにコピーした場合は、各自のテスト/チェックコマンドに読み替える。

### セキュリティ検査

3層で担保する: **gitleaks**（pre-commit でシークレットのコミットを機構的に防止。誤検知は `.gitleaksignore` で除外）／実装完了時に **`/security-review`** 必須／`/verify` で **`/security-review` + `make audit`**（依存の脆弱性監査）必須。

### GitHub Issues 連携（任意）

タスクをチーム/ステークホルダーに可視化したい場合、`tasks.yaml` を **GitHub Issues へ一方向ミラー**できる（`make issue-sync`）。

- **既定オフ**。`.agentloop/config.yaml` の `github.enabled: true` で有効化。`gh` CLI と GitHub remote が前提で、無ければ自動スキップ（オフライン・コピー直後でも壊れない）。
- 各タスク T-NNN ↔ Issue 1件。Issue 番号は tasks.yaml に書かず、ラベル＋本文の不可視マーカー `<!-- agentloop:T-NNN -->` で突き合わせる（Issue のタイトルを変えても対応が壊れない）。`done` は close。
- **付与ラベルで判別できる**: `kind:*`（種別）/ `status:*`（状態）/ `phase:*`（工程 requirements/design/build/verify）/ `req:*`（対応要件）。使用ラベルは `gh label create --force` で**自動作成（provisioning）**されるため、ラベル未作成の repo でも初回から失敗しない。
- **一方向のみ**: `tasks.yaml` が常に SSOT。Issues 側の編集は読み戻さない（確定駆動・オフライン性を保つ）。`make issue-sync ARGS=--dry-run` で予定だけ確認できる。
- Issue 書き込みは外向き操作のため、`github.enabled: true` の opt-in が同意を兼ねる。

## トラブルシューティング

- **タスクが `blocked` になった** — ステップのリトライ予算内で品質ゲートを通せなかった。`.agentloop/build-loop.log` に追記された要約（と `state.md` のエスカレーションログ）を読み、原因（またはタスク票）を直し、`.agentloop/tasks.yaml` の該当タスクの `status` を `todo` に戻して `make build-loop` を再実行する。原因が上流（要件/設計）の不備なら代わりに `/revise <phase>` で差し戻す。
- **ループが中断した**（Ctrl-C・クラッシュ・ネットワーク）— そのまま `make build-loop` を再実行すればよい。起動時に `in_progress` のまま残ったタスクを `todo` に戻し、残った worktree/ブランチも作り直すので、再開は安全。
- **ゲートガードに編集を拒否された**（"Blocked: gate not approved…"）— 前提ゲートが `pending` のまま次フェーズの成果物を編集しようとしている。機構が正しく働いている状態なので、まずゲートの承認を得る。状態が本当に誤っているなら `.agentloop/state.md` の `gates.*` を直す（承認は人の判断）。緊急脱出口は `.agentloop/config.yaml` の `gates.enforce_hook: false`。
- **ガード対象の編集が全部拒否され、state.md が読めない旨のメッセージが出る** — `.agentloop/state.md` が無いか front-matter が壊れており、ガードが fail-closed している。`gates:` ブロックがパースできるようファイルを復旧する（必要なら git 履歴から）。
- **`make build-loop` が「template placeholders」で起動を拒否する** — 先に `make init NAME=<product>` を実行する（セットアップ参照）。
- **state.md と実態がずれた**（タスク表が古い等）— タスクの真実は `tasks.yaml`。人間向けビューは `uv run --no-project --with pyyaml python scripts/agentloop/dag.py --render` で再生成して `state.md` に貼り直す。ゲートとフェーズは `state.md` が真実なので、意図をもって修正する（ゲートを開く・巻き戻すのは人だけ）。
- **導入先リポジトリで `make` が無い/使えない** — AgentLoop のターゲットは `agentloop.mk` に自己完結している（必要なのは `uv` バイナリだけ）。`make -f agentloop.mk build-loop` で単体実行するか、`uv run --no-project --with pyyaml python scripts/agentloop/build_loop.py` のようにスクリプトを直接呼ぶ。
- **`agentloop-upgrade`/`agentloop-uninstall` が「no adopt-manifest」と言う** — この2つは adopt 専用。greenfield の `make init` はマニフェストを記録しない（コピーしたテンプレート全体があなたの所有物なので区別する対象がない）。マニフェスト導入前に adopt したリポジトリでは、`make adopt` をもう一度実行すれば（既存ファイルは全てスキップされ）マニフェストだけが記録され、以後アップグレードできる。

## 構成

| パス | 役割 |
|------|------|
| `.agentloop/state.md` | フェーズ・ゲート・ログの SSOT |
| `.agentloop/tasks.yaml` | タスクグラフ(DAG)の機械可読 SSOT |
| `.agentloop/config.yaml` | 確定実行のノブ源（並列・worktree・ゲート強制）と DoD の唯一の定義（`quality_gate.steps`） |
| `scripts/agentloop/` | 確定オーケストレーション（`dag.py`／`build_loop.py`／`gate_guard.py`／`init.py`／`adopt.py`／`cycle.py`）。プロダクト用は `scripts/` 直下 |
| `agentloop.mk` | AgentLoop の make ターゲット。自己完結（uv のみ）で、既存リポジトリはこの1ファイルだけ持っていける |
| `CLAUDE.md` | エージェント運用規約・ゲート規則 |
| `.claude/commands/` | 各工程の入口（`/req`〜`/verify` に加え `/onboard`・`/revise`・`/status`） |
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
