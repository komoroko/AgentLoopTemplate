"""The Cold Maintainer Reviewer (plan §12.6): can a stranger maintain this from the docs alone?

Run on high/critical changes. The reviewer is given the change and the *permanent
documentation* — and pointedly not the plan's grounding narrative, the Challenge answers, or
the implementer's explanation. The question is whether someone arriving cold, months later,
with only what is written down, could understand and safely change this code. If the only thing
that makes the change comprehensible is a conversation that is about to scroll out of history,
the code is not maintainable yet (plan §2.7).

The reviewer answers eight fixed questions (:data:`QUESTIONS`). An answer it cannot give from
the permanent docs is a `maintainability_gap` or a `knowledge_gap` — recorded, not glossed. A
gap that needs a doc update is fixed before gate 4, and the machine review is regenerated
against the final change digest so the approval covers the documentation too (plan §12.6).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agentloop import review_policy

#: The eight questions a cold maintainer must be able to answer from the permanent docs (§12.6).
QUESTIONS: tuple[str, ...] = (
    "where is the entry point",
    "what state changed",
    "what is the most important invariant",
    "how does it fail",
    "how is it rolled back",
    "where would the next change go",
    "which test or oracle guarantees it",
    "is it understandable from the permanent docs alone",
)

#: Keys that must never reach the cold maintainer — anything that explains the change outside
#: the permanent record (plan §12.6). The point is to test the docs, not a private briefing.
FORBIDDEN_KEYS = frozenset(
    {"plan", "grounding", "rationale", "challenge", "challenges", "challenge_answers", "implementer_explanation"}
)


class ColdMaintainerError(RuntimeError):
    """The cold-maintainer input was contaminated, or its output could not be trusted."""


def build_request(*, diff_text: str, permanent_docs: Mapping[str, str], subject_head_sha: str) -> dict[str, Any]:
    """The cold maintainer's input: the change and the permanent docs — nothing that primes it."""
    request = {
        "subject_head_sha": subject_head_sha,
        "diff": diff_text,
        "permanent_docs": {str(p): str(b) for p, b in permanent_docs.items()},
        "questions": list(QUESTIONS),
    }
    assert_uncontaminated(request)
    return request


def assert_uncontaminated(request: Mapping[str, Any]) -> None:
    found = sorted(_forbidden_in(request))
    if found:
        raise ColdMaintainerError(
            f"the cold-maintainer request carries priming keys {found} — the point is to test the "
            "permanent docs, not a private explanation (plan §12.6)"
        )


def _forbidden_in(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in FORBIDDEN_KEYS:
                found.add(str(key))
            found |= _forbidden_in(value)
    elif isinstance(node, list):
        for item in node:
            found |= _forbidden_in(item)
    return found


@dataclass(frozen=True)
class ColdMaintainerResult:
    """The validated answers and the gaps the maintainer could not close from the docs."""

    answers: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, Any], ...]

    @property
    def understandable(self) -> bool:
        """True only when the maintainer left no gap — a gap blocks gate 4 (plan §12.6, E2E-10)."""
        return not self.gaps


def run_cold_maintainer(request: Mapping[str, Any], reviewer: review_policy.Reviewer) -> ColdMaintainerResult:
    """Run the cold maintainer and validate its output (plan §12.6, §12.7)."""
    assert_uncontaminated(request)
    document = review_policy.parse_reviewer_output(reviewer(request), what="cold maintainer")

    answers_raw = document.get("answers")
    answers = [dict(a) for a in answers_raw if isinstance(a, Mapping)] if isinstance(answers_raw, list) else []

    gaps_raw = document.get("gaps")
    gaps: list[dict[str, Any]] = []
    for gap in gaps_raw if isinstance(gaps_raw, list) else []:
        if not isinstance(gap, Mapping):
            continue
        kind = str(gap.get("kind", ""))
        if kind not in {"maintainability_gap", "knowledge_gap"}:
            raise ColdMaintainerError(
                f"cold maintainer: gap kind {kind!r} must be maintainability_gap or knowledge_gap"
            )
        gaps.append(dict(gap))

    return ColdMaintainerResult(answers=tuple(answers), gaps=tuple(gaps))
