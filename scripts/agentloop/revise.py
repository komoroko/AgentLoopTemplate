"""差し戻し（上流への後戻り）の確定ヘルパ。

人が承認を巻き戻す**一級操作**。戻し先 phase のゲート以降を**連鎖**して pending に戻し、
`current_phase` と `updated_at` を更新し、差し戻しログに1行追記する。これにより
「上流が pending なのに下流が approved のまま」という stale 承認の不整合を機構的に防ぐ
（以後の編集順は gate_guard が強制する）。

state.md はコメント・体裁を保つため、**対象行だけ正規表現で手術的に書き換える**
（yaml 全書き換えはしない）。タスク状態には触らない——タスク影響分析は
`/revise`→`/design`・`/tasks` の手順と `dag.py --impacted`（推移的被依存）が担当する。

使い方:
  uv run python scripts/agentloop/revise.py --to design --reason "認証方式の見直し"
  uv run python scripts/agentloop/revise.py --to requirements --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

STATE_PATH = ".agentloop/state.md"
# 前進ゲートの順序。戻し先以降を連鎖して pending に戻す。
GATE_ORDER = ("requirements", "design", "tasks", "build", "release")
# 差し戻し先 phase -> 連鎖開始ゲート（verify は release ゲートの前段なので戻し先には取らない）。
_PHASE_GATE = {"requirements": "requirements", "design": "design", "tasks": "tasks", "build": "build"}
REVISE_MARKER = "<!-- REVISE-LOG -->"


class ReviseError(ValueError):
    """不正な戻し先など、差し戻し操作の失敗。"""


def cascade_gates(target_phase: str) -> list[str]:
    """戻し先 phase に対応するゲート以降（pending へ戻す対象）を返す。"""
    if target_phase not in _PHASE_GATE:
        raise ReviseError(f"不正な戻し先 '{target_phase}'（{sorted(_PHASE_GATE)} のいずれか）")
    start = GATE_ORDER.index(_PHASE_GATE[target_phase])
    return list(GATE_ORDER[start:])


def _set_gate_pending(text: str, gate: str) -> str:
    """フロントマターの "  <gate>: approved   # コメント" の値だけ pending に（コメント保持）。"""
    pattern = re.compile(rf"^(\s*{re.escape(gate)}:\s*)approved(.*)$", re.MULTILINE)
    return pattern.sub(r"\1pending\2", text)


def _set_current_phase(text: str, value: str) -> str:
    pattern = re.compile(r"^(\s*current_phase:\s*)\S+(\s*(?:#.*)?)$", re.MULTILINE)
    return pattern.sub(rf"\g<1>{value}\2", text)


def _set_updated_at(text: str, today: str) -> str:
    pattern = re.compile(r"^(\s*updated_at:\s*).*$", re.MULTILINE)
    return pattern.sub(rf'\g<1>"{today}"', text)


def _insert_log(text: str, target: str, gates: list[str], reason: str, today: str) -> str:
    """差し戻しログ表へ1行追記（マーカー直前）。マーカーが無ければ何もしない。"""
    if REVISE_MARKER not in text:
        return text
    row = f"| {today} | {target} | {', '.join(gates)} | {reason or '-'} |"
    return text.replace(REVISE_MARKER, f"{row}\n{REVISE_MARKER}", 1)


def apply_revision(text: str, target: str, reason: str, today: str) -> str:
    """state.md テキストへ差し戻しを適用して新テキストを返す（純粋関数）。"""
    gates = cascade_gates(target)
    new = text
    for gate in gates:
        new = _set_gate_pending(new, gate)
    new = _set_current_phase(new, target)
    new = _set_updated_at(new, today)
    new = _insert_log(new, target, gates, reason, today)
    return new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="差し戻し（上流ゲートを連鎖して pending に戻す）")
    parser.add_argument("--to", required=True, choices=sorted(_PHASE_GATE), help="戻し先フェーズ")
    parser.add_argument("--reason", default="", help="差し戻し理由（差し戻しログに記録）")
    parser.add_argument("--dry-run", action="store_true", help="state.md を書かず計画のみ表示")
    args = parser.parse_args(argv)

    gates = cascade_gates(args.to)
    today = date.today().isoformat()

    if args.dry_run:
        print(f"[dry-run] 戻し先 phase: {args.to}")
        print(f"[dry-run] pending に戻すゲート: {', '.join(gates)}")
        print(f"[dry-run] current_phase -> {args.to} / updated_at -> {today}")
        return 0

    try:
        text = Path(STATE_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"state.md を読めません: {exc}", file=sys.stderr)
        return 1
    Path(STATE_PATH).write_text(apply_revision(text, args.to, args.reason, today), encoding="utf-8")
    print(f"差し戻し完了: phase={args.to}。pending に戻したゲート: {', '.join(gates)}")
    print(
        "次の手順: 該当フェーズのコマンドで作り直し→再承認。"
        "既存タスクは破棄せず、dag.py --impacted で波及を洗い出して reconcile してください。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
