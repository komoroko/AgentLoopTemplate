"""Verify adopt.py's merge logic and never-overwrite installation."""

from __future__ import annotations

import json
from pathlib import Path

import adopt
import pytest

# --- pure logic ----------------------------------------------------------------

_CONFIG = """gates:
  enforce_hook: true
  template_mode: true
  guard_paths:
    docs/20-design.md: requirements
    docs/tasks/: design
    backend/: tasks
    frontend/: tasks
    scripts/: tasks        # product scripts (scripts/agentloop/ is always allowed)
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
    out = adopt.brownfield_config(_CONFIG, "npm test", "npm run lint")
    assert "template_mode: false" in out
    # Code paths are commented out (existing development keeps flowing), docs stay guarded.
    assert "\n    # backend/: tasks" in out
    assert "\n    # frontend/: tasks" in out
    assert "\n    # scripts/: tasks" in out
    assert "docs/tasks/: design" in out
    assert 'run: "npm test"' in out
    assert 'run: "npm run lint"' in out


def test_brownfield_config_keeps_make_cmds_when_flags_absent() -> None:
    out = adopt.brownfield_config(_CONFIG, "", "")
    assert 'run: "make test"' in out
    assert 'run: "make check"' in out


def test_merge_settings_appends_missing_only() -> None:
    existing = {
        "permissions": {"allow": ["Read", "Bash(npm test:*)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./my-hook.sh"}]}]},
    }
    template = {
        "permissions": {"allow": ["Read", "Bash(make build-loop:*)"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "python gate_guard.py"}]}
            ],
            "SessionStart": [{"hooks": [{"type": "command", "command": "cat state.md"}]}],
        },
    }
    merged, notes = adopt.merge_settings(existing, template)
    assert merged["permissions"]["allow"] == ["Read", "Bash(npm test:*)", "Bash(make build-loop:*)"]
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "./my-hook.sh"  # existing kept first
    assert merged["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == "python gate_guard.py"
    assert [g["hooks"][0]["command"] for g in merged["hooks"]["SessionStart"]] == ["cat state.md"]
    assert notes
    # Idempotent: a second merge adds nothing.
    merged2, notes2 = adopt.merge_settings(merged, template)
    assert notes2 == []
    assert merged2 == merged


# --- installation against a mini-template + existing target ---------------------


@pytest.fixture
def template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "template"
    (root / ".agentloop").mkdir(parents=True)
    (root / ".agentloop" / "state.md").write_text(
        '---\nproject: "<enter the product name>"\nbranch: "<enter the work branch name>"\n'
        'updated_at: "<YYYY-MM-DD>"\n---\n',
        encoding="utf-8",
    )
    (root / ".agentloop" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (root / ".agentloop" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    (root / "scripts" / "agentloop").mkdir(parents=True)
    (root / "scripts" / "agentloop" / "dag.py").write_text("# tool\n", encoding="utf-8")
    (root / ".claude" / "commands").mkdir(parents=True)
    (root / ".claude" / "commands" / "req.md").write_text("# /req\n", encoding="utf-8")
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(make build-loop:*)"]}, "hooks": {}}), encoding="utf-8"
    )
    (root / "docs").mkdir()
    (root / "docs" / "00-product-brief.md").write_text("# Brief\n", encoding="utf-8")
    (root / "docs" / "10-requirements.md").write_text("# Requirements scaffold\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# AgentLoop rules\n", encoding="utf-8")
    (root / "agentloop.mk").write_text("build-loop:\n\ttrue\n", encoding="utf-8")
    monkeypatch.setattr(adopt, "TEMPLATE_ROOT", root)
    return root


@pytest.fixture
def target(tmp_path: Path) -> Path:
    root = tmp_path / "existing-repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('existing')\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# My project rules\nDo the thing.\n", encoding="utf-8")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(npm test:*)"]}}), encoding="utf-8"
    )
    (root / "docs").mkdir()
    (root / "docs" / "10-requirements.md").write_text("EXISTING product requirements\n", encoding="utf-8")
    return root


def test_adopt_installs_without_overwriting(template: Path, target: Path) -> None:
    rc = adopt.main(["--target", str(target), "--name", "demo", "--test-cmd", "npm test"])
    assert rc == 0
    # New machinery landed.
    assert (target / "agentloop.mk").exists()
    assert (target / "scripts" / "agentloop" / "dag.py").exists()
    assert (target / ".claude" / "commands" / "req.md").exists()
    # Existing files were not overwritten.
    assert (target / "docs" / "10-requirements.md").read_text(encoding="utf-8") == "EXISTING product requirements\n"
    assert "My project rules" in (target / "CLAUDE.md").read_text(encoding="utf-8")
    # state.md placeholders were filled; config got brownfield defaults.
    state = (target / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert 'project: "demo"' in state and 'branch: "build/demo"' in state
    config = (target / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert "template_mode: false" in config and "\n    # backend/: tasks" in config
    assert 'run: "npm test"' in config
    # The brief carries the brownfield note.
    assert "/onboard" in (target / "docs" / "00-product-brief.md").read_text(encoding="utf-8")


def test_adopt_merges_claude_md_and_settings(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    claude = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith("# My project rules")
    assert adopt.CLAUDE_IMPORT_MARKER in claude
    assert f"@{adopt.AGENTLOOP_RULES_PATH}" in claude
    assert (target / adopt.AGENTLOOP_RULES_PATH).read_text(encoding="utf-8") == "# AgentLoop rules\n"
    settings = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["permissions"]["allow"] == ["Bash(npm test:*)", "Bash(make build-loop:*)"]


def test_adopt_rerun_is_idempotent(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    claude_before = (target / "CLAUDE.md").read_text(encoding="utf-8")
    settings_before = (target / ".claude" / "settings.json").read_text(encoding="utf-8")
    rc = adopt.main(["--target", str(target), "--name", "other"])  # different name must not clobber
    assert rc == 0
    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == claude_before  # @import appended once
    assert (target / ".claude" / "settings.json").read_text(encoding="utf-8") == settings_before
    assert 'project: "demo"' in (target / ".agentloop" / "state.md").read_text(encoding="utf-8")


def test_adopt_takes_scaffold_snapshot(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    snap = target / ".agentloop" / "scaffold" / "docs"
    # The snapshot holds the target's docs as installed (pristine scaffolds + preexisting docs).
    assert (snap / "00-product-brief.md").exists()


def test_adopt_dry_run_writes_nothing(template: Path, target: Path) -> None:
    rc = adopt.main(["--target", str(target), "--name", "demo", "--dry-run"])
    assert rc == 0
    assert not (target / ".agentloop").exists()
    assert not (target / "agentloop.mk").exists()


def test_adopt_refuses_template_itself(template: Path) -> None:
    assert adopt.main(["--target", str(template), "--name", "demo"]) == 1


def test_adopt_requires_target_and_name(template: Path) -> None:
    assert adopt.main([]) == 2
