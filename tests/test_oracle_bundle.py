"""Freezing an oracle bundle is the "you cannot move the goalposts later" mechanism.

The digest is over the *committed* tree, so these tests init a real git repo and commit the
bundle — a bundle that is only on disk is not frozen. They carry the `integration` marker for
that reason (real git/subprocess), matching the rest of the suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentloop import models, oracle_bundle
from agentloop import repo as repo_mod

pytestmark = pytest.mark.integration


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo_with_bundle(root: Path, files: dict[str, str], *, oracle_id: str = "O-001") -> repo_mod.Repo:
    """A committed oracle bundle under `.agentloop/oracles/<id>/`, ready to freeze."""
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.x")
    _git(root, "config", "user.name", "T")
    for rel, body in files.items():
        target = root / ".agentloop" / "oracles" / oracle_id / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "bundle")
    return repo_mod.Repo(root)


def _oracle(
    oracle_id: str = "O-001", *, digest: str = "", risk: str = "low", controls: list[dict[str, object]] | None = None
) -> models.Oracle:
    bundle: dict[str, str] = {"root": f".agentloop/oracles/{oracle_id}"}
    if digest:
        bundle["digest"] = digest
    raw: dict[str, object] = {
        "id": oracle_id,
        "claim_ids": ["C-001"],
        "risk": risk,
        "bundle": bundle,
    }
    if controls is not None:
        raw["negative_controls"] = controls
    return models.Oracle(raw)


def test_freeze_digest_is_stable_across_runs(tmp_path: Path) -> None:
    repo = _repo_with_bundle(tmp_path, {"harness.py": "print(1)\n", "fixtures/a.txt": "x\n"})
    first = oracle_bundle.freeze(repo, _oracle())
    second = oracle_bundle.freeze(repo, _oracle())
    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")
    assert {b.path for b in first.blobs} == {
        ".agentloop/oracles/O-001/harness.py",
        ".agentloop/oracles/O-001/fixtures/a.txt",
    }


def test_verify_frozen_accepts_an_unchanged_bundle(tmp_path: Path) -> None:
    repo = _repo_with_bundle(tmp_path, {"harness.py": "print(1)\n"})
    digest = oracle_bundle.freeze(repo, _oracle()).digest
    ok, message = oracle_bundle.verify_frozen(repo, _oracle(digest=digest))
    assert ok
    assert "intact" in message


def test_verify_frozen_catches_an_edited_fixture(tmp_path: Path) -> None:
    """The classic 'make the oracle pass by editing what it checks' — the digest must move."""
    repo = _repo_with_bundle(tmp_path, {"harness.py": "print(1)\n", "expected.txt": "42\n"})
    frozen = oracle_bundle.freeze(repo, _oracle()).digest
    (tmp_path / ".agentloop/oracles/O-001/expected.txt").write_text("99\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "tamper")
    ok, message = oracle_bundle.verify_frozen(repo, _oracle(digest=frozen))
    assert not ok
    assert "no longer matches" in message


def test_freeze_refuses_an_uncommitted_bundle(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@e.x")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / ".agentloop/oracles/O-001").mkdir(parents=True)
    (tmp_path / ".agentloop/oracles/O-001/harness.py").write_text("x\n", encoding="utf-8")
    repo = repo_mod.Repo(tmp_path)
    with pytest.raises(oracle_bundle.OracleBundleError):
        oracle_bundle.freeze(repo, _oracle())


def test_negative_control_readiness_rejects_a_high_oracle_without_one() -> None:
    problems = oracle_bundle.check_negative_controls(_oracle(risk="high", controls=[]))
    assert any("negative control" in p for p in problems)


def test_negative_control_readiness_rejects_expected_exit_zero() -> None:
    controls = [{"id": "NC-1", "subject_fixture": "bad.py", "expected_exit_code": 0}]
    problems = oracle_bundle.check_negative_controls(_oracle(risk="high", controls=controls))
    assert any("exit 0" in p for p in problems)


def test_negative_control_readiness_passes_a_well_formed_control() -> None:
    controls = [{"id": "NC-1", "subject_fixture": "bad.py", "expected_exit_code": 1}]
    assert oracle_bundle.check_negative_controls(_oracle(risk="high", controls=controls)) == []


def test_freeze_all_collects_bundles_and_problems(tmp_path: Path) -> None:
    # Two distinct committed bundles: a well-formed low oracle and a high one missing its control.
    for oracle_id in ("O-001", "O-002"):
        target = tmp_path / ".agentloop" / "oracles" / oracle_id / "harness.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {oracle_id}\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@e.x")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "bundles")
    repo = repo_mod.Repo(tmp_path)
    good = _oracle("O-001")
    high_without_control = _oracle("O-002", risk="high", controls=[])
    plan = models.Plan({"oracles": [good.raw, high_without_control.raw]})
    report = oracle_bundle.freeze_all(repo, plan)
    assert not report.ok  # the high oracle has no negative control
    assert any("negative control" in p for p in report.problems)
    assert len(report.bundles) == 2  # both bundles still freeze
    assert report.bundle_set_digest().startswith("sha256:")
