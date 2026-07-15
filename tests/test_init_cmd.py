"""Verify init_cmd.py: seeding from package data, brownfield detection, and idempotence."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agentloop import init_cmd
from agentloop import lock as lock_mod

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


# --- pure text surgery ---------------------------------------------------------------


def test_fill_state_fills_placeholders_and_keeps_comments() -> None:
    out = init_cmd.fill_state(_STATE, "demo", "build/demo", "2026-07-02")
    assert 'project: "demo"' in out
    assert 'branch: "build/demo"  # e.g. build/<product>. Implement on this branch.' in out
    assert 'updated_at: "2026-07-02"' in out
    assert "current_phase: brief" in out  # the rest is untouched


def test_disable_template_mode_flips_only_that_flag() -> None:
    out = init_cmd.disable_template_mode(_CONFIG)
    assert "template_mode: false" in out
    assert "enforce_hook: true" in out


def test_transforms_are_idempotent() -> None:
    once = init_cmd.fill_state(_STATE, "demo", "build/demo", "2026-07-02")
    assert init_cmd.fill_state(once, "demo", "build/demo", "2026-07-02") == once
    once = init_cmd.disable_template_mode(_CONFIG)
    assert init_cmd.disable_template_mode(once) == once


def test_fill_brief_inserts_once_and_never_overwrites() -> None:
    out = init_cmd.fill_brief(_BRIEF, "A todo CLI.")
    assert "A todo CLI." in out
    assert out.index("<!--") < out.index("A todo CLI.")  # after the scaffold's example comment
    assert init_cmd.fill_brief(out, "Something else.") == out  # existing words are never replaced
    assert init_cmd.fill_brief("# no heading\n", "X") == "# no heading\n"


_GUARD_CONFIG = """gates:
  enforce_hook: true
  template_mode: true
  guard_paths:
    docs/20-design.md: requirements
    docs/tasks/: design
    src/: tasks
    backend/: tasks
    frontend/: tasks
    scripts/: tasks        # product scripts
build:
  quality_gate:
    steps:
      - name: test
        kind: cmd
        run: "make test"
      - name: check
        kind: cmd
        run: "make check"
"""


def test_brownfield_config_scopes_guard_to_docs_and_sets_cmds() -> None:
    out = init_cmd.brownfield_config(_GUARD_CONFIG, "npm test", "npm run lint")
    assert "template_mode: false" in out
    # Code paths are commented out (existing development keeps flowing), docs stay guarded.
    assert "\n    # src/: tasks" in out
    assert "\n    # backend/: tasks" in out
    assert "\n    # scripts/: tasks" in out
    assert "docs/tasks/: design" in out
    assert 'run: "npm test"' in out
    assert 'run: "npm run lint"' in out


def test_brownfield_config_keeps_make_cmds_when_flags_absent() -> None:
    out = init_cmd.brownfield_config(_GUARD_CONFIG, "", "")
    assert 'run: "make test"' in out
    assert 'run: "make check"' in out


def test_detect_commands_recognizes_the_common_stacks() -> None:
    node = init_cmd.detect_commands({"package.json": '{"scripts": {"test": "vitest", "lint": "eslint ."}}'})
    assert node["test"] == ["npm test"] and node["check"] == ["npm run lint"]
    py = init_cmd.detect_commands({"pyproject.toml": "[tool.pytest]\n[tool.ruff]\n", "uv.lock": ""})
    assert py["test"] == ["uv run pytest"] and py["check"] == ["ruff check ."]
    mk = init_cmd.detect_commands({"makefile": "test:\n\ttrue\ncheck:\n\ttrue\n"})
    assert mk["test"] == ["make test"] and mk["check"] == ["make check"]
    assert init_cmd.detect_commands({}) == {"test": [], "check": []}


def test_is_brownfield_detects_code_markers(tmp_path: Path) -> None:
    assert init_cmd.is_brownfield(tmp_path) is False
    (tmp_path / "docs").mkdir()  # the tool's own dirs never count
    assert init_cmd.is_brownfield(tmp_path) is False
    (tmp_path / "src").mkdir()
    assert init_cmd.is_brownfield(tmp_path) is True


# --- run_init (greenfield) --------------------------------------------------------------


def test_run_init_seeds_a_bare_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "git+https://example.com/agentloop") == 0
    # The SSOT trio, placeholder-filled, gate guard live.
    state = (tmp_path / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert 'project: "demo"' in state and 'branch: "build/demo"' in state
    config = (tmp_path / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert "template_mode: false" in config
    assert "schema_version: 1" in (tmp_path / ".agentloop" / "tasks.yaml").read_text(encoding="utf-8")
    # Docs scaffolds + the pristine snapshot cycle-close restores from.
    assert (tmp_path / "docs" / "00-product-brief.md").is_file()
    assert (tmp_path / "docs" / "10-requirements.md").is_file()
    assert (tmp_path / ".agentloop" / "scaffold" / "docs" / "10-requirements.md").is_file()
    # Materialized artifacts (repo-relative — the wrappers' @-imports depend on these paths).
    assert (tmp_path / ".agentloop" / "prompts" / "commands" / "req.md").is_file()
    assert (tmp_path / ".agentloop" / "schema" / "config.schema.json").is_file()
    assert (tmp_path / ".agentloop" / "AGENTS.agentloop.md").is_file()
    # The agent-neutral pointer, and NO agent surfaces (those are opt-in).
    assert "agentloop-rules" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".github").exists()
    # The lock records the source, the seeds, and the materialized files.
    data = lock_mod.read(tmp_path / ".agentloop" / "agentloop.lock")
    assert data is not None
    assert data["agentloop"]["source"] == "git+https://example.com/agentloop"
    assert ".agentloop/state.md" in data["seeded"]
    assert "prompts/commands/req.md" in data["prompts"]["files"]


def test_run_init_rerun_never_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    state_path = tmp_path / ".agentloop" / "state.md"
    state_path.write_text(state_path.read_text(encoding="utf-8").replace("brief", "design"), encoding="utf-8")
    (tmp_path / "docs" / "10-requirements.md").write_text("FILLED\n", encoding="utf-8")
    capsys.readouterr()
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    out = capsys.readouterr().out
    assert "skip" in out
    assert "design" in state_path.read_text(encoding="utf-8")  # the human's edit survives
    assert (tmp_path / "docs" / "10-requirements.md").read_text(encoding="utf-8") == "FILLED\n"


def test_run_init_brownfield_adapts_config_and_brief(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('existing')\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest", "lint": "eslint ."}}', encoding="utf-8")
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    out = capsys.readouterr().out
    assert "brownfield" in out and "/onboard" in out
    config = (tmp_path / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert "\n    # src/: tasks" in config  # code paths unguarded until re-enabled
    assert 'run: "npm test"' in config and 'run: "npm run lint"' in config
    brief = (tmp_path / "docs" / "00-product-brief.md").read_text(encoding="utf-8")
    assert "Adopted into an existing codebase" in brief
    # The guard config still parses and validates as YAML.
    parsed = yaml.safe_load(config)
    assert parsed["gates"]["guard_paths"].get("docs/tasks/") == "design"
    assert "src/" not in parsed["gates"]["guard_paths"]


def test_main_requires_a_name_without_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert init_cmd.main(["--repo", str(tmp_path)]) == 2
    assert "--name" in capsys.readouterr().err


def test_main_greenfield_flag_overrides_detection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "src").mkdir()  # would auto-detect brownfield
    assert init_cmd.main(["--name", "demo", "--greenfield", "--repo", str(tmp_path)]) == 0
    assert "greenfield" in capsys.readouterr().out
    config = (tmp_path / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert "\n    src/: tasks" in config  # code paths stay guarded (greenfield semantics)
