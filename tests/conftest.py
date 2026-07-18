"""Shared fixtures. The pure, importable helpers they build on live in `tests/_support.py`.

`make_repo` seeds a tmp `.agentloop/` repo and (via `monkeypatch.chdir`, which auto-restores)
chdirs into it — replacing the hand-rolled `os.getcwd()`/try/finally dance every test fixture
used to carry. `chdir_tmp` does the chdir alone for tests that seed their own files.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests._support import seed_repo


@pytest.fixture
def make_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Factory: seed a repo under tmp_path and chdir into it (auto-restored). Kwargs → `seed_repo`."""

    def _make(*, chdir: bool = True, **kwargs: object) -> Path:
        seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
        if chdir:
            monkeypatch.chdir(tmp_path)
        return tmp_path

    return _make


@pytest.fixture
def chdir_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir into an empty tmp repo (auto-restored); for tests that seed their own files."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
