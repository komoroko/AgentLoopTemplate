# scripts/

スクリプトの置き場。**2種類を混在させない**よう用途で分ける。

| パス | 用途 | 所有 |
|------|------|------|
| `scripts/agentloop/` | AgentLoop テンプレートの基盤ツール（確定オーケストレータ `build_loop.py`／DAG 導出 `dag.py`／ゲートフック `gate_guard.py`／Issues 一方向ミラー `issue_sync.py` とその単体テスト）。テンプレートに同梱され、`make build-loop`・`make test-tools`・`make issue-sync`・`.claude/settings.json` のフックが参照する。 | テンプレート |
| `scripts/`（直下・その他サブフォルダ） | **プロダクト固有**のスクリプト（データ整備・運用補助など）。プロダクトごとに自由に追加してよい。 | プロダクト |

`scripts/agentloop/` 配下はテンプレートの一部なので、プロダクト都合で書き換えない（設定で変えたい挙動は `.agentloop/config.yaml` で調整する）。

## ゲート（`gate_guard.py`）との関係

- `scripts/`（直下・プロダクト用）への Write/Edit は **実装コード扱い**で、`gates.tasks` が approved でないと機構フックに **deny** される（`backend/**`・`frontend/**` と同じ）。
- `scripts/agentloop/`（基盤ツール）は **ゲートに関わらず常に許可**（フック自身の保守・先回り作業を妨げないため）。
