"""Tests for approve.py — readiness, the attestation request, and recording an approval.

The single most important assertion in this file is that **`agentloop approve` never opens a
gate**. In 0.8.x the command *was* the approval; here it produces a request that a human has
to sign with a key the external Trust Manifest authorizes, and the gate stays pending until
that signature is imported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop import approve, digests, models
from agentloop import repo as repo_mod
from agentloop import store as store_mod
from tests._support import (
    chain,
    make_claim,
    make_obligation,
    make_plan,
    make_review,
    make_state,
    make_task,
    seed_repo,
)

PENDING_ALL = dict.fromkeys(models.GATE_ORDER, "pending")


def repo_at(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return repo_mod.Repo(tmp_path)


# --- the command never opens a gate -------------------------------------------


def test_approve_leaves_the_gate_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])]),
    )
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 0
    assert "Nothing is approved yet" in capsys.readouterr().out

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.gate_status("requirements") == "pending"


def test_there_is_no_force_and_no_by(capsys: pytest.CaptureFixture[str]) -> None:
    # `--force` skipped the evidence check; `--by` let you type an identity. Neither is an
    # identity or a check, so neither exists.
    with pytest.raises(SystemExit):
        approve.main(["--help"])
    helptext = capsys.readouterr().out
    assert "--force" not in helptext
    assert "--by" not in helptext


def test_record_approval_is_not_reachable_from_the_cli() -> None:
    """The only route to an approved gate is a verified signature. A command that recorded an
    approval without one would *be* an alternative route."""
    import inspect

    source = inspect.getsource(approve.main)
    assert "record_approval" not in source


# --- readiness ----------------------------------------------------------------


def test_an_empty_plan_has_nothing_to_approve(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[], obligations=[], tasks=[]),
    )
    blockers = approve.readiness(repo, "requirements")
    assert any("states no claims" in b for b in blockers)


def test_a_claim_with_no_obligation_blocks(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001", obligation_ids=[])], obligations=[], tasks=[]),
    )
    assert any("an opinion with an id" in b for b in approve.readiness(repo, "requirements"))


def test_gates_open_in_order(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    blockers = approve.readiness(repo, "design")
    assert any("gate 'requirements' is still pending" in b for b in blockers)


def test_an_already_approved_gate_is_a_blocker(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)  # approved through tasks
    assert any("already approved" in b for b in approve.readiness(repo, "tasks"))


def test_readiness_reports_every_blocker_not_just_the_first(tmp_path: Path) -> None:
    """Being handed one blocker, fixing it, and being handed the next is the review friction
    the whole release budgets against."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(
            claims=[make_claim("C-001", risk="critical", epistemic_status="unknown", obligation_ids=[])],
            obligations=[],
            tasks=[],
        ),
    )
    assert len(approve.readiness(repo, "requirements")) >= 2


def test_a_damaged_audit_chain_blocks_every_gate(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        events=chain("cycle_initialized", "task_completed"),
    )
    log = repo.events
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other", 1), encoding="utf-8")
    assert any("audit chain has" in b for b in approve.readiness(repo, "requirements"))


def test_an_unknown_gate_is_refused(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    with pytest.raises(approve.ApprovalError, match="unknown gate"):
        approve.readiness(repo, "nonexistent")


# --- gate 3: the plan has to be buildable -------------------------------------


def test_gate_three_needs_a_task_for_every_claim(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates={"tasks": "pending", "build": "pending", "release": "pending"}, phase="tasks"),
        plan=make_plan(
            claims=[make_claim("C-001"), make_claim("C-002", obligation_ids=["EO-001"])],
            obligations=[make_obligation("EO-001", subject_ids=["C-001", "C-002"])],
            tasks=[make_task("T-001", claim_ids=["C-001"])],
        ),
    )
    assert any("C-002: no task is answerable" in b for b in approve.readiness(repo, "tasks"))


def test_a_design_decision_needs_a_stated_alternative(tmp_path: Path) -> None:
    solution = {"id": "D-001", "claim_ids": ["C-001"], "decision": "do the thing"}
    repo = repo_at(
        tmp_path,
        state=make_state(
            gates={"design": "pending", "tasks": "pending", "build": "pending", "release": "pending"},
            phase="design",
            plan_status="draft",
        ),
        plan=make_plan(solutions=[solution]),
    )
    assert any("decision nobody actually made" in b for b in approve.readiness(repo, "design"))


# --- a tampered oracle bundle blocks a downstream gate (E2E-12) ---------------


@pytest.mark.integration
def test_gate_four_blocks_when_a_frozen_oracle_bundle_was_tampered(tmp_path: Path) -> None:
    """A bundle edited after gate 3 (the 'make the oracle pass' move) must fail readiness."""
    import subprocess

    from agentloop import oracle_bundle
    from tests._support import make_oracle

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    root = ".agentloop/oracles/O-001"
    bundle_file = tmp_path / root / "harness.py"
    bundle_file.parent.mkdir(parents=True, exist_ok=True)
    bundle_file.write_text("assert conforms()\n", encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "t@e.x")
    git("config", "user.name", "T")
    git("add", "-A")
    git("commit", "-q", "-m", "bundle")

    frozen = oracle_bundle.freeze(repo_mod.Repo(tmp_path), models.Oracle(make_oracle(bundle_root=root)))
    oracle = make_oracle(
        bundle_root=root,
        bundle_digest=frozen.digest,
        git_blobs=[{"path": b.path, "blob": f"git-blob:{b.blob}"} for b in frozen.blobs],
    )
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        plan=make_plan(oracles=[oracle]),
        review=make_review(generated=True, human_status="frozen"),
    )
    # Intact: the frozen digest still describes the committed bundle.
    assert not any("no longer matches" in b for b in approve.readiness(repo, "build"))

    # Now tamper with the committed bundle — the digest must move and readiness must block.
    bundle_file.write_text("assert True  # neutered\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "tamper")
    assert any("no longer matches" in b for b in approve.readiness(repo, "build"))


# --- gate 4: a review, not a green test run -----------------------------------


def test_gate_four_needs_a_generated_review(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(tasks={"T-001": "done"}), review=make_review(generated=False))
    blockers = approve.readiness(repo, "build")
    assert any("not a green test run" in b for b in blockers)


def test_gate_four_blocks_on_an_insufficient_coverage_manifest(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, coverage_status="insufficient", human_status="frozen"),
    )
    assert any("undeterminable, not zero" in b for b in approve.readiness(repo, "build"))


def test_gate_four_blocks_on_a_blocking_security_finding(tmp_path: Path) -> None:
    finding = {
        "id": "SEC-001",
        "severity": "high",
        "category": "credential_exposure",
        "attack_scenario": "the reviewer container reaches a host credential",
        "blocking": True,
    }
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, human_status="frozen", security_findings=[finding]),
    )
    assert any("SEC-001" in b for b in approve.readiness(repo, "build"))


def test_gate_four_blocks_until_the_human_review_is_frozen(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, human_status="in_progress"),
    )
    assert any("not 'frozen'" in b for b in approve.readiness(repo, "build"))


def test_gate_four_blocks_while_tasks_are_unfinished(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path, state=make_state(tasks={"T-001": "todo"}), review=make_review(generated=True, human_status="frozen")
    )
    assert any("tasks not done: T-001" in b for b in approve.readiness(repo, "build"))


# --- the attestation request --------------------------------------------------


def test_the_request_binds_every_digest_the_approval_would_cover(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        events=chain("cycle_initialized"),
    )
    envelope = approve.request_envelope(repo, "requirements")
    subject = envelope["subject"]
    assert isinstance(subject, dict)
    assert digests.is_digest(subject["plan_digest"])
    assert digests.is_digest(subject["config_digest"])
    assert subject["event_chain_root_before"] == store_mod.Store(repo).chain_root()
    assert subject["cycle_id"] == "demo-cycle"


def test_the_request_is_a_valid_attestation_envelope(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    for gate in models.GATE_ORDER:
        envelope = approve.request_envelope(repo, gate)
        assert models.schema_errors(envelope, "attestation") == []


def test_the_request_names_the_role_the_trust_manifest_must_authorize(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    assert approve.request_envelope(repo, "release")["actor"]["role"] == "release_approver"  # type: ignore[index]
    assert approve.request_envelope(repo, "build")["actor"]["role"] == "gate_reviewer"  # type: ignore[index]


def test_the_request_file_is_written_and_the_next_steps_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_repo(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001")]),
    )
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 0
    written = json.loads((tmp_path / "requirements-attestation.json").read_text(encoding="utf-8"))
    assert written["type"] == "requirements_approval"
    out = capsys.readouterr().out
    assert "attestation sign" in out and "attestation import" in out


def test_check_writes_no_request(tmp_path: Path) -> None:
    seed_repo(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    approve.main(["requirements", "--check", "--repo", str(tmp_path)])
    assert not (tmp_path / "requirements-attestation.json").exists()


def test_blockers_exit_nonzero_and_write_nothing(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[], obligations=[], tasks=[]),
    )
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 1
    assert not (tmp_path / "requirements-attestation.json").exists()


# --- recording an approval (the `attestation import` half) --------------------


def _attestation(repo: repo_mod.Repo, gate: str) -> models.Attestation:
    envelope = approve.request_envelope(repo, gate)
    envelope["actor"] = {"principal": "maintainer@example.com", "role": approve._role_for(gate)}
    return models.Attestation.from_mapping(envelope)


def test_recording_an_approval_writes_a_receipt_and_an_event(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    approve.record_approval(repo, "requirements", _attestation(repo, "requirements"))

    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    assert state.gate_status("requirements") == "approved"
    assert state.current_phase == "design"  # the gate opens the door to the next phase
    receipt = state.gate_receipt("requirements")
    assert receipt is not None and receipt["attestation_id"].startswith("ATT-REQUIREMENTS-")

    events = store.read_events()
    assert [e.event for e in events] == ["gate_approved"]
    assert events[0].actor == "maintainer@example.com"
    assert "requirements" in events[0].subject_ids


def test_an_attestation_bound_to_a_different_chain_root_is_refused(tmp_path: Path) -> None:
    """Events were appended, removed, or regenerated since the human signed. The signature
    covers a log that no longer exists."""
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    attestation = _attestation(repo, "requirements")

    with store_mod.Store(repo).transaction() as tx:
        tx.append("knowledge_gap", cycle_id="demo-cycle")

    with pytest.raises(approve.ApprovalError, match="different audit-chain root"):
        approve.record_approval(repo, "requirements", attestation)


def test_recording_refuses_a_damaged_chain(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        events=chain("cycle_initialized"),
    )
    attestation = _attestation(repo, "requirements")
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    with pytest.raises(approve.ApprovalError, match="damaged audit chain"):
        approve.record_approval(repo, "requirements", attestation)


def test_every_gate_maps_to_exactly_one_attestation_type() -> None:
    assert set(approve.GATE_ATTESTATION) == set(models.GATE_ORDER)
    assert len(set(approve.GATE_ATTESTATION.values())) == len(models.GATE_ORDER)


def test_an_unsupported_layout_stops_the_command(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".agentloop" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    assert approve.main(["build", "--repo", str(tmp_path)]) == 1
