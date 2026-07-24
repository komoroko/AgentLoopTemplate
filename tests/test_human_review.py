"""The human review is a decision procedure, so every rule here is a fixed fact (plan §14, §30).

These tests never open a browser: they build a `review.yaml` in memory and pin the sequence gate,
the challenge/mismatch/counterfactual chain, the expertise routing (E2E-05), the budget block
(E2E-30), and the machine-digest staleness that refuses a raced write (E2E-08) while leaving a
human-only update non-staling (E2E-09).
"""

from __future__ import annotations

from typing import Any

import pytest

from agentloop import human_review, models


def _review(*, machine: dict[str, Any] | None = None, human: dict[str, Any] | None = None) -> models.Review:
    base_machine: dict[str, Any] = {
        "status": "generated",
        "binding": {
            "change_digest": "sha256:" + "a" * 64,
            "plan_digest": "sha256:" + "b" * 64,
            "toolchain_digest": "sha256:" + "c" * 64,
        },
        "coverage": [
            {
                "diff_digest": "sha256:" + "d" * 64,
                "analyzed_files": 1,
                "truncated": False,
                "coverage_status": "sufficient",
            }
        ],
        "actual_extraction": [],
        "claims": [],
    }
    base_machine.update(machine or {})
    return models.Review({"machine": base_machine, "human": human or {"status": "not_started"}})


def _challenge(cid: str, expected: str, *, risk: str = "high", claim_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": cid,
        "risk": risk,
        "claim_ids": claim_ids or [],
        "scenario": "the remote committed, the response was lost, the client retried",
        "choices": [{"id": "A", "text": "double-charges"}, {"id": "B", "text": "same logical request"}],
        "reveal": {"expected_choice": expected, "counterfactual": "trace the second call's key"},
    }


# --- challenge sequence (plan §14.2, §21.2) -----------------------------------


def test_next_challenge_strips_the_reveal() -> None:
    review = _review(machine={"challenges": [_challenge("CH-001", "B")]})
    nxt = human_review.next_challenge(review, dict(review.human))
    assert nxt is not None and nxt["id"] == "CH-001"
    assert "reveal" not in nxt  # the expected choice must not ride along with the question


def test_priming_stages_are_locked_until_challenges_are_complete() -> None:
    review = _review(machine={"challenges": [_challenge("CH-001", "B")]})
    human = dict(review.human)
    assert human_review.stage_locked(review, human, "expected_actual") is True
    assert human_review.stage_locked(review, human, "risk_brief") is False  # a pre-reveal stage
    answered = human_review.record_challenge_answer(review, human, "CH-001", "B", confidence="low")
    assert human_review.stage_locked(review, answered, "expected_actual") is False


def test_a_mismatch_opens_a_counterfactual_that_one_answer_does_not_close() -> None:
    review = _review(machine={"challenges": [_challenge("CH-001", "B")]})
    human = human_review.record_challenge_answer(review, dict(review.human), "CH-001", "A", confidence="high")
    assert human_review.mismatched_challenges(review, human) == ["CH-001"]
    assert human_review.open_counterfactuals(review, human) == ["CH-001"]
    # A mismatch stays open until a corrected model is recorded — the priming stages stay locked.
    assert human_review.stage_locked(review, human, "scenarios") is True
    resolved = human_review.record_counterfactual(human, "CH-001", corrected_model="the key is reused on retry")
    assert human_review.open_counterfactuals(review, resolved) == []
    assert human_review.stage_locked(review, resolved, "scenarios") is False


def test_answering_an_unknown_challenge_is_rejected() -> None:
    review = _review(machine={"challenges": [_challenge("CH-001", "B")]})
    with pytest.raises(ValueError, match="unknown challenge"):
        human_review.record_challenge_answer(review, dict(review.human), "CH-999", "B", confidence="low")


# --- expertise routing (plan §14.9, E2E-05) -----------------------------------


def _critical_decision_review(human: dict[str, Any] | None = None) -> models.Review:
    return _review(
        machine={
            "decision_cards": [
                {"id": "DC-001", "question": "how does retry behave?", "risk": "critical",
                 "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
                 "requires_domains": ["idempotency"]}
            ]
        },
        human=human,
    )


def test_unfamiliar_domain_blocks_without_a_remedy() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "unfamiliar"}]}
    review = _critical_decision_review(human)
    gaps = human_review.expertise_gaps(review, dict(review.human))
    assert gaps == [{"domain": "idempotency", "level": "unfamiliar"}]


def test_undeclared_domain_is_itself_a_gap() -> None:
    review = _critical_decision_review()
    assert human_review.expertise_gaps(review, dict(review.human)) == [{"domain": "idempotency", "level": "undeclared"}]


def test_a_requested_expert_discharges_the_gap() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "unfamiliar"}]}
    review = _critical_decision_review(human)
    remedied = human_review.request_expert(dict(review.human), "idempotency", ["DC-001"], reason="need a domain check")
    assert human_review.expertise_gaps(review, remedied) == []


def test_a_scope_reduction_disposition_on_the_card_discharges_the_gap() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "partial"}]}
    review = _critical_decision_review(human)
    remedied = human_review.record_disposition(dict(review.human), "DC-001", "reduce_scope", note="drop the retry path")
    assert human_review.expertise_gaps(review, remedied) == []


def test_familiar_domain_is_never_a_gap() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "familiar"}]}
    review = _critical_decision_review(human)
    assert human_review.expertise_gaps(review, dict(review.human)) == []


# --- review budget (plan §14.10, E2E-30) --------------------------------------


def test_too_many_critical_decisions_requires_a_scope_split() -> None:
    cards = [
        {"id": f"DC-{i:03d}", "question": "q", "risk": "critical",
         "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}]}
        for i in range(1, 7)  # six critical cards, limit is five
    ]
    review = _review(machine={"decision_cards": cards})
    blown = human_review.scope_split_required(review, dict(review.human))
    assert blown == ["max_critical_decisions"]


def test_budget_within_limits_is_not_blown() -> None:
    review = _review(machine={"scenarios": [{"id": "SCN-001", "kind": "happy_path", "statement_ids": ["STMT-001"]}]})
    assert human_review.scope_split_required(review, dict(review.human)) == []


def test_a_config_limit_overrides_the_default() -> None:
    statements = [{"id": f"STMT-{i:03d}", "text": "x", "epistemic_status": "machine_inferred"} for i in range(1, 4)]
    review = _review(machine={"statements": statements})
    assert human_review.scope_split_required(review, dict(review.human)) == []
    blown = human_review.scope_split_required(review, dict(review.human), {"max_human_statements": 2})
    assert blown == ["max_human_statements"]


# --- staleness / optimistic concurrency (plan §17.5, E2E-08/09) ---------------


def test_a_stale_machine_digest_is_refused() -> None:
    review = _review()
    with pytest.raises(human_review.StaleReview, match="changed since"):
        human_review.assert_machine_current(review, "sha256:" + "0" * 64)


def test_the_current_machine_digest_passes() -> None:
    review = _review()
    human_review.assert_machine_current(review, review.machine_digest())  # no raise


def test_a_human_only_update_does_not_change_the_machine_digest() -> None:
    # E2E-09: answering a challenge changes `human`, never `machine`.
    review = _review(machine={"challenges": [_challenge("CH-001", "B")]})
    before = review.machine_digest()
    human = human_review.record_challenge_answer(review, dict(review.human), "CH-001", "B", confidence="low")
    after = models.Review({"machine": dict(review.machine), "human": human})
    assert after.machine_digest() == before
    assert after.human_digest() != review.human_digest()


# --- completion readiness (plan §21.5) ----------------------------------------


def test_completion_is_blocked_on_an_ungenerated_review() -> None:
    review = models.Review({"machine": {"status": "not_generated"}, "human": {"status": "not_started"}})
    assert human_review.completion_blockers(review)


def test_a_clean_review_can_freeze() -> None:
    review = _review(machine={"challenges": [_challenge("CH-001", "B")]})
    human = human_review.record_challenge_answer(review, dict(review.human), "CH-001", "B", confidence="high")
    assert human_review.completion_blockers(review, human) == []
    frozen = human_review.freeze(review, human)
    assert frozen["status"] == "frozen"


def test_freeze_refuses_while_a_blocker_stands() -> None:
    review = _review(machine={"challenges": [_challenge("CH-001", "B")]})  # unanswered challenge
    with pytest.raises(ValueError, match="cannot freeze"):
        human_review.freeze(review, dict(review.human))


def test_blocking_security_finding_blocks_completion() -> None:
    finding = {
        "id": "SEC-001",
        "severity": "critical",
        "category": "authz_bypass",
        "attack_scenario": "x",
        "blocking": True,
    }
    review = _review(machine={"security": {"findings": [finding]}})
    assert any("security" in b for b in human_review.completion_blockers(review, dict(review.human)))


def test_oracle_failure_blocks_completion() -> None:
    claim = {
        "claim_id": "C-001",
        "verdict": "diverged",
        "integrity": {"status": "verified"},
        "semantic_support": {"status": "unknown", "assessment_basis": "machine_assessed"},
        "conformance": {"status": "oracle_failed"},
    }
    review = _review(machine={"claims": [claim]})
    assert any("oracle" in b for b in human_review.completion_blockers(review, dict(review.human)))
