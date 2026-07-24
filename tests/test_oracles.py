"""The oracle runner turns a sandbox run into a *bound* verdict — reusable only against an
identical world, and only conclusive when the negative controls also behaved.

Binding, reuse, profile selection, and the cache round-trip are pure and need no container.
The `run_oracle` path needs a committed, frozen bundle (real git) but a fake executor stands
in for docker, so it stays fast; those tests carry the `integration` marker for the git use.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from agentloop import executors, models, oracles
from agentloop import repo as repo_mod
from agentloop import store as store_mod

# --- pure: binding and reuse --------------------------------------------------


def _oracle(**overrides: object) -> models.Oracle:
    raw: dict[str, object] = {
        "id": "O-001",
        "claim_ids": ["C-001"],
        "risk": "low",
        "bundle": {"root": ".agentloop/oracles/O-001", "digest": "sha256:" + "b" * 64},
        "command": ["pytest", "-q"],
        "subject_paths": ["src/"],
        "runner": {"image": "localhost/agentloop-oracle@sha256:" + "c" * 64, "network_profile": "none"},
    }
    raw.update(overrides)
    return models.Oracle(raw)


def test_result_binding_has_all_seven_parts() -> None:
    binding = oracles.result_binding(
        change_digest="sha256:" + "1" * 64,
        oracle=_oracle(),
        image_digest="sha256:" + "c" * 64,
        subject_mount_digest="sha256:" + "2" * 64,
    )
    assert set(binding) == {
        "change_digest",
        "oracle_bundle_digest",
        "runner_image_digest",
        "environment_digest",
        "network_profile_digest",
        "command_digest",
        "subject_mount_digest",
    }
    assert binding["oracle_bundle_digest"] == _oracle().bundle_digest


def test_can_reuse_requires_every_part_to_match() -> None:
    binding = oracles.result_binding(
        change_digest="sha256:" + "1" * 64,
        oracle=_oracle(),
        image_digest="sha256:" + "c" * 64,
        subject_mount_digest="sha256:" + "2" * 64,
    )
    assert oracles.can_reuse(binding, dict(binding))
    for key in binding:
        drifted = dict(binding)
        drifted[key] = "sha256:" + "9" * 64
        assert not oracles.can_reuse(binding, drifted), f"a change in {key} must force a re-run"


def test_can_reuse_refuses_empty_digests() -> None:
    """Two runs that both recorded nothing are not 'the same world' — empty never reuses."""
    empty = dict.fromkeys(
        (
            "change_digest",
            "oracle_bundle_digest",
            "runner_image_digest",
            "environment_digest",
            "network_profile_digest",
            "command_digest",
            "subject_mount_digest",
        ),
        "",
    )
    assert not oracles.can_reuse(empty, dict(empty))


def test_profile_for_refuses_a_host_runner() -> None:
    oracle = _oracle(runner={"executor": "host"})
    with pytest.raises(oracles.OracleError, match="sandboxed"):
        oracles._profile_for(None, oracle)


def test_profile_for_wraps_a_runner_image_in_an_oci_profile() -> None:
    profile = oracles._profile_for(None, _oracle())
    assert profile.is_sandboxed
    assert profile.image_digest == "sha256:" + "c" * 64


# --- cache round-trip ---------------------------------------------------------


def _result(passed: bool = True) -> oracles.OracleResult:
    binding = oracles.result_binding(
        change_digest="sha256:" + "1" * 64,
        oracle=_oracle(),
        image_digest="sha256:" + "c" * 64,
        subject_mount_digest="sha256:" + "2" * 64,
    )
    return oracles.OracleResult(
        oracle_id="O-001",
        passed=passed,
        exit_code=0 if passed else 1,
        output="pretend output",
        image_digest="sha256:" + "c" * 64,
        binding=binding,
    )


def test_cache_round_trip_reuses_a_matching_binding(make_repo_obj: Callable[..., repo_mod.Repo]) -> None:
    repo = make_repo_obj()
    result = _result()
    oracles.cache_result(repo, result)
    loaded = oracles.load_cached(repo, "O-001", result.binding)
    assert loaded is not None
    assert loaded.passed
    assert loaded.exit_code == 0


def test_cache_miss_on_a_drifted_binding(make_repo_obj: Callable[..., repo_mod.Repo]) -> None:
    repo = make_repo_obj()
    oracles.cache_result(repo, _result())
    drifted = dict(_result().binding)
    drifted["change_digest"] = "sha256:" + "9" * 64
    assert oracles.load_cached(repo, "O-001", drifted) is None


def test_cache_files_are_owner_only(make_repo_obj: Callable[..., repo_mod.Repo]) -> None:
    repo = make_repo_obj()
    path = oracles.cache_result(repo, _result())
    assert (path.stat().st_mode & 0o777) == 0o600


# --- run_oracle with a committed bundle + fake executor -----------------------


class FakeExecutor(executors.Executor):
    """Stands in for docker: returns an exit code decided per-spec, records the specs it saw."""

    def __init__(self, exit_for: Callable[[executors.ExecutionSpec], int]) -> None:
        self._exit_for = exit_for
        self.specs: list[executors.ExecutionSpec] = []

    def run(self, spec: executors.ExecutionSpec) -> executors.ExecutionResult:
        self.specs.append(spec)
        code = self._exit_for(spec)
        return executors.ExecutionResult(exit_code=code, output="", image_digest="sha256:" + "c" * 64)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _frozen_oracle(root: Path, **overrides: object) -> models.Oracle:
    """Commit a one-file bundle and return an oracle carrying its real frozen digest."""
    from agentloop import oracle_bundle

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.x")
    _git(root, "config", "user.name", "T")
    target = root / ".agentloop/oracles/O-001/harness.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print(1)\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "bundle")
    repo = repo_mod.Repo(root)
    base = _oracle(**overrides)
    digest = oracle_bundle.freeze(repo, base).digest
    raw = dict(base.raw)
    raw["bundle"] = {**base.raw["bundle"], "digest": digest}  # type: ignore[dict-item]
    return models.Oracle(raw)


@pytest.mark.integration
def test_run_oracle_passes_when_the_command_matches_expected(tmp_path: Path) -> None:
    oracle = _frozen_oracle(tmp_path, expected_exit_code=0)
    fake = FakeExecutor(lambda spec: 0)
    result = oracles.run_oracle(repo_mod.Repo(tmp_path), oracle, change_digest="sha256:" + "1" * 64, executor=fake)
    assert result.passed
    assert result.conclusive
    # A pytest command must have the plugin-autoload hardening forced in.
    assert fake.specs[0].env.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"


@pytest.mark.integration
def test_run_oracle_fails_on_an_unexpected_exit(tmp_path: Path) -> None:
    oracle = _frozen_oracle(tmp_path, expected_exit_code=0)
    result = oracles.run_oracle(
        repo_mod.Repo(tmp_path),
        oracle,
        change_digest="sha256:" + "1" * 64,
        executor=FakeExecutor(lambda spec: 1),
    )
    assert not result.passed


@pytest.mark.integration
def test_run_oracle_refuses_a_drifted_bundle(tmp_path: Path) -> None:
    oracle = _frozen_oracle(tmp_path)
    (tmp_path / ".agentloop/oracles/O-001/harness.py").write_text("tampered\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "tamper")
    with pytest.raises(oracles.OracleError, match="no longer matches"):
        oracles.run_oracle(
            repo_mod.Repo(tmp_path),
            oracle,
            change_digest="sha256:" + "1" * 64,
            executor=FakeExecutor(lambda spec: 0),
        )


@pytest.mark.integration
def test_negative_control_that_does_not_reject_makes_the_pass_inconclusive(tmp_path: Path) -> None:
    """A high oracle that exits 0 on both the real subject and a known violation is not looking."""
    controls = [{"id": "NC-1", "subject_fixture": "bad.py", "expected_exit_code": 1}]
    oracle = _frozen_oracle(tmp_path, risk="high", expected_exit_code=0, negative_controls=controls)
    # The fake exits 0 for *everything* — including the violating subject it was supposed to reject.
    result = oracles.run_oracle(
        repo_mod.Repo(tmp_path),
        oracle,
        change_digest="sha256:" + "1" * 64,
        executor=FakeExecutor(lambda spec: 0),
    )
    assert result.passed  # the real-subject run passed
    assert not result.negative_controls_ok  # but the control did not reject
    assert not result.conclusive  # so the pass is worthless


@pytest.mark.integration
def test_negative_control_that_rejects_keeps_the_pass_conclusive(tmp_path: Path) -> None:
    controls = [{"id": "NC-1", "subject_fixture": "bad.py", "expected_exit_code": 1}]
    oracle = _frozen_oracle(tmp_path, risk="high", expected_exit_code=0, negative_controls=controls)
    # Exit 0 on the real subject, exit 1 on the negative control (it correctly rejects the violation).
    fake = FakeExecutor(lambda spec: 1 if "--agentloop-subject" in spec.command else 0)
    result = oracles.run_oracle(repo_mod.Repo(tmp_path), oracle, change_digest="sha256:" + "1" * 64, executor=fake)
    assert result.conclusive
    assert result.negative_controls[0].rejected


@pytest.mark.integration
def test_run_oracle_refuses_a_command_less_oracle(tmp_path: Path) -> None:
    oracle = _frozen_oracle(tmp_path, command=[])
    with pytest.raises(oracles.OracleError, match="no command"):
        oracles.run_oracle(
            repo_mod.Repo(tmp_path),
            oracle,
            change_digest="sha256:" + "1" * 64,
            executor=FakeExecutor(lambda spec: 0),
        )


def test_load_cached_survives_a_corrupt_file(make_repo_obj: Callable[..., repo_mod.Repo]) -> None:
    repo = make_repo_obj()
    result = _result()
    path = oracles.cache_result(repo, result)
    path.write_text("{ this is not json", encoding="utf-8")
    assert oracles.load_cached(repo, "O-001", result.binding) is None


def test_cache_dir_is_under_xdg_cache(make_repo_obj: Callable[..., repo_mod.Repo]) -> None:
    repo = make_repo_obj()
    path = oracles.cache_result(repo, _result())
    assert store_mod.cache_dir(repo) in path.parents
