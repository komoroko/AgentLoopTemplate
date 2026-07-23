"""Status aggregation: one JSON object, and one deterministic "what should I do next".

`/status`, `agentloop next`, and the dashboard all read this, so the answer to "where am I"
is computed once rather than narrated three times by three agents. :func:`next_action` is a
first-match decision table — the same state always yields the same recommendation, which is
what lets a human predict the tool instead of interviewing it.

Read-only, and **tolerant on purpose**: a missing plan (normal before `/tasks`), a half-edited
config, or a damaged audit chain must degrade to a warning rather than a crash. The dashboard
has to stay up precisely when the state is odd — that is when a human most needs to look at it.

The one thing tolerance never extends to is *reporting a problem as fine*. A damaged chain, a
broken gate ladder, and an unreadable state each get their own row near the top of the table,
so the recommendation is "diagnose this", never a phase command that would build on top of it.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from agentloop import common, dag, dag_trace, event_chain, models, strict_yaml
from agentloop import events as events_mod
from agentloop import lock as lock_mod
from agentloop import repo as repo_mod
from agentloop import store as store_mod

logger = logging.getLogger(__name__)

GATE_ORDER = models.GATE_ORDER
PHASE_ORDER = models.PHASE_ORDER

#: Phase → the gate its command presents for approval.
PHASE_GATE: dict[str, str] = {
    "requirements": "requirements",
    "design": "design",
    "tasks": "tasks",
    "build": "build",
    "verify": "release",
}
PHASE_COMMAND: dict[str, str] = {
    "requirements": "/req",
    "design": "/design",
    "tasks": "/tasks",
    "build": "/build",
    "verify": "/verify",
}
#: The phase each gate belongs to on the stepper (release is presented from /verify).
GATE_PHASE: dict[str, str] = {**{g: g for g in GATE_ORDER[:-1]}, "release": "verify"}

_PLACEHOLDER_HINTS = ("<enter", "product", "build/product")


@dataclass(frozen=True)
class Recommendation:
    """The next action, as a copy-able command plus a one-sentence why."""

    command: str
    kind: str  # run_phase | approve_gate | reconcile | resolve | setup | close | fix
    reason: str
    also: tuple[str, ...] = ()


def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and any(hint in value for hint in _PLACEHOLDER_HINTS)


def _no_agent_surface(repo: repo_mod.Repo) -> bool:
    """True when the lock is readable and records no installed agent surface."""
    try:
        data = lock_mod.read(repo.lock)
    except lock_mod.LockError:
        return False
    return data is not None and not (data.get("integrations") or {})


def next_action(
    *,
    current_phase: str,
    gates: dict[str, str],
    counts: dict[str, int] | None,
    attention_count: int,
    chain_defects: int,
    template_mode: bool,
    placeholders: bool,
    gate_chain_broken: bool,
    plan_missing: bool,
    unsandboxed_profiles: list[str],
) -> Recommendation:
    """The deterministic decision table (first match wins)."""
    # 1. The audit chain is the substrate every receipt binds. Nothing else matters until it is intact.
    if chain_defects:
        return Recommendation(
            command="agentloop events --verify",
            kind="fix",
            reason=f"The audit chain has {chain_defects} defect(s). No gate receipt can be issued against a "
            "damaged log — restore events.ndjson from git rather than rewriting it to agree.",
        )
    # 2. Not a product yet: the template must be initialized.
    if template_mode or placeholders:
        return Recommendation(
            command="agentloop init --name <product>",
            kind="setup",
            reason="This checkout is still the raw template (template_mode / placeholder state); "
            "initialize it into a product first.",
        )
    # 3. A broken gate ladder means an approval survived a roll back — repair, do not infer a phase from it.
    if gate_chain_broken:
        return Recommendation(
            command="agentloop doctor",
            kind="fix",
            reason="A downstream gate is approved while an upstream one is pending: an approval survived a "
            "roll back, so downstream work is standing on a decision that was withdrawn.",
        )
    # 4. needs-revision tasks park everything until the /tasks reconcile reclassifies them.
    if counts is not None and counts.get("needs-revision", 0) > 0:
        return Recommendation(
            command="/tasks",
            kind="reconcile",
            reason="needs-revision tasks exist; reconcile them (keep / modify / obsolete / new) and re-approve gate 3.",
            also=("agentloop dag --render",),
        )
    # 5. Sandboxing is a precondition for running anything, so it precedes the phase rows.
    if unsandboxed_profiles:
        return Recommendation(
            command="agentloop oci build --profile " + unsandboxed_profiles[0],
            kind="fix",
            reason="Profile(s) " + ", ".join(unsandboxed_profiles) + " run repository-derived code on the host. "
            "Build the packaged sandbox image and pin its digest in config.yaml.",
        )
    # 6. Events awaiting a human decision block the release gate.
    if current_phase == "verify" and attention_count:
        return Recommendation(
            command="agentloop events --summary",
            kind="resolve",
            reason=f"{attention_count} event(s) await a human decision; record a disposition for each before "
            "the gate 5 release decision.",
        )
    # 7. Before the lifecycle starts, the human writes the brief.
    if current_phase == "brief":
        return Recommendation(
            command="/req",
            kind="run_phase",
            reason="Fill docs/00-product-brief.md, then run /req to start the requirements phase (gate 1).",
        )
    # 8. Everything approved: the cycle is over.
    if current_phase == "done" or all(gates.get(g) == "approved" for g in GATE_ORDER):
        return Recommendation(
            command="agentloop cycle-close --name <slug>",
            kind="close",
            reason="All gates are approved; archive this cycle's deliverables and reset for the next one.",
        )
    if plan_missing and current_phase not in {"brief", "requirements"}:
        return Recommendation(
            command="agentloop doctor",
            kind="fix",
            reason=f"current_phase is '{current_phase}' but there is no .agentloop/plan.yaml to work from.",
        )
    # 9. Inside a phase: pending gate → finish that phase; approved → advance.
    gate = PHASE_GATE.get(current_phase)
    if gate is not None:
        index = GATE_ORDER.index(gate) + 1
        if gates.get(gate) != "approved":
            also: tuple[str, ...] = (f"agentloop approve {gate} --check",)
            if current_phase == "build":
                also = ("agentloop build", *also)
            return Recommendation(
                command=PHASE_COMMAND[current_phase],
                kind="run_phase",
                reason=f"Phase '{current_phase}' is in progress; it ends by presenting gate {index} for a "
                "signed approval.",
                also=also,
            )
        next_phase = PHASE_ORDER[PHASE_ORDER.index(current_phase) + 1]
        if next_phase == "done":
            return Recommendation(
                command="agentloop cycle-close --name <slug>",
                kind="close",
                reason="The release gate is approved; close the cycle.",
            )
        also = ("agentloop build",) if next_phase == "build" else ()
        return Recommendation(
            command=PHASE_COMMAND[next_phase],
            kind="run_phase",
            reason=f"Gate {index} ({gate}) is approved; advance to the {next_phase} phase.",
            also=also,
        )
    return Recommendation(
        command="agentloop doctor",
        kind="fix",
        reason=f"current_phase '{current_phase}' is not in the lifecycle vocabulary; diagnose the SSOT.",
    )


def _tasks_block(graph: dag.Graph) -> dict[str, object]:
    """The task-graph slice of the status object — every value derived, nothing stored."""
    fan = graph.fan_out()
    counts = graph.counts()
    return {
        "counts": {s: counts[s] for s in dag.STATUS_ORDER},
        "total": len(graph.tasks),
        "layers": graph.layers(),
        "critical_path": graph.critical_path(),
        "frontier": [
            {"id": t.id, "title": t.title, "kind": t.kind, "risk": t.risk, "fan_out": fan[t.id]}
            for t in graph.order_frontier()
        ],
        "rows": [
            {
                "id": t.id,
                "title": t.title,
                "kind": t.kind,
                "status": t.status,
                "risk": t.risk,
                "blocked_by": list(t.blocked_by),
                "claim_ids": list(t.claim_ids),
                "oracle_ids": list(t.oracle_ids),
                "fan_out": fan[t.id],
            }
            for t in graph.tasks
        ],
    }


def _plan_block(plan: models.Plan) -> dict[str, object]:
    """The evidence slice: how much of the plan is grounded, and what is still owed."""
    ungrounded = plan.ungrounded(floor="low")
    return {
        "cycle_id": plan.cycle_id,
        "digest": plan.digest(),
        "claims": len(plan.claims),
        "technical_facts": len(plan.technical_facts),
        "oracles": len(plan.oracles),
        "obligations": {
            "total": len(plan.obligations),
            "satisfied": sum(1 for o in plan.obligations if o.satisfied),
            "unsatisfied_high": [o.id for o in plan.unsatisfied_obligations(floor="high")],
        },
        "ungrounded": [{"id": e.id, "risk": getattr(e, "risk", "low")} for e in ungrounded],
        "unavailable_providers": sorted({p for s in plan.searches for p in s.unavailable_providers}),
    }


def _review_block(review: models.Review | None) -> dict[str, object]:
    """The review slice. Deliberately reports the three axes separately — never one `verified`."""
    if review is None or not review.is_generated:
        return {"status": "not_generated"}
    verdicts: dict[str, int] = {}
    for result in review.claim_results:
        verdict = str(result.get("verdict", "unknown"))
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    return {
        "status": "generated",
        "machine_digest": review.machine_digest(),
        "human_status": review.human_status,
        "verdicts": verdicts,
        # "sufficient" or "undeterminable" — never a count that reads as "we checked and found none".
        "coverage": "sufficient" if review.coverage_sufficient else "undeterminable",
        "extra_behaviors": len(review.extra_behaviors) if review.coverage_sufficient else None,
        "blocking_security": len(review.blocking_security_findings),
    }


def collect_status(
    root: str | Path | repo_mod.Repo = ".",
    *,
    events_scanner: Callable[[Path], tuple[list[models.Event], list[event_chain.ChainDefect]]] | None = None,
) -> dict[str, object]:
    """The whole status object for the repository at `root`. Never raises for a readable repo.

    `events_scanner` is a seam for the dashboard: /api/status is the always-on poll, and
    answering it means reading the *whole* chain (a root is a digest over every record).
    Without the seam that reparse happens every few seconds for a file that rarely changes.
    """
    repo = root if isinstance(root, repo_mod.Repo) else repo_mod.Repo(Path(root).resolve())
    warnings: list[str] = []
    store = store_mod.Store(repo)

    legacy = repo.legacy_markers()
    if legacy:
        return {
            "unsupported_layout": True,
            "legacy_markers": list(legacy),
            "next": asdict(
                Recommendation(
                    command="agentloop doctor --unsupported-layout",
                    kind="fix",
                    reason=repo_mod.UNSUPPORTED_LAYOUT_MESSAGE.replace("\n", " "),
                )
            ),
            "warnings": [repo_mod.UNSUPPORTED_LAYOUT_MESSAGE],
            "generated_at": event_chain.now_iso(),
        }

    state: models.State | None = None
    try:
        state = store.read_state()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read state.yaml: {exc}")
    if state is None and not warnings:
        warnings.append("no .agentloop/state.yaml yet")

    plan: models.Plan | None = None
    try:
        plan = store.read_plan()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read plan.yaml: {exc}")

    review: models.Review | None = None
    try:
        review = store.read_review()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read review.yaml: {exc}")

    config: models.Config | None = None
    try:
        config = store.read_config()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read config.yaml: {exc}")

    gates = {g: state.gate_status(g) for g in GATE_ORDER} if state else dict.fromkeys(GATE_ORDER, "pending")
    current_phase = state.current_phase if state else "brief"

    tasks_block: dict[str, object] | None = None
    counts: dict[str, int] | None = None
    trace_block: dict[str, object] | None = None
    if plan is not None:
        try:
            graph = dag.join(plan, state)
            tasks_block = _tasks_block(graph)
            counts = graph.counts()
            report = dag_trace.trace(plan, graph)
            trace_block = {"ok": report.ok, "errors": report.errors, "warnings": report.warnings}
        except dag.DagError as exc:
            warnings.append(f"the task graph is inconsistent: {exc}")

    events, defects = (events_scanner or event_chain.scan)(repo.events)
    if defects:
        warnings.append(f"the audit chain has {len(defects)} defect(s)")
    attention = [e for e in events if e.event in events_mod.ATTENTION_EVENTS]

    recommendation = next_action(
        current_phase=current_phase,
        gates=gates,
        counts=counts,
        attention_count=len(attention),
        chain_defects=len(defects),
        template_mode=config.template_mode if config else False,
        placeholders=_is_placeholder(state.project) if state else True,
        gate_chain_broken=bool(state.gate_chain_violations()) if state else False,
        plan_missing=plan is None,
        unsandboxed_profiles=config.unsandboxed_code_profiles() if config else [],
    )
    # A /-command only exists inside an agent whose surface was installed; recommending one in a
    # repo with no integration would send the user to a command their agent has never heard of.
    if recommendation.command.startswith("/") and _no_agent_surface(repo):
        recommendation = dataclasses.replace(
            recommendation,
            reason=recommendation.reason
            + " (No agent surface is installed — run `agentloop install claude|copilot`, then open a new"
            " session so the /-commands exist in your agent.)",
        )

    return {
        "project": state.project if state else None,
        "cycle_id": state.cycle_id if state else None,
        "branch": config.work_branch if config else None,
        "current_phase": current_phase,
        "updated_at": state.raw.get("updated_at") if state else None,
        "phase_order": list(PHASE_ORDER),
        "gates": [
            {
                "name": g,
                "status": gates[g],
                "index": i + 1,
                "phase": GATE_PHASE[g],
                "attestation_id": (state.gate_receipt(g) or {}).get("attestation_id") if state else None,
            }
            for i, g in enumerate(GATE_ORDER)
        ],
        "plan": _plan_block(plan) if plan is not None else None,
        "plan_status": state.plan_status if state else "draft",
        "review": _review_block(review),
        "tasks": tasks_block,
        "trace": trace_block,
        "template_mode": config.template_mode if config else False,
        "github_enabled": bool(config.github.get("enabled")) if config else False,
        "chain": {
            "root": event_chain.chain_root(events),
            "events": len(events),
            "defects": [str(d) for d in defects],
        },
        "attention": [
            {"seq": e.seq, "ts": e.ts, "event": e.event, "subject_ids": list(e.subject_ids)} for e in attention
        ],
        "next": asdict(recommendation),
        "warnings": warnings,
        "generated_at": event_chain.now_iso(),
    }


def render_next(next_obj: dict[str, object]) -> str:
    """The recommendation as 2–3 human lines (`agentloop next`)."""
    lines = [f"next: {next_obj.get('command', '')}", f"  why: {next_obj.get('reason', '')}"]
    also = next_obj.get("also") or ()
    if isinstance(also, (list, tuple)) and also:
        lines.append(f"  also: {', '.join(str(a) for a in also)}")
    return "\n".join(lines)


def render(status: dict[str, object]) -> str:
    """The human-facing board: where you are, what is approved, what is grounded, what is next."""
    if status.get("unsupported_layout"):
        return repo_mod.UNSUPPORTED_LAYOUT_MESSAGE

    lines = [
        f"project: {status.get('project')}   cycle: {status.get('cycle_id')}   "
        f"phase: {status.get('current_phase')}   plan: {status.get('plan_status')}",
        "",
        "### Gates",
    ]
    gates = status.get("gates")
    if isinstance(gates, list):
        for gate in gates:
            attestation = gate.get("attestation_id") or "-"
            lines.append(f"- {gate['index']}. {gate['name']}: {gate['status']}  (attestation: {attestation})")

    plan = status.get("plan")
    if isinstance(plan, dict):
        obligations = plan["obligations"]
        assert isinstance(obligations, dict)
        lines += [
            "",
            "### Evidence",
            f"- claims: {plan['claims']}   technical facts: {plan['technical_facts']}   oracles: {plan['oracles']}",
            f"- obligations satisfied: {obligations['satisfied']}/{obligations['total']}",
        ]
        ungrounded = plan["ungrounded"]
        assert isinstance(ungrounded, list)
        if ungrounded:
            lines.append("- ungrounded: " + ", ".join(f"{u['id']}({u['risk']})" for u in ungrounded))
        unavailable = plan["unavailable_providers"]
        assert isinstance(unavailable, list)
        if unavailable:
            lines.append("- providers unavailable during search: " + ", ".join(unavailable))

    review = status.get("review")
    if isinstance(review, dict) and review.get("status") == "generated":
        extras = review.get("extra_behaviors")
        extras_text = "undeterminable (coverage gap)" if extras is None else str(extras)
        lines += [
            "",
            "### Review",
            f"- coverage: {review['coverage']}   extra behaviours: {extras_text}",
            f"- verdicts: {review.get('verdicts')}   human review: {review.get('human_status')}",
        ]

    tasks = status.get("tasks")
    if isinstance(tasks, dict):
        counts = tasks["counts"]
        assert isinstance(counts, dict)
        lines += ["", "### Tasks", "- " + " / ".join(f"{k}={v}" for k, v in counts.items())]

    chain = status.get("chain")
    if isinstance(chain, dict):
        defects = chain["defects"]
        assert isinstance(defects, list)
        lines += ["", f"### Audit chain\n- {chain['events']} event(s), {len(defects)} defect(s)"]

    warnings = status.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines += ["", "### Warnings"] + [f"- {w}" for w in warnings]

    next_obj = status.get("next")
    if isinstance(next_obj, dict):
        lines += ["", render_next(next_obj)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the deterministic status object and next action")
    parser.add_argument("--json", action="store_true", help="print the whole status object as JSON")
    parser.add_argument("--next", action="store_true", help="print only the next recommended command")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    status = collect_status(repo)
    if args.next:
        # --next is the narrower request, so it wins when both are given: `agentloop next
        # --json` is what an integration calls for one machine-readable recommendation.
        next_obj = status.get("next")
        next_obj = next_obj if isinstance(next_obj, dict) else {}
        print(json.dumps(next_obj, ensure_ascii=False, default=str) if args.json else render_next(next_obj))
        return 0
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False, default=str))
        return 0
    print(render(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
