"""Verify adopt.py's merge logic, never-overwrite installation, and manifest-driven upgrade."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentloop import adopt

# --- pure logic ----------------------------------------------------------------

_CONFIG = """gates:
  enforce_hook: true
  template_mode: true
  guard_paths:
    docs/20-design.md: requirements
    docs/tasks/: design
    backend/: tasks
    frontend/: tasks
    scripts/: tasks        # product scripts (src/agentloop/ is always allowed)
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


def test_merge_settings_appends_missing_only_and_records_added() -> None:
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
    merged, notes, added = adopt.merge_settings(existing, template)
    assert merged["permissions"]["allow"] == ["Read", "Bash(npm test:*)", "Bash(make build-loop:*)"]
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "./my-hook.sh"  # existing kept first
    assert merged["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == "python gate_guard.py"
    assert [g["hooks"][0]["command"] for g in merged["hooks"]["SessionStart"]] == ["cat state.md"]
    assert notes
    # `added` records exactly what was appended — not the preexisting entries.
    assert added["permissions_allow"] == ["Bash(make build-loop:*)"]
    assert {e for e in added["hooks"]} == {"PreToolUse", "SessionStart"}
    # Idempotent: a second merge adds nothing and records nothing.
    merged2, notes2, added2 = adopt.merge_settings(merged, template)
    assert notes2 == []
    assert merged2 == merged
    assert added2 == {"permissions_allow": [], "hooks": {}}


def test_norm_hash_normalizes_crlf() -> None:
    assert adopt.norm_hash(b"a\r\nb\n") == adopt.norm_hash(b"a\nb\n")
    assert adopt.norm_hash(b"x").startswith("sha256:")
    assert adopt.norm_hash(b"x") != adopt.norm_hash(b"y")


def test_default_owner_classification() -> None:
    assert adopt.default_owner("src/agentloop/dag.py") == "template"
    assert adopt.default_owner(".claude/commands/req.md") == "template"
    assert adopt.default_owner(".claude/agents/architect.md") == "template"
    assert adopt.default_owner(".agentloop/prompts/commands/req.md") == "template"
    assert adopt.default_owner(".agentloop/prompts/agents/architect.md") == "template"
    assert adopt.default_owner(".github/prompts/req.prompt.md") == "template"
    assert adopt.default_owner(".github/agents/architect.agent.md") == "template"
    assert adopt.default_owner(".github/hooks/agentloop.json") == "template"
    assert adopt.default_owner(".github/instructions/agentloop.instructions.md") == "template"
    assert adopt.default_owner("agentloop.mk") == "template"
    assert adopt.default_owner("agentloop") == "template"  # the ./agentloop entry-point wrapper
    assert adopt.default_owner(adopt.AGENTLOOP_RULES_PATH) == "template"
    assert adopt.default_owner(".agentloop/tasks.yaml") == "seeded"
    assert adopt.default_owner("docs/10-requirements.md") == "seeded"


def test_manifest_roundtrip_and_version_check() -> None:
    manifest = adopt.build_manifest(
        {"a.py": {"hash": "sha256:x", "owner": "template"}},
        {"created": False, "permissions_allow": [], "hooks": {}},
        {"mode": "merged"},
        "src",
        "main",
        "abc123",
        "2026-07-03",
        None,
    )
    out = adopt.parse_manifest(yaml.safe_dump(manifest, sort_keys=False))
    assert out["files"]["a.py"]["owner"] == "template"
    assert out["template"] == {"source": "src", "commit": "abc123", "ref": "main"}
    assert out["adopted_at"] == "2026-07-03" and out["upgraded_at"] is None
    with pytest.raises(ValueError):
        adopt.parse_manifest("version: 2\n")


def test_build_manifest_records_version_and_mode() -> None:
    m = adopt.build_manifest(
        {}, {}, {"mode": "merged"}, "src", "", "abc", "2026-07-08", None, version="0.1.0", mode="init"
    )
    assert m["mode"] == "init"
    assert m["template"]["version"] == "0.1.0"
    default = adopt.build_manifest({}, {}, {"mode": "merged"}, "src", "", "abc", "2026-07-08", None)
    assert default["mode"] == "adopt"
    assert "version" not in default["template"]  # pre-VERSION template: field omitted, like ref


_CHANGELOG = """# Changelog

## [0.3.0] - 2026-07-08
### Added
- three

## [0.2.0] - 2026-06-01
### Fixed
- two

## [0.1.0] - 2026-05-01
### Added
- one
"""


def test_changelog_between_returns_sections_newer_than_installed() -> None:
    out = adopt.changelog_between(_CHANGELOG, "0.1.0", "0.3.0")
    assert "- three" in out and "- two" in out and "- one" not in out


def test_changelog_between_unknown_installed_shows_latest_with_note() -> None:
    out = adopt.changelog_between(_CHANGELOG, "", "0.3.0")
    assert "installed version unknown" in out
    assert "- three" in out and "- two" not in out


def test_changelog_between_same_version_is_empty() -> None:
    assert adopt.changelog_between(_CHANGELOG, "0.3.0", "0.3.0") == ""


def test_changelog_between_without_headings_is_empty() -> None:
    assert adopt.changelog_between("free-form notes, no release headings\n", "0.1.0", "0.2.0") == ""


def test_plan_upgrade_decision_table() -> None:
    mf = {rel: {"hash": "sha256:old", "owner": "template"} for rel in "abcdefg"}
    tpl = {
        "a": "sha256:new",
        "b": "sha256:new",
        "c": "sha256:new",
        "d": "sha256:new",
        "h": "sha256:new",
        "i": "sha256:new",
    }
    cur: dict[str, str | None] = {
        "a": "sha256:old",  # updated upstream, pristine            → update
        "b": "sha256:edited",  # updated upstream, locally modified    → skip-modified
        "c": None,  # locally deleted                       → restore
        "d": "sha256:new",  # already matches (crash recovery)      → unchanged
        "e": "sha256:old",  # removed upstream, pristine            → remove
        "f": "sha256:edited",  # removed upstream, locally modified    → leave-modified
        "g": None,  # removed upstream, already gone        → unchanged (dropped)
        "h": None,  # new in template, absent               → new
        "i": "sha256:mine",  # new in template, exists (not ours)    → skip-modified
    }
    ops = {i.rel: i.op for i in adopt.plan_upgrade(mf, tpl, cur, force=False)}
    assert ops == {
        "a": "update",
        "b": "skip-modified",
        "c": "restore",
        "d": "unchanged",
        "e": "remove",
        "f": "leave-modified",
        "g": "unchanged",
        "h": "new",
        "i": "skip-modified",
    }
    forced = {i.rel: i.op for i in adopt.plan_upgrade(mf, tpl, cur, force=True)}
    assert forced["b"] == "update" and forced["f"] == "remove" and forced["i"] == "update"


def test_upgrade_settings_replaces_pristine_group_without_duplication() -> None:
    ours_old = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "python gate_guard.py OLD"}]}
    users = {"matcher": "Bash", "hooks": [{"type": "command", "command": "./my-hook.sh"}]}
    existing = {
        "permissions": {"allow": ["Read", "Bash(make old:*)"]},
        "hooks": {"PreToolUse": [users, json.loads(json.dumps(ours_old))]},
    }
    installed = {"permissions_allow": ["Bash(make old:*)"], "hooks": {"PreToolUse": [ours_old]}}
    template = {
        "permissions": {"allow": ["Bash(make new:*)"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "python gate_guard.py NEW"}]}
            ]
        },
    }
    merged, notes, added = adopt.upgrade_settings(existing, installed, template)
    cmds = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert cmds == ["./my-hook.sh", "python gate_guard.py NEW"]  # ours replaced (no dup), the user's kept
    assert merged["permissions"]["allow"] == ["Read", "Bash(make new:*)"]  # stale entry dropped, new added
    assert added["permissions_allow"] == ["Bash(make new:*)"]
    assert added["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python gate_guard.py NEW"
    assert any("dropped by the template" in n for n in notes)


def test_upgrade_settings_leaves_modified_group_alone() -> None:
    installed_group = {"matcher": "Write", "hooks": [{"type": "command", "command": "python gate_guard.py"}]}
    modified = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "python gate_guard.py"}]}
    existing = {"permissions": {"allow": []}, "hooks": {"PreToolUse": [modified]}}
    installed = {"permissions_allow": [], "hooks": {"PreToolUse": [installed_group]}}
    template = {"hooks": {"PreToolUse": [installed_group]}}
    merged, notes, added = adopt.upgrade_settings(existing, installed, template)
    # The user widened the matcher: the group is theirs now — left as-is, and the template's
    # version is NOT re-added (its command is already present), so no near-duplicate appears.
    assert merged["hooks"]["PreToolUse"] == [modified]
    assert any("locally modified" in n for n in notes)
    assert added["hooks"] == {}


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
    (root / "src" / "agentloop").mkdir(parents=True)
    (root / "src" / "agentloop" / "dag.py").write_text("# tool\n", encoding="utf-8")
    (root / ".claude" / "commands").mkdir(parents=True)
    (root / ".claude" / "commands" / "req.md").write_text("# /req\n", encoding="utf-8")
    (root / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(make build-loop:*)"]},
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "python gate_guard.py v1"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (root / ".github" / "prompts").mkdir(parents=True)
    (root / ".github" / "prompts" / "req.prompt.md").write_text("# /req (copilot)\n", encoding="utf-8")
    (root / ".github" / "hooks").mkdir(parents=True)
    (root / ".github" / "hooks" / "agentloop.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "00-product-brief.md").write_text("# Brief\n", encoding="utf-8")
    (root / "docs" / "10-requirements.md").write_text("# Requirements scaffold\n", encoding="utf-8")
    (root / "docs" / "20-design.md").write_text("# Design scaffold\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# AgentLoop rules\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# Claude capability mapping\n@AGENTS.md\n", encoding="utf-8")
    (root / "agentloop.mk").write_text("build-loop:\n\ttrue\n", encoding="utf-8")
    (root / "agentloop").write_text("#!/bin/sh\nexec true\n", encoding="utf-8")
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


def _manifest(target: Path) -> dict[str, Any]:
    return adopt.parse_manifest((target / adopt.MANIFEST_PATH).read_text(encoding="utf-8"))


def test_adopt_installs_without_overwriting(template: Path, target: Path) -> None:
    rc = adopt.main(["--target", str(target), "--name", "demo", "--test-cmd", "npm test"])
    assert rc == 0
    # New machinery landed.
    assert (target / "agentloop.mk").exists()
    assert (target / "agentloop").exists()  # the ./agentloop entry-point wrapper travels along
    assert (target / "src" / "agentloop" / "dag.py").exists()
    assert (target / ".claude" / "commands" / "req.md").exists()
    assert (target / ".github" / "prompts" / "req.prompt.md").exists()
    assert (target / ".github" / "hooks" / "agentloop.json").exists()
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
    # The manifest stays coherent across re-runs (original records kept).
    files = _manifest(target)["files"]
    assert files["src/agentloop/dag.py"]["owner"] == "template"


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


# --- manifest recording ----------------------------------------------------------


def test_adopt_writes_manifest_with_ownership(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    data = _manifest(target)
    files = data["files"]
    assert files["src/agentloop/dag.py"]["owner"] == "template"
    assert files["agentloop.mk"]["owner"] == "template"
    assert files["agentloop"]["owner"] == "template"
    assert files[adopt.AGENTLOOP_RULES_PATH]["owner"] == "template"
    assert files[".agentloop/config.yaml"]["owner"] == "seeded"
    assert files["docs/20-design.md"]["owner"] == "seeded"  # live docs belong to the repo once filled
    # Snapshot ownership is per file: the template-copied scaffold is ours to upgrade,
    # the snapshot of the user's preexisting doc (name collision) is not.
    assert files[".agentloop/scaffold/docs/20-design.md"]["owner"] == "template"
    assert files[".agentloop/scaffold/docs/10-requirements.md"]["owner"] == "seeded"
    # What adopt skipped was never adopted: no record, and the manifest never lists itself.
    assert "docs/10-requirements.md" not in files
    assert adopt.MANIFEST_PATH not in files
    assert data["claude_md"] == {"mode": "merged"}
    assert data["settings"]["created"] is False
    assert data["settings"]["permissions_allow"] == ["Bash(make build-loop:*)"]
    assert data["template"]["source"] == str(template)


def test_adopt_never_copies_cache_dirs(template: Path, target: Path) -> None:
    cache = template / "src" / "agentloop" / ".mypy_cache"
    cache.mkdir()
    (cache / "cache.db").write_text("x", encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    assert not (target / "src" / "agentloop" / ".mypy_cache").exists()
    assert "src/agentloop/.mypy_cache/cache.db" not in _manifest(target)["files"]


def test_adopt_never_copies_a_manifest_from_the_source(template: Path, target: Path) -> None:
    # Adopting *from* an adopted repo must not carry its stale manifest over.
    (template / ".agentloop" / "adopt-manifest.yaml").write_text("version: 1\nfiles: {stale: {}}\n", encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    data = _manifest(target)
    assert "stale" not in (data["files"] or {})
    assert data["template"]["source"] == str(template)


def test_adopt_creates_minimal_claude_md_when_absent(template: Path, target: Path) -> None:
    (target / "CLAUDE.md").unlink()
    adopt.main(["--target", str(target), "--name", "demo"])
    text = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.startswith(adopt.CLAUDE_IMPORT_MARKER)  # just the import shim, not the rules body
    assert f"@{adopt.AGENTLOOP_RULES_PATH}" in text
    assert (target / adopt.AGENTLOOP_RULES_PATH).read_text(encoding="utf-8") == "# AgentLoop rules\n"
    data = _manifest(target)
    assert data["claude_md"]["mode"] == "created"
    assert data["claude_md"]["hash"] == adopt.norm_hash(text.encode("utf-8"))


def test_adopt_records_template_version(template: Path, target: Path) -> None:
    (template / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    data = _manifest(target)
    assert data["mode"] == "adopt"
    assert data["template"]["version"] == "0.1.0"
    # VERSION itself is not copied — the manifest field is the identity record.
    assert not (target / "VERSION").exists()


# --- upgrade ----------------------------------------------------------------------


def test_upgrade_requires_manifest(template: Path, target: Path) -> None:
    assert adopt.main(["--target", str(target), "--upgrade"]) == 1


def test_upgrade_refreshes_pristine_and_respects_local_edits(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    # The template evolves: a tool changes, a command is added, one is removed, the rules move on.
    (template / "src" / "agentloop" / "dag.py").write_text("# tool v2\n", encoding="utf-8")
    (template / ".claude" / "commands" / "build.md").write_text("# /build\n", encoding="utf-8")
    (template / ".claude" / "commands" / "req.md").unlink()
    (template / "AGENTS.md").write_text("# AgentLoop rules v2\n", encoding="utf-8")
    # Meanwhile the user modified one installed file locally.
    (target / "agentloop.mk").write_text("build-loop:\n\techo custom\n", encoding="utf-8")
    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 0
    assert (target / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8") == "# tool v2\n"
    assert (target / ".claude" / "commands" / "build.md").exists()
    assert not (target / ".claude" / "commands" / "req.md").exists()
    assert (target / adopt.AGENTLOOP_RULES_PATH).read_text(encoding="utf-8") == "# AgentLoop rules v2\n"
    assert "echo custom" in (target / "agentloop.mk").read_text(encoding="utf-8")  # local edit survives
    # Seeded repo state is never touched.
    assert 'project: "demo"' in (target / ".agentloop" / "state.md").read_text(encoding="utf-8")
    data = _manifest(target)
    assert data["upgraded_at"]
    assert ".claude/commands/req.md" not in data["files"]
    assert data["files"][".claude/commands/build.md"]["owner"] == "template"
    # The skipped file keeps its original record, so a later upgrade can still see the drift.
    assert data["files"]["agentloop.mk"]["hash"] == adopt.norm_hash(b"build-loop:\n\ttrue\n")


def test_upgrade_updates_scaffold_snapshot_not_live_docs(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    (template / "docs" / "20-design.md").write_text("# Design scaffold v2\n", encoding="utf-8")
    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 0
    snap = target / ".agentloop" / "scaffold" / "docs"
    assert (snap / "20-design.md").read_text(encoding="utf-8") == "# Design scaffold v2\n"
    # The live doc is seeded (may be mid-cycle) — upgrade leaves it; cycle-close restores the new scaffold.
    assert (target / "docs" / "20-design.md").read_text(encoding="utf-8") == "# Design scaffold\n"
    # The snapshot of the user's own preexisting doc is untouched.
    assert (snap / "10-requirements.md").read_text(encoding="utf-8") == "EXISTING product requirements\n"


def test_upgrade_replaces_changed_hook_without_duplication(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    settings_path = template / ".claude" / "settings.json"
    tpl_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    tpl_settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "python gate_guard.py v2"
    settings_path.write_text(json.dumps(tpl_settings), encoding="utf-8")
    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 0
    merged = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
    cmds = [h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert cmds.count("python gate_guard.py v2") == 1
    assert "python gate_guard.py v1" not in cmds
    assert _manifest(target)["settings"]["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "python gate_guard.py v2"


def test_upgrade_rerun_converges(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    (template / "src" / "agentloop" / "dag.py").write_text("# tool v2\n", encoding="utf-8")
    adopt.main(["--target", str(target), "--upgrade"])
    manifest_before = (target / adopt.MANIFEST_PATH).read_text(encoding="utf-8")
    dag_before = (target / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8")
    rc = adopt.main(["--target", str(target), "--upgrade"])  # crash-recovery path: all unchanged
    assert rc == 0
    assert (target / adopt.MANIFEST_PATH).read_text(encoding="utf-8") == manifest_before
    assert (target / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8") == dag_before


def test_upgrade_dry_run_writes_nothing(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    (template / "src" / "agentloop" / "dag.py").write_text("# tool v2\n", encoding="utf-8")
    manifest_before = (target / adopt.MANIFEST_PATH).read_text(encoding="utf-8")
    rc = adopt.main(["--target", str(target), "--upgrade", "--dry-run"])
    assert rc == 0
    assert (target / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8") == "# tool\n"
    assert (target / adopt.MANIFEST_PATH).read_text(encoding="utf-8") == manifest_before


def test_upgrade_prints_version_transition_and_changelog(
    template: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (template / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    (template / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (template / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.2.0] - 2026-07-08\n### Added\n- shiny\n\n## [0.1.0] - 2026-06-01\n- old\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "template version: 0.1.0 → 0.2.0" in out
    assert "- shiny" in out and "- old" not in out
    assert _manifest(target)["template"]["version"] == "0.2.0"  # carried forward by the rebuild


def test_upgrade_without_version_files_degrades_gracefully(
    template: Path, target: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    capsys.readouterr()
    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "template commit:" in out  # no VERSION on either side -> commit fallback
    assert "template version:" not in out


def test_upgrade_greenfield_mode_leaves_rules_and_settings_alone(template: Path, tmp_path: Path) -> None:
    # A greenfield repo (mode: init): the whole template copy IS the product, so its root
    # CLAUDE.md and settings.json are product-owned and no rules body may be injected.
    gf = tmp_path / "greenfield"
    (gf / "src" / "agentloop").mkdir(parents=True)
    (gf / "src" / "agentloop" / "dag.py").write_text("# tool\n", encoding="utf-8")
    (gf / ".claude").mkdir(parents=True)
    (gf / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Mine"]}}), encoding="utf-8")
    (gf / "CLAUDE.md").write_text("# Product rules (greenfield copy)\n", encoding="utf-8")
    (gf / ".agentloop").mkdir()
    files = {"src/agentloop/dag.py": {"hash": adopt.norm_hash(b"# tool\n"), "owner": "template"}}
    manifest = adopt.build_manifest(
        files, {"mode": "owned"}, {"mode": "owned"}, "src", "", "unknown", "2026-07-08", None, mode="init"
    )
    (gf / adopt.MANIFEST_PATH).write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    settings_before = (gf / ".claude" / "settings.json").read_text(encoding="utf-8")
    claude_before = (gf / "CLAUDE.md").read_text(encoding="utf-8")
    (template / "src" / "agentloop" / "dag.py").write_text("# tool v2\n", encoding="utf-8")
    rc = adopt.main(["--target", str(gf), "--upgrade"])
    assert rc == 0
    assert (gf / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8") == "# tool v2\n"
    assert not (gf / adopt.AGENTLOOP_RULES_PATH).exists()
    assert (gf / ".claude" / "settings.json").read_text(encoding="utf-8") == settings_before
    assert (gf / "CLAUDE.md").read_text(encoding="utf-8") == claude_before
    data = adopt.parse_manifest((gf / adopt.MANIFEST_PATH).read_text(encoding="utf-8"))
    assert data["mode"] == "init"  # survives the rebuild
    assert data["settings"] == {"mode": "owned"}


# --- uninstall ---------------------------------------------------------------------


def test_plan_uninstall_pristine_only() -> None:
    mf = {
        "a": {"hash": "sha256:x", "owner": "template"},
        "b": {"hash": "sha256:x", "owner": "seeded"},
        "c": {"hash": "sha256:x", "owner": "template"},
    }
    cur: dict[str, str | None] = {"a": "sha256:x", "b": "sha256:edited", "c": None}
    ops = {i.rel: i.op for i in adopt.plan_uninstall(mf, cur, force=False)}
    assert ops == {"a": "remove", "b": "leave-modified", "c": "unchanged"}
    forced = {i.rel: i.op for i in adopt.plan_uninstall(mf, cur, force=True)}
    assert forced["b"] == "remove"


def test_unmerge_settings_removes_only_recorded_entries() -> None:
    ours = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "python gate_guard.py"}]}
    users = {"matcher": "Bash", "hooks": [{"type": "command", "command": "./my-hook.sh"}]}
    session = {"hooks": [{"type": "command", "command": "cat state.md"}]}
    existing = {
        "permissions": {"allow": ["Read", "Bash(make build-loop:*)"]},
        "hooks": {"PreToolUse": [users, json.loads(json.dumps(ours))], "SessionStart": [session]},
    }
    installed = {
        "permissions_allow": ["Bash(make build-loop:*)"],
        "hooks": {"PreToolUse": [ours], "SessionStart": [session]},
    }
    merged, notes = adopt.unmerge_settings(existing, installed)
    assert merged["permissions"]["allow"] == ["Read"]
    assert merged["hooks"]["PreToolUse"] == [users]
    assert "SessionStart" not in merged["hooks"]  # emptied event pruned
    assert notes


def test_remove_claude_import_is_idempotent() -> None:
    original = "# My rules\nDo the thing.\n"
    merged = original.rstrip("\n") + "\n" + adopt.claude_import_block()
    stripped = adopt.remove_claude_import(merged)
    assert stripped == original
    assert adopt.remove_claude_import(stripped) == stripped


def test_remove_agents_pointer_is_idempotent() -> None:
    original = "# My agents rules\nDo the thing.\n"
    merged = original.rstrip("\n") + "\n" + adopt.agents_pointer_block()
    stripped = adopt.remove_agents_pointer(merged)
    assert stripped == original
    assert adopt.remove_agents_pointer(stripped) == stripped


# --- AGENTS.md create/append/skip ---------------------------------------------------


def test_adopt_merges_existing_agents_md(template: Path, target: Path) -> None:
    (target / "AGENTS.md").write_text("# My agent rules\nBe careful.\n", encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    text = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# My agent rules")
    assert adopt.CLAUDE_IMPORT_MARKER in text
    assert adopt.AGENTLOOP_RULES_PATH in text
    assert adopt.AGENTS_MARKER_END in text
    assert _manifest(target)["agents_md"] == {"mode": "merged"}
    # Idempotent: a re-run appends nothing.
    adopt.main(["--target", str(target), "--name", "demo"])
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == text


def test_adopt_creates_minimal_agents_md_when_absent(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])  # the target fixture has no AGENTS.md
    text = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith(adopt.CLAUDE_IMPORT_MARKER)  # just the pointer shim, not the rules body
    assert adopt.AGENTLOOP_RULES_PATH in text
    data = _manifest(target)
    assert data["agents_md"]["mode"] == "created"
    assert data["agents_md"]["hash"] == adopt.norm_hash(text.encode("utf-8"))


def test_uninstall_restores_pre_adopt_agents_md(template: Path, target: Path) -> None:
    original = "# My agent rules\nBe careful.\n"
    (target / "AGENTS.md").write_text(original, encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    rc = adopt.main(["--target", str(target), "--uninstall"])
    assert rc == 0
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == original


# --- legacy rules-path migration (pre-multi-agent installs) --------------------------


def test_upgrade_migrates_legacy_rules_import(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    # Reconstruct a pre-multi-agent install: the rules lived in .agentloop/CLAUDE.agentloop.md
    # and CLAUDE.md imported that path.
    legacy_import = f"@{adopt.LEGACY_RULES_PATH}"
    claude_path = target / "CLAUDE.md"
    claude_path.write_text(
        claude_path.read_text(encoding="utf-8").replace(f"@{adopt.AGENTLOOP_RULES_PATH}", legacy_import),
        encoding="utf-8",
    )
    rules_text = (target / adopt.AGENTLOOP_RULES_PATH).read_text(encoding="utf-8")
    (target / adopt.AGENTLOOP_RULES_PATH).unlink()
    (target / adopt.LEGACY_RULES_PATH).write_text(rules_text, encoding="utf-8")
    manifest_path = target / adopt.MANIFEST_PATH
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["files"][adopt.LEGACY_RULES_PATH] = data["files"].pop(adopt.AGENTLOOP_RULES_PATH)
    manifest_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 0
    claude = claude_path.read_text(encoding="utf-8")
    assert f"@{adopt.AGENTLOOP_RULES_PATH}" in claude
    assert legacy_import not in claude
    assert (target / adopt.AGENTLOOP_RULES_PATH).exists()
    assert not (target / adopt.LEGACY_RULES_PATH).exists()  # pristine legacy file retired


def test_uninstall_requires_manifest_and_rejects_from_git(template: Path, target: Path) -> None:
    assert adopt.main(["--target", str(target), "--uninstall"]) == 1
    assert adopt.main(["--target", str(target), "--uninstall", "--from-git", "https://x.git"]) == 2


def test_uninstall_restores_pre_adopt_state(template: Path, target: Path) -> None:
    before = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    claude_before = (target / "CLAUDE.md").read_text(encoding="utf-8")
    settings_before = (target / ".claude" / "settings.json").read_text(encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    rc = adopt.main(["--target", str(target), "--uninstall"])
    assert rc == 0
    after = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert after == before
    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == claude_before
    # Byte-for-byte, not just semantically: the manifest keeps the pre-adopt text.
    assert (target / ".claude" / "settings.json").read_text(encoding="utf-8") == settings_before
    assert not (target / ".agentloop").exists()
    assert not (target / "src" / "agentloop").exists()


def test_uninstall_sweeps_interpreter_caches(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    # Running the installed tooling regenerates caches — they must not keep dirs alive.
    cache = target / "src" / "agentloop" / "__pycache__"
    cache.mkdir()
    (cache / "adopt.cpython-313.pyc").write_bytes(b"\x00")
    rc = adopt.main(["--target", str(target), "--uninstall"])
    assert rc == 0
    assert not (target / "src" / "agentloop").exists()


def test_uninstall_of_uncommitted_adoption_needs_no_force(template: Path, target: Path) -> None:
    _git("init", cwd=target)
    _git("add", "-A", cwd=target)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pre-adopt", cwd=target)
    adopt.main(["--target", str(target), "--name", "demo"])
    # CLAUDE.md / settings.json are now tracked-dirty, but the adoption itself was never
    # committed (the manifest is untracked) — aborting the trial must not demand --force.
    rc = adopt.main(["--target", str(target), "--uninstall"])
    assert rc == 0
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True)
    assert proc.stdout == ""  # the tree is byte-identical to the pre-adopt commit


def test_uninstall_leaves_modified_files(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    (target / ".agentloop" / "config.yaml").write_text("gates: {}\n", encoding="utf-8")  # the human tuned it
    rc = adopt.main(["--target", str(target), "--uninstall"])
    assert rc == 0
    assert (target / ".agentloop" / "config.yaml").read_text(encoding="utf-8") == "gates: {}\n"
    assert not (target / adopt.MANIFEST_PATH).exists()
    assert not (target / "src" / "agentloop").exists()  # pristine tooling is still removed


def test_uninstall_deletes_created_claude_md_and_settings(template: Path, target: Path) -> None:
    (target / "CLAUDE.md").unlink()
    (target / ".claude" / "settings.json").unlink()
    adopt.main(["--target", str(target), "--name", "demo"])
    assert (target / "CLAUDE.md").exists()
    assert (target / ".claude" / "settings.json").exists()
    rc = adopt.main(["--target", str(target), "--uninstall"])
    assert rc == 0
    assert not (target / "CLAUDE.md").exists()
    assert not (target / ".claude").exists()


def test_uninstall_dry_run_changes_nothing(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    rc = adopt.main(["--target", str(target), "--uninstall", "--dry-run"])
    assert rc == 0
    assert (target / adopt.MANIFEST_PATH).exists()
    assert (target / "src" / "agentloop" / "dag.py").exists()
    assert adopt.CLAUDE_IMPORT_MARKER in (target / "CLAUDE.md").read_text(encoding="utf-8")


# --- --from-git sourcing and command detection ------------------------------------


def test_detect_commands_node_with_lockfiles() -> None:
    pkg = json.dumps({"scripts": {"test": "vitest", "lint": "eslint ."}})
    assert adopt.detect_commands({"package.json": pkg}) == {"test": ["npm test"], "check": ["npm run lint"]}
    assert adopt.detect_commands({"package.json": pkg, "pnpm-lock.yaml": ""})["test"] == ["pnpm test"]
    assert adopt.detect_commands({"package.json": pkg, "yarn.lock": ""})["check"] == ["yarn run lint"]


def test_detect_commands_python_rust_go_make() -> None:
    out = adopt.detect_commands({"pyproject.toml": "[tool.pytest.ini_options]\n[tool.ruff]\n", "uv.lock": ""})
    assert out == {"test": ["uv run pytest"], "check": ["ruff check ."]}
    assert adopt.detect_commands({"Cargo.toml": "[package]"})["test"] == ["cargo test"]
    assert adopt.detect_commands({"go.mod": "module x"})["check"] == ["go vet ./..."]
    out = adopt.detect_commands({"makefile": "test:\n\tpytest\nlint:\n\truff check .\n"})
    assert out == {"test": ["make test"], "check": ["make lint"]}
    assert adopt.detect_commands({}) == {"test": [], "check": []}


def test_adopt_prints_detected_suggestions(template: Path, target: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pkg = json.dumps({"scripts": {"test": "vitest", "lint": "eslint"}})
    (target / "package.json").write_text(pkg, encoding="utf-8")
    adopt.main(["--target", str(target), "--name", "demo"])
    out = capsys.readouterr().out
    assert "npm test" in out and "npm run lint" in out and "suggestions only" in out


def test_ref_requires_from_git(template: Path, target: Path) -> None:
    assert adopt.main(["--target", str(target), "--name", "demo", "--ref", "main"]) == 2


def test_from_git_clones_and_cleans_up(template: Path, target: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"cleanup": 0}

    def fake_resolve(url: str, ref: str) -> tuple[Path, object]:
        assert url == "https://example.com/tpl.git" and ref == "v2"

        def _cleanup() -> None:
            calls["cleanup"] += 1

        return template, _cleanup

    monkeypatch.setattr(adopt, "resolve_template_root", fake_resolve)
    rc = adopt.main(
        ["--target", str(target), "--name", "demo", "--from-git", "https://example.com/tpl.git", "--ref", "v2"]
    )
    assert rc == 0
    assert calls["cleanup"] == 1  # the temp clone is removed even on success
    data = _manifest(target)
    assert data["template"]["source"] == "https://example.com/tpl.git"
    assert data["template"]["ref"] == "v2"


def test_self_upgrade_falls_back_to_recorded_source(
    template: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    (template / "src" / "agentloop" / "dag.py").write_text("# tool v2\n", encoding="utf-8")
    # Simulate running the *installed* adopt.py from inside the adopted repo: TEMPLATE_ROOT
    # resolves to the repo itself, so the manifest's recorded source must take over.
    monkeypatch.setattr(adopt, "TEMPLATE_ROOT", target)
    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 0
    assert (target / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8") == "# tool v2\n"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def test_upgrade_refuses_uncommitted_tracked_changes(template: Path, target: Path) -> None:
    adopt.main(["--target", str(target), "--name", "demo"])
    _git("init", cwd=target)
    _git("add", "-A", cwd=target)  # adoption staged but not committed — the upgrade would blur into it
    (template / "src" / "agentloop" / "dag.py").write_text("# tool v2\n", encoding="utf-8")
    rc = adopt.main(["--target", str(target), "--upgrade"])
    assert rc == 1
    assert (target / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8") == "# tool\n"
    rc = adopt.main(["--target", str(target), "--upgrade", "--force"])
    assert rc == 0
    assert (target / "src" / "agentloop" / "dag.py").read_text(encoding="utf-8") == "# tool v2\n"
