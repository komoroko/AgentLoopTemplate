# ponytail 比較分析(テンプレート保守メモ)

> **Note**: これはテンプレートリポジトリ自身の保守メモです。greenfield コピーでプロダクトに
> 混入した場合は削除して構いません(upgrade/uninstall の対象外)。

2026-07-08、[DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) と本テンプレートを
比較し、「機構として学べる点」と「カテゴリ差として詰めるべきでない点」を切り分けた記録。

## ponytail の正体

開発ライフサイクルのテンプレートではなく、「YAGNI・最小コード」を強制する lazy senior dev
ペルソナを 16 種のコーディングエージェント(Claude Code / Cursor / Copilot / Windsurf / Kiro /
opencode / Gemini CLI …)に plugin / skill / ルールファイルとして配布するプロジェクト。本体は
実質 SKILL.md 1 枚+ホスト別アダプタ群+agentic ベンチマーク基盤。

## カテゴリが直交している

| 軸 | AgentLoopTemplate | ponytail |
|---|---|---|
| 種別 | プロセス機構(ゲート・SSOT・決定論的ループ) | コーディング規律(ペルソナプロンプト) |
| 作用点 | 「何をいつ作り、誰が承認するか」 | 「1 回の生成をどれだけ小さく書くか」 |
| 導入 | リポジトリに植える(copy / adopt) | エージェントに載せる(plugin / skill) |
| 正しさの根拠 | ゲート+exit code の機構的保証 | agentic ベンチマーク実測 |
| 状態 | SSOT 3 ファイル | ほぼステートレス |

競合せず併用可能。ponytail 的思想は本テンプレートの quality_gate `review`(/code-review +
/simplify の規律)と Principles「Reusing existing implementation comes first」に既に部分的に
存在する。

## 学べる点(採用)

1. **ルール文書 drift 検査**(`check-rule-copies.js` 相当)— 規範ファイル間の整合を CI で機械
   検査する。完全一致比較が不可能な組(翻訳ペア、散文↔コード)は「load-bearing な語彙・構造の
   canary 検査」に落とす。本テンプレートの同型問題は README.md↔README.ja.md の drift と、
   CLAUDE.md↔commands↔scripts の機械可読語彙(gate 名、kind/status 値、quality_gate ステップ名)
   の drift だった。→ `scripts/agentloop/template_lint.py` として導入(テンプレートリポジトリ
   専用、`gates.template_mode: false` のプロダクトでは自動スキップ)。
2. **バージョン整合検査**(`check-versions.js` 相当)— ponytail は「全 manifest が揃って stale」
   という実事故(v4.8.0, issue #260/#262: 相互一致だけ検査していたので全員一緒に古くても通った)
   をこの検査で塞いだ。本テンプレートの VERSION↔CHANGELOG 先頭見出しの一致検査がちょうど同型。
   → 同じく `template_lint.py` に同梱。
3. (姿勢のみ・実装不要)**訂正の透明性** — ベンチマークの過大主張を issue 指摘後に README へ
   経緯ごと残す文化。本テンプレートは CHANGELOG の Known limitations 節で既に同型を実施済み。
4. **最小実装(YAGNI)規律の明文化**(2026-07 採用)— ponytail の核である「受け入れ基準が要求
   する最小の実装。投機的一般化を作らない」を、ペルソナ配布ではなく既存面への織り込みで標準化:
   AGENTS.md Principles 第1項、implementer プロトコル、quality_gate `review` ステップのプロンプト
   (`_review_prompt`)、build.md の review 説明。新ステップ・intensity levels は追加しない
   (下記「詰めるべきでない点」4 のとおりゲート構造には触れない)。

## 詰めるべきでない点(非採用と理由)

1. **マルチエージェント対応(16 ホスト)** — 本テンプレートの核は gate_guard(PreToolUse hook)・
   subagents・commands という Claude Code 機構への深い結合。ポータブル化は「ゲートが慣習だけの
   instruction-tier 劣化版」にしかならない。ponytail がポータブルなのは本体がプロンプト 1 枚だから。
   > **2026-07 方針転換**: VS Code Copilot が Claude Code 互換の hooks(PreToolUse の同一 deny
   > 契約)・prompt files・custom agents を備えたため、「ゲートが慣習に劣化する」という上の前提が
   > 崩れた — 機構レイヤーごと移植できる。AGENTS.md を正本化し、手順本文を `.agentloop/prompts/`
   > に共有化して Claude / Copilot をフル対応、Codex は規約+手順レベル(ゲートは慣習のみ)で対応
   > した。「16 ホスト対応」ではなく、hooks 互換のホストに限る点は変えていない。ヘッドレスの
   > モード A(`claude -p`)は Claude Code 専用のまま。
   >
   > **2026-07 追補**: `gate_guard.py --check-diff`(pre-commit local hook、`make check` にも
   > 乗る)により commit 段の機構層はエージェント非依存になった。Codex でも「編集時は慣習のみ、
   > commit/DoD 段は機構検査」まで引き上がり、ツールフックを素通りするシェル経由の編集も同じ網に
   > かかる。編集時 intercept が hooks 互換ホスト限定である点は変わらない。
2. **plugin / marketplace 配布** — 配るものが「リポジトリの骨格」(docs/, .agentloop/, scripts/ が
   プロダクトの一部として git 管理される)であり、plugin の守備範囲(エージェントの挙動)外。
   copy / adopt + adopt-manifest が正しい形。
3. **効果ベンチマーク** — ponytail は「1 タスクの diff サイズ」という測れる量があるから成立する。
   ライフサイクル効果(手戻り減など)の対照実験は数週間単位・人間承認込みで非現実的。
   「実測なき定量主張をしない」姿勢だけ守る(README は定量効果を謳っていない)。
4. **intensity levels(lite/full/ultra)的な「ゲート緩めモード」** — Human-on-the-Loop の核を毀損
   する。hotfix には minimal delta cycle という正しい逃げ道が既にある(CLAUDE.md)。
5. **ステートレス化方向** — SSOT を削るとゲートの決定論が壊れる。1 枚で済む美徳はカテゴリ差の帰結。
