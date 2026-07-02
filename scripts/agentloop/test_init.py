"""Verify init.py's placeholder surgery and idempotence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import init
import pytest

_PYPROJECT = '[project]\nname = "project-name"\nrequires-python = ">=3.13"\n'

_STATE = """---
project: "<enter the product name>"
branch: "<enter the work branch name>"  # e.g. build/<product>. Implement on this branch.
current_phase: brief
gates:
  requirements: pending
updated_at: "<YYYY-MM-DD>"
---
# board
"""

_CONFIG = "gates:\n  enforce_hook: true\n  template_mode: true\n"


def test_replace_pyproject_name_touches_only_the_name() -> None:
    out = init.replace_pyproject_name(_PYPROJECT, "demo")
    assert 'name = "demo"' in out
    assert 'requires-python = ">=3.13"' in out


def test_fill_state_fills_placeholders_and_keeps_comments() -> None:
    out = init.fill_state(_STATE, "demo", "build/demo", "2026-07-02")
    assert 'project: "demo"' in out
    assert 'branch: "build/demo"  # e.g. build/<product>. Implement on this branch.' in out
    assert 'updated_at: "2026-07-02"' in out
    assert "current_phase: brief" in out  # the rest is untouched


def test_disable_template_mode_flips_only_that_flag() -> None:
    out = init.disable_template_mode(_CONFIG)
    assert "template_mode: false" in out
    assert "enforce_hook: true" in out


def test_transforms_are_idempotent() -> None:
    once = init.fill_state(_STATE, "demo", "build/demo", "2026-07-02")
    assert init.fill_state(once, "demo", "build/demo", "2026-07-02") == once
    assert init.disable_template_mode(init.disable_template_mode(_CONFIG)) == init.disable_template_mode(_CONFIG)


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (tmp_path / ".agentloop" / "state.md").write_text(_STATE, encoding="utf-8")
    (tmp_path / ".agentloop" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_main_fills_everything_and_reruns_as_noop(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init.main(["--name", "demo"]) == 0
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert 'project: "demo"' in state
    assert 'branch: "build/demo"' in state  # branch defaults to build/<name>
    assert "template_mode: false" in (project / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert 'name = "demo"' in (project / "pyproject.toml").read_text(encoding="utf-8")
    capsys.readouterr()
    assert init.main(["--name", "demo"]) == 0  # idempotent re-run
    assert "ok (already set)" in capsys.readouterr().out


def test_main_requires_a_name(project: Path) -> None:
    assert init.main([]) == 2
