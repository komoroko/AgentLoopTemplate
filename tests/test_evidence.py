"""Tests for evidence.py — obligations from what a claim is, coverage from what the plan holds.

Two distinctions the module must never blur: execution is not coverage (a search that ran and
found nothing is complete and unsatisfied), and risk alone does not generate an obligation
(what must be proven follows from the claim's decision class and domains). A test failing here
means an AI's guess could be recorded as a settled fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import digests, evidence, models
from agentloop import repo as repo_mod
from tests._support import make_claim, make_obligation, make_plan, make_source, seed_repo


def claim(**kwargs: object) -> models.Claim:
    return models.Claim(make_claim(**kwargs))  # type: ignore[arg-type]


# --- obligation derivation ----------------------------------------------------


def test_a_business_policy_owes_a_human_decision() -> None:
    rules = evidence.obligations_for(claim(claim_id="C-001"))  # default decision_class is business_policy
    assert any(r.rule == "business-policy-human-decision" for r in rules)


def test_a_technical_fact_owes_a_source_never_a_human_decision() -> None:
    fact = models.Claim({**make_claim("C-001"), "decision_class": "technical_fact"})
    rules = evidence.obligations_for(fact)
    assert any(r.rule == "technical-fact-source" for r in rules)
    assert not any(r.rule == "business-policy-human-decision" for r in rules)


def test_a_critical_external_claim_owes_the_external_side_effect_obligation() -> None:
    c = models.Claim({**make_claim("C-002", risk="critical"), "domains": ["payment"]})
    rules = {r.rule for r in evidence.obligations_for(c)}
    assert "external-side-effect-critical" in rules


def test_a_security_claim_owes_the_boundary_obligation_at_critical_risk() -> None:
    c = models.Claim({**make_claim("C-003", risk="high"), "domains": ["auth"]})
    external = next(r for r in evidence.obligations_for(c) if r.rule == "security-boundary")
    assert external.risk == "critical"  # a security boundary is treated as critical regardless


def test_a_claim_can_owe_more_than_one_obligation() -> None:
    # A critical payment business policy owes both the external obligation and the policy one.
    c = models.Claim({**make_claim("C-004", risk="critical"), "domains": ["payment"]})
    rules = {r.rule for r in evidence.obligations_for(c)}
    assert {"business-policy-human-decision", "external-side-effect-critical"} <= rules


def test_even_a_low_risk_claim_owes_something() -> None:
    """A claim that owes no evidence is an opinion with an id (plan §16.2)."""
    fact = models.Claim({**make_claim("C-005", risk="low"), "decision_class": "implementation_choice"})
    assert evidence.obligations_for(fact)


def test_derivation_is_deterministic_and_idempotent() -> None:
    plan = models.Plan(make_plan(claims=[make_claim("C-001"), make_claim("C-002", obligation_ids=["EO-x"])]))
    first = evidence.obligations_for_plan(plan)
    assert first == evidence.obligations_for_plan(plan)
    ids = [str(o["id"]) for o in first]
    assert ids == sorted(ids)


def test_a_derived_obligation_validates_against_the_plan_schema() -> None:
    plan = models.Plan(make_plan(claims=[make_claim("C-001", risk="critical", source_ids=["SRC-001"])]))
    obligations = evidence.obligations_for_plan(plan)
    candidate = {**make_plan(claims=[make_claim("C-001")]), "evidence_obligations": obligations}
    assert models.schema_errors(candidate, "plan") == []


# --- coverage assessment ------------------------------------------------------


def test_coverage_reads_the_plan_not_a_status_field() -> None:
    """A source with the wrong authority does not satisfy an obligation, whatever the
    obligation's `coverage_status` field claims."""
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001", source_ids=["SRC-001"])],
            sources=[make_source("SRC-001", authority="descriptive", kind="repository_code")],
            obligations=[
                {
                    "id": "EO-001",
                    "subject_ids": ["C-001"],
                    "rule": "business-policy-human-decision",
                    "risk": "high",
                    "alternatives": [{"id": "official", "requires": {"source_class": "official_external_spec"}}],
                    "execution_status": "complete",
                    "coverage_status": "satisfied",  # the plan claims satisfied; the audit disagrees
                }
            ],
        )
    )
    result = evidence.assess_coverage(plan, plan.obligations[0])
    assert not result.satisfied
    assert "no declared evidence path" in result.reason


def test_a_met_path_satisfies_the_obligation() -> None:
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001", source_ids=["SRC-001"])],
            sources=[make_source("SRC-001", kind="official_external_spec")],
            obligations=[
                {
                    "id": "EO-001",
                    "subject_ids": ["C-001"],
                    "rule": "technical-fact-source",
                    "risk": "high",
                    "alternatives": [{"id": "official", "requires": {"source_class": "official_external_spec"}}],
                    "execution_status": "complete",
                    "coverage_status": "satisfied",
                }
            ],
        )
    )
    result = evidence.assess_coverage(plan, plan.obligations[0])
    assert result.satisfied and result.satisfied_by_path == "official"


def test_an_oracle_requirement_needs_an_oracle_on_a_subject() -> None:
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001", source_ids=["SRC-001"], oracle_ids=["O-001"])],
            sources=[make_source("SRC-001", kind="official_external_spec")],
            oracles=[
                {
                    "id": "O-001",
                    "claim_ids": ["C-001"],
                    "risk": "critical",
                    "kind": "property_test",
                    "bundle": {
                        "root": ".agentloop/oracles/O-001",
                        "digest": "sha256:" + "1" * 64,
                        "git_blobs": [{"path": ".agentloop/oracles/O-001/o.yaml", "blob": "git-blob:" + "a" * 40}],
                    },
                    "runner": {"executor": "oci", "network_profile": "none"},
                    "command": ["pytest"],
                    "expected_exit_code": 0,
                    "negative_controls": [{"id": "NC-001-1", "subject_fixture": "bad", "expected_exit_code": 1}],
                }
            ],
            obligations=[
                {
                    "id": "EO-001",
                    "subject_ids": ["C-001"],
                    "rule": "external-side-effect-critical",
                    "risk": "critical",
                    "alternatives": [
                        {"id": "path", "requires": {"source_class": "official_external_spec", "oracle": "hermetic"}}
                    ],
                    "execution_status": "complete",
                    "coverage_status": "satisfied",
                }
            ],
        )
    )
    assert evidence.assess_coverage(plan, plan.obligations[0]).satisfied


# --- search records -----------------------------------------------------------


def test_all_no_match_is_a_complete_search_with_insufficient_coverage() -> None:
    """`no_match` means "we looked, nothing there" — not "there is nothing to find", and not
    "the claim is settled" (plan §6.4)."""
    record = evidence.SearchRecord(
        id="SEARCH-001",
        obligation_ids=("EO-001",),
        purpose="find the retry spec",
        attempts=(
            evidence.ProviderAttempt("repository", "retry", "no_match"),
            evidence.ProviderAttempt("vendor-docs", "retry", "no_match"),
        ),
    )
    entry = record.to_dict()
    assert entry["execution_status"] == "complete"
    assert entry["coverage_status"] == "insufficient"


def test_a_match_makes_coverage_sufficient() -> None:
    record = evidence.SearchRecord(
        id="SEARCH-002",
        obligation_ids=("EO-001",),
        purpose="x",
        attempts=(evidence.ProviderAttempt("vendor-docs", "q", "matched", source_ids=("SRC-001",)),),
    )
    assert record.coverage_status == "sufficient"


def test_an_unavailable_provider_stays_visible_and_is_execution_failed() -> None:
    """A provider outage stays visible even when an alternate path succeeded (plan §15.3)."""
    record = evidence.SearchRecord(
        id="SEARCH-003",
        obligation_ids=("EO-001",),
        purpose="x",
        attempts=(
            evidence.ProviderAttempt("vendor-docs", "q", "matched", source_ids=("SRC-001",)),
            evidence.ProviderAttempt("repository", "q", "unavailable", reason="digest mismatch"),
        ),
    )
    assert record.coverage_status == "sufficient"  # the match satisfied it
    assert record.unavailable_providers == ("repository",)  # …but the outage is still recorded
    attempts = record.to_dict()["provider_attempts"]
    assert isinstance(attempts, list)
    assert attempts[1]["execution_status"] == "failed"


def test_a_search_record_validates_in_a_plan() -> None:
    record = evidence.SearchRecord(
        id="SEARCH-001",
        obligation_ids=("EO-001",),
        purpose="find the retry spec",
        attempts=(evidence.ProviderAttempt("vendor-docs", "q", "matched", source_ids=("SRC-001",)),),
    )
    # make_plan's default obligation is EO-001 and its default source SRC-001, so every
    # reference in the search record resolves.
    candidate = {**make_plan(), "searches": [record.to_dict()]}
    assert models.schema_errors(candidate, "plan") == []


# --- snapshots ----------------------------------------------------------------


def repo_at(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path)
    return repo_mod.Repo(tmp_path)


def test_a_snapshot_is_content_addressed(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    a = evidence.store_snapshot(repo, b"the retry spec")
    b = evidence.store_snapshot(repo, b"the retry spec")
    assert a.digest == b.digest
    assert a.locator == b.locator
    assert a.locator.startswith("evidence://sha256/")


def test_different_bytes_never_collide(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    assert evidence.store_snapshot(repo, b"one").digest != evidence.store_snapshot(repo, b"two").digest


def test_a_snapshot_round_trips(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    snapshot = evidence.store_snapshot(repo, b"content", media_type="text/markdown")
    assert evidence.load_snapshot(repo, snapshot.digest) == b"content"


def test_an_absent_snapshot_loads_as_none(tmp_path: Path) -> None:
    """A high/critical source whose snapshot is gone is an integrity failure, not a pass."""
    repo = repo_at(tmp_path)
    assert evidence.load_snapshot(repo, "sha256:" + "0" * 64) is None


def test_a_tampered_snapshot_loads_as_none(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    snapshot = evidence.store_snapshot(repo, b"original")
    snapshot.cache_path.write_bytes(b"swapped for other bytes")
    assert evidence.load_snapshot(repo, snapshot.digest) is None


def test_the_plan_snapshot_carries_a_digest_and_an_opaque_locator(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    entry = evidence.store_snapshot(repo, b"vendor doc body").to_plan_snapshot()
    assert digests.is_digest(entry["digest"])
    assert str(entry["cache_locator"]).startswith("evidence://sha256/")
    assert "vendor doc body" not in str(entry)  # the bytes are not in the plan


# --- the read-only CLI --------------------------------------------------------


def test_obligations_cli_lists_what_each_claim_owes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path, plan=make_plan(claims=[make_claim("C-001", risk="critical")]))
    assert evidence.main(["obligations", "--repo", str(tmp_path)]) == 0
    assert "C-001" in capsys.readouterr().out


def test_coverage_cli_exits_nonzero_when_something_is_unsatisfied(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_repo(
        tmp_path,
        plan=make_plan(
            claims=[make_claim("C-001", source_ids=["SRC-001"])],
            sources=[make_source("SRC-001", authority="descriptive", kind="repository_code")],
            obligations=[make_obligation("EO-001", satisfied=False)],
        ),
    )
    assert evidence.main(["coverage", "--repo", str(tmp_path)]) == 1
    assert "UNSATISFIED" in capsys.readouterr().out
