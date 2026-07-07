"""Verify init.py's placeholder surgery, greenfield manifest recording, and idempotence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import adopt
import cycle
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
    # A slice of the copied template around the .agentloop core (greenfield = the whole repo).
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "00-product-brief.md").write_text("# Brief\n", encoding="utf-8")
    (tmp_path / "docs" / "10-requirements.md").write_text("# Requirements scaffold\n", encoding="utf-8")
    (tmp_path / "scripts" / "agentloop").mkdir(parents=True)
    (tmp_path / "scripts" / "agentloop" / "dag.py").write_text("# tool\n", encoding="utf-8")
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "req.md").write_text("# /req\n", encoding="utf-8")
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / "agentloop.mk").write_text("build-loop:\n\ttrue\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Rules\n", encoding="utf-8")
    (tmp_path / "VERSION").write_text("0.1.0\n", encoding="utf-8")
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


# --- greenfield manifest ---------------------------------------------------------


def _manifest(root: Path) -> dict:
    return adopt.parse_manifest((root / adopt.MANIFEST_PATH).read_text(encoding="utf-8"))


def test_init_records_greenfield_manifest(project: Path) -> None:
    assert init.main(["--name", "demo", "--source", "https://github.com/you/tpl.git"]) == 0
    data = _manifest(project)
    assert data["mode"] == "init"
    assert data["template"]["source"] == "https://github.com/you/tpl.git"
    assert data["template"]["commit"] == "unknown"  # post rm -rf .git, HEAD is not the template's
    assert data["template"]["version"] == "0.1.0"
    files = data["files"]
    assert files["scripts/agentloop/dag.py"]["owner"] == "template"
    assert files[".claude/commands/req.md"]["owner"] == "template"
    assert files["agentloop.mk"]["owner"] == "template"
    assert files[".agentloop/config.yaml"]["owner"] == "seeded"
    assert files["docs/10-requirements.md"]["owner"] == "seeded"
    assert files[".agentloop/scaffold/docs/10-requirements.md"]["owner"] == "template"
    assert files[cycle.SCAFFOLD_STATE]["owner"] == "seeded"
    # The product owns its root CLAUDE.md and settings.json: marker records only, no rules body.
    assert adopt.AGENTLOOP_RULES_PATH not in files
    assert "CLAUDE.md" not in files
    assert data["settings"] == {"mode": "owned"}
    assert data["claude_md"] == {"mode": "owned"}
    # Seeded hashes reflect the on-disk state after the fills (the pristine baseline for upgrade).
    assert files[".agentloop/state.md"]["hash"] == adopt.norm_hash((project / ".agentloop" / "state.md").read_bytes())


def test_init_manifest_rerun_is_noop(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init.main(["--name", "demo", "--source", "src"]) == 0
    before = (project / adopt.MANIFEST_PATH).read_text(encoding="utf-8")
    capsys.readouterr()
    assert init.main(["--name", "demo", "--source", "src"]) == 0
    assert (project / adopt.MANIFEST_PATH).read_text(encoding="utf-8") == before
    assert f"ok (already set): {adopt.MANIFEST_PATH}" in capsys.readouterr().out


def test_init_backfills_only_an_empty_source(project: Path) -> None:
    assert init.main(["--name", "demo"]) == 0  # FROM omitted
    assert _manifest(project)["template"]["source"] == ""
    files_before = _manifest(project)["files"]
    # Later re-run with FROM= records the source WITHOUT rebasing the hashes on edited files.
    (project / "scripts" / "agentloop" / "dag.py").write_text("# locally edited\n", encoding="utf-8")
    assert init.main(["--name", "demo", "--source", "https://github.com/you/tpl.git"]) == 0
    data = _manifest(project)
    assert data["template"]["source"] == "https://github.com/you/tpl.git"
    assert data["files"] == files_before


def test_init_never_overwrites_a_recorded_source(project: Path) -> None:
    assert init.main(["--name", "demo", "--source", "first"]) == 0
    assert init.main(["--name", "demo", "--source", "second"]) == 0
    assert _manifest(project)["template"]["source"] == "first"
