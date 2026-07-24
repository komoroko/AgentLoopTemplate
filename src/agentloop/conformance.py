"""The Conformance Comparator: does the Actual match the Expected? (plan §12.3)

The Comparator is the second, independent half of the review. It sees the frozen Expected
Model, the verified Source snapshots, the Actual Statements (already digest-bound by the blind
extractor), the Oracle results, and the Evidence Obligation status — and it decides, per claim,
whether the code does what the plan said it would.

What it may *not* do is the point (plan §24.3). It cannot:

  - rewrite or add an Actual Statement — the Actual is the extractor's, referenced read-only,
    and a gap in it is an `actual_coverage_gap`, not something the Comparator fills in;
  - fabricate a Source or Oracle id — every citation must resolve in the frozen plan;
  - promote a descriptive source to normative authority (existing code says what the system
    *does*, never what it *should* do — E2E-06);
  - call a machine assessment an expert attestation;
  - overturn an Oracle failure with prose;
  - fill an Unknown with natural language.

For a *critical* change the Comparator and the extractor must be in distinct independence
groups (plan §12.4): one model session answering both halves is not a second opinion (E2E-26).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from agentloop import digests, models, review_policy
from agentloop import repo as repo_mod

#: Aligned/verified verdicts — a claim the Comparator says the code *does* satisfy.
_AFFIRMING_VERDICTS = frozenset({"aligned", "verified"})

#: Authorities that describe existing behavior rather than specify required behavior (plan §6.2,
#: E2E-06). An affirming verdict resting only on these is an Authority Gap, not a pass.
_NON_NORMATIVE = models.AUTHORITY_CLASS_VALUES - models.NORMATIVE_AUTHORITY


class ComparatorError(RuntimeError):
    """The Comparator was not independent, or produced output that could not be trusted."""


def build_request(
    *,
    expected_model: Mapping[str, Any],
    source_snapshots: Iterable[Mapping[str, Any]],
    actual_statements: Iterable[Mapping[str, Any]],
    actual_digest: str,
    oracle_results: Iterable[Mapping[str, Any]],
    obligation_status: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble the Comparator's input (plan §12.3). The Actual arrives read-only, digest-bound."""
    return {
        "expected_model": dict(expected_model),
        "source_snapshots": [dict(s) for s in source_snapshots],
        "actual_statements": [dict(a) for a in actual_statements],
        "actual_digest": actual_digest,
        "oracle_results": [dict(o) for o in oracle_results],
        "obligation_status": [dict(o) for o in obligation_status],
    }


@dataclass(frozen=True)
class ComparatorResult:
    """The validated per-claim comparison and the actual-coverage gaps the Comparator found."""

    claims: tuple[dict[str, Any], ...]
    actual_coverage_gaps: tuple[dict[str, Any], ...]


def run_comparator(
    request: Mapping[str, Any],
    reviewer: review_policy.Reviewer,
    *,
    repo: repo_mod.Repo,
    commit: str,
    actual_statement_ids: Iterable[str],
    known_ids: Iterable[str],
    source_authority: Mapping[str, str],
    oracle_passed: Mapping[str, bool],
    effective_risk: str,
    independence: Mapping[str, Any],
) -> ComparatorResult:
    """Run the Comparator and validate it against the never-list (plan §24.3) and independence."""
    ok, message = review_policy.independence_ok(independence, effective_risk)
    if not ok:
        raise ComparatorError(message)

    document = review_policy.parse_reviewer_output(reviewer(request), what="conformance")

    # §24.3: the Comparator must not rewrite the Actual. It references statements read-only.
    for forbidden in ("actual_statements", "actual_extraction"):
        if forbidden in document:
            raise ComparatorError(f"the Comparator returned `{forbidden}` — it cannot rewrite or add Actual Statements")
    if str(document.get("actual_digest", actual_digest_of(request))) != actual_digest_of(request):
        raise ComparatorError("the Comparator's actual_digest does not match the extraction it was given")

    actual_ids = set(actual_statement_ids)
    known = set(known_ids)
    problems: list[str] = []
    claims: list[dict[str, Any]] = []
    raw_claims = document.get("claims")
    if not isinstance(raw_claims, list):
        raise ComparatorError("conformance: `claims` must be a list")
    for index, claim in enumerate(raw_claims):
        if not isinstance(claim, Mapping):
            problems.append(f"claims[{index}] is not a mapping")
            continue
        problems += _validate_claim(
            claim,
            actual_ids=actual_ids,
            known=known,
            source_authority=source_authority,
            oracle_passed=oracle_passed,
            effective_risk=effective_risk,
        )
        claims.append(dict(claim))

    gaps_raw = document.get("actual_coverage_gaps")
    gaps = [dict(g) for g in gaps_raw if isinstance(g, Mapping)] if isinstance(gaps_raw, list) else []

    if problems:
        raise ComparatorError("conformance rejected:\n" + "\n".join(f"  - {p}" for p in problems))
    return ComparatorResult(claims=tuple(claims), actual_coverage_gaps=tuple(gaps))


def actual_digest_of(request: Mapping[str, Any]) -> str:
    return str(request.get("actual_digest", ""))


def _validate_claim(
    claim: Mapping[str, Any],
    *,
    actual_ids: set[str],
    known: set[str],
    source_authority: Mapping[str, str],
    oracle_passed: Mapping[str, bool],
    effective_risk: str,
) -> list[str]:
    problems: list[str] = []
    cid = str(claim.get("claim_id", "?"))
    verdict = str(claim.get("verdict", ""))

    # The Comparator references the extractor's statements read-only — it cannot invent one.
    referenced_actual = _string_list(claim.get("actual_statement_ids"))
    for aid in referenced_actual:
        if aid not in actual_ids:
            problems.append(f"{cid}: references Actual Statement {aid!r}, which the extractor never produced")

    # §12.7 / §24.3: no fabricated Source or Oracle citations.
    cited = _cited_ids(claim)
    problems += [f"{cid}: {p}" for p in review_policy.validate_citations(cited, known, what="conformance")]

    # §24.2: the Comparator cannot self-report `integrity: verified`.
    problems += review_policy.reject_self_attestation(claim)

    # §24.3: an affirming verdict cannot rest on a descriptive/inferred source as if it were
    # normative — existing code describes, it does not specify (E2E-06).
    if verdict in _AFFIRMING_VERDICTS:
        problems += _reject_descriptive_as_normative(cid, claim, source_authority)
        problems += _reject_oracle_override(cid, claim, oracle_passed)

    # §24.3: a machine assessment is not an expert attestation.
    semantic = claim.get("semantic_support")
    if isinstance(semantic, Mapping):
        basis = str(semantic.get("assessment_basis", ""))
        if basis == "expert_attestation" and not _string_list(semantic.get("expert_attestation_ids")):
            problems.append(
                f"{cid}: claims an expert_attestation basis but cites no attestation "
                "— a machine assessment is not an expert"
            )

    # An AI cannot lower the change's risk below its effective floor.
    if claim.get("risk") is not None:
        problems += review_policy.reject_risk_downgrade(str(claim.get("risk")), effective_risk, subject=cid)
    return problems


def _reject_descriptive_as_normative(
    cid: str, claim: Mapping[str, Any], source_authority: Mapping[str, str]
) -> list[str]:
    expected = claim.get("expected")
    source_ids = _string_list(expected.get("source_ids")) if isinstance(expected, Mapping) else []
    if not source_ids:
        return []
    authorities = {sid: source_authority.get(sid, "") for sid in source_ids}
    if all(auth in _NON_NORMATIVE for auth in authorities.values()):
        return [
            f"{cid}: an affirming verdict rests only on non-normative sources {sorted(authorities)} "
            "(descriptive/inferred) — existing behavior is not a specification (E2E-06)"
        ]
    return []


def _reject_oracle_override(cid: str, claim: Mapping[str, Any], oracle_passed: Mapping[str, bool]) -> list[str]:
    conformance = claim.get("conformance")
    oracle_ids = _string_list(conformance.get("oracle_ids")) if isinstance(conformance, Mapping) else []
    failed = [oid for oid in oracle_ids if oracle_passed.get(oid) is False]
    if failed:
        return [
            f"{cid}: an affirming verdict cites failing oracle(s) {failed} "
            "— a failure cannot be overturned with prose"
        ]
    return []


def _cited_ids(claim: Mapping[str, Any]) -> set[str]:
    cited: set[str] = set()
    expected = claim.get("expected")
    if isinstance(expected, Mapping):
        cited |= set(_string_list(expected.get("source_ids")))
    conformance = claim.get("conformance")
    if isinstance(conformance, Mapping):
        cited |= set(_string_list(conformance.get("oracle_ids")))
    return cited


def _string_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def digest_result(result: ComparatorResult) -> str:
    """A digest over the validated comparison, for binding into the machine review."""
    return digests.of({"claims": list(result.claims), "actual_coverage_gaps": list(result.actual_coverage_gaps)})
