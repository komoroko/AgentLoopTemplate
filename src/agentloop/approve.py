"""Deterministic helper for recording a human gate approval — the forward twin of revise.py.

The **single sanctioned write path** for `gates.<name>: pending → approved`. AGENTS.md gate
rule 2 ("only humans open a gate") used to be convention only: the agent was instructed to
edit state.md itself after the human said "approve", which left the flip one prompt
misinterpretation away. Now the flip is an *operation*: this script stamps the gate line with
the date (and approver), advances `current_phase`, and appends a `gate_approved` event to
`.agentloop/events.ndjson` — the machine record the commit-stage gate guard cross-checks a
state.md flip against. gate_guard.py denies agent Write/Edit flips at edit time, so the only
ways a gate opens are a human running this (directly, via `agentloop approve`, or acknowledging
the agent's *non-pre-authorized* `agentloop approve` permission prompt) or a human editing
state.md by hand.

Like revise.py it preserves state.md byte-for-byte outside the touched lines
(common.rewrite_gate_line / set_current_phase / set_updated_at — regex line surgery, never a
YAML round-trip), and it enforces the gate-chain invariant: approving a gate whose upstream
is still pending is refused.

ApproveError carries an `http.HTTPStatus` (BAD_REQUEST / CONFLICT / INTERNAL_SERVER_ERROR for a
broken state.md) so ui.py's approval endpoint can map failures to responses directly; the CLI
folds them all to exit 1 (except the already-approved no-op, exit 0).

Usage:
  agentloop approve design --by alice
"""

from __future__ import annotations

import argparse
import logging
from datetime import date
from http import HTTPStatus
from pathlib import Path

from agentloop import common, events
from agentloop import repo as repo_mod

logger = logging.getLogger(__name__)

STATE_PATH = common.STATE_PATH
GATE_ORDER = common.GATE_ORDER
# The phase entered once each gate is approved (the forward counterpart of revise._PHASE_GATE).
NEXT_PHASE = {"requirements": "design", "design": "tasks", "tasks": "build", "build": "verify", "release": "done"}

# --- readiness preconditions (machine anchors only — never a markdown parse) -----------------
# Each gate's procedure promises evidence before the human is asked; these checks turn the
# promise into mechanism, the same convention→mechanism migration gate flips already made.
# Deliberately limited to anchors code can decide (a literal marker string, the report's
# Reviewed-HEAD line, the structured event log) — adequacy stays the human's judgment.
SECURITY_REVIEW_PATH = ".agentloop/security-review.md"
_CLARIFICATION_MARKER = "[NEEDS CLARIFICATION"
_GATE_DELIVERABLE = {"requirements": "docs/10-requirements.md", "design": "docs/20-design.md"}


def _reviewed_head(report: Path) -> str:
    try:
        first = report.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return ""
    return first.removeprefix("Reviewed-HEAD:").strip() if first.startswith("Reviewed-HEAD:") else ""


def _holds_unresolved_marker(text: str) -> bool:
    """A live marker is inline plain text; the scaffold *teaches* the marker inside backtick
    spans and HTML comments, which must not read as unresolved work."""
    import re  # lazy: match the toolset convention (module import stays stdlib+common only)

    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", "", text)
    return _CLARIFICATION_MARKER in text


def readiness_findings(gate: str, root: Path, events_path: str) -> list[str]:
    """What blocks presenting this gate as ready — [] when the recorded evidence is in order."""
    findings: list[str] = []
    doc = _GATE_DELIVERABLE.get(gate)
    if doc:
        try:
            text = (root / doc).read_text(encoding="utf-8")
        except OSError:
            text = ""
        if _holds_unresolved_marker(text):
            findings.append(f"{doc} still holds an unresolved {_CLARIFICATION_MARKER}] marker")
    if gate == "build":
        reviewed = _reviewed_head(root / SECURITY_REVIEW_PATH)
        if not reviewed:
            findings.append(f"no security-review report bound to this build ({SECURITY_REVIEW_PATH} — /build gate ④)")
        else:
            rc, out = common.run(["git", "rev-parse", "HEAD"], cwd=str(root))
            head = out.strip() if rc == 0 else ""
            if reviewed != head:  # unknown HEAD fails closed, like every other unverifiable gate input
                findings.append(f"security review is stale: Reviewed-HEAD {reviewed[:12]} ≠ HEAD {head[:12] or '?'}")
    if gate in ("build", "release"):
        open_ids = [f"#{e.id}" for e in events.open_escalations(events.load_events(events_path))]
        if open_ids:
            findings.append(f"open escalation event(s) {', '.join(open_ids)} — resolve them (`agentloop events`) first")
    return findings


class ApproveError(Exception):
    """A refused approval. `status` is an `http.HTTPStatus` so ui.py can answer with it directly."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class AlreadyApproved(ApproveError):
    """The gate is already approved — a no-op for the CLI, a 409 for the UI."""

    def __init__(self, gate: str) -> None:
        super().__init__(HTTPStatus.CONFLICT, f"gate '{gate}' is already approved")


def apply_approval(text: str, gate: str, today: str, by: str = "") -> str:
    """Record the approval in the state.md text and return the new text (pure function).

    Flips the gate line to `approved   # <date> [<by>]`, advances `current_phase` to the
    approved gate's next phase, and bumps `updated_at`. Refuses (ApproveError) an unknown
    gate, a broken front matter, an already-approved gate, and a chain violation.
    """
    import yaml  # lazy: match the toolset convention (module import stays stdlib+common only)

    if gate not in GATE_ORDER:
        raise ApproveError(HTTPStatus.BAD_REQUEST, f"unknown gate '{gate}' (one of {', '.join(GATE_ORDER)})")
    try:
        front = common.parse_frontmatter(text)
    except yaml.YAMLError as exc:
        raise ApproveError(
            HTTPStatus.INTERNAL_SERVER_ERROR, f"state.md front-matter is not valid YAML: {exc}"
        ) from None
    if front is None:
        raise ApproveError(HTTPStatus.INTERNAL_SERVER_ERROR, "state.md has no YAML front-matter")
    gates = common.gates_of(front) or {}
    current = gates.get(gate, "")
    if current == "approved":
        raise AlreadyApproved(gate)
    upstream = common.pending_upstream(gates, gate)
    if upstream is not None:
        raise ApproveError(HTTPStatus.CONFLICT, f"cannot approve '{gate}': upstream gate '{upstream}' is still pending")
    stamp = f"approved   # {today} {by}".rstrip()
    new_text, n = common.rewrite_gate_line(text, gate, current, stamp, keep_trailer=False)
    if n == 0:
        raise ApproveError(
            HTTPStatus.INTERNAL_SERVER_ERROR, f"gate line '{gate}: {current}' not found in state.md front-matter"
        )
    new_text = common.set_current_phase(new_text, NEXT_PHASE[gate])
    new_text = common.set_updated_at(new_text, today)
    return new_text


def record_approval(
    gate: str, by: str = "", *, state_path: str = STATE_PATH, events_path: str = events.EVENTS_PATH, force: bool = False
) -> str:
    """Apply the approval to state.md on disk and append the `gate_approved` event.

    Returns the date stamped on the gate line. The event is what the commit-stage gate guard
    matches a state.md flip against, so it is written in the same operation — never separately.
    Readiness preconditions run first (readiness_findings); `force` overrides them, and the
    override is recorded in the event detail so the skip is auditable, never silent.
    """
    try:
        text = Path(state_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ApproveError(HTTPStatus.INTERNAL_SERVER_ERROR, f"cannot read {state_path}: {exc}") from None
    root = Path(state_path).resolve().parents[1]
    findings = readiness_findings(gate, root, events_path)
    if findings and not force:
        listed = "; ".join(findings)
        raise ApproveError(HTTPStatus.CONFLICT, f"gate '{gate}' is not ready: {listed} (--force overrides)")
    today = date.today().isoformat()
    detail = by if not findings else f"{by} [forced past: {'; '.join(findings)}]".strip()
    Path(state_path).write_text(apply_approval(text, gate, today, by), encoding="utf-8")
    events.append_event("gate_approved", gate=gate, detail=detail, path=events_path)
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="approve despite failed readiness preconditions (the override is recorded in the gate_approved event)",
    )
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        today = record_approval(
            args.gate, args.by, state_path=str(repo.state), events_path=str(repo.events), force=args.force
        )
    except AlreadyApproved as exc:
        print(exc.message + " (nothing to do)")
        return 0
    except ApproveError as exc:
        logger.error(f"error: {exc.message}")
        return 1
    print(
        f"gate '{args.gate}' approved   # {today}{' ' + args.by if args.by else ''} — "
        f"current_phase → {NEXT_PHASE[args.gate]} (recorded as a gate_approved event)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
