"""Tests for doctor.py — the read-only diagnosis of everything the guarantees rest on.

Two rules make the output trustworthy, and both are asserted here.

**It never reports "not measured" as "fine".** A missing Trust Manifest is a FAIL, not a
silent pass; an insufficient coverage manifest is a FAIL, not a zero.

**It never repairs anything.** Several of the things it inspects — an approval, an audit
record — must only ever change by a deliberate human action, and a doctor that fixes what it
finds is a doctor whose findings nobody reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import doctor, models
from agentloop import repo as repo_mod
from tests._support import SANDBOXED_PROFILES, chain, make_config, make_review, make_state, seed_repo


def findings(repo: repo_mod.Repo) -> dict[str, list[doctor.Finding]]:
    """run_checks grouped by area, for readable assertions."""
    grouped: dict[str, list[doctor.Finding]] = {}
    for finding in doctor.run_checks(repo):
        grouped.setdefault(finding.area, []).append(finding)
    return grouped


def levels(items: list[doctor.Finding], substring: str) -> list[str]:
    return [f.level for f in items if substring in f.message]


def trust_manifest(tmp_path: Path, *, identities: int = 1) -> Path:
    path = tmp_path / "config" / "agentloop" / "trust.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "identities:\n" + "".join(
        f"  - principal: p{i}@example.com\n    key_fingerprint: SHA256:abc\n" for i in range(identities)
    )
    path.write_text(body if identities else "identities: []\n", encoding="utf-8")
    return path


def healthy(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    """A repo that should produce no FAIL: sandboxed profiles and a Trust Manifest present."""
    trust_manifest(tmp_path)
    kwargs.setdefault("config", make_config(profiles=SANDBOXED_PROFILES))
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return repo_mod.Repo(tmp_path)


# --- format -------------------------------------------------------------------


def test_a_legacy_layout_short_circuits_every_other_check(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".agentloop" / "state.md").write_text("legacy\n", encoding="utf-8")
    results = doctor.run_checks(repo_mod.Repo(tmp_path))
    assert len(results) == 1
    assert results[0].level == "FAIL"
    assert "does not read or migrate" in results[0].message


def test_missing_ssot_documents_are_named(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=None, review=None)
    assert any(
        "plan.yaml" in f.message and "review.yaml" in f.message for f in doctor.check_layout(repo_mod.Repo(tmp_path))
    )


def test_an_invalid_document_fails_with_its_validation_errors(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".agentloop" / "plan.yaml").write_text("cycle: {}\nclaims: []\n", encoding="utf-8")
    results, _ = doctor.check_documents(repo_mod.Repo(tmp_path))
    assert any(f.level == "FAIL" and "plan.yaml" in f.message for f in results)


def test_a_healthy_repo_validates_all_four_documents(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    _, loaded = doctor.check_documents(repo)
    assert set(loaded) == {"config", "state", "plan", "review"}


def test_the_lock_format_is_reported(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    assert any("agentloop-grounded-v1" in f.message for f in doctor.check_lock(repo))


def test_a_pre_090_lock_fails(tmp_path: Path) -> None:
    seed_repo(tmp_path, lock=False)
    (tmp_path / ".agentloop" / "agentloop.lock").write_text("version: 1\n", encoding="utf-8")
    results = doctor.check_lock(repo_mod.Repo(tmp_path))
    assert results[0].level == "FAIL"
    assert "predates AgentLoop 0.9.0" in results[0].message


# --- trust --------------------------------------------------------------------


def test_a_missing_trust_manifest_is_a_fail_not_a_shrug(tmp_path: Path) -> None:
    """Without it there is no authorized principal, so no gate can open. Reporting that as
    healthy would describe a repository in which nothing can ever be approved as fine."""
    seed_repo(tmp_path)
    results = doctor.check_trust()
    assert any(f.level == "FAIL" and "no Trust Manifest" in f.message for f in results)
    assert any("OUTSIDE the repository" in f.message for f in results)


def test_a_manifest_with_no_identities_still_fails(tmp_path: Path) -> None:
    trust_manifest(tmp_path, identities=0)
    assert any(f.level == "FAIL" for f in doctor.check_trust() if "identity" in f.message)


def test_a_readable_manifest_passes(tmp_path: Path) -> None:
    trust_manifest(tmp_path, identities=2)
    assert any(f.level == "PASS" and "2 identity" in f.message for f in doctor.check_trust())


def test_a_group_readable_manifest_warns(tmp_path: Path) -> None:
    path = trust_manifest(tmp_path)
    path.chmod(0o644)
    assert any(f.level == "WARN" and "group/world accessible" in f.message for f in doctor.check_trust())


def test_an_approved_gate_must_cite_an_attestation_that_exists(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    results = doctor.check_attestations(repo, models.State(make_state()))
    assert any(f.level == "FAIL" and "not in .agentloop/attestations/" in f.message for f in results)


def test_a_present_attestation_passes(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    repo.attestations.mkdir(parents=True, exist_ok=True)
    for gate in ("requirements", "design", "tasks"):
        (repo.attestations / f"ATT-{gate.upper()}-0001.json").write_text("{}", encoding="utf-8")
    results = doctor.check_attestations(repo, models.State(make_state()))
    assert all(f.level == "PASS" for f in results)


# --- runtime and sandbox ------------------------------------------------------


def test_an_unsandboxed_profile_is_a_fail_with_the_command_to_fix_it(tmp_path: Path) -> None:
    config = models.Config(make_config())  # host profiles
    results = doctor.check_sandbox(config)
    assert results[0].level == "FAIL"
    assert "agentloop oci build" in results[0].message


def test_a_digest_pinned_profile_passes(tmp_path: Path) -> None:
    config = models.Config(make_config(profiles=SANDBOXED_PROFILES))
    assert not [f for f in doctor.check_sandbox(config) if f.level == "FAIL" and "run repository" in f.message]


def test_an_oci_profile_with_no_pinned_digest_fails() -> None:
    profiles = {"oracle": {"kind": "oci", "network_profile": "none"}}
    config = models.Config(make_config(profiles={**SANDBOXED_PROFILES, **profiles}))
    assert any(f.level == "FAIL" and "no digest-pinned image" in f.message for f in doctor.check_sandbox(config))


def test_a_shared_independence_group_fails() -> None:
    config = make_config()
    config["agents"]["comparator"]["independence_group"] = "claude/opus"  # type: ignore[index]
    results = doctor.check_independence(models.Config(config))
    assert results[0].level == "FAIL"
    assert "share the independence group" in results[0].message


def test_two_models_of_one_provider_pass_but_warn() -> None:
    """A mechanical pass that is weaker than two providers, and the honest thing is to say so
    rather than let a green check imply more independence than exists."""
    results = doctor.check_independence(models.Config(make_config()))
    assert results[0].level == "WARN"
    assert "same provider" in results[0].message


def test_two_providers_pass_cleanly() -> None:
    config = make_config()
    config["agents"]["comparator"]["independence_group"] = "openai/gpt"  # type: ignore[index]
    assert doctor.check_independence(models.Config(config))[0].level == "PASS"


def test_the_runtime_fallback_warns_that_it_is_weaker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    repo = healthy(tmp_path)
    assert any(f.level == "WARN" and "isolation is weaker" in f.message for f in doctor.check_runtime(repo))


def test_a_leftover_journal_is_reported(tmp_path: Path) -> None:
    from agentloop import store as store_mod

    repo = healthy(tmp_path)
    store = store_mod.Store(repo)
    store_mod.ensure_private_dir(store.runtime)
    store._write_journal({"tx_id": "abc", "phase": "prepared"})
    assert any("transaction was interrupted" in f.message for f in doctor.check_runtime(repo))


# --- gates, plan, evidence ----------------------------------------------------


def test_a_broken_gate_ladder_fails(tmp_path: Path) -> None:
    state = models.State(make_state(gates={"requirements": "pending"}))
    results = doctor.check_gate_chain(state)
    assert results[0].level == "FAIL"
    assert "survived a roll back" in results[0].message


def test_a_healthy_ladder_passes() -> None:
    assert doctor.check_gate_chain(models.State(make_state()))[0].level == "PASS"


def test_a_whole_evidence_thread_passes(tmp_path: Path) -> None:
    repo = healthy(tmp_path)
    grouped = findings(repo)
    assert any(f.level == "PASS" for f in grouped["evidence"])
    assert not [f for f in grouped["plan"] if f.level == "FAIL"]


def test_a_failed_source_verification_fails(tmp_path: Path) -> None:
    from tests._support import make_plan, make_source

    source = make_source()
    source["verification"] = {"status": "failed"}
    plan = models.Plan(make_plan(sources=[source]))
    assert any(f.level == "FAIL" and "source verification failed" in f.message for f in doctor.check_plan(plan, None))


# --- the audit chain and the review -------------------------------------------


def test_an_intact_chain_reports_its_root(tmp_path: Path) -> None:
    repo = healthy(tmp_path, events=chain("cycle_initialized"))
    results = doctor.check_chain(repo)
    assert results[0].level == "PASS"
    assert "root sha256:" in results[0].message


def test_a_damaged_chain_fails_and_says_restore_not_rewrite(tmp_path: Path) -> None:
    repo = healthy(tmp_path, events=chain("cycle_initialized", "task_completed"))
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    results = doctor.check_chain(repo)
    assert results[0].level == "FAIL"
    assert "never rewrite it to agree" in results[0].message


def test_an_ungenerated_review_is_info_not_a_pass() -> None:
    results = doctor.check_review(models.Review(make_review(generated=False)))
    assert results[0].level == "INFO"


def test_an_insufficient_coverage_manifest_fails() -> None:
    review = models.Review(make_review(generated=True, coverage_status="insufficient"))
    assert any(f.level == "FAIL" and "undeterminable, not zero" in f.message for f in doctor.check_review(review))


def test_a_blocking_security_finding_fails() -> None:
    finding = {
        "id": "SEC-001",
        "severity": "critical",
        "category": "sandbox_escape",
        "attack_scenario": "the oracle reaches the docker socket",
        "blocking": True,
    }
    review = models.Review(make_review(generated=True, security_findings=[finding]))
    assert any(f.level == "FAIL" and "1 blocking security" in f.message for f in doctor.check_review(review))


# --- the CLI ------------------------------------------------------------------


def test_a_healthy_repo_has_no_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = healthy(tmp_path)
    repo.attestations.mkdir(parents=True, exist_ok=True)
    for gate in ("requirements", "design", "tasks"):
        (repo.attestations / f"ATT-{gate.upper()}-0001.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    fails = [f for f in doctor.run_checks(repo) if f.level == "FAIL"]
    assert fails == [], "\n".join(f"{f.area}: {f.message}" for f in fails)


def test_the_cli_exits_1_when_something_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_repo(tmp_path)  # no Trust Manifest, host profiles
    monkeypatch.chdir(tmp_path)
    assert doctor.main([]) == 1


def test_unsupported_layout_mode_diagnoses_a_08x_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".agentloop" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert doctor.main(["--unsupported-layout"]) == 1
    out = capsys.readouterr().out
    assert "tasks.yaml" in out
    assert "manufacturing evidence" in out  # why there is deliberately no migration


def test_unsupported_layout_mode_on_a_clean_repo_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    healthy(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert doctor.main(["--unsupported-layout"]) == 0


def test_doctor_never_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A doctor that repairs what it finds is a doctor whose findings nobody reads."""
    from agentloop import store as store_mod

    repo = healthy(tmp_path)
    before = {name: store_mod.Store(repo).document_digest(name) for name in ("plan", "state", "review")}
    monkeypatch.chdir(tmp_path)
    doctor.main([])
    after = {name: store_mod.Store(repo).document_digest(name) for name in ("plan", "state", "review")}
    assert before == after
