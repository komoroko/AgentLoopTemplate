"""Deterministic status aggregation for the AgentLoop SSOT — one JSON object, one next action.

Composes the existing derivation pieces (common.read_frontmatter, dag.Graph, events.open_escalations,
revise.GATE_ORDER) into a single machine-readable status object, and — the part that previously lived only
as natural language in .agentloop/prompts/commands/status.md — computes the **next recommended command**
deterministically from the phase/gate/task state (first-match decision table in `next_action`).
Consumed by src/agentloop/ui.py (the local dashboard) and runnable standalone:

  agentloop status --json

Read-only: this module never writes to the SSOT. Reads are tolerant — a missing tasks.yaml (normal before
/tasks) or a half-edited config must degrade to a warning, never a crash (the dashboard has to stay up
precisely when the state is odd).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import yaml

from agentloop import common, dag, revise
from agentloop import events as events_mod
from agentloop import lock as lock_mod

GATE_ORDER = common.GATE_ORDER
PHASE_ORDER = common.PHASE_ORDER
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


def _no_agent_surface(root: Path) -> bool:
    """True when the lock is readable and records no installed integration (agent surface).

    A missing or broken lock stays False — pre-lock repos are legitimate, and this hint must
    never turn a readable status into an error.
    """
    try:
        data = lock_mod.read(root / lock_mod.LOCK_NAME)
    except lock_mod.LockError:
        return False
    if data is None:
        return False
    return not (data.get("integrations") or {})


def _is_placeholder(value: object) -> bool:
    """True for an unfilled scaffold value (`<enter the product name>` style) or a non-string."""
    return not isinstance(value, str) or not value or value.startswith("<")


def _gate_chain_broken(gates: dict[str, str]) -> bool:
    """True when a downstream gate is approved while an upstream one is pending (the invariant /revise keeps)."""
    return bool(common.gate_chain_violations(gates))


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
            command="agentloop init --name <product>",
            kind="setup",
            reason="This checkout is still the raw template (template_mode / placeholder state.md); "
            "initialize it into a product first.",
            also=("/onboard",) if has_adopt_manifest else (),
        )
    # 2. A corrupt gate chain means the SSOT itself needs repair — do not guess a phase from it.
    if _gate_chain_broken(gates):
        return Recommendation(
            command="agentloop doctor",
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
            also=("agentloop revise",),
        )
    # 4. /verify must close every open escalation before presenting gate ⑤.
    if current_phase == "verify" and open_escalation_count > 0:
        return Recommendation(
            command='agentloop events --resolve <ID> --note "..."',
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
            command="agentloop cycle-close --name <slug>",
            kind="close",
            reason="All gates are approved; archive this delta cycle's deliverables and reset for the next one.",
        )
    # 7. Inside a phase: pending gate → run (or finish) that phase's command; approved → advance.
    gate = _PHASE_GATE.get(current_phase)
    if gate is not None:
        index = GATE_ORDER.index(gate) + 1
        if gates.get(gate) != "approved":
            also = ("agentloop build",) if current_phase == "build" else ()
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
                command="agentloop cycle-close --name <slug>",
                kind="close",
                reason="The release gate is approved; close the cycle.",
            )
        also = ("agentloop build",) if next_phase == "build" else ()
        return Recommendation(
            command=_PHASE_COMMAND[next_phase],
            kind="run_phase",
            reason=f"Gate {index} ({gate}) is approved; advance to the {next_phase} phase.",
            also=also,
        )
    # Unknown phase value — the SSOT is off-vocabulary; diagnose instead of guessing.
    return Recommendation(
        command="agentloop doctor",
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
                "test": t.test,
            }
            for t in sorted(graph.tasks, key=lambda t: t.id)
        ],
    }


def _read_optional(path: Path) -> str | None:
    """Return the file text, or None for any unreadable case (absent/dir/permission) — used to skip a panel."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _trace_block(root: Path, graph: dag.Graph) -> dict[str, object] | None:
    """Requirement → design → task coverage for the dashboard, reusing dag.trace (no new logic).

    Reads docs/10-requirements.md (and docs/20-design.md when present) tolerantly. Returns None when the
    requirements document is absent or carries no R-N/NFR-N ids (nothing to show yet). The compact shape
    keeps the deterministic TraceReport but drops what a glance doesn't need.
    """
    req_text = _read_optional(root / "docs" / "10-requirements.md")
    if req_text is None:
        return None
    requirement_ids = dag.parse_requirement_ids(req_text)
    if not requirement_ids:
        return None
    design_text = _read_optional(root / "docs" / "20-design.md")
    design_ids = dag.parse_requirement_ids(design_text) if design_text is not None else None
    report = dag.trace(graph, requirement_ids, design_ids, None)

    missing_design = set(report.requirements_missing_design) | set(report.nfrs_missing_design)
    coverage = {**report.req_to_tasks, **report.nfr_to_tasks}
    requirements = [
        {
            "id": rid,
            "nfr": dag.is_nfr(rid),
            "design": (None if not report.design_checked else (rid not in missing_design)),
            "tasks": coverage.get(rid, []),
        }
        for rid in requirement_ids
    ]
    findings = [f"{r}: no task covering it" for r in report.uncovered_requirements]
    findings += [f"{r}: no design section" for r in report.requirements_missing_design]
    findings += [f"design references unknown {d}" for d in report.unknown_in_design]
    findings += [f"{tid}: references unknown {r}" for tid, r in report.unknown_in_tasks]
    return {
        "requirements": requirements,
        "design_checked": report.design_checked,
        "findings": findings,
        "ok": report.ok,
    }


def _section_table(text: str, heading: str) -> list[list[str]]:
    """Extract the data rows of the first Markdown table under `## <heading>` (tolerant, best-effort).

    Drops the header row, the `|---|` separator, and placeholder rows (`_(…)_`). Cells are trimmed.
    Any structural surprise yields fewer rows, never an exception — the dashboard tolerates a hand-edited
    state.md the same way the escalation view does.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip().startswith("## ") and heading in ln)
    except StopIteration:
        return []
    rows: list[list[str]] = []
    seen_header = False
    for ln in lines[start + 1 :]:
        stripped = ln.strip()
        if stripped.startswith("## "):
            break  # next section
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not seen_header:  # the first table row is the column header
            seen_header = True
            continue
        if all(set(c) <= {"-", ":"} for c in cells):
            continue  # the |---|---| separator
        if any("_(" in c for c in cells):
            continue  # scaffold placeholder row
        rows.append(cells)
    return rows


def _state_body_logs(text: str | None) -> dict[str, list[list[str]]]:
    """The two hand-maintained state.md tables the dashboard mirrors: speculative work + roll-back history."""
    if text is None:
        return {"speculative": [], "rollback": []}
    return {
        "speculative": _section_table(text, "Speculative work log"),
        "rollback": _section_table(text, "Roll-back (revision) log"),
    }


def collect_status(
    root: str | Path = ".",
    *,
    events_loader: Callable[[str], list[events_mod.Event]] = events_mod.load_events,
) -> dict[str, object]:
    """Assemble the whole status object from the SSOT under `root`. Never raises for a readable repo.

    `events_loader` is a seam for callers that already cache the parsed log: the dashboard polls this
    function every few seconds and would otherwise re-parse the whole events.ndjson each time.
    """
    root = Path(root)
    warnings: list[str] = []

    # state.md feeds both the front matter and the two body log tables — read it once for both.
    state_text = _read_optional(root / ".agentloop" / "state.md")
    front: dict[str, object] = {}
    if state_text is None:
        warnings.append("cannot read state.md front-matter: file is unreadable or absent")
    else:
        try:
            front = common.parse_frontmatter(state_text) or {}
        except yaml.YAMLError as exc:
            warnings.append(f"cannot read state.md front-matter: {exc}")
    gates = common.gates_of(front) or {}
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
    trace: dict[str, object] | None = None
    try:
        graph = dag.load(root / ".agentloop" / "tasks.yaml")
        tasks = _tasks_block(graph)
        counts = graph.counts()
        trace = _trace_block(root, graph)
    except OSError:
        pass  # no tasks.yaml yet (normal before /tasks)
    except (dag.DagError, yaml.YAMLError) as exc:
        warnings.append(f"tasks.yaml is inconsistent: {exc}")

    all_events = events_loader(str(root / ".agentloop" / "events.ndjson"))
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
    # A /-command only exists inside an agent whose surface was installed; recommending one
    # in a repo with no integration would send the user to a command their agent has never
    # heard of (the exact Troubleshooting item), so close the chain here.
    if recommendation.command.startswith("/") and _no_agent_surface(root):
        recommendation = dataclasses.replace(
            recommendation,
            reason=recommendation.reason
            + " (No agent surface is installed — run `agentloop install claude|copilot` first, then open a"
            " new session so the /-commands exist in your agent.)",
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
        "trace": trace,
        "logs": _state_body_logs(state_text),
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


def render_next(next_obj: dict[str, object]) -> str:
    """The recommendation as 2–3 human lines (`agentloop next`): command, why, and the also row if any."""
    lines = [f"next: {next_obj.get('command', '')}", f"  why: {next_obj.get('reason', '')}"]
    also = next_obj.get("also") or ()
    if isinstance(also, (list, tuple)) and also:
        lines.append(f"  also: {', '.join(str(a) for a in also)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="aggregate the SSOT into one JSON status object")
    parser.add_argument("--root", "--repo", dest="root", default="", help="repository root (default: discovered)")
    parser.add_argument("--json", action="store_true", help="compact one-line JSON (default: indented)")
    parser.add_argument(
        "--next",
        action="store_true",
        dest="next_only",
        help="print only the next recommended command (with --json: the recommendation object alone)",
    )
    args = parser.parse_args(argv)
    if not args.root:
        from agentloop import repo as repo_mod  # lazy: collect_status stays callable with a bare root

        try:
            args.root = str(repo_mod.get().root)
        except repo_mod.RepoNotFoundError:
            args.root = "."  # a repo with no .agentloop/ yet: collect_status reports setup guidance
    status = collect_status(args.root)
    if args.next_only:
        next_obj = status["next"]
        assert isinstance(next_obj, dict)  # asdict(Recommendation) — always a dict
        print(json.dumps(next_obj, ensure_ascii=False) if args.json else render_next(next_obj))
        return 0
    print(json.dumps(status, ensure_ascii=False) if args.json else json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
