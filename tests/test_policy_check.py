"""policy_check is the base-side CI meta-policy: it must refuse a head that weakens the base (plan §29).

Every check runs over a real git tree so the base-side read is exercised as CI does it: an exact SHA
requirement, a legacy marker reappearing, a banned config key, and a broken audit chain (E2E-21/22).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentloop import policy_check
from agentloop import repo as repo_mod
from tests._support import chain, make_config, make_state, seed_repo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, state=make_state(project="p"), config=make_config())
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return repo_mod.Repo(tmp_path)


def _head(repo: repo_mod.Repo) -> str:
    return repo._git_rc("rev-parse", "HEAD")[1].strip()


@pytest.mark.integration
def test_a_clean_head_passes(repo: repo_mod.Repo) -> None:
    head = _head(repo)
    assert policy_check.check(repo, "0" * 40, head) == []


@pytest.mark.integration
def test_a_mutable_ref_is_refused(repo: repo_mod.Repo) -> None:
    head = _head(repo)
    problems = policy_check.check(repo, "main", head)  # a branch name, not an exact SHA
    assert problems and "mutable ref is not a base" in problems[0]


def test_a_short_sha_is_refused() -> None:
    repo = repo_mod.Repo(Path("/nonexistent"))  # SHA-shape check happens before any git call
    assert policy_check.check(repo, "0" * 40, "abc123")


@pytest.mark.integration
def test_a_legacy_marker_in_the_head_tree_fails(repo: repo_mod.Repo) -> None:
    (repo.root / ".agentloop" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "reintroduce tasks.yaml")
    problems = policy_check.check(repo, "0" * 40, _head(repo))
    assert any("0.8.x layout marker" in p for p in problems)


@pytest.mark.integration
def test_a_banned_config_key_fails(repo: repo_mod.Repo) -> None:
    # Re-introducing `gates.enforce_hook` is a gate-weakening bypass 0.9.0 deleted (plan §4.1).
    (repo.root / ".agentloop" / "config.yaml").write_text(
        "project:\n  name: p\ngates:\n  enforce_hook: false\n", encoding="utf-8"
    )
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "weaken the gate")
    problems = policy_check.check(repo, "0" * 40, _head(repo))
    assert any("gates.enforce_hook" in p for p in problems)


@pytest.mark.integration
def test_a_broken_audit_chain_fails(repo: repo_mod.Repo) -> None:
    from agentloop import event_chain

    events = chain("task_completed", "task_completed")
    event_chain.append_lines(repo.events, events)
    # Corrupt the chain: drop the first line so the second's prev-link dangles.
    lines = repo.events.read_text(encoding="utf-8").splitlines()
    repo.events.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    problems = policy_check.check(repo, "0" * 40, _head(repo))
    assert any("audit chain" in p for p in problems)


def test_banned_key_scan_is_recursive() -> None:
    text = "project:\n  name: p\nbuild:\n  headless:\n    cmd: claude -p\n"
    assert "build.headless.cmd" in policy_check._banned_config_keys(text)
