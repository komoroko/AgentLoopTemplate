"""Verify init.py's placeholder surgery, greenfield manifest recording, and idempotence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentloop import adopt, cycle, init

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

_CONFIG = """build:
  headless:
    cmd: ["claude", "-p"]
gates:
  enforce_hook: true
  template_mode: true
"""

_BRIEF = """# Product Brief

## What do you want to build? (1-3 lines)
<!-- e.g. A CLI tool. -->


## For whom / what problem to solve
-
"""


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
    (tmp_path / "docs" / "00-product-brief.md").write_text(_BRIEF, encoding="utf-8")
    (tmp_path / "docs" / "10-requirements.md").write_text("# Requirements scaffold\n", encoding="utf-8")
    (tmp_path / "src" / "agentloop").mkdir(parents=True)
    (tmp_path / "src" / "agentloop" / "dag.py").write_text("# tool\n", encoding="utf-8")
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


def _manifest(root: Path) -> dict[str, Any]:
    return adopt.parse_manifest((root / adopt.MANIFEST_PATH).read_text(encoding="utf-8"))


def test_init_records_greenfield_manifest(project: Path) -> None:
    assert init.main(["--name", "demo", "--source", "https://github.com/you/tpl.git"]) == 0
    data = _manifest(project)
    assert data["mode"] == "init"
    assert data["template"]["source"] == "https://github.com/you/tpl.git"
    assert data["template"]["commit"] == "unknown"  # post rm -rf .git, HEAD is not the template's
    assert data["template"]["version"] == "0.1.0"
    files = data["files"]
    assert files["src/agentloop/dag.py"]["owner"] == "template"
    assert files[".claude/commands/req.md"]["owner"] == "template"
    assert files["agentloop.mk"]["owner"] == "template"
    assert files[".agentloop/config.yaml"]["owner"] == "seeded"
    assert files["docs/10-requirements.md"]["owner"] == "seeded"
    assert files[".agentloop/scaffold/docs/10-requirements.md"]["owner"] == "template"
    assert files[cycle.SCAFFOLD_STATE]["owner"] == "seeded"
    # Snapshot of a SPECIAL (target-adapted) doc is seeded like in adopt — upgrade must not
    # plan a remove for it just because template_items has no entry for SPECIAL files.
    assert files[".agentloop/scaffold/docs/00-product-brief.md"]["owner"] == "seeded"
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
    (project / "src" / "agentloop" / "dag.py").write_text("# locally edited\n", encoding="utf-8")
    assert init.main(["--name", "demo", "--source", "https://github.com/you/tpl.git"]) == 0
    data = _manifest(project)
    assert data["template"]["source"] == "https://github.com/you/tpl.git"
    assert data["files"] == files_before


def test_init_never_overwrites_a_recorded_source(project: Path) -> None:
    assert init.main(["--name", "demo", "--source", "first"]) == 0
    assert init.main(["--name", "demo", "--source", "second"]) == 0
    assert _manifest(project)["template"]["source"] == "first"


# --- fill_brief: the wizard's brief insertion (pure) ------------------------------


def test_fill_brief_inserts_after_the_example_comment() -> None:
    out = init.fill_brief(_BRIEF, "A CLI task tool.\nLocal-first.")
    lines = out.splitlines()
    at = lines.index("<!-- e.g. A CLI tool. -->")
    assert lines[at + 1 : at + 3] == ["A CLI task tool.", "Local-first."]
    assert "## For whom / what problem to solve" in out  # the rest survives


def test_fill_brief_never_overwrites_existing_content() -> None:
    filled = init.fill_brief(_BRIEF, "first answer")
    assert init.fill_brief(filled, "second answer") == filled


def test_fill_brief_tolerates_a_missing_heading() -> None:
    assert init.fill_brief("# Custom Brief\nno sections\n", "x") == "# Custom Brief\nno sections\n"


# --- wizard: interactive first-run setup (`./agentloop start`) --------------------


def _feed(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    feed = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(feed))


def test_wizard_full_answers(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # name, branch(default), source, agent CLI choice = codex, two brief lines, blank to finish
    _feed(monkeypatch, ["demo", "", "https://github.com/you/tpl.git", "2", "A CLI task tool.", ""])
    assert init.wizard() == 0
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert 'project: "demo"' in state and 'branch: "build/demo"' in state
    config = (project / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert 'cmd: ["codex", "exec"]' in config and "template_mode: false" in config
    assert "A CLI task tool." in (project / "docs" / "00-product-brief.md").read_text(encoding="utf-8")
    assert _manifest(project)["template"]["source"] == "https://github.com/you/tpl.git"
    # The scaffold snapshot stays pristine — the brief answer lands only in the live doc.
    snapshot = project / ".agentloop" / "scaffold" / "docs" / "00-product-brief.md"
    assert "A CLI task tool." not in snapshot.read_text(encoding="utf-8")


def test_wizard_all_defaults(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # name, then Enter through branch / source / agent CLI (claude) / brief (skip)
    _feed(monkeypatch, ["demo", "", "", "", ""])
    assert init.wizard() == 0
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert 'project: "demo"' in state
    config = (project / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert 'cmd: ["claude", "-p"]' in config  # default preset — untouched
    brief = (project / "docs" / "00-product-brief.md").read_text(encoding="utf-8")
    assert brief == _BRIEF  # skipped — scaffold untouched


def test_wizard_reasks_until_a_name_is_given(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _feed(monkeypatch, ["", "", "demo", "", "", "", ""])
    assert init.wizard() == 0
    assert 'project: "demo"' in (project / ".agentloop" / "state.md").read_text(encoding="utf-8")


def test_wizard_ctrl_c_writes_nothing(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(_prompt: str = "") -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert init.wizard() == 130
    assert "nothing was written" in capsys.readouterr().err
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert 'project: "<enter the product name>"' in state  # untouched
