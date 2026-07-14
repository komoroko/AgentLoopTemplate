"""Deterministic helper for recording a human gate approval — the forward twin of revise.py.

The **single sanctioned write path** for `gates.<name>: pending → approved`. AGENTS.md gate
rule 2 ("only humans open a gate") used to be convention only: the agent was instructed to
edit state.md itself after the human said "approve", which left the flip one prompt
misinterpretation away. Now the flip is an *operation*: this script stamps the gate line with
the date (and approver), advances `current_phase`, and appends a `gate_approved` event to
`.agentloop/events.ndjson` — the machine record the commit-stage gate guard cross-checks a
state.md flip against. gate_guard.py denies agent Write/Edit flips at edit time, so the only
ways a gate opens are a human running this (directly, via `make approve`, or acknowledging
the agent's *non-pre-authorized* `make approve` permission prompt) or a human editing
state.md by hand.

Like revise.py it preserves state.md byte-for-byte outside the touched lines
(common.rewrite_gate_line / set_current_phase / set_updated_at — regex line surgery, never a
YAML round-trip), and it enforces the gate-chain invariant: approving a gate whose upstream
is still pending is refused.

ApproveError carries an HTTP-ish `status` (400 bad request / 409 conflict / 500 broken
state.md) so ui.py's approval endpoint can map failures to responses directly; the CLI folds
them all to exit 1 (except the already-approved no-op, exit 0).

Usage:
  uv run --no-project --with pyyaml python src/agentloop/approve.py design --by alice
  make approve GATE=design BY=alice
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from agentloop import common, events
from agentloop import repo as repo_mod

STATE_PATH = common.STATE_PATH
GATE_ORDER = common.GATE_ORDER
# The phase entered once each gate is approved (the forward counterpart of revise._PHASE_GATE).
NEXT_PHASE = {"requirements": "design", "design": "tasks", "tasks": "build", "build": "verify", "release": "done"}


class ApproveError(Exception):
    """A refused approval. `status` is an HTTP-ish code so ui.py can answer with it directly."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class AlreadyApproved(ApproveError):
    """The gate is already approved — a no-op for the CLI, a 409 for the UI."""

    def __init__(self, gate: str) -> None:
        super().__init__(409, f"gate '{gate}' is already approved")


def apply_approval(text: str, gate: str, today: str, by: str = "") -> str:
    """Record the approval in the state.md text and return the new text (pure function).

    Flips the gate line to `approved   # <date> [<by>]`, advances `current_phase` to the
    approved gate's next phase, and bumps `updated_at`. Refuses (ApproveError) an unknown
    gate, a broken front matter, an already-approved gate, and a chain violation.
    """
    import yaml  # lazy: match the toolset convention (module import stays stdlib+common only)

    if gate not in GATE_ORDER:
        raise ApproveError(400, f"unknown gate '{gate}' (one of {', '.join(GATE_ORDER)})")
    try:
        front = common.parse_frontmatter(text)
    except yaml.YAMLError as exc:
        raise ApproveError(500, f"state.md front-matter is not valid YAML: {exc}") from None
    if front is None:
        raise ApproveError(500, "state.md has no YAML front-matter")
    gates = common.gates_of(front) or {}
    current = gates.get(gate, "")
    if current == "approved":
        raise AlreadyApproved(gate)
    upstream = common.pending_upstream(gates, gate)
    if upstream is not None:
        raise ApproveError(409, f"cannot approve '{gate}': upstream gate '{upstream}' is still pending")
    stamp = f"approved   # {today} {by}".rstrip()
    new_text, n = common.rewrite_gate_line(text, gate, current, stamp, keep_trailer=False)
    if n == 0:
        raise ApproveError(500, f"gate line '{gate}: {current}' not found in state.md front-matter")
    new_text = common.set_current_phase(new_text, NEXT_PHASE[gate])
    new_text = common.set_updated_at(new_text, today)
    return new_text


def record_approval(
    gate: str, by: str = "", *, state_path: str = STATE_PATH, events_path: str = events.EVENTS_PATH
) -> str:
    """Apply the approval to state.md on disk and append the `gate_approved` event.

    Returns the date stamped on the gate line. The event is what the commit-stage gate guard
    matches a state.md flip against, so it is written in the same operation — never separately.
    """
    try:
        text = Path(state_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ApproveError(500, f"cannot read {state_path}: {exc}") from None
    today = date.today().isoformat()
    Path(state_path).write_text(apply_approval(text, gate, today, by), encoding="utf-8")
    events.append_event("gate_approved", gate=gate, detail=by, path=events_path)
    return today


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="record a human gate approval (the only sanctioned pending→approved write)"
    )
    parser.add_argument("gate", choices=GATE_ORDER, help="the gate the human approved")
    parser.add_argument(
        "--by",
        default="",
        help="approver name, stamped on the gate line (recommended when several humans share the repo)",
    )
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        today = record_approval(args.gate, args.by, state_path=str(repo.state), events_path=str(repo.events))
    except AlreadyApproved as exc:
        print(exc.message + " (nothing to do)")
        return 0
    except ApproveError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    print(
        f"gate '{args.gate}' approved   # {today}{' ' + args.by if args.by else ''} — "
        f"current_phase → {NEXT_PHASE[args.gate]} (recorded as a gate_approved event)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
