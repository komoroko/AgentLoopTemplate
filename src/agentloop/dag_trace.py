"""The traceability thread: requirement → claim → evidence → task → oracle.

0.8.x traced `R-N` headings scraped out of a markdown requirements document against a `req:`
string on each task. That caught dangling references and nothing else — a requirement could be
"covered" by a task whose only connection to it was a matching number in a free-text field.

0.9.0 traces the structure instead, and asks four questions the old thread could not:

  **Coverage**  does every requirement have a claim, and every claim a task?
  **Grounding** is every high/critical claim `grounded`, rather than `unknown`/`conflicted`?
  **Evidence**  is every evidence obligation satisfied?
  **Judgement** does every high/critical claim have an oracle that can actually fail?

A break in any of these is a gate ①–③ readiness failure, reported here rather than discovered
at gate ④ as a `missing` verdict nobody can explain. Requirement ids come from the plan's
claims (`requirement_ids`), never from scraped prose — a heading an AI invented is not a
requirement, and matching one against a claim would launder it into being one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agentloop import dag, models
from agentloop import repo as repo_mod

logger = logging.getLogger(__name__)


def is_nfr(requirement_id: str) -> bool:
    """True for a non-functional requirement (NFR-N).

    NFRs trace with softer *coverage* rules: many are cross-cutting and are demonstrated at
    `/verify` rather than owned by one task, so a missing claim is a warning. Grounding is not
    softened — "we could not find out" does not become acceptable because the requirement is
    non-functional.
    """
    return requirement_id.startswith("NFR-")


@dataclass
class TraceReport:
    """Everything the thread found. No errors and no warnings means the thread is whole."""

    requirements: list[str] = field(default_factory=list)
    claims_by_requirement: dict[str, list[str]] = field(default_factory=dict)
    tasks_by_claim: dict[str, list[str]] = field(default_factory=dict)

    requirements_without_claims: list[str] = field(default_factory=list)
    claims_without_tasks: list[str] = field(default_factory=list)
    ungrounded: list[tuple[str, str, str]] = field(default_factory=list)  # (id, risk, epistemic_status)
    unsatisfied_obligations: list[tuple[str, str]] = field(default_factory=list)  # (id, risk)
    claims_without_oracles: list[tuple[str, str]] = field(default_factory=list)  # (id, risk)
    oracles_without_negative_controls: list[tuple[str, str]] = field(default_factory=list)  # (id, risk)
    orphan_tasks: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        """Findings that block a gate."""
        problems: list[str] = []
        problems += [
            f"{rid}: no claim states what this requirement means"
            for rid in self.requirements_without_claims
            if not is_nfr(rid)
        ]
        problems += [
            f"{cid}: risk {risk}, epistemic_status '{status}' — a high/critical statement may not stay ungrounded"
            for cid, risk, status in self.ungrounded
        ]
        problems += [
            f"{oid}: evidence obligation unsatisfied at risk {risk}" for oid, risk in self.unsatisfied_obligations
        ]
        problems += [
            f"{cid}: risk {risk}, no oracle — a high/critical claim needs a judgement boundary "
            "the implementer did not write"
            for cid, risk in self.claims_without_oracles
        ]
        problems += [
            f"{oid}: risk {risk}, no negative control — an oracle that never fails proves nothing"
            for oid, risk in self.oracles_without_negative_controls
        ]
        problems += [f"{tid}: task answers for no claim" for tid in self.orphan_tasks]
        return problems

    @property
    def warnings(self) -> list[str]:
        problems = [
            f"{rid}: no claim yet (NFR — often demonstrated at /verify rather than owned by one task)"
            for rid in self.requirements_without_claims
            if is_nfr(rid)
        ]
        problems += [f"{cid}: no task is answerable for this claim" for cid in self.claims_without_tasks]
        return problems

    @property
    def ok(self) -> bool:
        return not self.errors


def trace(plan: models.Plan, graph: dag.Graph | None = None) -> TraceReport:
    """Follow the thread through `plan`. `graph` supplies the task side; None traces the plan alone."""
    report = TraceReport()
    tasks = graph.tasks if graph is not None else ()
    task_claims: dict[str, list[str]] = {}
    for task in tasks:
        for cid in task.claim_ids:
            task_claims.setdefault(cid, []).append(task.id)
    if graph is not None:
        report.orphan_tasks = sorted(t.id for t in tasks if not t.claim_ids)

    for claim in plan.claims:
        for rid in claim.requirement_ids:
            report.claims_by_requirement.setdefault(rid, []).append(claim.id)
        report.tasks_by_claim[claim.id] = sorted(task_claims.get(claim.id, []))

        if claim.epistemic_status != "grounded" and claim.risk in models.ELEVATED_RISKS:
            report.ungrounded.append((claim.id, claim.risk, claim.epistemic_status))
        if graph is not None and not report.tasks_by_claim[claim.id]:
            report.claims_without_tasks.append(claim.id)
        if claim.risk in models.ELEVATED_RISKS and not claim.oracle_ids:
            report.claims_without_oracles.append((claim.id, claim.risk))

    for fact in plan.technical_facts:
        if fact.epistemic_status != "grounded" and fact.risk in models.ELEVATED_RISKS:
            report.ungrounded.append((fact.id, fact.risk, fact.epistemic_status))

    for obligation in plan.obligations:
        if not obligation.satisfied:
            report.unsatisfied_obligations.append((obligation.id, obligation.risk))

    for oracle in plan.oracles:
        if oracle.requires_negative_control and not oracle.negative_controls:
            report.oracles_without_negative_controls.append((oracle.id, oracle.risk))

    report.requirements = sorted(report.claims_by_requirement)
    return report


def render_trace(report: TraceReport) -> str:
    """The thread as a human-facing report."""
    lines = ["### Traceability thread (requirement → claim → task / oracle)", ""]
    if report.requirements:
        lines.append("| Requirement | Claims | Tasks |")
        lines.append("|-------------|--------|-------|")
        for rid in report.requirements:
            claims = report.claims_by_requirement.get(rid, [])
            tasks = sorted({t for cid in claims for t in report.tasks_by_claim.get(cid, [])})
            lines.append(f"| {rid} | {', '.join(claims) or '-'} | {', '.join(tasks) or '-'} |")
    else:
        lines.append("- (no claim carries a requirement id yet)")
    lines.append("")

    errors, warnings = report.errors, report.warnings
    if errors:
        lines.append(f"### Blocking ({len(errors)})")
        lines += [f"- {e}" for e in errors]
        lines.append("")
    if warnings:
        lines.append(f"### Warnings ({len(warnings)})")
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    if not errors and not warnings:
        lines.append("The thread is whole: every requirement has a claim, every claim a task and its evidence.")
    return "\n".join(lines).rstrip()


def run(repo: repo_mod.Repo) -> int:
    """`agentloop dag --trace`: 0 when whole, 1 on a blocking break, 2 when there is no plan yet."""
    from agentloop import store as store_mod

    store = store_mod.Store(repo)
    try:
        plan = store.read_plan()
    except models.DocumentError as exc:
        logger.error(str(exc))
        return 1
    if plan is None:
        logger.warning(f"no plan at {repo.plan} yet — nothing to trace")
        return 2

    graph: dag.Graph | None
    try:
        graph = dag.join(plan, store.read_state())
    except (dag.DagError, models.DocumentError):
        graph = None  # trace the plan side even when the state cannot be joined

    report = trace(plan, graph)
    print(render_trace(report))
    return 0 if report.ok else 1
