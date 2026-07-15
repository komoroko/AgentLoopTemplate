"""Structured orchestration events (`.agentloop/events.ndjson`) — the machine-readable escalation log.

Why a structured log: the escalation record used to be split between a hand-edited markdown
table in state.md and free-text appended to build-loop.log — neither aggregatable ("which task
blocked how often, at which gate step, and which commit fixed it") and the table was fragile to
machine-update. Following the same pattern as tasks.yaml → `dag.py --render`, the NDJSON file is
the truth and state.md embeds only a **generated view** (between the ESCALATION-VIEW markers).

One JSON object per line: `{id, ts, event, task, step, detail, commit, ref}` (empty fields are
omitted). `id` is a monotonically increasing integer; a `resolve` event closes the escalation
whose id its `ref` names. Escalation kinds (`blocked` / `merge_conflict` / `integration_red` /
`no_runnable` / `gate_violation`) stay **open** until resolved — /verify closes them before gate ⑤.

Writers: build_loop.py appends automatically; a human or the interactive-mode agent appends via
`agentloop events --add blocked --task T-003 --detail "..."` and resolves via
`agentloop events --resolve 3 --note "fixed by abc123"`. Reads are tolerant: a corrupt line is
skipped, never a crash (the log must not be able to take the orchestrator down).

Usage:
  uv run --no-project --with pyyaml python src/agentloop/events.py --render
  uv run --no-project --with pyyaml python src/agentloop/events.py --add blocked --task T-003 --detail "..."
  uv run --no-project --with pyyaml python src/agentloop/events.py --resolve 3 --note "fixed by abc123"
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from agentloop import common
from agentloop import repo as repo_mod

EVENTS_PATH = ".agentloop/events.ndjson"
STATE_PATH = common.STATE_PATH
EVENTS_MAX_BYTES = 256 * 1024  # rotate past this (context hygiene; open escalations are carried over)

VIEW_BEGIN = "<!-- ESCALATION-VIEW:BEGIN -->"
VIEW_END = "<!-- ESCALATION-VIEW:END -->"

# Events that need a human decision; open until a `resolve` event's ref names their id.
ESCALATION_EVENTS = frozenset({"blocked", "merge_conflict", "integration_red", "no_runnable", "gate_violation"})
# Display order of the full vocabulary (validated on append so a typo cannot create an
# unaggregatable kind; the set is this tuple, kept in one place).
# `gate_approved` is the machine record of a human gate approval (written by approve.py; the
# commit-stage gate guard cross-checks a state.md flip against it). `branch_salvaged` records
# a restart preserving a previous run's unmerged leaf branch under a salvage name.
EVENT_ORDER = (
    "blocked",
    "merge_conflict",
    "integration_red",
    "no_runnable",
    "gate_violation",
    "step_fail",
    "task_done",
    "gate_approved",
    "branch_salvaged",
    "security_review",
    "resolve",
)
EVENT_VALUES = frozenset(EVENT_ORDER)

# Parallel leaves emit events from worker threads of one process; serialize the read-max-id +
# append pair so ids stay unique. (Cross-process writes are already excluded by build-loop.lock.)
_LOCK = threading.Lock()


@dataclass(frozen=True)
class Event:
    """One structured event. `ref` links a `resolve` to the escalation id it closes (0 = none).

    `gate` names the lifecycle gate a `gate_approved` event records — a first-class field
    (not free text in `detail`) because gate_guard's commit-stage check matches on it.
    """

    id: int
    ts: str  # ISO "YYYY-MM-DDTHH:MM:SS"
    event: str
    task: str = ""
    step: str = ""
    gate: str = ""
    detail: str = ""
    commit: str = ""
    ref: int = 0

    @property
    def date(self) -> str:
        return self.ts[:10]


def load_events(path: str = EVENTS_PATH) -> list[Event]:
    """Read the NDJSON log. Tolerant: unreadable file = empty, a corrupt line is skipped."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    result: list[Event] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                continue
            result.append(
                Event(
                    id=int(raw["id"]),
                    ts=str(raw.get("ts", "")),
                    event=str(raw["event"]),
                    task=str(raw.get("task", "")),
                    step=str(raw.get("step", "")),
                    gate=str(raw.get("gate", "")),
                    detail=str(raw.get("detail", "")),
                    commit=str(raw.get("commit", "")),
                    ref=int(raw.get("ref", 0)),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue  # a corrupt line must not take the orchestrator down
    return result


def _dump(event: Event) -> str:
    """One NDJSON line; empty/zero optional fields are omitted (lean, diff-friendly)."""
    raw = {k: v for k, v in asdict(event).items() if v or k in ("id", "ts", "event")}
    return json.dumps(raw, ensure_ascii=False)


def append_event(
    event: str,
    *,
    task: str = "",
    step: str = "",
    gate: str = "",
    detail: str = "",
    commit: str = "",
    ref: int = 0,
    path: str = EVENTS_PATH,
) -> Event:
    """Append one event with the next id and return it. Unknown kinds are rejected."""
    if event not in EVENT_VALUES:
        raise ValueError(f"unknown event {event!r} (one of {', '.join(EVENT_ORDER)})")
    with _LOCK:
        existing = load_events(path)
        next_id = max((e.id for e in existing), default=0) + 1
        entry = Event(
            id=next_id,
            ts=datetime.now().isoformat(timespec="seconds"),
            event=event,
            task=task,
            step=step,
            gate=gate,
            detail=detail,
            commit=commit,
            ref=ref,
        )
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(_dump(entry) + "\n")
    return entry


def open_escalations(events: list[Event]) -> list[Event]:
    """Escalation events not yet closed by a `resolve` whose ref names their id."""
    resolved = {e.ref for e in events if e.event == "resolve"}
    return [e for e in events if e.event in ESCALATION_EVENTS and e.id not in resolved]


def _cell(text: str, limit: int = 96) -> str:
    """First line of `text`, pipe-escaped and truncated, for a markdown table cell."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    line = line.replace("|", "\\|")
    return line[: limit - 1] + "…" if len(line) > limit else line


def render_view(events: list[Event]) -> str:
    """The human-facing escalation view (counts + open-escalation table), deterministically.

    state.md embeds it between the ESCALATION-VIEW markers; --render prints it as-is.
    """
    counts = {kind: 0 for kind in EVENT_ORDER}
    for e in events:
        if e.event in counts:
            counts[e.event] += 1
    lines = ["Events: " + " / ".join(f"{kind}={counts[kind]}" for kind in EVENT_ORDER), ""]
    lines.append('### Open escalations (resolve with `agentloop events --resolve <ID> --note "..."`)')
    opened = open_escalations(events)
    if opened:
        lines.append("| ID | Date | Event | Task | Step | Detail |")
        lines.append("|----|------|-------|------|------|--------|")
        for e in opened:
            lines.append(
                f"| {e.id} | {e.date} | {e.event} | {e.task or '-'} | {e.step or '-'} | {_cell(e.detail) or '-'} |"
            )
    else:
        lines.append("- (none)")
    return "\n".join(lines)


def _tally(pairs: list[str]) -> str:
    """Deterministic "key×count" listing, most frequent first (ties by key)."""
    counts: dict[str, int] = {}
    for key in pairs:
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k}×{n}" for k, n in ranked) if ranked else "(none)"


def render_summary(events: list[Event]) -> str:
    """Aggregates over the log — the "which task / which step / how often" questions."""
    escalations = [e for e in events if e.event in ESCALATION_EVENTS]
    opened = open_escalations(events)
    lines = ["### Aggregates"]
    lines.append(f"- escalations: {len(escalations)} total, {len(opened)} open")
    lines.append("- escalations by task: " + _tally([e.task or "(no task)" for e in escalations]))
    fails = [e.step or "(no step)" for e in events if e.event == "step_fail"]
    lines.append("- step failures by step: " + _tally(fails))
    lines.append(f"- tasks done: {sum(1 for e in events if e.event == 'task_done')}")
    return "\n".join(lines)


def refresh_state_view(path: str = EVENTS_PATH, state_path: str = STATE_PATH) -> bool:
    """Re-render state.md's generated escalation block (between the ESCALATION-VIEW markers).

    Same contract as build_loop.update_state_view: the NDJSON stays the truth, this only keeps the
    human-facing board fresh. No markers (a hand-restructured state.md) or unreadable file = no-op.
    """
    try:
        text = Path(state_path).read_text(encoding="utf-8")
    except OSError:
        return False
    begin = text.find(VIEW_BEGIN)
    end = text.find(VIEW_END)
    if begin == -1 or end == -1 or end < begin:
        return False
    new = text[: begin + len(VIEW_BEGIN)] + "\n" + render_view(load_events(path)) + "\n" + text[end:]
    Path(state_path).write_text(new, encoding="utf-8")
    return True


def rotate_if_large(path: str = EVENTS_PATH, max_bytes: int = EVENTS_MAX_BYTES) -> bool:
    """Rotate an oversized log to `<path>.1`, carrying **open escalations** into the fresh file.

    Left unbounded the append-only log bloats the context both humans and agents re-read (Context
    Rot). Open escalations keep their original ids in the fresh file, so pending `--resolve <id>`
    references stay valid and id monotonicity is preserved. Best-effort: any OSError = no rotation.
    """
    p = Path(path)
    try:
        if p.stat().st_size <= max_bytes:
            return False
        events = load_events(path)
        carried = open_escalations(events)
        p.replace(Path(f"{path}.1"))
        if carried:
            with p.open("w", encoding="utf-8") as fh:
                for e in carried:
                    fh.write(_dump(e) + "\n")
    except OSError:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="structured orchestration events (the escalation log's truth)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render", action="store_true", help="print the escalation view (default)")
    group.add_argument("--summary", action="store_true", help="print aggregates (per task / per step counts)")
    group.add_argument("--add", metavar="EVENT", help=f"append an event by hand (one of {', '.join(EVENT_ORDER)})")
    group.add_argument("--resolve", type=int, metavar="ID", help="close an open escalation (records a resolve event)")
    group.add_argument("--refresh-state", action="store_true", help="re-render state.md's ESCALATION-VIEW block only")
    parser.add_argument("--task", default="", help="task id for --add (e.g. T-003)")
    parser.add_argument("--step", default="", help="quality-gate step for --add (e.g. test)")
    parser.add_argument("--detail", default="", help="free-text detail for --add")
    parser.add_argument("--commit", default="", help="related commit hash for --add")
    parser.add_argument("--note", default="", help="how it was resolved, for --resolve")
    parser.add_argument("--path", default="", help=f"events file (default: {EVENTS_PATH} under the discovered root)")
    parser.add_argument("--state", default="", help=f"state.md carrying the view (default: discovered {STATE_PATH})")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    if not args.path or not args.state:
        try:
            repo = repo_mod.get(args.repo)
        except repo_mod.RepoNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        args.path = args.path or str(repo.events)
        args.state = args.state or str(repo.state)

    if args.add is not None:
        try:
            entry = append_event(
                args.add, task=args.task, step=args.step, detail=args.detail, commit=args.commit, path=args.path
            )
        except ValueError as exc:
            print(f"error: {exc} — valid events: {', '.join(EVENT_ORDER)}", file=sys.stderr)
            return 2
        print(f"added #{entry.id} {entry.event}" + (f" ({entry.task})" if entry.task else ""))
        refresh_state_view(args.path, args.state)
        return 0

    if args.resolve is not None:
        events = load_events(args.path)
        target = next((e for e in events if e.id == args.resolve), None)
        if target is None or target.event not in ESCALATION_EVENTS:
            print(
                f"error: no escalation event with id {args.resolve} — list the open ones with"
                " `agentloop events --render`",
                file=sys.stderr,
            )
            return 2
        if target.id not in {e.id for e in open_escalations(events)}:
            print(f"error: escalation #{args.resolve} is already resolved", file=sys.stderr)
            return 2
        append_event("resolve", task=target.task, detail=args.note, commit=args.commit, ref=target.id, path=args.path)
        print(f"resolved #{target.id} {target.event}" + (f" ({target.task})" if target.task else ""))
        refresh_state_view(args.path, args.state)
        return 0

    if args.refresh_state:
        ok = refresh_state_view(args.path, args.state)
        print("state view refreshed" if ok else "no ESCALATION-VIEW markers in state.md (nothing refreshed)")
        return 0

    events = load_events(args.path)
    print(render_view(events))
    if args.summary:
        print()
        print(render_summary(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
