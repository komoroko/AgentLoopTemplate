"""Deterministic status aggregation for the AgentLoop SSOT — one JSON object, one next action.

Composes the existing derivation pieces (build_loop.read_frontmatter, dag.Graph, events.open_escalations,
revise.GATE_ORDER) into a single machine-readable status object, and — the part that previously lived only
as natural language in .agentloop/prompts/commands/status.md — computes the **next recommended command**
deterministically from the phase/gate/task state (first-match decision table in `next_action`).
Consumed by scripts/agentloop/ui.py (the local dashboard) and runnable standalone:

  uv run --no-project --with pyyaml python scripts/agentloop/status_api.py --json

Read-only: this module never writes to the SSOT. Reads are tolerant — a missing tasks.yaml (normal before
/tasks) or a half-edited config must degrade to a warning, never a crash (the dashboard has to stay up
precisely when the state is odd).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import build_loop
import dag
import events as events_mod
import revise
import yaml

GATE_ORDER = revise.GATE_ORDER
PHASE_ORDER = ("brief", "requirements", "design", "tasks", "build", "verify", "done")
# Phase -> the gate its command's approval-presentation targets (revise._PHASE_GATE stops at build
# because verify is not a roll-back target; for status purposes verify presents the release gate).
_PHASE_GATE = {**revise._PHASE_GATE, "verify": "release"}
_PHASE_COMMAND = {
    "requirements": "/req",
    "design": "/design",
    "tasks": "/tasks",
    "build": "/build",
    "verify": "/verify",
}
# The phase each gate belongs to on the stepper (release is approved from /verify).
_GATE_PHASE = {**{g: g for g in GATE_ORDER[:-1]}, "release": "verify"}


@dataclass(frozen=True)
class Recommendation:
    """The next recommended action, as a copy-able command plus a one-sentence why."""

    command: str
    kind: str  # run_phase | approve_gate | reconcile | resolve | setup | close | fix
    reason: str
    also: tuple[str, ...] = ()


def _is_placeholder(value: object) -> bool:
    """True for an unfilled scaffold value (`<enter the product name>` style) or a non-string."""
    return not isinstance(value, str) or not value or value.startswith("<")


def _gate_chain_broken(gates: dict[str, str]) -> bool:
    """True when a downstream gate is approved while an upstream one is pending (the invariant /revise keeps)."""
    seen_pending = False
    for gate in GATE_ORDER:
        status = gates.get(gate, "pending")
        if status != "approved":
            seen_pending = True
        elif seen_pending:
            return True
    return False


def next_action(
    *,
    current_phase: str,
    gates: dict[str, str],
    counts: dict[str, int] | None,
    open_escalation_count: int,
    template_mode: bool,
    placeholders: bool,
    has_adopt_manifest: bool,
) -> Recommendation:
    """The deterministic decision table (first match wins) for "what should the human do next"."""
    # 1. Not a product yet: the template must be initialized (or was adopted brownfield → survey first).
    if template_mode or placeholders:
        return Recommendation(
            command="make init NAME=<product>",
            kind="setup",
            reason="This checkout is still the raw template (template_mode / placeholder state.md); "
            "initialize it into a product first.",
            also=("/onboard",) if has_adopt_manifest else (),
        )
    # 2. A corrupt gate chain means the SSOT itself needs repair — do not guess a phase from it.
    if _gate_chain_broken(gates):
        return Recommendation(
            command="make doctor",
            kind="fix",
            reason="Gate chain invariant is broken (a downstream gate is approved while an upstream one is "
            "pending); diagnose before continuing.",
        )
    # 3. needs-revision tasks park everything until the /tasks reconcile reclassifies them at gate ③.
    if counts is not None and counts.get("needs-revision", 0) > 0:
        return Recommendation(
            command="/tasks",
            kind="reconcile",
            reason="needs-revision tasks exist; reconcile them (keep/modify/obsolete/new) and re-approve gate ③.",
            also=("make revise",),
        )
    # 4. /verify must close every open escalation before presenting gate ⑤.
    if current_phase == "verify" and open_escalation_count > 0:
        return Recommendation(
            command="make events ARGS='--resolve <ID> --note \"...\"'",
            kind="resolve",
            reason=f"{open_escalation_count} open escalation(s) must be closed before the gate ⑤ release decision.",
        )
    # 5. Before the lifecycle starts, the human writes the brief.
    if current_phase == "brief":
        return Recommendation(
            command="/req",
            kind="run_phase",
            reason="Fill docs/00-product-brief.md, then run /req to start the requirements phase (gate ①).",
        )
    # 6. Everything approved: the cycle is over.
    if current_phase == "done" or all(gates.get(g) == "approved" for g in GATE_ORDER):
        return Recommendation(
            command="make cycle-close NAME=<slug>",
            kind="close",
            reason="All gates are approved; archive this delta cycle's deliverables and reset for the next one.",
        )
    # 7. Inside a phase: pending gate → run (or finish) that phase's command; approved → advance.
    gate = _PHASE_GATE.get(current_phase)
    if gate is not None:
        index = GATE_ORDER.index(gate) + 1
        if gates.get(gate) != "approved":
            also = ("make build-loop",) if current_phase == "build" else ()
            return Recommendation(
                command=_PHASE_COMMAND[current_phase],
                kind="run_phase",
                reason=f"Phase '{current_phase}' is in progress; its command ends with the gate {index} "
                "approval presentation.",
                also=also,
            )
        next_phase = PHASE_ORDER[PHASE_ORDER.index(current_phase) + 1]
        if next_phase == "done":  # release approved but phase not yet flipped — same as row 6
            return Recommendation(
                command="make cycle-close NAME=<slug>",
                kind="close",
                reason="The release gate is approved; close the cycle.",
            )
        also = ("make build-loop",) if next_phase == "build" else ()
        return Recommendation(
            command=_PHASE_COMMAND[next_phase],
            kind="run_phase",
            reason=f"Gate {index} ({gate}) is approved; advance to the {next_phase} phase.",
            also=also,
        )
    # Unknown phase value — the SSOT is off-vocabulary; diagnose instead of guessing.
    return Recommendation(
        command="make doctor",
        kind="fix",
        reason=f"current_phase '{current_phase}' is not in the lifecycle vocabulary; diagnose the SSOT.",
    )


def _tasks_block(graph: dag.Graph) -> dict[str, object]:
    """The task-graph slice of the status object (all values derived, nothing stored)."""
    fan = graph.fan_out()
    return {
        "counts": {s: graph.counts()[s] for s in dag.STATUS_ORDER},
        "total": len(graph.tasks),
        "frontier": [
            {"id": t.id, "title": t.title, "kind": t.kind, "fan_out": fan[t.id]} for t in graph.order_frontier()
        ],
        "layers": graph.layers(),
        "critical_path": graph.critical_path(),
        "needs_revision": sorted(t.id for t in graph.tasks if t.status == "needs-revision"),
        "blocked": sorted(t.id for t in graph.tasks if t.status == "blocked"),
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "kind": t.kind,
                "blocked_by": list(t.blocked_by),
                "req": t.req,
            }
            for t in sorted(graph.tasks, key=lambda t: t.id)
        ],
    }


def collect_status(root: str | Path = ".") -> dict[str, object]:
    """Assemble the whole status object from the SSOT under `root`. Never raises for a readable repo."""
    root = Path(root)
    warnings: list[str] = []

    front: dict[str, object] = {}
    try:
        front = build_loop.read_frontmatter(str(root / ".agentloop" / "state.md"))
    except (OSError, yaml.YAMLError) as exc:
        warnings.append(f"cannot read state.md front-matter: {exc}")
    raw_gates = front.get("gates")
    gates = {str(k): str(v) for k, v in raw_gates.items()} if isinstance(raw_gates, dict) else {}
    current_phase = str(front.get("current_phase", ""))

    template_mode = False
    github_enabled = False
    try:
        config = yaml.safe_load((root / ".agentloop" / "config.yaml").read_text(encoding="utf-8")) or {}
        template_mode = bool((config.get("gates") or {}).get("template_mode", False))
        github_enabled = bool((config.get("github") or {}).get("enabled", False))
    except (OSError, yaml.YAMLError) as exc:
        warnings.append(f"cannot read config.yaml: {exc}")

    tasks: dict[str, object] | None = None
    counts: dict[str, int] | None = None
    try:
        graph = dag.load(root / ".agentloop" / "tasks.yaml")
        tasks = _tasks_block(graph)
        counts = graph.counts()
    except OSError:
        pass  # no tasks.yaml yet (normal before /tasks)
    except (dag.DagError, yaml.YAMLError) as exc:
        warnings.append(f"tasks.yaml is inconsistent: {exc}")

    all_events = events_mod.load_events(str(root / ".agentloop" / "events.ndjson"))
    opened = events_mod.open_escalations(all_events)

    recommendation = next_action(
        current_phase=current_phase,
        gates=gates,
        counts=counts,
        open_escalation_count=len(opened),
        template_mode=template_mode,
        placeholders=_is_placeholder(front.get("project")) or _is_placeholder(front.get("branch")),
        has_adopt_manifest=(root / ".agentloop" / "adopt-manifest.yaml").exists(),
    )

    return {
        "project": front.get("project"),
        "branch": front.get("branch"),
        "current_phase": current_phase,
        "updated_at": front.get("updated_at"),
        "phase_order": list(PHASE_ORDER),
        "gates": [
            {"name": g, "status": gates.get(g, "pending"), "index": i + 1, "phase": _GATE_PHASE[g]}
            for i, g in enumerate(GATE_ORDER)
        ],
        "template_mode": template_mode,
        "github_enabled": github_enabled,
        "tasks": tasks,
        "escalations": {
            "open": [
                {"id": e.id, "date": e.date, "event": e.event, "task": e.task, "step": e.step, "detail": e.detail}
                for e in opened
            ],
            "total_open": len(opened),
        },
        "next": asdict(recommendation),
        "warnings": warnings,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="aggregate the SSOT into one JSON status object")
    parser.add_argument("--root", default=".", help="repository root holding .agentloop/ (default: cwd)")
    parser.add_argument("--json", action="store_true", help="compact one-line JSON (default: indented)")
    args = parser.parse_args(argv)
    status = collect_status(args.root)
    print(json.dumps(status, ensure_ascii=False) if args.json else json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
