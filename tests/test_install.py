"""Verify install.py: settings merge/unmerge (ported from adopt), sync's pristine rules,
and the install→guard→uninstall end-to-end path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop import gate_guard, init_cmd, install
from agentloop import lock as lock_mod
from agentloop import repo as repo_mod

# --- pure settings logic (semantics preserved from adopt.py) ---------------------------


def test_merge_settings_appends_missing_only_and_records_added() -> None:
    existing = {
        "permissions": {"allow": ["Read", "Bash(npm test:*)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./my-hook.sh"}]}]},
    }
    template = {
        "permissions": {"allow": ["Read", "Bash(agentloop build:*)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "agentloop guard"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "cat state.md"}]}],
        },
    }
    merged, notes, added = install.merge_settings(existing, template)
    assert merged["permissions"]["allow"] == ["Read", "Bash(npm test:*)", "Bash(agentloop build:*)"]
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "./my-hook.sh"  # existing kept first
    assert merged["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == "agentloop guard"
    assert notes
    assert added["permissions_allow"] == ["Bash(agentloop build:*)"]
    assert set(added["hooks"]) == {"PreToolUse", "SessionStart"}
    # Idempotent: a second merge adds nothing and records nothing.
    merged2, notes2, added2 = install.merge_settings(merged, template)
    assert notes2 == [] and merged2 == merged
    assert added2 == {"permissions_allow": [], "hooks": {}}


def test_unmerge_settings_retracts_only_the_recorded_entries() -> None:
    template = {
        "permissions": {"allow": ["Bash(agentloop build:*)"]},
        "hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "agentloop guard"}]}]},
    }
    existing = {"permissions": {"allow": ["Bash(npm test:*)"]}}
    merged, _notes, added = install.merge_settings(existing, template)
    unmerged, notes = install.unmerge_settings(merged, added)
    assert unmerged == {"permissions": {"allow": ["Bash(npm test:*)"]}}
    assert any("-=" in n for n in notes)


def test_unmerge_settings_leaves_locally_modified_groups() -> None:
    group = {"matcher": "Write", "hooks": [{"type": "command", "command": "agentloop guard"}]}
    installed = {"permissions_allow": [], "hooks": {"PreToolUse": [group]}}
    modified = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "agentloop guard"}]}
    existing = {"hooks": {"PreToolUse": [modified]}}
    unmerged, notes = install.unmerge_settings(existing, installed)
    assert unmerged["hooks"]["PreToolUse"] == [modified]  # theirs now — left alone
    assert any("locally modified" in n for n in notes)


def test_upgrade_settings_replaces_pristine_and_keeps_modified() -> None:
    old_group = {"matcher": "Write", "hooks": [{"type": "command", "command": "old guard"}]}
    new_group = {"matcher": "Write", "hooks": [{"type": "command", "command": "agentloop guard"}]}
    installed = {"permissions_allow": [], "hooks": {"PreToolUse": [old_group]}}
    existing = {"hooks": {"PreToolUse": [dict(old_group)]}}
    merged, _notes, added = install.upgrade_settings(existing, installed, {"hooks": {"PreToolUse": [new_group]}})
    assert merged["hooks"]["PreToolUse"] == [new_group]  # pristine → replaced without duplication
    assert added["hooks"]["PreToolUse"] == [new_group]


# --- changelog / marker blocks -----------------------------------------------------------

_CHANGELOG = "# Changelog\n\n## [0.3.0] - 2026-07-08\n- three\n\n## [0.2.0] - 2026-06-01\n- two\n"


def test_changelog_between_returns_sections_newer_than_installed() -> None:
    out = install.changelog_between(_CHANGELOG, "0.2.0", "0.3.0")
    assert "- three" in out and "- two" not in out
    assert install.changelog_between(_CHANGELOG, "0.3.0", "0.3.0") == ""
    assert "installed version unknown" in install.changelog_between(_CHANGELOG, "", "0.3.0")


def test_claude_import_block_roundtrip() -> None:
    text = "# My rules\nDo the thing.\n" + install.claude_import_block()
    assert install.remove_claude_import(text) == "# My rules\nDo the thing.\n"
    assert install.remove_claude_import("# plain\n") == "# plain\n"


def test_agents_pointer_block_roundtrip() -> None:
    text = "# Repo rules\n" + install.agents_pointer_block()
    assert install.remove_agents_pointer(text) == "# Repo rules\n"


# --- sync / install / uninstall end-to-end ---------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    """An initialized repo (greenfield init, gate guard live)."""
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "src") == 0
    return repo_mod.Repo(tmp_path)


def test_sync_check_is_clean_after_init_and_flags_drift(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert install.sync(repo, check=True) == 0
    req = repo.path(".agentloop/prompts/commands/req.md")
    req.write_text(req.read_text(encoding="utf-8") + "\nlocal note\n", encoding="utf-8")
    capsys.readouterr()
    assert install.sync(repo, check=True) == 1
    assert "prompts/commands/req.md" in capsys.readouterr().out


def test_sync_keeps_local_modifications_unless_forced(repo: repo_mod.Repo) -> None:
    req = repo.path(".agentloop/prompts/commands/req.md")
    pristine = req.read_text(encoding="utf-8")
    req.write_text(pristine + "\nlocal note\n", encoding="utf-8")
    assert install.sync(repo) == 0
    assert "local note" in req.read_text(encoding="utf-8")  # skip-modified
    assert install.sync(repo, force=True) == 0
    assert req.read_text(encoding="utf-8") == pristine  # forced back to the payload


def test_sync_refreshes_a_pristine_file_deleted_locally(repo: repo_mod.Repo) -> None:
    req = repo.path(".agentloop/prompts/commands/req.md")
    req.unlink()
    assert install.sync(repo) == 0
    assert req.is_file()


def test_install_claude_writes_surfaces_and_merges_settings(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    assert repo.path(".claude/commands/req.md").is_file()
    assert repo.path(".claude/agents/architect.md").is_file()
    settings = json.loads(repo.path(".claude/settings.json").read_text(encoding="utf-8"))
    hook_cmds = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert any("gate_guard" in c or "agentloop guard" in c for c in hook_cmds)
    assert "agentloop-rules" in repo.path("CLAUDE.md").read_text(encoding="utf-8")
    data = lock_mod.read(repo.lock)
    assert data is not None and "claude" in data["integrations"]
    assert ".claude/commands/req.md" in data["integrations"]["claude"]["files"]
    assert "settings" in data["integrations"]["claude"]


def test_install_copilot_writes_the_github_surfaces(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "copilot") == 0
    assert repo.path(".github/prompts/req.prompt.md").is_file()
    assert repo.path(".github/agents/architect.agent.md").is_file()
    assert repo.path(".github/hooks/agentloop.json").is_file()
    assert repo.path(".github/instructions/agentloop.instructions.md").is_file()
    assert not repo.path(".claude").exists()  # strictly the asked-for surface


def test_guard_denies_a_pending_gate_write_in_an_initialized_repo(repo: repo_mod.Repo) -> None:
    ok, reason = gate_guard.evaluate(str(repo.path("docs/20-design.md")), repo)
    assert ok is False and "requirements" in reason
    # tests/ stays deliberately unguarded (speculative work keeps flowing).
    ok, _ = gate_guard.evaluate(str(repo.path("tests/test_x.py")), repo)
    assert ok is True


def test_uninstall_claude_restores_the_pre_install_state(repo: repo_mod.Repo) -> None:
    before_settings = '{\n  "permissions": {\n    "allow": [\n      "Bash(npm test:*)"\n    ]\n  }\n}\n'
    repo.path(".claude").mkdir()
    repo.path(".claude/settings.json").write_text(before_settings, encoding="utf-8")
    repo.path("CLAUDE.md").write_text("# My rules\n", encoding="utf-8")
    assert install.install_integration(repo, "claude") == 0
    assert install.uninstall_integration(repo, "claude") == 0
    assert not repo.path(".claude/commands").exists()
    assert json.loads(repo.path(".claude/settings.json").read_text(encoding="utf-8")) == json.loads(before_settings)
    assert repo.path("CLAUDE.md").read_text(encoding="utf-8") == "# My rules\n"
    data = lock_mod.read(repo.lock)
    assert data is not None and "claude" not in data.get("integrations", {})


def test_uninstall_keeps_locally_modified_wrapper(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    wrapper = repo.path(".claude/commands/req.md")
    wrapper.write_text("customized\n", encoding="utf-8")
    assert install.uninstall_integration(repo, "claude") == 0
    assert wrapper.read_text(encoding="utf-8") == "customized\n"  # theirs now


def test_install_rerun_refreshes_pristine_files(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    wrapper = repo.path(".claude/commands/req.md")
    pristine = wrapper.read_text(encoding="utf-8")
    wrapper.write_text("clobbered\n", encoding="utf-8")
    assert install.install_integration(repo, "claude") == 0
    assert wrapper.read_text(encoding="utf-8") == "clobbered\n"  # modified → kept
    assert install.install_integration(repo, "claude", force=True) == 0
    assert wrapper.read_text(encoding="utf-8") == pristine


def test_uninstall_all_leaves_only_repo_state(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    assert install.install_integration(repo, "copilot") == 0
    assert install.uninstall_all(repo) == 0
    assert not repo.path(".claude").exists()
    assert not repo.path(".github").exists()
    assert not repo.path(".agentloop/prompts").exists()
    assert not repo.path(".agentloop/AGENTS.agentloop.md").exists()
    assert not repo.lock.exists()
    # The repo's own state survives untouched.
    assert repo.state.is_file() and repo.config.is_file() and repo.tasks.is_file()
    assert repo.path("docs/00-product-brief.md").is_file()
    agents = repo.path("AGENTS.md")
    assert not agents.exists() or "agentloop-rules" not in agents.read_text(encoding="utf-8")


def test_upgrade_refreshes_and_reports(repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert install.install_integration(repo, "claude") == 0
    data = lock_mod.read(repo.lock)
    assert data is not None
    data["agentloop"]["version"] = "0.1.0"  # simulate a repo written by an older tool
    lock_mod.write(repo.lock, data)
    capsys.readouterr()
    assert install.upgrade(repo) == 0
    out = capsys.readouterr().out
    assert "0.1.0 →" in out
    refreshed = lock_mod.read(repo.lock)
    assert refreshed is not None and lock_mod.tool_version_of(refreshed) != "0.1.0"


def test_cmd_wrappers_parse_their_flags(repo: repo_mod.Repo) -> None:
    assert install.cmd_sync(["--check", "--repo", str(repo.root)]) == 0
    assert install.cmd_install(["copilot", "--dry-run", "--repo", str(repo.root)]) == 0
    assert install.cmd_uninstall(["copilot", "--dry-run", "--repo", str(repo.root)]) == 0
    assert install.cmd_upgrade(["--dry-run", "--repo", str(repo.root)]) == 0
