"""PreToolUse フック: 前提ゲート未承認のまま「次フェーズの成果物」を編集させない機構層。

CLAUDE.md の規約層（各コマンドが自分でゲートを確認する）に依存せず、コードで阻止する。
Claude Code の PreToolUse フックとして Write/Edit に対し登録し、編集対象パスと
`.agentloop/state.md` の gates を突き合わせ、前提が approved でなければ **deny** する。

判定:
  docs/20-design.md, docs/decisions/**        → requirements が approved 必須
  docs/tasks/**                               → design       が approved 必須
  backend/**, frontend/**, scripts/**（実装コード） → tasks   が approved 必須
  docs/test/**（テスト結果記入）               → build        が approved 必須
ただし scripts/agentloop/**（テンプレート基盤ツール）は **常に許可**（フック自身の保守・
先回り作業を妨げないため）。上記以外のパスも無条件で許可する。

`.agentloop/config.yaml` の gates.enforce_hook が false なら常に許可する
（config が読めない場合は既定で有効＝enforce-on とみなす）。
state.md の gates が読めない等の異常時は **fail-open（許可）** する
（build フェーズの確実な停止は scripts/agentloop/build_loop.py のコード判定が別途担保する）。

入出力は Claude Code フックの規約に従う:
  stdin  : フックのイベント JSON（tool_name, tool_input.file_path 等）
  stdout : deny 時に hookSpecificOutput を持つ JSON を出力。許可時は何も出さない。
  exit   : 常に 0（判定は JSON で伝える）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath

import yaml

STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"

# ガード対象外（ゲートに関わらず常に許可）。テンプレート基盤ツールはフック自身が
# 動く場所であり、build ゲートで保守をブロックしてはならない。
_UNGUARDED_PREFIXES: tuple[str, ...] = ("scripts/agentloop/",)

# (パス判定, 必要な gate 名)。先頭から評価し最初に一致したものを使う。
# scripts/ はプロダクト用スクリプトの実装コードとして tasks 承認を要する
# （scripts/agentloop/ は上の除外で先に弾かれる）。
_RULES: list[tuple[str, str]] = [
    ("docs/decisions/", "requirements"),
    ("docs/tasks/", "design"),
    ("docs/test/", "build"),
    ("backend/", "tasks"),
    ("frontend/", "tasks"),
    ("scripts/", "tasks"),
]
_EXACT: dict[str, str] = {
    "docs/20-design.md": "requirements",
}

_PHASE_LABEL = {
    "requirements": "/req（要件）",
    "design": "/design（設計）",
    "tasks": "/tasks（タスク計画）",
    "build": "/build（実装）",
}


def _repo_relative(file_path: str) -> str | None:
    """編集対象をリポジトリ相対の posix パスへ正規化する。リポジトリ外なら None。"""
    try:
        rel = os.path.relpath(os.path.abspath(file_path), os.getcwd())
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return PurePosixPath(rel).as_posix()


def required_gate(file_path: str) -> str | None:
    """この編集が前提とする gate 名。ガード対象外なら None。"""
    rel = _repo_relative(file_path)
    if rel is None:
        return None
    if any(rel.startswith(p) for p in _UNGUARDED_PREFIXES):
        return None
    if rel in _EXACT:
        return _EXACT[rel]
    for prefix, gate in _RULES:
        if rel.startswith(prefix):
            return gate
    return None


def _enforce_enabled() -> bool:
    try:
        data = yaml.safe_load(Path(CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return True  # config が無ければ既定で有効（fail-secure。state 不在時のみ fail-open）
    gates = data.get("gates") or {}
    return bool(gates.get("enforce_hook", True))


def _read_gates() -> dict[str, str] | None:
    """state.md フロントマターの gates を読む。読めなければ None（fail-open）。"""
    try:
        text = Path(STATE_PATH).read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    gates = front.get("gates")
    if not isinstance(gates, dict):
        return None
    return {str(k): str(v) for k, v in gates.items()}


def evaluate(file_path: str) -> tuple[bool, str]:
    """(allowed, reason) を返す。allowed=False のとき reason が deny 理由。"""
    gate = required_gate(file_path)
    if gate is None:
        return True, ""
    if not _enforce_enabled():
        return True, ""
    gates = _read_gates()
    if gates is None:
        return True, ""  # state 不明は fail-open
    if gates.get(gate) == "approved":
        return True, ""
    phase = _PHASE_LABEL.get(gate, gate)
    return False, (
        f"ゲート未承認のためブロックしました: この編集は前提ゲート '{gate}' の承認を要します。"
        f" 先に {phase} を完了し人の承認を得てください（.agentloop/state.md の gates.{gate} を確認）。"
    )


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # 解釈できなければ介入しない
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0
    allowed, reason = evaluate(file_path)
    if not allowed:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(decision, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
