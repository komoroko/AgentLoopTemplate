---
description: フェーズ1 要件定義。brief を起点に壁打ちし、要件を固めてゲート①で承認を仰ぐ。
---

# /req — 要件定義フェーズ

あなたはこのプロジェクトの要件定義を進行する。**Human on the Loop**：作業はあなた、確定は人。

## 手順
1. `.agentloop/state.md` を読む。`current_phase` を確認する。
   - すでに `gates.requirements == approved` なら「要件は承認済み。変更するなら再承認が必要」と伝え、人の指示を待つ。
2. `docs/00-product-brief.md` を読む。空ならまず人に記入を促して止まる。
3. `requirements-analyst` サブエージェントに委譲し、要件草案・抜け漏れ・論点を出す。
4. 曖昧点・重要な分岐は **AskUserQuestion** で人に確認して埋める。
5. 合意できた内容を `docs/10-requirements.md` に（雛形構造で）書き出す。
6. **ゲート①**: plan mode であれば ExitPlanMode で要件サマリを提示して承認を仰ぐ。plan mode でなければ要件サマリを提示し「この内容で要件を凍結してよいか」を明示的に確認する。確認事項は **1回の AskUserQuestion にまとめて** 聞く。
   - **自己評価を必ず併せて提示**する（CLAUDE.md「ゲート自己評価」）: 置いた前提・要件ごとの確信度（高/中/低）・未解決の論点・想定リスク。`10-requirements.md` の「自己評価」節にも残す。確信度が低い要件は人の注意を促す。

## 承認待ち中（ボトルネック最小化）
ゲート①提示後、承認を待つ間に以下を進めてよい（**結果非依存・破棄前提**。CLAUDE.md「承認待ち中のボトルネック最小化」参照）。やったことは `state.md` の「先回り作業ログ」に記録する。
- `PushNotification` で人へ承認待ちを通知。
- リポジトリ雛形・ディレクトリ構成・開発環境/CI の骨組み。
- brief に出ている候補技術の **読み取り調査**（設計の確定はしない）。
- **禁止**: 要件を先取りした設計本体の記述。

## 承認されたら
- `state.md` の `gates.requirements` を `approved`、`current_phase` を `design`、`updated_at` を更新。
- 「次は `/design`」と案内する。

承認が得られるまで gate は `pending` のまま。勝手に approved にしない。
