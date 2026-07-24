"""Assemble the grounded machine review — the artefact gate ④ actually approves (plan §12, §17).

This is the orchestration the build loop hands off to and `agentloop review generate` runs. It is
deliberately thin over parts that already exist and are tested on their own: the deterministic
Coverage Manifest and risk floor (diff_facts), the frozen acceptance oracles (oracles), and the four
untrusted reviewer stages (actual_extraction → conformance → security_review → cold_maintainer), each
of which validates its own output against the never-lists in review_policy. What lives *here* is the
wiring and the schema-valid assembly into ``review.yaml``'s ``machine`` half, plus the two lifecycle
verbs the human loop needs: ``complete`` (freeze the human review once every blocker is clear) and
``show``.

Two boundaries are load-bearing:

- **The reviewer is injected.** ``generate`` takes a ``review_policy.Reviewer`` — a callable that
  turns a request into JSON — so the deterministic assembly is testable with a fake, and the CLI
  supplies the real adapter-backed one. The extractor is *never* handed the plan, the expected
  claims, or the implementer's explanation (actual_extraction enforces this); the comparator gets the
  Actual read-only and digest-bound.
- **The machine half is written whole and resets the human half.** Regenerating the review moves
  ``machine`` and therefore its digest, which is exactly what must invalidate every human answer and
  signature built on the previous one (plan §6.6, §17.5). ``complete`` only ever touches ``human``.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from agentloop import (
    actual_extraction,
    common,
    conformance,
    diff_facts,
    digests,
    event_chain,
    human_review,
    models,
    review_policy,
    security_review,
)
from agentloop import repo as repo_mod
from agentloop import store as store_mod

logger = logging.getLogger(__name__)

#: The SSOT artefacts are bound by their own digests, so the change under review is the tree with
#: them excluded — otherwise a review that writes review.yaml would invalidate itself (plan §17.3).
_CHANGE_EXCLUDE: tuple[str, ...] = (".agentloop/",)


class ReviewError(Exception):
    """A review could not be generated or completed — carries a human-readable reason."""


# -- deterministic digests over the committed tree ----------------------------


def change_digest(repo: repo_mod.Repo, commit: str) -> str:
    """The digest of the code under review at `commit`: the committed tree minus the SSOT dir."""
    rc, out = repo._git_rc("ls-tree", "-r", "-z", commit)
    if rc != 0:
        raise ReviewError(f"cannot read the tree at {commit}: {out.strip()}")
    entries = digests.filter_tree(digests.parse_ls_tree(out), exclude_prefixes=_CHANGE_EXCLUDE)
    return digests.tree_digest(entries)


def _diff(repo: repo_mod.Repo, base: str, head: str) -> str:
    rc, out = repo._git_rc("diff", f"{base}..{head}")
    if rc != 0:
        raise ReviewError(f"cannot diff {base}..{head}: {out.strip()}")
    return out


def _exists(repo: repo_mod.Repo, ref: str) -> bool:
    return bool(ref) and repo._git_rc("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] == 0


def _resolve_base(repo: repo_mod.Repo, plan: models.Plan | None, base: str | None) -> str:
    """The trusted base a review is taken against: an explicit arg, the plan's base, else a fallback.

    Each candidate is verified to exist in *this* repository before it is used — a plan carrying a
    base commit that is not present here (a fork, a shallow clone) falls back rather than failing the
    whole review on a `git diff` against a missing object.
    """
    if _exists(repo, base or ""):
        return base or ""
    if plan is not None and _exists(repo, plan.base_commit):
        return plan.base_commit
    for candidate in ("main", "master"):
        if _exists(repo, candidate):
            return repo._git_rc("rev-parse", candidate)[1].strip()
    rc, out = repo._git_rc("rev-parse", "HEAD")
    return out.strip() if rc == 0 else "HEAD"


# -- oracle conformance -------------------------------------------------------


def _run_oracles(
    repo: repo_mod.Repo,
    plan: models.Plan | None,
    config: models.Config | None,
    change: str,
    *,
    executor: Any = None,
) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    """Run every frozen oracle; return (oracle_id → passed) and their result bindings.

    An oracle whose bundle is not frozen, or with no executor available, is skipped rather than
    guessed — conformance for its claims stays `unknown`, never a fabricated pass (plan §9).
    """
    from agentloop import oracles as oracles_mod

    passed: dict[str, bool] = {}
    results: list[dict[str, Any]] = []
    if plan is None:
        return passed, results
    for oracle in plan.oracles:
        try:
            result = oracles_mod.run_oracle(repo, oracle, change_digest=change, config=config, executor=executor)
        except oracles_mod.OracleError as exc:
            logger.warning("oracle %s skipped: %s", oracle.id, exc)
            continue
        passed[oracle.id] = result.passed
        results.append({"oracle_id": oracle.id, "passed": result.passed, "result_digest": result.binding})
    return passed, results


# -- the expected model handed to the comparator ------------------------------


def _expected_model(plan: models.Plan | None) -> dict[str, Any]:
    """The plan's claims as the comparator's Expected — the only place the plan enters the pipeline."""
    if plan is None:
        return {"claims": []}
    return {
        "claims": [
            {
                "id": c.id,
                "statement": c.raw.get("statement", ""),
                "risk": c.risk,
                "source_ids": list(c.raw.get("source_ids", [])),
            }
            for c in plan.claims
        ]
    }


# -- assembly (pure, schema-valid) --------------------------------------------


def assemble(
    *,
    binding: Mapping[str, Any],
    coverage: Mapping[str, Any],
    actual_statements: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]] = (),
    extra_behaviors: Sequence[Mapping[str, Any]] = (),
    security: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose a schema-valid `machine` half from the validated pieces (plan §6.6).

    Every list is already the validated output of its own stage; this only shapes them and fills the
    summary counts. Keeping it pure is what lets a test assert the assembled shape without a model.
    """
    verdicts = [str(c.get("verdict", "unknown")) for c in claims]
    summary = {
        "claims_total": len(claims),
        "aligned": verdicts.count("aligned"),
        "diverged": verdicts.count("diverged"),
        "missing": verdicts.count("missing"),
        "unverified": verdicts.count("unverified"),
        "unknown": verdicts.count("unknown"),
    }
    machine: dict[str, Any] = {
        "status": "generated",
        "binding": dict(binding),
        "summary": summary,
        "coverage": [dict(coverage)],
        "actual_extraction": [dict(a) for a in actual_statements],
        "claims": [dict(c) for c in claims],
        "security": dict(security) if security is not None else {"findings": []},
    }
    if gaps:
        machine["gaps"] = [dict(g) for g in gaps]
    if extra_behaviors:
        machine["extra_behaviors"] = [dict(e) for e in extra_behaviors]
    return machine


# -- generation ---------------------------------------------------------------


def generate(
    repo: repo_mod.Repo,
    reviewer: review_policy.Reviewer,
    *,
    executor: Any = None,
    base: str | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """Run the whole pipeline and write `review.yaml`'s machine half; return the assembled machine.

    Deterministic pieces (coverage, oracles) run unconditionally; the reviewer stages are called and
    their *validated* outputs merged. The human half is reset to `not_started` — a fresh machine
    review is a fresh review, and no prior human answer speaks for it (plan §6.6).
    """
    store = store_mod.Store(repo)
    plan = store.read_plan()
    config = store.read_config()
    state = store.read_state()

    rc, head_out = repo._git_rc("rev-parse", "HEAD")
    if rc != 0:
        raise ReviewError("cannot resolve HEAD — is this a git repository with commits?")
    head = head_out.strip()
    trusted_base = _resolve_base(repo, plan, base)
    change = change_digest(repo, head)
    diff_text = _diff(repo, trusted_base, head)

    facts = diff_facts.analyze(diff_text)
    coverage = facts.coverage.to_manifest()
    risk_floor = facts.risk_floor

    oracle_passed, oracle_results = _run_oracles(repo, plan, config, change, executor=executor)

    # Blind actual extraction — the plan is deliberately absent from this request (§12.2).
    extract_request = actual_extraction.build_request(
        trusted_base_sha=trusted_base,
        subject_head_sha=head,
        diff_text=diff_text,
        relevant_code={},
        deterministic_facts={"coverage": coverage, "risk_floor": risk_floor},
    )
    extraction = actual_extraction.run_extractor(
        extract_request, reviewer, repo=repo, commit=head, risk_floor=risk_floor
    )

    # Expected vs Actual — the Actual arrives read-only and digest-bound (§12.3).
    compare_request = conformance.build_request(
        expected_model=_expected_model(plan),
        source_snapshots=[],
        actual_statements=extraction.actual_statements,
        actual_digest=extraction.actual_digest,
        oracle_results=oracle_results,
        obligation_status=[],
    )
    known_ids = _known_ids(plan, extraction.actual_statements)
    comparison = conformance.run_comparator(
        compare_request,
        reviewer,
        repo=repo,
        commit=head,
        actual_statement_ids=[str(a.get("id")) for a in extraction.actual_statements],
        known_ids=known_ids,
        source_authority=_source_authority(plan),
        oracle_passed=oracle_passed,
        effective_risk=risk_floor,
        independence=_independence(config),
    )

    security_request = security_review.build_request(
        diff_text=diff_text,
        relevant_code={},
        deterministic_facts={"signals": [h.signal for h in facts.signals]},
        trusted_base_sha=trusted_base,
        subject_head_sha=head,
    )
    security = security_review.run_security_review(security_request, reviewer, repo=repo, commit=head)

    binding = {
        "change_digest": change,
        "plan_digest": plan.digest() if plan is not None else digests.of({}),
        "config_digest": config.digest() if config is not None else digests.of({}),
        "toolchain_digest": _toolchain_digest(config),
        "coverage_digest": digests.of(coverage),
        "actual_digest": extraction.actual_digest,
        "trusted_base_sha": trusted_base,
        "subject_head_sha": head,
        "generated_at": event_chain.now_iso(),
    }
    machine = assemble(
        binding=binding,
        coverage=coverage,
        actual_statements=extraction.actual_statements,
        claims=comparison.claims,
        gaps=_coverage_gaps(comparison.actual_coverage_gaps),
        security=security.to_section(),
    )

    cycle = state.cycle_id if state else (plan.cycle_id if plan else "")
    document = {"machine": machine, "human": {"status": "not_started"}}
    with store.transaction() as tx:
        tx.write("review", document, expect_digest=tx.store.document_digest("review"))
        tx.append("coverage_generated", cycle_id=cycle, actor=actor)
        tx.append("actual_extraction_generated", cycle_id=cycle, actor=actor)
        tx.append("comparison_generated", cycle_id=cycle, actor=actor)
        tx.append("security_review_generated", cycle_id=cycle, actor=actor)
        tx.append("review_generated", cycle_id=cycle, actor=actor, detail={"change_digest": change})
    return machine


def _known_ids(plan: models.Plan | None, actual_statements: Sequence[Mapping[str, Any]]) -> list[str]:
    ids = [str(a.get("id")) for a in actual_statements]
    if plan is not None:
        ids += [c.id for c in plan.claims] + [s.id for s in plan.sources] + [o.id for o in plan.oracles]
    return ids


def _source_authority(plan: models.Plan | None) -> dict[str, str]:
    """source_id → authority class, so the comparator cannot promote a descriptive source (§12.3)."""
    if plan is None:
        return {}
    return {s.id: str(s.raw.get("authority_class", "descriptive")) for s in plan.sources}


def _independence(config: models.Config | None) -> dict[str, Any]:
    """The declared reviewer groups. A single-adapter environment reuses one group (doctor WARNs)."""
    group = "claude/opus"
    return {"actual_extractor": {"group": group}, "comparator": {"group": group}}


def _toolchain_digest(config: models.Config | None) -> str:
    if config is None:
        return digests.of({})
    profiles = config.raw.get("executors") if isinstance(config.raw, Mapping) else None
    return digests.of({"executors": profiles} if profiles is not None else {})


def _coverage_gaps(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Comparator-reported actual-coverage gaps, shaped as review.yaml gap records where possible."""
    out: list[dict[str, Any]] = []
    for index, gap in enumerate(gaps, start=1):
        out.append(
            {
                "id": str(gap.get("id", f"GAP-{index:03d}")),
                "kind": str(gap.get("kind", "actual_coverage_gap")),
                "statement_id": str(gap.get("statement_id", f"STMT-{index:03d}")),
                "risk": str(gap.get("risk", "medium")),
                "blocking": bool(gap.get("blocking", False)),
            }
        )
    return out


# -- lifecycle verbs ----------------------------------------------------------


def complete(repo: repo_mod.Repo, *, actor: str = "") -> None:
    """Freeze the human review, refusing while any completion blocker stands (plan §21.5)."""
    store = store_mod.Store(repo)
    review = store.read_review()
    if review is None or not review.is_generated:
        raise ReviewError("no machine review to complete — run `agentloop review generate` first")
    try:
        new_human = human_review.freeze(review, dict(review.human))
    except ValueError as exc:
        raise ReviewError(str(exc)) from None
    state = store.read_state()
    with store.transaction() as tx:
        tx.write("review", {**review.raw, "human": new_human}, expect_digest=tx.store.document_digest("review"))
        tx.append("human_review_frozen", cycle_id=state.cycle_id if state else "", actor=actor)


# -- CLI ----------------------------------------------------------------------


def _adapter_reviewer(repo: repo_mod.Repo) -> review_policy.Reviewer:
    """A production reviewer that shells the request as JSON to the configured agent adapter.

    Kept small on purpose: the request is written to the adapter on stdin and the adapter is expected
    to answer with the single JSON document the stage validators parse. Every stage revalidates the
    output, so this function is a transport, not a trust boundary.
    """
    from agentloop import build_loop

    config = store_mod.Store(repo).read_config()
    adapter = config.adapter("reviewer") or "claude" if config is not None else "claude"
    argv = build_loop.ADAPTERS.get(adapter)
    if argv is None:
        raise ReviewError(f"agents.reviewer.adapter is {adapter!r}, which this release cannot launch")

    def call(request: Mapping[str, Any]) -> str:
        rc, out = common.run([*argv, json.dumps(request, ensure_ascii=False)], timeout=900)
        if rc != 0:
            raise review_policy.ReviewPolicyError(f"the reviewer adapter exited {rc}")
        return out

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentloop review", description="the grounded machine review (gate ④)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="run the review pipeline and write review.yaml")
    sub.add_parser("complete", help="freeze the human review (all blockers must be clear)")
    sub.add_parser("show", help="print the current review.yaml")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(None)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    repo.require_supported_layout()

    try:
        if args.cmd == "generate":
            generate(repo, _adapter_reviewer(repo))
            print("review.yaml generated — review it in `agentloop ui`, then `agentloop review complete`")
            return 0
        if args.cmd == "complete":
            complete(repo)
            print("human review frozen — the gate ④ attestation request can now be built")
            return 0
        if args.cmd == "show":
            text = repo.review.read_text(encoding="utf-8") if repo.review.exists() else "(no review.yaml yet)"
            print(text)
            return 0
    except (ReviewError, review_policy.ReviewPolicyError, store_mod.StoreError, models.DocumentError) as exc:
        logger.error(str(exc))
        return 1
    return 0
