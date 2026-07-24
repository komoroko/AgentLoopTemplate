"""Evidence Obligations, provider search, and snapshots — the "is this actually grounded?" layer.

Two distinctions this module refuses to blur, because blurring either is how an AI's guess
gets recorded as a fact.

**Execution is not coverage** (plan §6.4). A search that ran and found nothing has *completed
execution* and satisfied *no coverage*. `no_match` means "we looked, there was nothing"; it is
not "there is nothing to find", and it is certainly not "the claim is settled". A provider that
could not be reached is `unavailable`, which is different again — and stays visible even when
an alternate path satisfies the obligation, because a hidden provider outage is exactly how
"no documentation exists" gets invented (plan §15.3).

**Risk alone does not generate an obligation** (plan §6.4). What a claim *needs* proven depends
on its decision class, its domains, and whether it touches an external dependency, a public
surface, persistence, or a side effect. A business policy needs an authorized human decision; a
technical fact never does; a critical external side effect needs a normative source *and* a
hermetic oracle *and* an expert. :func:`obligations_for` derives those, so the requirement to
find evidence is a property of the claim, not a number someone picked.

An obligation is satisfied when one of its *alternative* evidence paths is met — "official
source unavailable" is a reason to take another route, not an automatic block, as long as the
route was declared in advance. Snapshots are content-addressed in the cache
(`$XDG_CACHE_HOME/agentloop/<repo-id>/evidence/`) so the plan carries only a digest and an
opaque locator, and a source cannot be swapped for different bytes behind the same locator.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentloop import digests, models
from agentloop import repo as repo_mod
from agentloop import store as store_mod

# --- obligation derivation (the Policy Engine's rule set) ----------------------
#
# Each rule names a condition on a claim and the evidence alternatives that satisfy it. The
# rules are data, not scattered `if`s, so `doctor` and the gate can explain *why* a claim owes
# what it owes — "critical + external side effect" is a reason a human can check, "risk >= high"
# is not.


@dataclass(frozen=True)
class EvidencePath:
    """One way to satisfy an obligation. `requires` mirrors the plan schema's `alternatives`."""

    id: str
    requires: dict[str, str]


@dataclass(frozen=True)
class ObligationRule:
    """A derived requirement: a rule name, a risk floor, and the alternative evidence paths."""

    id: str
    rule: str
    risk: str
    alternatives: tuple[EvidencePath, ...]

    def to_dict(self, subject_ids: Sequence[str]) -> dict[str, object]:
        return {
            "id": self.id,
            "subject_ids": list(subject_ids),
            "rule": self.rule,
            "risk": self.risk,
            "alternatives": [{"id": p.id, "requires": p.requires} for p in self.alternatives],
            "execution_status": "pending",
            "coverage_status": "unsatisfied",
        }


#: Domain markers that raise the evidence bar. A claim in any of these touches something whose
#: misbehaviour is not an internal detail (plan §15.1).
_SECURITY_DOMAINS = frozenset({"security", "auth", "authz", "authn", "crypto", "sandbox"})
_EXTERNAL_DOMAINS = frozenset({"payment", "billing", "external", "vendor", "integration", "webhook"})
_PERSISTENCE_DOMAINS = frozenset({"persistence", "schema", "migration", "database", "storage"})


def _paths(*specs: tuple[str, dict[str, str]]) -> tuple[EvidencePath, ...]:
    return tuple(EvidencePath(id=name, requires=req) for name, req in specs)


def obligations_for(claim: models.Claim) -> list[ObligationRule]:
    """The evidence obligations a claim owes, derived from what it is — not from its risk alone.

    A claim can owe more than one (a critical payment claim owes both the external-side-effect
    obligation and, if it is a business policy, the human-decision one). Returning them all is
    what makes the requirement legible: each obligation names a condition a human can verify.
    """
    obligations: list[ObligationRule] = []
    domains = frozenset(claim.domains)
    security = bool(domains & _SECURITY_DOMAINS)
    external = bool(domains & _EXTERNAL_DOMAINS)
    persistence = bool(domains & _PERSISTENCE_DOMAINS)
    elevated = claim.risk in models.ELEVATED_RISKS
    index = claim.id.split("-")[-1]

    if claim.decision_class == "business_policy":
        obligations.append(
            ObligationRule(
                id=f"EO-POLICY-{index}",
                rule="business-policy-human-decision",
                risk=claim.risk,
                alternatives=_paths(("human-decision", {"source_class": "human_decision"})),
            )
        )

    if claim.decision_class == "technical_fact":
        # A technical fact is never settled by a human decision; it needs a source that says
        # how the thing actually works, or a reproducible experiment.
        obligations.append(
            ObligationRule(
                id=f"EO-FACT-{index}",
                rule="technical-fact-source",
                risk=claim.risk,
                alternatives=_paths(
                    ("official-source", {"source_class": "official_external_spec"}),
                    ("experiment", {"experiment": "reproducible"}),
                ),
            )
        )

    if security and elevated:
        obligations.append(
            ObligationRule(
                id=f"EO-SECURITY-{index}",
                rule="security-boundary",
                risk="critical",
                alternatives=_paths(
                    ("source-check-expert", {"source_class": "official_external_spec", "test": "unit"}),
                ),
            )
        )

    if external and claim.risk == "critical":
        obligations.append(
            ObligationRule(
                id=f"EO-EXTERNAL-{index}",
                rule="external-side-effect-critical",
                risk="critical",
                alternatives=_paths(
                    ("normative-oracle-expert", {"source_class": "official_external_spec", "oracle": "hermetic"}),
                    ("experiment-expert", {"experiment": "reproducible"}),
                ),
            )
        )

    if persistence and elevated:
        obligations.append(
            ObligationRule(
                id=f"EO-PERSIST-{index}",
                rule="persistence-schema",
                risk=claim.risk,
                alternatives=_paths(("schema-oracle", {"source_class": "internal_spec", "oracle": "conformance"})),
            )
        )

    if not obligations:
        # Even a low-risk internal claim owes *something*: a descriptive source plus a test.
        # A claim that owes no evidence at all is an opinion with an id (plan §16.2).
        obligations.append(
            ObligationRule(
                id=f"EO-BASE-{index}",
                rule="internal-low-risk",
                risk=claim.risk,
                alternatives=_paths(("descriptive-test", {"source_class": "repository_code", "test": "unit"})),
            )
        )
    return obligations


def obligations_for_plan(plan: models.Plan) -> list[dict[str, object]]:
    """Every obligation the plan's claims and technical facts owe, as plan-ready mappings.

    Deterministic and idempotent: `/req` and `/design` regenerate this and get the same ids, so
    a re-run does not fork the obligation set.
    """
    result: dict[str, dict[str, object]] = {}
    subjects: dict[str, list[str]] = {}
    for claim in plan.claims:
        for rule in obligations_for(claim):
            result.setdefault(rule.id, rule.to_dict([]))
            subjects.setdefault(rule.id, []).append(claim.id)
    for obligation_id, subject_ids in subjects.items():
        result[obligation_id]["subject_ids"] = sorted(set(subject_ids))
    return [result[k] for k in sorted(result)]


# --- coverage assessment -------------------------------------------------------


@dataclass(frozen=True)
class CoverageResult:
    """Whether an obligation is satisfied, and by which path — or why not."""

    obligation_id: str
    satisfied: bool
    satisfied_by_path: str = ""
    reason: str = ""


def assess_coverage(plan: models.Plan, obligation: models.EvidenceObligation) -> CoverageResult:
    """Is `obligation` met? Satisfied when one alternative path's requirements are all present.

    The check looks at what the plan actually contains — the authority class of the sources it
    cites, the oracles bound to its subject claims — never at a `coverage_status` field an AI
    could have written. The field is an assertion; this is the audit.
    """
    if not obligation.alternatives:
        # No declared path means the only way to satisfy it is a normative source directly
        # cited by every subject — the strict default when a plan omits its alternatives.
        return _assess_direct(plan, obligation)

    for alternative in obligation.alternatives:
        requires = alternative.get("requires")
        requires = requires if isinstance(requires, dict) else {}
        missing = _unmet_requirements(plan, obligation, requires)
        if not missing:
            return CoverageResult(obligation.id, True, satisfied_by_path=str(alternative.get("id", "")))
    return CoverageResult(
        obligation.id,
        False,
        reason="no declared evidence path is fully met by the plan's sources, oracles, and attestations",
    )


def _assess_direct(plan: models.Plan, obligation: models.EvidenceObligation) -> CoverageResult:
    for subject in plan.subjects_of(obligation.id):
        supporting = getattr(subject, "supporting_source_ids", ())
        if not any(plan.source(sid) and plan.source(sid).is_normative for sid in supporting):  # type: ignore[union-attr]
            return CoverageResult(obligation.id, False, reason=f"{subject.id} has no normative source supporting it")
    return CoverageResult(obligation.id, True, satisfied_by_path="direct-normative")


def _unmet_requirements(
    plan: models.Plan, obligation: models.EvidenceObligation, requires: dict[str, object]
) -> list[str]:
    """The requirements of one evidence path that the plan does not satisfy."""
    unmet: list[str] = []
    subjects = plan.subjects_of(obligation.id)

    source_class = requires.get("source_class")
    if source_class:
        if not _has_source_of_class(plan, subjects, str(source_class)):
            unmet.append(f"a {source_class} source cited by a subject")

    oracle_mode = requires.get("oracle")
    if oracle_mode and not _has_oracle(plan, subjects):
        unmet.append(f"a {oracle_mode} oracle bound to a subject claim")

    if requires.get("experiment") and not any(sid in obligation.satisfied_by for sid in _experiment_ids(plan)):
        unmet.append("a reproducible experiment receipt")

    expert = requires.get("expert_attestation")
    if expert and not _has_expert(obligation):
        unmet.append(f"an expert attestation ({expert})")
    return unmet


def _has_source_of_class(plan: models.Plan, subjects: Sequence[models.Element], source_class: str) -> bool:
    for subject in subjects:
        for sid in getattr(subject, "supporting_source_ids", ()):
            source = plan.source(sid)
            if source is not None and source.kind == source_class:
                return True
    return False


def _has_oracle(plan: models.Plan, subjects: Sequence[models.Element]) -> bool:
    return any(getattr(subject, "oracle_ids", ()) for subject in subjects)


def _experiment_ids(plan: models.Plan) -> frozenset[str]:
    return frozenset(s.id for s in plan.sources if s.kind == "experiment_receipt")


def _has_expert(obligation: models.EvidenceObligation) -> bool:
    return any(models.ID_PATTERNS["attestation"].match(sid) for sid in obligation.satisfied_by)


# --- provider search records ---------------------------------------------------


@dataclass(frozen=True)
class ProviderAttempt:
    """One provider's answer to one search. `result` distinguishes the three honest outcomes."""

    provider: str
    query: str
    result: str  # matched | no_match | unavailable
    source_ids: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "provider": self.provider,
            "query": self.query,
            "execution_status": "failed" if self.result == "unavailable" else "complete",
            "result": self.result,
        }
        if self.source_ids:
            entry["source_ids"] = list(self.source_ids)
        if self.reason:
            entry["reason"] = self.reason
        return entry


@dataclass(frozen=True)
class SearchRecord:
    """A completed evidence search across providers, as a plan-ready `searches[]` entry."""

    id: str
    obligation_ids: tuple[str, ...]
    purpose: str
    attempts: tuple[ProviderAttempt, ...] = field(default_factory=tuple)

    @property
    def coverage_status(self) -> str:
        # Sufficient only when at least one provider matched. Every provider returning
        # `no_match` is a complete search with insufficient coverage — a real, common state.
        return "sufficient" if any(a.result == "matched" for a in self.attempts) else "insufficient"

    @property
    def unavailable_providers(self) -> tuple[str, ...]:
        return tuple(a.provider for a in self.attempts if a.result == "unavailable")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "obligation_ids": list(self.obligation_ids),
            "purpose": self.purpose,
            "provider_attempts": [a.to_dict() for a in self.attempts],
            "execution_status": "complete",
            "coverage_status": self.coverage_status,
        }


# --- snapshots -----------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSnapshot:
    """A content-addressed snapshot of a source's bytes, held in the cache.

    The plan carries only the digest and an opaque `evidence://sha256/<digest>` locator, so a
    source cannot be swapped for different bytes behind the same human-facing locator (plan
    §8.4, E2E-27). High/critical sources keep their snapshot from the gate through post-build
    review, so what was cited is still what is checked.
    """

    digest: str
    media_type: str
    size_bytes: int
    cache_path: Path

    @property
    def locator(self) -> str:
        return f"evidence://sha256/{self.digest.removeprefix('sha256:')}"

    def to_plan_snapshot(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "cache_locator": self.locator,
        }


def store_snapshot(repo: repo_mod.Repo, content: bytes, *, media_type: str = "text/markdown") -> EvidenceSnapshot:
    """Write `content` into the evidence cache under its own digest, returning the snapshot.

    Content-addressed: writing the same bytes twice is a no-op that returns the same locator,
    and two different byte strings can never collide onto one locator.
    """
    digest = digests.of_bytes(content)
    directory = store_mod.cache_dir(repo) / "evidence" / digest.removeprefix("sha256:")
    store_mod.ensure_private_dir(directory.parent.parent)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "content"
    if not target.exists():
        store_mod.atomic_write(target, content, mode=0o600)
    return EvidenceSnapshot(digest=digest, media_type=media_type, size_bytes=len(content), cache_path=target)


def load_snapshot(repo: repo_mod.Repo, digest: str) -> bytes | None:
    """The cached bytes for a snapshot digest, or None when it is not (or no longer) cached.

    None is a real answer a caller must handle: a high/critical source whose snapshot is gone
    has an integrity failure, not a passing check (plan §15.4).
    """
    target = store_mod.cache_dir(repo) / "evidence" / digest.removeprefix("sha256:") / "content"
    try:
        content = target.read_bytes()
    except OSError:
        return None
    return content if digests.matches(digests.of_bytes(content), digest) else None


# --- the read-only CLI (`agentloop evidence obligations|coverage`) -------------


def _render_obligations(plan: models.Plan) -> str:
    lines = ["### Evidence obligations (derived from each claim, not from risk alone)", ""]
    for claim in plan.claims:
        rules = obligations_for(claim)
        owed = ", ".join(f"{r.rule} [{r.risk}]" for r in rules)
        lines.append(f"- {claim.id} ({claim.decision_class}, {claim.risk}): {owed}")
    return "\n".join(lines)


def _render_coverage(plan: models.Plan) -> tuple[str, bool]:
    lines = ["### Coverage (satisfied when one declared evidence path is fully met)", ""]
    all_satisfied = True
    for obligation in plan.obligations:
        result = assess_coverage(plan, obligation)
        if not result.satisfied:
            all_satisfied = False
        mark = f"satisfied via {result.satisfied_by_path}" if result.satisfied else f"UNSATISFIED — {result.reason}"
        lines.append(f"- {obligation.id} [{obligation.risk}]: {mark}")
        for provider in _unavailable_for(plan, obligation):
            # A provider outage stays visible even when an alternate path succeeded (plan §15.3).
            lines.append(f"    ! provider unavailable during search: {provider}")
    return "\n".join(lines), all_satisfied


def _unavailable_for(plan: models.Plan, obligation: models.EvidenceObligation) -> list[str]:
    out: list[str] = []
    for search in plan.searches:
        if obligation.id in search.obligation_ids:
            out.extend(search.unavailable_providers)
    return sorted(set(out))


def main(argv: list[str] | None = None) -> int:
    import argparse

    from agentloop import common
    from agentloop import repo as repo_mod_cli

    parser = argparse.ArgumentParser(prog="agentloop evidence", description="inspect evidence obligations and coverage")
    parser.add_argument("what", choices=["obligations", "coverage"], help="what to show")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod_cli.get(args.repo)
        repo.require_supported_layout()
    except (repo_mod_cli.RepoNotFoundError, repo_mod_cli.UnsupportedLayoutError) as exc:
        import logging

        logging.getLogger(__name__).error(str(exc))
        return 1

    plan = store_mod.Store(repo).read_plan()
    if plan is None:
        print("no plan yet — run /req first")
        return 1
    if args.what == "obligations":
        print(_render_obligations(plan))
        return 0
    rendered, ok = _render_coverage(plan)
    print(rendered)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
