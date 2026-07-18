# AgentLoop

[English](README.md) | **日本語**

**Human on the Loop** で開発を進めるための、コーディングエージェント用ハーネス。
要件定義からテストまで、作業も成果物の作成も自己テストもエージェントが担当する。
**人間は各フェーズの境界にある「ゲート」で承認・判断するだけでよい。**

ハーネスの本体は**インストールして使う CLI**(`agentloop`)である。プロダクトのリポジトリ側に残るのは
*状態*だけで、`.agentloop/`(SSOT〈信頼できる唯一の情報源〉・lock・実体化された prompts/schema)と
`docs/`(フェーズ成果物)がそれにあたる。
**Claude Code** と **VS Code GitHub Copilot** はフックによるゲート強制まで含めてフル対応。
**Codex** など `AGENTS.md` を読むエージェントも、規約と手順のレベルで動く(ゲートは慣習で維持)。
詳しくは「エージェント対応」の節を参照。

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

    subgraph TASKS["タスク群(依存グラフ DAG)"]
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
    g3 -->|"並列消化(最大3)"| build
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

🟦 エージェントが実行するフェーズ / 🟧 **人間だけ**が開けるゲート①〜⑤ / 🟩 人間の関与ポイント /
🟪 タスク(DAG: 基盤 → 並列の葉 → 統合)。

フローは上から下へ進み、**前提のゲートが未承認のうちは次のフェーズへ進めない**。`/build` は
タスク群を最大3並列で消化する。赤い点線は `/revise` による上流への差し戻しで、戻し先以降の
ゲートを連鎖的に `pending` へ戻す — これも人間の判断でのみ行う。

## どこから始めるか

最初に一度だけ CLI をインストールする(`uv tool install git+<AgentLoop リポジトリ>` で
`agentloop` コマンドが PATH に入る)。その後は状況に応じて:

| いまの状況 | 入口 |
|---|---|
| ゼロから新しいプロダクトを作る(greenfield) | 「セットアップ」→「使い方」 |
| 開発中の既存リポジトリに導入する(brownfield) | 「セットアップ」(`agentloop init` が自動判定)→ `/onboard` |
| 導入済みのリポジトリで次の変更を始める | `docs/00-product-brief.md` に変更内容を書いて `/req`(前サイクルが未クローズなら先に `agentloop cycle-close --name <slug>`) |
| リリース判断(ゲート⑤)が済んだ | `agentloop cycle-close --name <slug>` — 今サイクルの docs をアーカイブし、次サイクルに向けてリセット |
| 実体化されたツール群を更新したい | `agentloop upgrade`(取り除くときは `agentloop uninstall --all`) |
| 現在地が分からない・中断から再開する | `/status`(次に打つコマンドも表示)か `agentloop ui`(ローカルのダッシュボード) |

人間が日常的に打つコマンドは、次の少数の動詞に絞ってある(それ以外はダッシュボードの
ボタンに相当する操作 — 一覧は `agentloop --help`):

```bash
agentloop start        # 初回: 対話ウィザードでセットアップ / 導入済みなら現在地と次の一手を表示
agentloop next         # 次に打つべきコマンドだけを表示(連携用に --json あり)
agentloop ui           # ローカルダッシュボード — ゲート承認や doctor・revise・cycle-close をページから実行
agentloop agent codex  # ヘッドレスで使うエージェント CLI を切替(claude | codex | gemini | 任意コマンド)
agentloop project add  # ダッシュボードのプロジェクト切替対象にリポジトリを登録
```

複数のリポジトリを行き来する場合は、`agentloop project add <name> <path>` でそれぞれを登録すると、
ダッシュボードのヘッダに**プロジェクト切替**(ドロップダウン)が現れ、サーバを立て直さずにボードの
対象を切り替えられる。`agentloop ui` は起動元のリポジトリを常に自動登録する。単発の指定も従来どおり可能で、
`agentloop --repo <path> <verb>`(または `AGENTLOOP_ROOT=<path>`)なら、ディレクトリを移動せずに個々の
コマンドを別リポジトリに向けられる。

## 設計原則

AgentLoop はそれ自体が複数エージェントのオーケストレーションであり、次の3つの軸で設計している。

- **Architecture** — 動く範囲で最もシンプルな構成にする。`agentloop build` は**決定論的な DAG
  スケジューラ**で、各フェーズの作業は専用のロールエージェントに委譲して関心を分離する。
- **Context** — コンテキストは必要最小限に保つ。真実は SSOT に置き、ロールエージェントは必要な
  分だけ読む。失敗はダンプせず要約し、ログは自動でローテーションし、記憶はセッション/サイクル/
  恒久の3層で管理する(`AGENTS.md` の「Context budget」参照)。
- **Tools** — ロールエージェントへのツール付与は最小限・用途限定にし、品質ゲートにはリトライ上限を
  設ける。

## セットアップ

前提環境は WSL / Linux / macOS。フックが PATH 上で `agentloop` を見つけられるよう、まず CLI を
インストールする:

```bash
uv tool install git+https://github.com/komoroko/AgentLoopTemplate   # `agentloop` コマンドが入る
```

モード A(`agentloop build`)を使うには、加えて**ヘッドレスで動くエージェント CLI** が必要になる。
既定は `claude -p` で、`agentloop agent codex` で切り替えられる(`.agentloop/config.yaml` の
`build.headless.cmd` を書き換える。`gemini` や任意のコマンドも指定できる)。用意できない場合は
対話型のモード B を使う(「エージェント対応」参照)。

次にリポジトリを初期化する。**greenfield**(新規)でも **brownfield**(既存)でもコマンドは同じで、
brownfield は自動判定される — 既存のコード構成を見つけると、`gates.guard_paths` を docs 成果物
だけに絞ってゲート未承認でも既存コードの開発を止めないようにし、test/check コマンドを
プロジェクトのツールから検出する:

```bash
cd myrepo && git init            # 新規でも既存でも同じ

# 対話ウィザード(推奨。質問はプロダクト名〔フォルダ名が既定〕と brief の1行のみ。
# ブランチは build/<name>、取得元はインストール元から自動検出、ヘッドレス CLI は既定のまま
# ——いずれも後から変更できる〔下記参照〕)
agentloop start
# 非対話で行う場合(何度実行しても安全):
#   agentloop init --name <product> [--branch build/<product>] [--source git+https://github.com/komoroko/AgentLoopTemplate]

# 任意・開発環境ごと — 使うエージェントの入口を必要になったら追加する:
agentloop install claude         # .claude/ のラッパーを書き、settings.json をマージ
agentloop install copilot        # .github/ に prompt / agent / hook のラッパーを書く
```

`agentloop init` が書き込むのは**状態だけ**である: SSOT の3ファイル(`state.md` / `config.yaml` /
`tasks.yaml`、プレースホルダ入り)と、docs のスキャフォールド。加えて、実体化された
`.agentloop/prompts`・`.agentloop/schema`・`.agentloop/AGENTS.agentloop.md` と、初期スキャフォールドの
スナップショット、`.agentloop/agentloop.lock`(ツールのバージョン・取得元と、導入ファイルごとの
内容ハッシュ)を置く。
あわせて `AGENTS.md` にマーカー付きのポインタブロックを追記し、作業ブランチを作成して切り替え
(実装は main ではなくこのブランチで行う)、ゲートガードを有効化する。それ以外には触れない —
ビルドファイルも makefile も書かず、エージェントの入口も `agentloop install` するまでは入らない。
brownfield の場合は `/onboard` への案内も添えられる。

`agentloop sync` は、インストール済みパッケージから prompts/schema を再実体化する(手を入れて
いないファイルは更新し、ローカルで変更したファイルは保持して一覧表示。`--force` で上書き、
`--check` は書き込まずズレの報告だけ)。`agentloop upgrade` は CHANGELOG の差分を表示したうえで、
ツールが実体化したものをすべて更新する。CLI 本体の更新は `uv tool upgrade agentloop`。

## 既存リポジトリへの導入(brownfield)

専用の導入コマンドはない — `agentloop init` が唯一の入口で、既存のコードベース(`src/`・
`package.json`・`pyproject.toml` など)を**自動判定**する。判定されると:

- `config.yaml` の `guard_paths` を docs 成果物だけに絞り、ゲートが未承認でも既存コードの開発が
  止まらないようにする(準備ができたら `src/: tasks` のようにコードのパスを戻す)。
- 品質ゲートの test/check コマンドを、認識できる範囲でプロジェクトのツールから埋める
  (`--test-cmd` / `--check-cmd` で上書き可能)。
- `docs/00-product-brief.md` に `/onboard` を案内する導入メモを付ける。

既存ファイルは**決して上書きしない**(再実行しても安全)。導入後の流れは:

1. **`/onboard`** — 既存コードベースを読み取り専用で調査し、**恒久ベースライン**
   `docs/05-current-state.md` を作る。既存の挙動を要件や完了済みタスクへ逆生成することは
   **しない** — ゲートを開くのは常に人間で、トレーサビリティ(R-N)は各サイクルの差分だけに
   適用される。作りかけの実装がある場合は、先頭に**吸収タスク**を置き、既存の部分実装を
   テストで green に固定してから新しい作業を積む。
2. **デルタサイクル** — `brief → /req → … → /verify` の1周で**1つの変更**を扱う(進め方は
   「使い方」と同じ)。リリース判断のあと `agentloop cycle-close --name <slug>` を実行すると、
   サイクルの docs がアーカイブされ、ゲートとフェーズがリセットされる。
   `docs/00-product-brief.md` と `docs/05-current-state.md` は残る。
3. **いつでも撤去できる** — `agentloop uninstall claude|copilot` はエージェントの入口を取り除き
   (手を入れていないファイルのみ。settings のマージはエントリ単位で戻す)、
   `agentloop uninstall --all` は実体化された成果物と lock をすべて削除する。リポジトリ自身の
   状態(SSOT と `docs/`)には触れない。

## 使い方

1. `docs/00-product-brief.md` に「何を作りたいか」を数行で書く(人間が書く出発点はこれだけ)。
2. 次のコマンドを順に実行する。各コマンドは最後に承認を求めて止まる。

   | 手順 | コマンド | 何が起きるか | あなた(人間)の役割 |
   |------|----------|--------------|--------------------|
   | 要件 | `/req`    | 対話で要件を構造化する | ① 要件を凍結する |
   | 設計 | `/design` | 実装方針と技術選定の選択肢を提示する | ② 技術選定を決めて承認する |
   | 分解 | `/tasks`  | テスト方針付きのタスク票を生成する | ③ タスク計画を承認する |
   | 実装 | `/build`  | ループで自律実装する(テスト green が完了条件) | ④ 実装をレビューして承認する |
   | 検証 | `/verify` | 機能テストと非機能テストを実行する | ⑤ リリース可否を判断する |

3. **ゲートを開く**: 承認は「操作」として記録する。`agentloop approve <gate> [--by <name>]` が
   ゲート行に日付と承認者を刻み、フェーズを進め、`gate_approved` イベントを記録する。あなたが
   明示的に「承認」と伝えたあとならエージェントがこのコマンドを実行してもよいが、事前許可
   (pre-authorize)は決してしない — 実行時の権限プロンプトそのものが人間の確認になっているからだ。
   ゲート行を手で編集してもガードが拒否する。
4. **差し戻す**: 上流(要件・設計)の不備が見つかったら `/revise <phase>` を実行する。戻し先以降の
   ゲートが連鎖的に `pending` へ戻り、影響を受けるタスクがマークされる
   (`agentloop revise --impacted T-00x` は、指定タスクとその下流をまとめて `needs-revision` に
   する)。承認の巻き戻しも人間の判断で行う。
5. **進捗を確認する**: `agentloop next` は次に打つべきコマンドだけを表示する(連携用に `--json`)。
   `/status` はチャットで全体像を示し、`agentloop ui` は同じ内容をブラウザで見られる(既定は
   読み取り専用。安全な操作の固定ホワイトリストとゲート承認の記録はページからも実行できる)。
   タスクの依存図は `agentloop dag --mermaid` で生成できる。
6. **PR にする**: `agentloop pr-draft` が SSOT から PR 本文を組み立てて `.agentloop/pr-draft.md`
   に書き出す(読み取り専用)。PR の作成や push は従来どおり人間の操作。
7. **サイクルを閉じる**: ゲート⑤のあと `agentloop cycle-close --name <slug>` を実行すると、docs が
   `docs/archive/<日付>-<slug>/` へアーカイブされ、新しいスキャフォールドが復元され、ゲートと
   フェーズがリセットされる。ゲートを開くのと同じく人間の操作である。

> **承認待ちの間も止まらない**: ゲートに到達すると通知が飛ぶ。承認を待つ間、エージェントは
> **承認結果に依存しない**作業(環境構築・調査・テストハーネスの整備など)だけを先回りして進める。
> 承認結果を先取りする作業はしないのでゲートの厳密さは保たれる。先回り分は暫定・破棄前提の扱いで、
> `state.md` の「先回り作業ログ」に記録される。

### 実装フェーズを自律で回す

挙動(DoD、並列・マージの規則)が同じ2つのモードがある。正式な手順は
`.agentloop/prompts/commands/build.md` と `AGENTS.md`。

**A. 確定実行(推奨)— `agentloop build`。** どのタスクを・何並列で・どの順にマージし・いつ
止めるかを、オーケストレータが `config.yaml` と `tasks.yaml` から決定論的に決める。LLM の裁量には
依存しない(`--dry-run` を付けると、エージェント CLI や git を呼ばずに制御フローだけ確認できる)。

**B. 対話ループ** — リード役のエージェントが会話の中でモード A と同じ手順を再現する。ヘッドレス
CLI がない環境で使える唯一のモード。Claude Code は `/loop /build`、Copilot は `/build` を繰り返し
起動、Codex は `/build` の手順を再実行する。

両モードに共通するルール:

- タスクは**品質ゲートのパイプラインをすべて通過して**はじめて完了になる。`config.yaml` の
  `quality_gate.steps` が **DoD の唯一の定義**(既定: `test` → `check` → `/code-review` +
  `/simplify` による review ステップ → 起動できる成果物なら実起動の smoke テスト)。各ステップには
  リトライ予算があり、使い切ると `blocked` になる。成果物が起動できるようになったら smoke ステップに
  `required: true` を設定する(コマンドが空のままだと、起動チェックを黙ってスキップせずビルド自体を
  拒否する)。
- **並列の葉タスクは隔離して実行する**: `git worktree` で分離して最大3並列(`max_parallel`)。
  完了後、タスク id の昇順で作業ブランチへマージする。1バッチで2つ以上の葉をマージしたときは、
  マージ後のブランチで cmd ステップを再実行する(統合ゲート)。またどのマージの前にも、タスクが
  変更した全パスをゲート規則に照らして再検査する — 違反は `gate_violation` としてエスカレーションし、
  blocked にしてマージさせない。
- 解決できないタスクは `blocked`、上流の不備は `needs-revision` としてエスカレーションし、ループは
  停止する。**`gates.build` にはオーケストレータも触れない**(ゲートを開けるのは人間だけ)。

> **DoD のコマンドはプロジェクト固有**: `quality_gate.steps` に一度だけ書く。同梱の既定値
> `make test` / `make check` はプレースホルダで、brownfield では `agentloop init` が検出した
> コマンドを埋める。それ以外の場合は自分のプロジェクトのコマンドに置き換えること。

### セキュリティ検査

3つの層で担保する。コミット段では **gitleaks** がシークレットのコミットを防ぐ(誤検知は
`.gitleaksignore` へ)。実装完了時には**セキュリティレビュー**が必須で、モード A では全タスク done
の時点で自動的にヘッドレス実行し、レビュー対象の HEAD を埋め込んだレポートを
`.agentloop/security-review.md` に残す。`/verify` では**セキュリティレビューと依存パッケージの
脆弱性監査**が必須になる。`/security-review` を持たないエージェントは、同等の検査を行って同じ形式で
記録する。

### GitHub Issues 連携(任意)

**既定はオフ**。`github.enabled: true` で有効化する(`gh` CLI と GitHub remote が前提。なければ
自動でスキップ)。`agentloop issue-sync` は `tasks.yaml` を Issues へ**一方向にミラー**する —
タスク T-NNN と Issue が1対1で対応し、不可視マーカー `<!-- agentloop:T-NNN -->` で突き合わせ、
`kind:*` / `status:*` / `phase:*` / `req:*` ラベル(自動作成)を付ける。Issues 側で編集しても
読み戻さない(SSOT は常に `tasks.yaml`)。Issue への書き込みは外向きの操作なので、オプトインが
そのまま同意の表明になる。

## トラブルシューティング

- **まずは `agentloop doctor`** — 環境と SSOT を読み取り専用で一括診断する(PATH 上のバイナリ、
  config/state/tasks の整合性、ゲート連鎖の不変条件、フック登録、worktree の残骸、未解決の
  エスカレーション、セキュリティレビューと HEAD の対応、lock の健全性、schema 検証)。以下の症状の
  多くはここに FAIL / WARN として現れる。
- **タスクが `blocked` になった** — リトライ予算内で品質ゲートを通せなかったということ。
  `agentloop events --render` でエスカレーションの内容を読み、原因(またはタスク票)を直し、
  `tasks.yaml` の `status` を `todo` に戻し、`agentloop events --resolve <ID> --note "…"` で
  イベントを閉じてから `agentloop build` を再実行する。上流の不備が原因なら、代わりに
  `/revise <phase>`。
- **ループが中断した**(Ctrl-C・クラッシュ)— そのまま `agentloop build` を再実行すればよい。
  起動時に `in_progress` のタスクを `todo` に戻し、残った worktree も掃除される。
- **ゲートガードに編集を拒否された** — 前提のゲートが `pending` のまま、次フェーズの成果物を
  編集しようとしている(つまり仕組みが正しく働いている)。まずゲートの承認を得ること。緊急時の
  脱出口は `gates.enforce_hook: false`。
- **「template placeholders」と言われて起動を拒否される** — 先に `agentloop start`(または
  `agentloop init --name <product>`)を実行する。
- **フック実行時に `agentloop: command not found`** — CLI を PATH に入れる
  (`uv tool install git+<AgentLoop リポジトリ>`)。フックのバイナリが見つからない状態は
  `agentloop doctor` でも FAIL になる。

## 構成

| パス | 役割 |
|------|------|
| `.agentloop/state.md` | フェーズ・ゲート・ログの SSOT |
| `.agentloop/tasks.yaml` | タスクグラフ(DAG)の機械可読な SSOT |
| `.agentloop/events.ndjson` | オーケストレーションのイベントログ — エスカレーションログの機械可読な真実(`agentloop events` で操作。最初のイベント発生時に作られる) |
| `.agentloop/config.yaml` | 確定実行の設定と、DoD の唯一の定義(`quality_gate.steps`) |
| `.agentloop/agentloop.lock` | ツールのバージョン・取得元、schema バージョン、導入ファイルごとの内容ハッシュ |
| `.agentloop/schema/` | `config.yaml` / `tasks.yaml` の JSON Schema(エディタでの検証と `agentloop doctor` が使う)— 実体化ファイル |
| `.agentloop/prompts/` | 全エージェントが読む共有のフェーズ手順とロール定義 — 実体化ファイル |
| `.agentloop/AGENTS.agentloop.md` | エージェントの入口が import する運用規約の本体 — 実体化ファイル |
| `AGENTS.md` / `CLAUDE.md` | エージェント中立な運用規約の正本 / Claude Code 向けの能力対応表 |
| `.claude/`・`.github/` | エージェント別の入口・ロールのラッパー・ゲートガードのフック登録(`agentloop install` で任意導入) |
| `docs/` | フェーズ成果物(要件・設計・ADR・タスク票・テスト計画) |

オーケストレーションのコード自体はインストールされた `agentloop` パッケージの中にあり、
リポジトリには置かれない。

## エージェント対応

規約(`AGENTS.md`)と手順(`.agentloop/prompts/`)はエージェント中立に書かれており、人間との
やり取りが必要な箇所を**能力ボキャブラリ**という共通の語彙で表す。それを各エージェントでどう
実現するかは、エージェントごとの対応表ファイルが定める。

| 能力 | Claude Code | VS Code Copilot | Codex(他の AGENTS.md 読者を含む) |
|---|---|---|---|
| フェーズの入口 | スラッシュコマンド(`.claude/commands/`) | prompt files(`.github/prompts/`) | フェーズ名を指示 → `.agentloop/prompts/commands/<name>.md` を読んで実行 |
| ゲート強制 | PreToolUse フック + コミット段のチェック | 同じフックを agent hooks(preview)で + コミット段のチェック | コミット段のチェックのみ(編集時は慣習で維持) |
| 人間への構造化質問 | AskUserQuestion | チャットで番号付きの選択肢 | チャットで番号付きの選択肢 |
| 承認の提示 | plan mode + ExitPlanMode | Plan モード / 明示的な「approve」 | 明示的な「approve」 |
| ロール委譲 | subagents(worktree で並列) | custom agents `@architect` など | インラインでロールを引き受け(直列) |
| 自律ビルド | `/loop /build`(B)・`agentloop build`(A) | `/build` を反復(B)・`agentloop build`(A) | `/build` を再実行(B)・`agentloop build`(A) |
| ゲート待ちの通知 | PushNotification | ターン終了時に明示 | ターン終了時に明示 |

エージェントの入口はオプトインで、`agentloop install claude|copilot` が書き込む。これらは
インストール済みの `agentloop` CLI を呼び出すため、フックの前提として `uv tool install` が必要に
なる。VS Code Copilot の agent hooks は **preview** 機能で、無効な場合でもゲートは慣習のレイヤーで
維持される。並列の葉タスクは、委譲が使えない環境では直列に劣化する。どのフックホストが登録済みかは
`agentloop doctor` が報告する。
