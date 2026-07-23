"""Tests for models.py — the vocabulary, the document views, and cross-reference validation.

The first section is a *drift canary*, in the same spirit as template_lint: every enum in a
shipped JSON Schema and every vocabulary constant in models.py are two spellings of one fact,
and a release whose schema says one thing while its code believes another is precisely the
quiet divergence 0.9.0 exists to make impossible.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentloop import digests, models, strict_yaml

SCHEMA_NAMES = ("plan", "state", "review", "event", "attestation", "config")


# --- drift canary: schema enums <-> models.py vocabulary ----------------------


def _python_vocabularies() -> dict[frozenset[str], list[str]]:
    """Every module-level frozenset-of-str in models.py, indexed by its value set."""
    index: dict[frozenset[str], list[str]] = {}
    for name, value in vars(models).items():
        if name.startswith("_") or not isinstance(value, frozenset) or not value:
            continue
        if all(isinstance(item, str) for item in value):
            index.setdefault(frozenset(value), []).append(name)
    return index


def _enums(node: Any, pointer: str = "") -> list[tuple[str, frozenset[str]]]:
    """Every `enum` array in a schema, as (json pointer, value set)."""
    found: list[tuple[str, frozenset[str]]] = []
    if isinstance(node, dict):
        raw = node.get("enum")
        if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
            found.append((pointer or "/", frozenset(raw)))
        for key, child in node.items():
            if key != "enum":
                found += _enums(child, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found += _enums(child, f"{pointer}/{index}")
    return found


# Enums that are deliberately narrower than the shared vocabulary they draw from, with the
# reason. Anything not listed here must match a models.py constant exactly.
LOCAL_ENUMS: dict[frozenset[str], str] = {
    frozenset({"code_reviewer"}): "quality_gate's agent step: only the code reviewer runs inside the gate",
}


@pytest.mark.parametrize("schema_name", SCHEMA_NAMES)
def test_every_schema_enum_has_a_models_constant(schema_name: str) -> None:
    known = _python_vocabularies()
    for pointer, values in _enums(models.schema(schema_name)):
        if values in LOCAL_ENUMS:
            continue
        assert values in known, (
            f"{schema_name}.schema.json{pointer}: enum {sorted(values)} matches no models.py constant. "
            "Add the constant (or record it in LOCAL_ENUMS with a reason) so the two cannot drift."
        )


def test_gate_and_phase_ladders_agree() -> None:
    assert set(models.PHASE_AFTER_GATE) == set(models.GATE_ORDER)
    assert set(models.PHASE_AFTER_GATE.values()) <= set(models.PHASE_ORDER)


def test_every_gate_opening_attestation_type_names_a_real_gate() -> None:
    assert set(models.ATTESTATION_GATE.values()) == set(models.GATE_ORDER)
    assert set(models.ATTESTATION_GATE) <= models.ATTESTATION_TYPE_VALUES
    assert set(models.REQUIRED_ROLE) == models.ATTESTATION_TYPE_VALUES


def test_central_only_capabilities_are_capabilities() -> None:
    assert models.CENTRAL_ONLY_CAPABILITIES < models.CAPABILITY_VALUES
    # The four verbs a leaf agent legitimately needs, and nothing more.
    assert models.CAPABILITY_VALUES - models.CENTRAL_ONLY_CAPABILITIES == {
        "decision.declare",
        "knowledge_gap.create",
        "task.status",
        "event.append",
    }


def test_assumed_is_not_an_authority_class() -> None:
    # "we assumed it" is not a class of evidence (plan §6.2).
    assert "assumed" not in models.AUTHORITY_CLASS_VALUES


def test_descriptive_authority_is_not_normative() -> None:
    # Existing code and README text say what the system does, never what it should do (E2E-06).
    assert "descriptive" not in models.NORMATIVE_AUTHORITY
    assert "inferred" not in models.NORMATIVE_AUTHORITY


def test_no_verified_value_in_the_semantic_axis() -> None:
    # `verified` belongs to integrity (a fact), never to semantic support (a judgement).
    assert "verified" in models.INTEGRITY_STATUS_VALUES
    assert "verified" not in models.SEMANTIC_SUPPORT_VALUES
    assert "verified" not in models.STATEMENT_STATUS_VALUES


def test_risk_acceptance_is_not_a_disposition() -> None:
    # A critical unknown cannot be closed by accepting it (plan §15.4).
    assert not any("accept" in action for action in models.DISPOSITION_VALUES)


def test_challenge_precedes_expected_actual_in_the_review_order() -> None:
    order = models.REVIEW_STAGE_ORDER
    assert order.index("challenge") < order.index("expected_actual")
    assert models.PRIMING_STAGES < models.REVIEW_STAGE_VALUES


@pytest.mark.parametrize("name", ["plan", "state", "review", "config"])
def test_the_shipped_scaffold_validates(name: str) -> None:
    """The scaffold `agentloop init` seeds must satisfy its own schema.

    A canary, not a formality: the scaffold is the first thing every new repository parses,
    and a schema change that invalidates it turns `init` into an immediate hard failure.
    """
    from agentloop import data, strict_yaml

    raw = strict_yaml.load_mapping(data.read_text(f"scaffold/agentloop/{name}.yaml"), what=f"{name}.yaml")
    errors = models.schema_errors(raw, name)
    if name == "plan" and not errors:
        errors = models.cross_reference_errors(models.Plan(raw))
    assert errors == []


def test_the_scaffold_smoke_command_is_a_string_not_a_boolean() -> None:
    # Unquoted `true` in YAML is the boolean, not /bin/true. The schema catches it; this test
    # keeps the scaffold from re-acquiring the footgun.
    from agentloop import data, strict_yaml

    config = strict_yaml.load_mapping(data.read_text("scaffold/agentloop/config.yaml"), what="config.yaml")
    for step in config["quality_gate"]:
        assert all(isinstance(arg, str) for arg in step.get("command", []))


# --- helpers ------------------------------------------------------------------


def test_risk_ladder() -> None:
    assert models.risk_at_least("critical", "high")
    assert models.risk_at_least("high", "high")
    assert not models.risk_at_least("medium", "high")
    assert models.max_risk(["low", "critical", "medium"]) == "critical"
    assert models.max_risk([]) == "low"


@pytest.mark.parametrize("path", ["src/app.py", "docs/a.md", "a", "a.b/c-d_e/f+g@h"])
def test_repo_path_accepts_safe_paths(path: str) -> None:
    assert models.is_repo_path(path)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside", "a/../../b", "a/..", "..", "C:\\win", "a\\b", "", "-leading-dash"],
)
def test_repo_path_rejects_escapes(path: str) -> None:
    assert not models.is_repo_path(path)


# --- plan fixture -------------------------------------------------------------

MINIMAL_PLAN = """
cycle:
  id: demo-cycle
  base_commit: 61f3d58c4e0b1122334455667788990011223344
  branch: build/demo
claims:
  - id: C-001
    statement: the thing does not double-charge
    kind: invariant
    decision_class: business_policy
    risk: low
    epistemic_status: unknown
"""

FULL_PLAN = """
cycle:
  id: payment-retry
  base_commit: 61f3d58c4e0b1122334455667788990011223344
  branch: build/payment-retry
evidence_obligations:
  - id: EO-001
    subject_ids: [C-002, TF-001]
    rule: external-side-effect-critical
    risk: critical
    execution_status: complete
    coverage_status: satisfied
    satisfied_by: [SRC-004, O-002]
searches:
  - id: SEARCH-001
    obligation_ids: [EO-001]
    purpose: confirm the resend condition after a lost response
    provider_attempts:
      - provider: vendor-docs
        query: response loss idempotency retry
        execution_status: complete
        result: matched
        source_ids: [SRC-004]
      - provider: repository
        query: payment retry idempotency
        execution_status: failed
        result: unavailable
        reason: provider executable digest mismatch
    execution_status: complete
    coverage_status: sufficient
sources:
  - id: SRC-004
    provider: vendor-docs
    kind: official_external_spec
    authority: {class: normative, derived_by_policy: true}
    title: Idempotency documentation
    locator: vendor-doc://payments/idempotency
  - id: SRC-009
    provider: repository
    kind: repository_code
    authority: {class: descriptive, derived_by_policy: true}
    title: current client
    locator: repo://src/payment/client.py
claims:
  - id: C-002
    requirement_ids: [R-3]
    statement: a lost response never double-commits the same logical request
    kind: invariant
    decision_class: business_policy
    risk: critical
    domains: [payment, idempotency]
    evidence_obligation_ids: [EO-001]
    evidence:
      - {source_id: SRC-004, relation: supports}
      - {source_id: SRC-009, relation: context}
    epistemic_status: grounded
    oracle_ids: [O-002]
technical_facts:
  - id: TF-001
    statement: a resend with the same idempotency key is one logical request
    risk: critical
    domains: [payment, idempotency]
    evidence_obligation_ids: [EO-001]
    evidence:
      - {source_id: SRC-004, relation: supports}
    epistemic_status: grounded
solutions:
  - id: D-001
    claim_ids: [C-002]
    technical_fact_ids: [TF-001]
    decision: keep one idempotency key across retries
    alternatives: ["do not retry on timeout"]
    rationale_source_ids: [SRC-004]
oracles:
  - id: O-002
    claim_ids: [C-002]
    risk: critical
    kind: property_test
    bundle:
      root: .agentloop/oracles/O-002
      digest: sha256:1111111111111111111111111111111111111111111111111111111111111111
      git_blobs:
        - path: .agentloop/oracles/O-002/oracle.yaml
          blob: git-blob:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    runner: {executor: oci, network_profile: none}
    command: [pytest, -q]
    expected_exit_code: 0
    negative_controls:
      - {id: NC-002-1, subject_fixture: duplicate-commit-adapter, expected_exit_code: 1}
    subject_paths: [src/payment/]
tasks:
  - id: T-001
    title: Foundation
    kind: foundation
    risk: low
  - id: T-002
    title: Preserve idempotency across retries
    kind: parallel
    blocked_by: [T-001]
    claim_ids: [C-002]
    oracle_ids: [O-002]
    domains: [payment]
    risk: critical
    scope: {include: [src/payment/], exclude: [.agentloop/oracles/]}
non_goals:
  - id: NG-001
    statement: retry count does not become a user setting
"""


def test_minimal_plan_parses() -> None:
    plan = models.Plan.parse(MINIMAL_PLAN)
    assert plan.cycle_id == "demo-cycle"
    assert [c.id for c in plan.claims] == ["C-001"]


def test_full_plan_parses_and_indexes() -> None:
    plan = models.Plan.parse(FULL_PLAN)
    assert plan.cycle_id == "payment-retry"
    assert plan.claim("C-002") is not None
    assert plan.oracle("O-002") is not None
    assert [t.id for t in plan.tasks] == ["T-001", "T-002"]
    assert plan.task("T-002").blocked_by == ("T-001",)  # type: ignore[union-attr]


def test_plan_digest_survives_a_reflow_but_not_an_edit() -> None:
    plan = models.Plan.parse(FULL_PLAN)
    assert digests.is_digest(plan.digest())

    # Same facts, different YAML layout: a reflow must not invalidate a signed gate receipt.
    reflowed = models.Plan.parse(
        FULL_PLAN.replace(
            "    runner: {executor: oci, network_profile: none}",
            "    runner:\n      network_profile: none\n      executor: oci",
        )
    )
    assert reflowed.digest() == plan.digest()

    # One word of one claim changed: the digest must move, or a review could be signed for
    # bytes nobody read.
    edited = models.Plan.parse(FULL_PLAN.replace("never double-commits", "sometimes double-commits"))
    assert edited.digest() != plan.digest()


def test_supporting_and_contradicting_evidence_are_separate() -> None:
    claim = models.Plan.parse(FULL_PLAN).claim("C-002")
    assert claim is not None
    assert claim.supporting_source_ids == ("SRC-004",)
    assert claim.contradicting_source_ids == ()


def test_descriptive_source_is_not_normative() -> None:
    plan = models.Plan.parse(FULL_PLAN)
    assert plan.source("SRC-004").is_normative  # type: ignore[union-attr]
    assert not plan.source("SRC-009").is_normative  # type: ignore[union-attr]


def test_unavailable_provider_is_surfaced_even_when_coverage_succeeded() -> None:
    # A provider outage must stay visible even when an alternate path satisfied the
    # obligation — hiding it is how "no documentation exists" gets invented (plan §15.3).
    search = models.Plan.parse(FULL_PLAN).searches[0]
    assert search.coverage_status == "sufficient"
    assert search.unavailable_providers == ("repository",)


def test_ungrounded_and_unsatisfied_helpers() -> None:
    plan = models.Plan.parse(FULL_PLAN)
    assert plan.unsatisfied_obligations(floor="high") == ()
    assert plan.ungrounded(floor="high") == ()
    minimal = models.Plan.parse(MINIMAL_PLAN)
    assert [e.id for e in minimal.ungrounded(floor="low")] == ["C-001"]


def test_subjects_of_an_obligation_span_claims_and_facts() -> None:
    plan = models.Plan.parse(FULL_PLAN)
    assert sorted(e.id for e in plan.subjects_of("EO-001")) == ["C-002", "TF-001"]


# --- schema rejections --------------------------------------------------------


def _plan_error(text: str) -> str:
    with pytest.raises(models.DocumentError) as excinfo:
        models.Plan.parse(text)
    return str(excinfo.value)


def test_unknown_field_rejected() -> None:
    assert "Additional properties" in _plan_error(MINIMAL_PLAN + "surprise: yes\n")


def test_unknown_epistemic_status_rejected() -> None:
    # There is no value meaning "reads plausibly" (plan §2.3).
    assert "epistemic_status" in _plan_error(
        MINIMAL_PLAN.replace("epistemic_status: unknown", "epistemic_status: assumed")
    )


def test_authority_derived_by_policy_must_be_true() -> None:
    # An AI may write the field, but only the Policy Engine's `true` passes validation.
    bad = FULL_PLAN.replace("derived_by_policy: true}", "derived_by_policy: false}", 1)
    assert "derived_by_policy" in _plan_error(bad)


def test_absolute_subject_path_rejected() -> None:
    assert "subject_paths" in _plan_error(FULL_PLAN.replace("subject_paths: [src/payment/]", "subject_paths: [/etc/]"))


def test_mutable_image_tag_rejected() -> None:
    bad = FULL_PLAN.replace(
        "runner: {executor: oci, network_profile: none}",
        "runner: {executor: oci, network_profile: none, image: 'registry.example/img:latest'}",
    )
    assert "image" in _plan_error(bad)


# --- cross-reference validation -----------------------------------------------


def test_dangling_source_reference_is_caught() -> None:
    bad = FULL_PLAN.replace("{source_id: SRC-004, relation: supports}", "{source_id: SRC-777, relation: supports}")
    assert "unknown source id 'SRC-777'" in _plan_error(bad)


def test_dangling_oracle_reference_is_caught() -> None:
    assert "unknown oracle id 'O-999'" in _plan_error(
        FULL_PLAN.replace("oracle_ids: [O-002]", "oracle_ids: [O-999]", 1)
    )


def test_duplicate_claim_id_is_caught() -> None:
    doubled = FULL_PLAN.replace(
        "technical_facts:",
        "  - id: C-002\n    statement: dup\n    kind: invariant\n"
        "    decision_class: business_policy\n    risk: low\n    epistemic_status: unknown\ntechnical_facts:",
    )
    assert "duplicate id 'C-002'" in _plan_error(doubled)


def test_task_dependency_cycle_is_caught() -> None:
    bad = FULL_PLAN.replace(
        "    title: Foundation\n    kind: foundation\n    risk: low",
        "    title: Foundation\n    kind: foundation\n    risk: low\n    blocked_by: [T-002]",
    )
    assert "dependency cycle" in _plan_error(bad)


def test_task_blocked_by_itself_is_caught() -> None:
    bad = FULL_PLAN.replace("blocked_by: [T-001]", "blocked_by: [T-002]")
    assert "blocked_by lists itself" in _plan_error(bad)


def test_critical_oracle_without_a_negative_control_is_rejected() -> None:
    # An oracle that never fails proves nothing (plan §9.4, E2E-25).
    bad = FULL_PLAN.replace(
        "    negative_controls:\n      - {id: NC-002-1, subject_fixture: duplicate-commit-adapter,"
        " expected_exit_code: 1}\n",
        "",
    )
    assert "requires at least one negative control" in _plan_error(bad)


def test_obligation_subject_must_be_a_claim_or_fact() -> None:
    bad = FULL_PLAN.replace("subject_ids: [C-002, TF-001]", "subject_ids: [C-002, SRC-004]")
    assert "is neither a claim nor a technical fact" in _plan_error(bad)


def test_all_errors_are_reported_not_just_the_first() -> None:
    bad = FULL_PLAN.replace("oracle_ids: [O-002]", "oracle_ids: [O-999]", 1).replace(
        "{source_id: SRC-004, relation: supports}", "{source_id: SRC-777, relation: supports}"
    )
    message = _plan_error(bad)
    assert "O-999" in message and "SRC-777" in message


# --- state --------------------------------------------------------------------

STATE = """
project: demo
cycle_id: payment-retry
current_phase: build
updated_at: "2026-07-23T17:00:00+09:00"
gates:
  requirements:
    status: approved
    receipt:
      attestation_id: ATT-REQ-001
      validation_digest: sha256:2222222222222222222222222222222222222222222222222222222222222222
      attested_chain_root: sha256:3333333333333333333333333333333333333333333333333333333333333333
      result_chain_root: sha256:4444444444444444444444444444444444444444444444444444444444444444
  design: {status: pending, receipt: null}
  tasks: {status: pending, receipt: null}
  build: {status: pending, receipt: null}
  release: {status: pending, receipt: null}
plan:
  status: frozen
  digest: sha256:5555555555555555555555555555555555555555555555555555555555555555
tasks:
  T-002: {status: done, attempts: 2}
"""


def test_state_parses() -> None:
    state = models.State.parse(STATE)
    assert state.current_phase == "build"
    assert state.gate_status("requirements") == "approved"
    assert state.gate_status("design") == "pending"
    assert state.approved_gates == ("requirements",)
    assert state.task_status == {"T-002": "done"}
    assert state.plan_status == "frozen"


def test_gate_status_of_an_absent_gate_reads_pending() -> None:
    # Fail closed: an unreadable gate is never "approved".
    assert models.State.parse(STATE).gate_status("nonexistent") == "pending"


def test_approved_gate_without_a_receipt_is_rejected() -> None:
    # There is no path to `approved` that is not a signed, digest-bound receipt.
    bad = STATE.replace("  design: {status: pending, receipt: null}", "  design: {status: approved, receipt: null}")
    with pytest.raises(models.DocumentError, match="receipt"):
        models.State.parse(bad)


def test_gate_chain_violation_detected() -> None:
    # An approval that survived a roll back: design approved while requirements is pending.
    bad = STATE.replace("  requirements:\n    status: approved", "  requirements:\n    status: pending")
    # Strip the now-inconsistent receipt so only the chain rule is under test.
    bad = bad.replace("status: pending\n    receipt:", "status: pending\n    receipt_disabled:")
    state = models.State(strict_yaml.load_mapping(bad.replace("receipt_disabled:", "receipt:")))
    assert state.gate_chain_violations() == []  # requirements pending, nothing downstream approved
    approved = models.State.parse(STATE)
    assert approved.pending_upstream("build") == "design"


# --- review -------------------------------------------------------------------

REVIEW = """
machine:
  status: generated
  binding:
    change_digest: sha256:6666666666666666666666666666666666666666666666666666666666666666
    plan_digest: sha256:7777777777777777777777777777777777777777777777777777777777777777
    toolchain_digest: sha256:8888888888888888888888888888888888888888888888888888888888888888
  coverage:
    - diff_digest: sha256:9999999999999999999999999999999999999999999999999999999999999999
      analyzed_files: 27
      truncated: false
      coverage_status: sufficient
  actual_extraction:
    - id: AST-003
      statement: the retry path passes the same idempotency key to the next attempt
      category: state_propagation
      confidence: medium
      code_anchors:
        - path: src/payment/client.py
          start_line: 81
          end_line: 114
          blob: 'git-blob:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
  claims:
    - claim_id: C-002
      actual_statement_ids: [AST-003]
      verdict: aligned
      integrity: {status: verified}
      semantic_support: {status: supported, assessment_basis: machine_assessed}
      conformance: {status: oracle_passed, oracle_ids: [O-002]}
  security:
    findings:
      - id: SEC-001
        severity: high
        category: credential_exposure
        attack_scenario: the reviewer container could reach a host credential
        blocking: true
human:
  status: not_started
"""


def test_review_parses_and_digests_the_halves_separately() -> None:
    review = models.Review.parse(REVIEW)
    assert review.human_status == "not_started"
    assert len(review.actual_statements) == 1
    assert review.coverage_sufficient
    assert review.machine_digest() != review.human_digest()


def test_blocking_security_finding_is_isolated() -> None:
    review = models.Review.parse(REVIEW)
    assert [f["id"] for f in review.blocking_security_findings] == ["SEC-001"]


def test_absent_coverage_is_not_sufficient() -> None:
    # "we did not measure" must never render as "we measured nothing missing" (plan §2.4).
    review = models.Review(strict_yaml.load_mapping("machine: {status: not_generated}\nhuman: {status: not_started}\n"))
    assert not review.coverage_sufficient
    assert not review.is_generated


def test_truncated_coverage_is_rejected_outright() -> None:
    # Reading only the head or tail of a huge diff and calling it analysed is not allowed;
    # the detector must partition instead (plan §13.4).
    with pytest.raises(models.DocumentError, match="truncated"):
        models.Review.parse(REVIEW.replace("truncated: false", "truncated: true"))


def test_answering_a_challenge_after_the_reveal_is_rejected() -> None:
    bad = REVIEW.replace(
        "human:\n  status: not_started",
        "human:\n  status: in_progress\n  challenge_answers:\n"
        "    - {challenge_id: CH-001, choice: B, confidence: low, answered_before_reveal: false}",
    )
    with pytest.raises(models.DocumentError, match="answered_before_reveal"):
        models.Review.parse(bad)


# --- event / attestation ------------------------------------------------------


def test_event_payload_excludes_its_own_digest() -> None:
    event = models.Event(
        seq=1,
        id="0123abcd",
        tx_id="4567ef01",
        ts="2026-07-23T18:10:00+09:00",
        event="gate_approved",
        cycle_id="demo",
        event_digest="sha256:" + "0" * 64,
    )
    assert "event_digest" not in event.payload()
    assert event.to_mapping()["event_digest"] == "sha256:" + "0" * 64


def test_event_round_trips_through_a_mapping() -> None:
    event = models.Event(
        seq=7,
        id="0123abcd",
        tx_id="4567ef01",
        ts="2026-07-23T18:10:00+09:00",
        event="task_completed",
        cycle_id="demo",
        actor="alice",
        subject_ids=("T-002",),
        prev_event_digest="sha256:" + "1" * 64,
        event_digest="sha256:" + "2" * 64,
        detail={"attempts": 2},
    )
    assert models.Event.from_mapping(event.to_mapping()) == event


def test_attestation_payload_includes_issued_at() -> None:
    # Without it, a signature could be lifted onto a later action by the same principal.
    att = models.Attestation(
        id="ATT-BUILD-001",
        type="human_review_approval",
        subject={"repository_id": "x", "cycle_id": "demo"},
        actor={"principal": "a@example.com", "role": "gate_reviewer"},
        issued_at="2026-07-23T18:10:00+09:00",
    )
    assert att.payload()["issued_at"] == "2026-07-23T18:10:00+09:00"
    assert "signature" not in att.payload()
    assert att.gate == "build"


def test_attestation_payload_digest_changes_with_the_subject() -> None:
    base = dict(
        id="ATT-BUILD-001",
        type="human_review_approval",
        actor={"principal": "a@example.com", "role": "gate_reviewer"},
        issued_at="2026-07-23T18:10:00+09:00",
    )
    one = models.Attestation(subject={"repository_id": "x", "cycle_id": "demo"}, **base)  # type: ignore[arg-type]
    two = models.Attestation(subject={"repository_id": "x", "cycle_id": "other"}, **base)  # type: ignore[arg-type]
    assert one.payload_digest() != two.payload_digest()


def test_expert_confirmation_opens_no_gate() -> None:
    att = models.Attestation(
        id="ATT-EXPERT-001",
        type="expert_confirmation",
        subject={"repository_id": "x", "cycle_id": "demo"},
        actor={"principal": "s@example.com", "role": "expert"},
        issued_at="2026-07-23T18:10:00+09:00",
    )
    assert att.gate == ""
