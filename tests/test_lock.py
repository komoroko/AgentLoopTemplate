"""Verify lock.py: the agentloop.lock read/write round-trip and the version-skew posture."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import lock
from agentloop import repo as repo_mod


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    (tmp_path / ".agentloop").mkdir()
    return repo_mod.Repo(tmp_path)


def test_new_write_read_roundtrip(repo: repo_mod.Repo) -> None:
    data = lock.new("0.7.0", "git+https://example.com/AgentLoopTemplate")
    data["prompts"]["files"]["prompts/commands/req.md"] = lock.norm_hash(b"body\n")
    lock.write(repo.lock, data)
    text = repo.lock.read_text(encoding="utf-8")
    assert text.startswith("#")  # the do-not-edit header survives
    loaded = lock.read(repo.lock)
    assert loaded is not None
    assert loaded["version"] == lock.FORMAT_VERSION
    assert lock.tool_version_of(loaded) == "0.7.0"
    assert loaded["schema"] == {"config": 1, "tasks": 1}
    assert loaded["prompts"]["files"]["prompts/commands/req.md"] == lock.norm_hash(b"body\n")
    assert loaded["created_at"] and loaded["updated_at"]


def test_read_absent_is_none_and_broken_raises(repo: repo_mod.Repo) -> None:
    assert lock.read(repo.lock) is None
    repo.lock.write_text(": not yaml [", encoding="utf-8")
    with pytest.raises(lock.LockError):
        lock.read(repo.lock)
    repo.lock.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(lock.LockError):
        lock.read(repo.lock)


def test_newer_lock_format_is_refused_with_upgrade_hint(repo: repo_mod.Repo) -> None:
    repo.lock.write_text(f"version: {lock.FORMAT_VERSION + 1}\n", encoding="utf-8")
    with pytest.raises(lock.LockError) as exc:
        lock.read(repo.lock)
    assert "upgrade the tool" in str(exc.value)


def test_startup_warning_matrix(repo: repo_mod.Repo) -> None:
    # no lock → silent
    assert lock.startup_warning(repo, "0.7.0") is None
    # same version → silent
    lock.write(repo.lock, lock.new("0.7.0", "src"))
    assert lock.startup_warning(repo, "0.7.0") is None
    # lock written by a newer tool → "upgrade the tool"
    lock.write(repo.lock, lock.new("0.8.0", "src"))
    warning = lock.startup_warning(repo, "0.7.0")
    assert warning and "upgrade the tool" in warning
    # lock written by an older tool → "agentloop sync"
    lock.write(repo.lock, lock.new("0.6.0", "src"))
    warning = lock.startup_warning(repo, "0.7.0")
    assert warning and "agentloop sync" in warning


def test_norm_hash_normalizes_crlf() -> None:
    assert lock.norm_hash(b"a\r\nb\n") == lock.norm_hash(b"a\nb\n")
    assert lock.norm_hash(b"x") != lock.norm_hash(b"y")


def test_schema_version_newer_than_parser_is_refused(tmp_path: Path) -> None:
    """dag.load / Config.load refuse a schema_version they do not know."""
    import yaml

    from agentloop import build_loop, dag

    tasks = tmp_path / "tasks.yaml"
    tasks.write_text("schema_version: 99\ntasks: []\n", encoding="utf-8")
    with pytest.raises(dag.DagError) as exc:
        dag.load(tasks)
    assert "upgrade the tool" in str(exc.value)

    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"schema_version": 99, "build": {"quality_gate": {"steps": []}}}), encoding="utf-8"
    )
    with pytest.raises(ValueError) as exc2:
        build_loop.Config.load(str(config))
    assert "upgrade the tool" in str(exc2.value)


def test_current_schema_version_is_accepted(tmp_path: Path) -> None:
    from agentloop import dag

    tasks = tmp_path / "tasks.yaml"
    tasks.write_text("schema_version: 1\ntasks: []\n", encoding="utf-8")
    assert dag.load(tasks).tasks == ()
