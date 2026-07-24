"""The reviewer orchestration: blind input contracts and untrusted-output validation (§30.9).

These cover the four LLM reviewers without an LLM — a fake reviewer returns a crafted string,
and the module either validates it or refuses it. The two things worth pinning:

  * the Actual Extractor and Cold Maintainer are handed inputs that provably exclude the
    Expected Model / private explanation (priming defense, E2E-19);
  * every reviewer's output is untrusted — a forged anchor, a self-granted verdict, a fabricated
    citation, a rewritten Actual, or a dropped block is refused.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from agentloop import (
    actual_extraction,
    cold_maintainer,
    conformance,
    plan_review,
    review_policy,
    security_review,
)
from agentloop import repo as repo_mod


def fake(payload: dict[str, Any]) -> review_policy.Reviewer:
    """A reviewer that ignores its request and returns a fixed JSON string."""

    def _reviewer(request: Mapping[str, Any]) -> str:
        return json.dumps(payload)

    return _reviewer


@pytest.fixture
def committed_repo(tmp_path: Path) -> repo_mod.Repo:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "t@e.x")
    git("config", "user.name", "T")
    git("add", "-A")
    git("commit", "-q", "-m", "c")
    return repo_mod.Repo(tmp_path)


def _blob(repo: repo_mod.Repo, path: str) -> str:
    return repo._git_rc("rev-parse", f"HEAD:{path}")[1].strip()


# --- actual extractor: blind input (plan §12.2, E2E-19) -----------------------


def test_extractor_request_excludes_the_expected_model() -> None:
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40,
        subject_head_sha="b" * 40,
        diff_text="diff",
        relevant_code={"src/app.py": "code"},
        deterministic_facts={"signals": []},
    )
    for forbidden in actual_extraction.FORBIDDEN_KEYS:
        assert forbidden not in request


def test_extractor_refuses_a_primed_request() -> None:
    with pytest.raises(actual_extraction.ExtractionError, match="Expected-Model"):
        actual_extraction.assert_blind({"diff": "x", "deterministic_facts": {"expected_claim": "should retry"}})


@pytest.mark.integration
def test_extractor_output_with_a_valid_anchor_is_accepted(committed_repo: repo_mod.Repo) -> None:
    blob = _blob(committed_repo, "src/app.py")
    reviewer = fake(
        {
            "actual_statements": [
                {
                    "id": "AST-001",
                    "statement": "reads the file",
                    "category": "io",
                    "confidence": "medium",
                    "code_anchors": [
                        {"path": "src/app.py", "start_line": 1, "end_line": 2, "blob": f"git-blob:{blob}"}
                    ],
                }
            ],
            "coverage": {"analyzed_files": 1},
        }
    )
    result = actual_extraction.run_extractor(
        actual_extraction.build_request(
            trusted_base_sha="a" * 40,
            subject_head_sha="b" * 40,
            diff_text="d",
            relevant_code={},
            deterministic_facts={},
        ),
        reviewer,
        repo=committed_repo,
        commit="HEAD",
    )
    assert result.actual_statements[0]["id"] == "AST-001"
    assert result.actual_digest.startswith("sha256:")


@pytest.mark.integration
def test_extractor_rejects_a_fabricated_anchor(committed_repo: repo_mod.Repo) -> None:
    reviewer = fake(
        {
            "actual_statements": [
                {
                    "id": "AST-001",
                    "statement": "x",
                    "category": "io",
                    "confidence": "high",
                    "code_anchors": [{"path": "src/ghost.py", "start_line": 1, "end_line": 1}],
                }
            ]
        }
    )
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40, subject_head_sha="b" * 40, diff_text="d", relevant_code={}, deterministic_facts={}
    )
    with pytest.raises(actual_extraction.ExtractionError, match="fabricated or stale"):
        actual_extraction.run_extractor(request, reviewer, repo=committed_repo, commit="HEAD")


@pytest.mark.integration
def test_extractor_rejects_a_self_granted_integrity(committed_repo: repo_mod.Repo) -> None:
    blob = _blob(committed_repo, "src/app.py")
    reviewer = fake(
        {
            "actual_statements": [
                {
                    "id": "AST-001",
                    "statement": "x",
                    "category": "io",
                    "confidence": "high",
                    "integrity": {"status": "verified"},
                    "code_anchors": [
                        {"path": "src/app.py", "start_line": 1, "end_line": 1, "blob": f"git-blob:{blob}"}
                    ],
                }
            ]
        }
    )
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40, subject_head_sha="b" * 40, diff_text="d", relevant_code={}, deterministic_facts={}
    )
    with pytest.raises(actual_extraction.ExtractionError, match="integrity"):
        actual_extraction.run_extractor(request, reviewer, repo=committed_repo, commit="HEAD")


def test_extractor_rejects_a_statement_without_an_anchor(committed_repo: repo_mod.Repo) -> None:
    reviewer = fake({"actual_statements": [{"id": "AST-1", "statement": "x", "category": "io", "confidence": "high"}]})
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40, subject_head_sha="b" * 40, diff_text="d", relevant_code={}, deterministic_facts={}
    )
    with pytest.raises(actual_extraction.ExtractionError, match="at least one code anchor"):
        actual_extraction.run_extractor(request, reviewer, repo=committed_repo, commit="HEAD")


# --- comparator: never-list and independence (plan §24.3, §12.4, E2E-26) ------

_DISTINCT = {"actual_extractor": {"group": "claude/opus"}, "comparator": {"group": "claude/sonnet"}}


def _comparator_request() -> dict[str, Any]:
    return conformance.build_request(
        expected_model={"claims": []},
        source_snapshots=[],
        actual_statements=[],
        actual_digest="sha256:" + "1" * 64,
        oracle_results=[],
        obligation_status=[],
    )


def test_comparator_rejects_a_same_group_critical_review() -> None:
    same = {"actual_extractor": {"group": "claude/opus"}, "comparator": {"group": "claude/opus"}}
    with pytest.raises(conformance.ComparatorError, match="not independent"):
        conformance.run_comparator(
            _comparator_request(),
            fake({"claims": []}),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=[],
            known_ids=[],
            source_authority={},
            oracle_passed={},
            effective_risk="critical",
            independence=same,
        )


def test_comparator_rejects_a_rewritten_actual() -> None:
    with pytest.raises(conformance.ComparatorError, match="rewrite or add Actual"):
        conformance.run_comparator(
            _comparator_request(),
            fake({"claims": [], "actual_statements": [{"id": "AST-9"}]}),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=[],
            known_ids=[],
            source_authority={},
            oracle_passed={},
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_comparator_rejects_an_invented_actual_reference() -> None:
    payload = {"claims": [{"claim_id": "C-001", "verdict": "aligned", "actual_statement_ids": ["AST-404"]}]}
    with pytest.raises(conformance.ComparatorError, match="never produced"):
        conformance.run_comparator(
            _comparator_request(),
            fake(payload),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=["AST-001"],
            known_ids=["C-001"],
            source_authority={},
            oracle_passed={},
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_comparator_rejects_descriptive_source_as_normative() -> None:
    payload = {"claims": [{"claim_id": "C-001", "verdict": "aligned", "expected": {"source_ids": ["SRC-001"]}}]}
    with pytest.raises(conformance.ComparatorError, match="non-normative"):
        conformance.run_comparator(
            _comparator_request(),
            fake(payload),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=[],
            known_ids=["C-001", "SRC-001"],
            source_authority={"SRC-001": "descriptive"},
            oracle_passed={},
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_comparator_rejects_overturning_a_failing_oracle() -> None:
    payload = {"claims": [{"claim_id": "C-001", "verdict": "aligned", "conformance": {"oracle_ids": ["O-001"]}}]}
    with pytest.raises(conformance.ComparatorError, match="cannot be overturned"):
        conformance.run_comparator(
            _comparator_request(),
            fake(payload),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=[],
            known_ids=["C-001", "O-001"],
            source_authority={},
            oracle_passed={"O-001": False},
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_comparator_accepts_a_clean_comparison() -> None:
    payload = {"claims": [{"claim_id": "C-001", "verdict": "aligned", "conformance": {"oracle_ids": ["O-001"]}}]}
    result = conformance.run_comparator(
        _comparator_request(),
        fake(payload),
        repo=repo_mod.Repo(Path("/x")),
        commit="HEAD",
        actual_statement_ids=[],
        known_ids=["C-001", "O-001"],
        source_authority={},
        oracle_passed={"O-001": True},
        effective_risk="high",
        independence=_DISTINCT,
    )
    assert result.claims[0]["claim_id"] == "C-001"


# --- security reviewer (plan §12.5) -------------------------------------------


def test_security_review_rejects_an_unknown_severity() -> None:
    payload = {"findings": [{"id": "SEC-1", "severity": "apocalyptic", "attack_scenario": "x", "blocking": True}]}
    with pytest.raises(security_review.SecurityReviewError, match="severity"):
        security_review.run_security_review({}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")


def test_security_review_refuses_to_drop_a_prior_blocking_finding() -> None:
    payload: dict[str, Any] = {"findings": []}
    with pytest.raises(security_review.SecurityReviewError, match="clear its own block"):
        security_review.run_security_review(
            {}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD", prior_blocking_ids=["SEC-1"]
        )


def test_security_review_accepts_a_well_formed_finding() -> None:
    payload = {
        "findings": [{"id": "SEC-1", "severity": "high", "attack_scenario": "reaches a host cred", "blocking": True}]
    }
    result = security_review.run_security_review({}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")
    assert len(result.blocking) == 1


# --- cold maintainer (plan §12.6, E2E-10) -------------------------------------


def test_cold_maintainer_request_excludes_priming() -> None:
    request = cold_maintainer.build_request(
        diff_text="d", permanent_docs={"docs/20-design.md": "text"}, subject_head_sha="a" * 40
    )
    for forbidden in cold_maintainer.FORBIDDEN_KEYS:
        assert forbidden not in request


def test_cold_maintainer_refuses_contaminated_input() -> None:
    with pytest.raises(cold_maintainer.ColdMaintainerError, match="priming"):
        cold_maintainer.assert_uncontaminated({"diff": "d", "permanent_docs": {"rationale": "because"}})


def test_cold_maintainer_gap_blocks_understandability() -> None:
    payload = {"answers": [], "gaps": [{"kind": "knowledge_gap", "question": "how does it fail"}]}
    result = cold_maintainer.run_cold_maintainer(
        cold_maintainer.build_request(diff_text="d", permanent_docs={}, subject_head_sha="a" * 40), fake(payload)
    )
    assert not result.understandable


def test_cold_maintainer_rejects_an_unknown_gap_kind() -> None:
    payload: dict[str, Any] = {"gaps": [{"kind": "vibes_gap"}]}
    with pytest.raises(cold_maintainer.ColdMaintainerError, match="gap kind"):
        cold_maintainer.run_cold_maintainer(
            cold_maintainer.build_request(diff_text="d", permanent_docs={}, subject_head_sha="a" * 40), fake(payload)
        )


# --- plan review (plan §12.1, E2E-06, E2E-07) ---------------------------------


def test_plan_review_flags_an_authority_gap() -> None:
    payload = {"findings": [{"subject_id": "C-001", "verdict": "supported", "source_ids": ["SRC-001"]}]}
    with pytest.raises(plan_review.PlanReviewError, match="authority gap"):
        plan_review.run_plan_review(
            {}, fake(payload), known_ids=["C-001", "SRC-001"], source_authority={"SRC-001": "descriptive"}
        )


def test_plan_review_rejects_a_fabricated_source() -> None:
    payload = {"findings": [{"subject_id": "C-001", "verdict": "sound", "source_ids": ["SRC-999"]}]}
    with pytest.raises(plan_review.PlanReviewError, match="unknown id"):
        plan_review.run_plan_review({}, fake(payload), known_ids=["C-001"], source_authority={})


def test_plan_review_accepts_a_normative_support() -> None:
    payload = {"findings": [{"subject_id": "C-001", "verdict": "supported", "source_ids": ["SRC-001"]}]}
    result = plan_review.run_plan_review(
        {}, fake(payload), known_ids=["C-001", "SRC-001"], source_authority={"SRC-001": "normative"}
    )
    assert not result.blocking
