"""Independent review of the plan at gates ①–③ (plan §12.1): is the Expected Model itself sound?

Before any code exists, the plan can already be wrong in two ways this review is built to catch:

  Entailment Gap   a claim cites a source that does not actually support it — the citation is
                   real, the entailment is not (E2E-07). An affirming review of such a claim is
                   refused; the gap is surfaced for a human.
  Authority Gap    a claim rests only on descriptive/inferred sources — existing code or README
                   prose saying what the system *does*, dressed up as what it *should* do (E2E-06).

The reviewer's output is untrusted like every other (plan §12.7): it may only cite ids that
exist in the plan, it cannot lower a claim's risk, and it cannot self-report `integrity:
verified`. This module validates those and turns the reviewer's findings into gate blockers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentloop import models, review_policy

#: The gates this review runs at, and the plan sections each is responsible for.
GATE_SCOPE = {
    "requirements": "claims and their sources",
    "design": "solutions and decisions",
    "tasks": "tasks, oracles, and evidence obligations",
}

_AFFIRMING = frozenset({"sound", "supported", "aligned"})
_NON_NORMATIVE = models.AUTHORITY_CLASS_VALUES - models.NORMATIVE_AUTHORITY


class PlanReviewError(RuntimeError):
    """The plan review produced output that could not be trusted."""


def build_request(
    *, gate: str, plan: Mapping[str, Any], source_snapshots: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """The plan reviewer's input: the frozen plan and the verified source snapshots for `gate`."""
    if gate not in GATE_SCOPE:
        raise PlanReviewError(f"plan review runs at {sorted(GATE_SCOPE)}, not {gate!r}")
    return {
        "gate": gate,
        "scope": GATE_SCOPE[gate],
        "plan": dict(plan),
        "source_snapshots": [dict(s) for s in source_snapshots],
    }


@dataclass(frozen=True)
class PlanReviewResult:
    """The validated per-claim findings and the gaps that block the gate."""

    findings: tuple[dict[str, Any], ...]

    @property
    def gaps(self) -> tuple[dict[str, Any], ...]:
        return tuple(f for f in self.findings if f.get("kind") in {"entailment_gap", "authority_gap"})

    @property
    def blocking(self) -> bool:
        return bool(self.gaps)


def run_plan_review(
    request: Mapping[str, Any],
    reviewer: review_policy.Reviewer,
    *,
    known_ids: Iterable[str],
    source_authority: Mapping[str, str],
    effective_risk: str = "low",
) -> PlanReviewResult:
    """Run the plan reviewer and validate its findings (plan §12.1, §12.7)."""
    document = review_policy.parse_reviewer_output(reviewer(request), what="plan review")
    raw = document.get("findings")
    if not isinstance(raw, list):
        raise PlanReviewError("plan review: `findings` must be a list")

    known = set(known_ids)
    problems: list[str] = []
    findings: list[dict[str, Any]] = []
    for index, finding in enumerate(raw):
        if not isinstance(finding, Mapping):
            problems.append(f"findings[{index}] is not a mapping")
            continue
        problems += _validate_finding(
            finding, known=known, source_authority=source_authority, effective_risk=effective_risk
        )
        findings.append(dict(finding))

    if problems:
        raise PlanReviewError("plan review rejected:\n" + "\n".join(f"  - {p}" for p in problems))
    return PlanReviewResult(findings=tuple(findings))


def _validate_finding(
    finding: Mapping[str, Any],
    *,
    known: set[str],
    source_authority: Mapping[str, str],
    effective_risk: str,
) -> list[str]:
    problems: list[str] = []
    subject = str(finding.get("subject_id", "?"))
    citation_problems = review_policy.validate_citations(_cited(finding), known, what="plan review")
    problems += [f"{subject}: {p}" for p in citation_problems]
    problems += review_policy.reject_self_attestation(finding)
    if finding.get("risk") is not None:
        problems += review_policy.reject_risk_downgrade(str(finding.get("risk")), effective_risk, subject=subject)

    # An affirming verdict resting only on descriptive/inferred sources is an authority gap, not
    # a pass — the reviewer cannot certify a claim into normativity it never had (E2E-06).
    verdict = str(finding.get("verdict", ""))
    source_ids = _cited(finding)
    if verdict in _AFFIRMING and source_ids:
        authorities = [source_authority.get(sid, "") for sid in source_ids]
        if authorities and all(auth in _NON_NORMATIVE for auth in authorities):
            problems.append(
                f"{subject}: an affirming verdict rests only on non-normative sources — that is an "
                "authority gap, not support (E2E-06)"
            )
    return problems


def _cited(finding: Mapping[str, Any]) -> set[str]:
    value = finding.get("source_ids")
    return {str(v) for v in value} if isinstance(value, list) else set()
