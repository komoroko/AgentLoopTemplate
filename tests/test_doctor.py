"""Verify doctor.py — the read-only environment / SSOT-consistency diagnosis."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentloop import build_loop, doctor, events

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
    "  quality_gate:\n"
    "    steps:\n"
    "      - {name: test, kind: cmd, run: 'make test'}\n"
    "      - {name: check, kind: cmd, run: 'make check'}\n"
    "gates:\n  enforce_hook: true\n  template_mode: false\n"
)

_TASKS = "tasks:\n  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: todo, test: make test}\n"

_SETTINGS = '{"hooks": {"PreToolUse": [{"hooks": [{"command": "uv run src/agentloop/gate_guard.py"}]}]}}\n'


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

    def fake_git(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "build/demo\n"
        return 0, ""  # branch --list etc.: nothing left behind on the healthy baseline

    monkeypatch.setattr(build_loop, "_run", fake_git)
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


def _steps_config(smoke_attrs: str) -> str:
    return (
        "build:\n"
        "  quality_gate:\n"
        "    steps:\n"
        "      - {name: test, kind: cmd, run: 'make test'}\n"
        f"      - {{name: smoke, kind: cmd, run: ''{smoke_attrs}}}\n"
        "gates:\n  enforce_hook: true\n  template_mode: false\n"
    )


def test_required_step_without_command_fails(project: Path) -> None:
    # The same contradiction build_loop refuses to start on must surface here, pre-launch.
    (project / ".agentloop" / "config.yaml").write_text(_steps_config(", required: true"), encoding="utf-8")
    findings = doctor.run_checks()
    assert any("'smoke' is `required: true` but has no command" in m for m in _messages(findings, "FAIL"))


def test_empty_smoke_without_required_key_warns(project: Path) -> None:
    # An undecided empty smoke is the classic silent DoD hole — nudge until a human decides.
    (project / ".agentloop" / "config.yaml").write_text(_steps_config(""), encoding="utf-8")
    findings = doctor.run_checks()
    assert any("smoke has no command" in m for m in _messages(findings, "WARN"))


def test_empty_smoke_with_explicit_required_false_is_accepted(project: Path) -> None:
    # `required: false` written out is the recorded human decision (not runnable) — no nagging.
    (project / ".agentloop" / "config.yaml").write_text(_steps_config(", required: false"), encoding="utf-8")
    findings = doctor.run_checks()
    assert not [m for m in _messages(findings, "WARN") if "smoke" in m]
    assert not _messages(findings, "FAIL")


def _install_schemas(project: Path) -> None:
    """Copy the template's real schemas into the fixture (they live outside the tmp cwd)."""
    src = Path(__file__).resolve().parents[1] / ".agentloop" / "schema"
    shutil.copytree(src, project / ".agentloop" / "schema")


def test_schema_validation_passes_on_healthy_files(project: Path) -> None:
    pytest.importorskip("jsonschema")
    _install_schemas(project)
    assert _levels(doctor.run_checks(), "schema") == ["PASS", "PASS"]


def test_schema_violation_fails_even_when_the_parser_tolerates_it(project: Path) -> None:
    # dag.load ignores unknown keys (tolerant runtime); the schema is the stricter lint that
    # catches the typo'd field a tolerant parser would silently drop.
    pytest.importorskip("jsonschema")
    _install_schemas(project)
    (project / ".agentloop" / "tasks.yaml").write_text(
        "tasks:\n  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: todo, blockedby: [T-002]}\n",
        encoding="utf-8",
    )
    findings = doctor.run_checks()
    assert any("violates its schema" in m for m in _messages(findings, "FAIL"))


def test_schema_files_absent_is_info_only(project: Path) -> None:
    # An adopted repo from an older template has no .agentloop/schema/ — degrade, don't fail.
    pytest.importorskip("jsonschema")
    assert _levels(doctor.run_checks(), "schema") == ["INFO", "INFO"]


# --- the practical checks: tickets, leaf branches, security binding, log size, guard typos


def test_missing_ticket_warns_and_orphan_ticket_is_info(project: Path) -> None:
    (project / "docs" / "tasks").mkdir(parents=True)
    (project / "docs" / "tasks" / "T-002.md").write_text("# T-002\n", encoding="utf-8")
    findings = doctor.run_checks()
    assert any("no ticket file under docs/tasks/ for: T-001" in m for m in _messages(findings, "WARN"))
    assert any("no tasks.yaml entry: T-002" in m for m in _messages(findings, "INFO"))


def test_no_tickets_dir_stays_silent(project: Path) -> None:
    # Nothing under docs/tasks/ at all (e.g. a brownfield repo before its first /tasks) — no noise.
    assert not any("ticket" in m for m in _messages(doctor.run_checks(), "WARN"))


def test_unmerged_leaf_branch_warns_and_merged_is_info(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "build/demo\n"
        if cmd[:3] == ["git", "branch", "--no-merged"]:
            return 0, "  build/demo-T-003\n"
        if cmd[:3] == ["git", "branch", "--list"]:
            return 0, "  build/demo-T-002\n  build/demo-T-003\n"
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_git)
    findings = doctor.run_checks()
    assert any("UNMERGED leaf branch(es): build/demo-T-003" in m for m in _messages(findings, "WARN"))
    assert any("merged leaf branch(es) left behind: build/demo-T-002" in m for m in _messages(findings, "INFO"))


def _all_done(project: Path) -> None:
    (project / ".agentloop" / "tasks.yaml").write_text(
        "tasks:\n  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: done, test: make test}\n",
        encoding="utf-8",
    )


def _git_with_head(monkeypatch: pytest.MonkeyPatch, head: str) -> None:
    def fake_git(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"]:
            return (0, "build/demo\n") if "--abbrev-ref" in cmd else (0, f"{head}\n")
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_git)


def test_all_done_without_security_report_is_info(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _all_done(project)
    _git_with_head(monkeypatch, "abc123")
    assert any("no .agentloop/security-review.md" in m for m in _messages(doctor.run_checks(), "INFO"))


def test_stale_security_review_warns_and_current_passes(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _all_done(project)
    _git_with_head(monkeypatch, "abc123def")
    report = project / ".agentloop" / "security-review.md"
    report.write_text("Reviewed-HEAD: 999999\n", encoding="utf-8")
    assert any("security review is STALE" in m for m in _messages(doctor.run_checks(), "WARN"))
    report.write_text("Reviewed-HEAD: abc123def\n", encoding="utf-8")
    assert any(f.area == "security" and f.level == "PASS" for f in doctor.run_checks())


def test_pending_build_stays_silent_on_security(project: Path) -> None:
    # T-001 is still todo in the baseline fixture — the report legitimately doesn't exist yet.
    assert not any(f.area == "security" for f in doctor.run_checks())


def test_large_events_log_is_flagged_before_rotation(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "EVENTS_MAX_BYTES", 100)
    (project / ".agentloop" / "events.ndjson").write_text("x" * 90 + "\n", encoding="utf-8")
    assert any("rotation" in m for m in _messages(doctor.run_checks(), "INFO"))


def test_guard_paths_typo_fails(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG + "  guard_paths:\n    docs/20-design.md: requirments\n", encoding="utf-8"
    )
    findings = doctor.run_checks()
    assert any("guard_paths values must be one of" in m and "requirments" in m for m in _messages(findings, "FAIL"))


def test_guard_paths_valid_passes(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG + "  guard_paths:\n    docs/20-design.md: requirements\n    docs/tasks/: design\n", encoding="utf-8"
    )
    assert any("guard_paths valid (2 entries)" in m for m in _messages(doctor.run_checks(), "PASS"))


def test_unparseable_config_fails(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text("build: [not a mapping", encoding="utf-8")
    assert any("not valid YAML" in m for m in _messages(doctor.run_checks(), "FAIL"))


def test_all_empty_cmd_steps_warn(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("run: 'make test'", "run: ''").replace("run: 'make check'", "run: ''"),
        encoding="utf-8",
    )
    assert any("would check nothing" in m for m in _messages(doctor.run_checks(), "WARN"))


def test_stale_legacy_config_keys_warn(project: Path) -> None:
    # Legacy keys next to a valid steps list are silently ignored — flag them so a reader
    # doesn't believe test_cmd still steers the gate.
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("  quality_gate:\n", "  retries: {test_fix: 2}\n  quality_gate:\n    test_cmd: make test\n"),
        encoding="utf-8",
    )
    warns = [m for m in _messages(doctor.run_checks(), "WARN") if "legacy pre-0.3.0 keys are ignored" in m]
    assert warns and "quality_gate.test_cmd" in warns[0] and "build.retries" in warns[0]


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
    assert any("agentloop init" in m for m in _messages(doctor.run_checks(), "FAIL"))
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("template_mode: false", "template_mode: true"), encoding="utf-8"
    )
    findings = doctor.run_checks()
    assert not any("agentloop init" in m for m in _messages(findings, "FAIL"))
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
    assert any("registered in neither" in m for m in _messages(doctor.run_checks(), "FAIL"))
    (project / ".claude" / "settings.json").unlink()
    assert any("registered in neither" in m for m in _messages(doctor.run_checks(), "FAIL"))
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("enforce_hook: true", "enforce_hook: false"), encoding="utf-8"
    )
    findings = doctor.run_checks()
    assert not _messages(findings, "FAIL")
    assert any("convention layer only" in m for m in _messages(findings, "INFO"))


def _copilot_hooks(project: Path) -> None:
    (project / ".github" / "hooks").mkdir(parents=True, exist_ok=True)
    (project / ".github" / "hooks" / "agentloop.json").write_text(
        '{"hooks": {"PreToolUse": [{"type": "command", "command": "uv run src/agentloop/gate_guard.py"}]}}\n',
        encoding="utf-8",
    )


def test_hook_single_surface_passes_with_an_info(project: Path) -> None:
    # claude only (the fixture baseline)
    findings = doctor.run_checks()
    assert any("gate_guard hook registered (claude)" in m for m in _messages(findings, "PASS"))
    assert any("only the claude hook host" in m for m in _messages(findings, "INFO"))
    # copilot only
    (project / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    _copilot_hooks(project)
    findings = doctor.run_checks()
    assert any("gate_guard hook registered (copilot)" in m for m in _messages(findings, "PASS"))
    assert any("only the copilot hook host" in m for m in _messages(findings, "INFO"))
    assert not _messages(findings, "FAIL")


def test_hook_both_surfaces_pass_without_info(project: Path) -> None:
    _copilot_hooks(project)
    findings = doctor.run_checks()
    assert any("gate_guard hook registered (claude, copilot)" in m for m in _messages(findings, "PASS"))
    assert not any("hook host" in m for m in _messages(findings, "INFO"))


def test_open_escalation_warns(project: Path) -> None:
    events.append_event("blocked", task="T-001", detail="red")
    warns = _messages(doctor.run_checks(), "WARN")
    assert any("open escalation" in m and "#1 blocked(T-001)" in m for m in warns)


def test_version_prefers_manifest_over_version_file(project: Path) -> None:
    (project / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    assert any("template repo, VERSION 0.2.0" in m for m in _messages(doctor.run_checks(), "INFO"))
    (project / ".agentloop" / "adopt-manifest.yaml").write_text("template:\n  version: 0.1.0\n", encoding="utf-8")
    assert any("template version 0.1.0" in m for m in _messages(doctor.run_checks(), "INFO"))


def test_headless_binary_check_follows_config(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The mode-A binary probe must track build.headless.cmd, not a hardcoded "claude".
    config = _CONFIG.replace("build:\n", 'build:\n  headless: {cmd: ["codex", "exec"]}\n')
    (project / ".agentloop" / "config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "codex" else f"/usr/bin/{name}")
    findings = doctor.run_checks()
    assert any("codex not found" in m for m in _messages(findings, "WARN"))
    assert doctor.main([]) == 0  # a missing headless CLI degrades mode A, it does not break the repo
