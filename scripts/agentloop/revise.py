"""Deterministic helper for rolling back (returning upstream).

The **first-class operation** for a human rewinding approval. Resets every gate from the target phase's gate onward
to pending **in a chain**, updates `current_phase` and `updated_at`, and appends one line to the roll-back log. This
mechanically prevents the stale-approval inconsistency of "upstream pending while downstream still approved"
(the editing order from then on is enforced by gate_guard).

To preserve state.md's comments and formatting, it **surgically rewrites only the target lines with regex**
(it does not rewrite the whole YAML). It does not touch task state — task impact analysis is handled by
the `/revise`→`/design`,`/tasks` flow and `dag.py --impacted` (transitive dependents).

Usage:
  uv run python scripts/agentloop/revise.py --to design --reason "rethink the auth method"
  uv run python scripts/agentloop/revise.py --to requirements --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

STATE_PATH = ".agentloop/state.md"
# The forward gate order. Reset the target onward to pending in a chain.
GATE_ORDER = ("requirements", "design", "tasks", "build", "release")
# Roll-back target phase -> the gate the chain starts at (verify precedes the release gate, so it is not a target).
_PHASE_GATE = {"requirements": "requirements", "design": "design", "tasks": "tasks", "build": "build"}
REVISE_MARKER = "<!-- REVISE-LOG -->"


class ReviseError(ValueError):
    """A roll-back operation failure, such as an invalid target."""


def cascade_gates(target_phase: str) -> list[str]:
    """Return the gates from the target phase's gate onward (the ones to reset to pending)."""
    if target_phase not in _PHASE_GATE:
        raise ReviseError(f"invalid target '{target_phase}' (one of {sorted(_PHASE_GATE)})")
    start = GATE_ORDER.index(_PHASE_GATE[target_phase])
    return list(GATE_ORDER[start:])


def _set_gate_pending(text: str, gate: str) -> str:
    """Set just the value of the front-matter "  <gate>: approved   # comment" to pending (preserving the comment)."""
    pattern = re.compile(rf"^(\s*{re.escape(gate)}:\s*)approved(.*)$", re.MULTILINE)
    return pattern.sub(r"\1pending\2", text)


def _set_current_phase(text: str, value: str) -> str:
    pattern = re.compile(r"^(\s*current_phase:\s*)\S+(\s*(?:#.*)?)$", re.MULTILINE)
    return pattern.sub(rf"\g<1>{value}\2", text)


def _set_updated_at(text: str, today: str) -> str:
    pattern = re.compile(r"^(\s*updated_at:\s*).*$", re.MULTILINE)
    return pattern.sub(rf'\g<1>"{today}"', text)


def _insert_log(text: str, target: str, gates: list[str], reason: str, today: str) -> str:
    """Append one row to the roll-back log table (right before the marker). Do nothing if the marker is absent."""
    if REVISE_MARKER not in text:
        return text
    row = f"| {today} | {target} | {', '.join(gates)} | {reason or '-'} |"
    return text.replace(REVISE_MARKER, f"{row}\n{REVISE_MARKER}", 1)


def apply_revision(text: str, target: str, reason: str, today: str) -> str:
    """Apply the roll-back to the state.md text and return the new text (pure function)."""
    gates = cascade_gates(target)
    new = text
    for gate in gates:
        new = _set_gate_pending(new, gate)
    new = _set_current_phase(new, target)
    new = _set_updated_at(new, today)
    new = _insert_log(new, target, gates, reason, today)
    return new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Roll back (reset upstream gates to pending in a chain)")
    parser.add_argument("--to", required=True, choices=sorted(_PHASE_GATE), help="the target phase to roll back to")
    parser.add_argument("--reason", default="", help="the roll-back reason (recorded in the roll-back log)")
    parser.add_argument("--dry-run", action="store_true", help="show the plan only without writing state.md")
    args = parser.parse_args(argv)

    gates = cascade_gates(args.to)
    today = date.today().isoformat()

    if args.dry_run:
        print(f"[dry-run] target phase: {args.to}")
        print(f"[dry-run] gates reset to pending: {', '.join(gates)}")
        print(f"[dry-run] current_phase -> {args.to} / updated_at -> {today}")
        return 0

    try:
        text = Path(STATE_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read state.md: {exc}", file=sys.stderr)
        return 1
    Path(STATE_PATH).write_text(apply_revision(text, args.to, args.reason, today), encoding="utf-8")
    print(f"roll-back complete: phase={args.to}. gates reset to pending: {', '.join(gates)}")
    print(
        "Next: rebuild with the command for that phase → re-approve. "
        "Do not discard existing tasks; enumerate the ripple with dag.py --impacted and reconcile."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
