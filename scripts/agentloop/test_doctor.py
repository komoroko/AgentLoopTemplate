"""Verify doctor.py — the read-only environment / SSOT-consistency diagnosis."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import build_loop
import doctor
import events
import pytest

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: build
gates:
  requirements: approved
  design: approved
  tasks: approved
  build: pending
  release: pending
updated_at: "2026-07-09"
---
# board
"""

_CONFIG = (
    "build:\n"
    "  max_parallel: 3\n"
    "  worktree: {enabled: true, dir: .worktrees, branch_pattern: '{branch}-{task_id}'}\n"
    "  quality_gate: {test_cmd: 'make test', check_cmd: 'make check'}\n"
    "gates:\n  enforce_hook: true\n  template_mode: false\n"
)

_TASKS = "tasks:\n  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: todo, test: make test}\n"

_SETTINGS = '{"hooks": {"PreToolUse": [{"hooks": [{"command": "uv run scripts/agentloop/gate_guard.py"}]}]}}\n'


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A healthy product repo: every check should PASS/INFO on this baseline."""
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".agentloop" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (tmp_path / ".agentloop" / "state.md").write_text(_STATE, encoding="utf-8")
    (tmp_path / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    (tmp_path / ".claude" / "settings.json").write_text(_SETTINGS, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd, timeout=None: (0, "build/demo\n"))
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def _levels(findings: list[doctor.Finding], area: str) -> list[str]:
    return [f.level for f in findings if f.area == area]


def _messages(findings: list[doctor.Finding], level: str) -> list[str]:
    return [f.message for f in findings if f.level == level]


def test_healthy_repo_has_no_fail_or_warn(project: Path) -> None:
    findings = doctor.run_checks()
    assert not _messages(findings, "FAIL")
    assert not _messages(findings, "WARN")
    assert doctor.main([]) == 0


def test_missing_required_binary_fails(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "uv" else f"/usr/bin/{name}")
    findings = doctor.run_checks()
    assert any("uv not found" in m for m in _messages(findings, "FAIL"))
    assert doctor.main([]) == 1


def test_missing_claude_is_warn_and_gh_is_info(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name in ("claude", "gh") else f"/usr/bin/{name}")
    findings = doctor.run_checks()
    assert any("claude not found" in m for m in _messages(findings, "WARN"))
    assert any("gh not found" in m for m in _messages(findings, "INFO"))
    assert doctor.main([]) == 0  # degraded features, not broken invariants


def test_unparseable_config_fails(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text("build: [not a mapping", encoding="utf-8")
    assert any("not valid YAML" in m for m in _messages(doctor.run_checks(), "FAIL"))


def test_all_empty_cmd_steps_warn(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("{test_cmd: 'make test', check_cmd: 'make check'}", "{test_cmd: '', check_cmd: ''}"),
        encoding="utf-8",
    )
    assert any("would check nothing" in m for m in _messages(doctor.run_checks(), "WARN"))


def test_gate_chain_violation_fails(project: Path) -> None:
    # design pending but tasks approved: an approval survived a roll back — the core invariant.
    broken = _STATE.replace("design: approved", "design: pending")
    (project / ".agentloop" / "state.md").write_text(broken, encoding="utf-8")
    fails = _messages(doctor.run_checks(), "FAIL")
    assert any("'tasks' is approved while upstream 'design' is pending" in m for m in fails)


def test_invalid_gate_value_and_phase_fail(project: Path) -> None:
    broken = _STATE.replace("build: pending", "build: yes").replace("current_phase: build", "current_phase: biuld")
    (project / ".agentloop" / "state.md").write_text(broken, encoding="utf-8")
    fails = _messages(doctor.run_checks(), "FAIL")
    assert any("invalid value" in m for m in fails)
    assert any("current_phase" in m for m in fails)


def test_missing_state_fails(project: Path) -> None:
    (project / ".agentloop" / "state.md").unlink()
    assert any("missing" in m for m in _messages(doctor.run_checks(), "FAIL"))


def test_placeholders_fail_in_product_but_info_in_template(project: Path) -> None:
    placeholder = _STATE.replace('project: "demo"', 'project: "<enter the product name>"')
    (project / ".agentloop" / "state.md").write_text(placeholder, encoding="utf-8")
    assert any("make init" in m for m in _messages(doctor.run_checks(), "FAIL"))
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("template_mode: false", "template_mode: true"), encoding="utf-8"
    )
    findings = doctor.run_checks()
    assert not any("make init" in m for m in _messages(findings, "FAIL"))
    assert any("template_mode" in m for m in _messages(findings, "INFO"))


def test_broken_dag_fails_and_absent_tasks_is_info(project: Path) -> None:
    (project / ".agentloop" / "tasks.yaml").write_text(
        "tasks:\n  - {id: T-001, title: a, kind: parallel, blockedBy: [T-001]}\n", encoding="utf-8"
    )
    assert any("does not load" in m for m in _messages(doctor.run_checks(), "FAIL"))
    (project / ".agentloop" / "tasks.yaml").unlink()
    assert any("before /tasks" in m for m in _messages(doctor.run_checks(), "INFO"))


def test_stuck_and_needy_tasks_warn(project: Path) -> None:
    (project / ".agentloop" / "tasks.yaml").write_text(
        "tasks:\n"
        "  - {id: T-001, title: a, kind: parallel, blockedBy: [], status: in_progress}\n"
        "  - {id: T-002, title: b, kind: parallel, blockedBy: [], status: blocked}\n",
        encoding="utf-8",
    )
    warns = _messages(doctor.run_checks(), "WARN")
    assert any("in_progress leftovers: T-001" in m for m in warns)
    assert any("awaiting human intervention: T-002" in m for m in warns)


def test_branch_mismatch_warns(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd, timeout=None: (0, "main\n"))
    assert any("≠ state.md branch" in m for m in _messages(doctor.run_checks(), "WARN"))


def test_not_a_git_repo_fails(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd, timeout=None: (128, "fatal: not a git repository"))
    assert any("not a git repository" in m for m in _messages(doctor.run_checks(), "FAIL"))


def test_leftover_worktrees_and_stale_lock_warn(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / ".worktrees" / "T-002").mkdir(parents=True)
    (project / ".agentloop" / "build-loop.lock").write_text("99999", encoding="utf-8")
    monkeypatch.setattr(build_loop, "_pid_alive", lambda pid: False)
    warns = _messages(doctor.run_checks(), "WARN")
    assert any("leftover worktrees" in m and "T-002" in m for m in warns)
    assert any("stale build-loop.lock" in m for m in warns)


def test_live_lock_is_info(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / ".agentloop" / "build-loop.lock").write_text("12345", encoding="utf-8")
    monkeypatch.setattr(build_loop, "_pid_alive", lambda pid: True)
    assert any("appears active" in m for m in _messages(doctor.run_checks(), "INFO"))


def test_hook_registration_checked_only_when_enforced(project: Path) -> None:
    (project / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    assert any("not registered" in m for m in _messages(doctor.run_checks(), "FAIL"))
    (project / ".claude" / "settings.json").unlink()
    assert any("missing while gates.enforce_hook" in m for m in _messages(doctor.run_checks(), "FAIL"))
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("enforce_hook: true", "enforce_hook: false"), encoding="utf-8"
    )
    findings = doctor.run_checks()
    assert not _messages(findings, "FAIL")
    assert any("convention layer only" in m for m in _messages(findings, "INFO"))


def test_open_escalation_warns(project: Path) -> None:
    events.append_event("blocked", task="T-001", detail="red")
    warns = _messages(doctor.run_checks(), "WARN")
    assert any("open escalation" in m and "#1 blocked(T-001)" in m for m in warns)


def test_version_prefers_manifest_over_version_file(project: Path) -> None:
    (project / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    assert any("template repo, VERSION 0.2.0" in m for m in _messages(doctor.run_checks(), "INFO"))
    (project / ".agentloop" / "adopt-manifest.yaml").write_text("template:\n  version: 0.1.0\n", encoding="utf-8")
    assert any("template version 0.1.0" in m for m in _messages(doctor.run_checks(), "INFO"))
