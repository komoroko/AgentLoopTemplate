"""Verify gate_guard.py's gate decision."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentloop import gate_guard
from tests._support import make_state, seed_repo

_CONFIG_ON = "build:\n  max_parallel: 3\ngates:\n  enforce_hook: true\n  template_mode: false\n"
_CONFIG_OFF = "build:\n  max_parallel: 3\ngates:\n  enforce_hook: false\n  template_mode: false\n"
_CONFIG_TEMPLATE = "build:\n  max_parallel: 3\ngates:\n  enforce_hook: true\n  template_mode: true\n"


def _setup(
    root: Path,
    *,
    requirements: str = "pending",
    design: str = "pending",
    tasks: str = "pending",
    build: str = "pending",
    config: str = _CONFIG_ON,
) -> None:
    gates = {"requirements": requirements, "design": design, "tasks": tasks, "build": build, "release": "pending"}
    seed_repo(root, state=make_state(gates=gates), config=config)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/20-design.md", "requirements"),
        ("docs/decisions/ADR-001.md", "requirements"),
        ("docs/tasks/T-001.md", "design"),
        ("src/pkg/main.py", "tasks"),
        ("lib/util.py", "tasks"),
        ("app/views.py", "tasks"),
        ("backend/app/main.py", "tasks"),
        ("frontend/src/index.ts", "tasks"),
        ("scripts/my_product_tool.py", "tasks"),  # product script
        ("docs/test/test-plan.md", "build"),
        # not guarded
        ("docs/10-requirements.md", None),
        ("README.md", None),
        ("tests/test_main.py", None),  # deliberate: speculative fixtures may flow
        ("app.py", None),  # prefix rules match directories, not lookalike files
    ],
)
def test_required_gate_mapping(chdir_tmp: Path, path: str, expected: str | None) -> None:
    assert gate_guard.required_gate(path) == expected


def test_blocks_impl_when_tasks_pending(chdir_tmp: Path) -> None:
    _setup(chdir_tmp, tasks="pending")
    allowed, reason = gate_guard.evaluate("backend/app/main.py")
    assert allowed is False
    assert "tasks" in reason


def test_allows_impl_when_tasks_approved(chdir_tmp: Path) -> None:
    _setup(chdir_tmp, tasks="approved")
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is True


def test_blocks_product_script_when_tasks_pending(chdir_tmp: Path) -> None:
    _setup(chdir_tmp, tasks="pending")
    allowed, reason = gate_guard.evaluate("scripts/my_product_tool.py")
    assert allowed is False
    assert "tasks" in reason


def test_allows_unguarded_path(chdir_tmp: Path) -> None:
    _setup(chdir_tmp)
    # tests/ is deliberately unguarded so approval-wait speculative work keeps flowing.
    assert gate_guard.evaluate("tests/test_feature.py") == (True, "")
    assert gate_guard.evaluate("README.md") == (True, "")


def test_enforce_hook_false_allows_everything(chdir_tmp: Path) -> None:
    _setup(chdir_tmp, tasks="pending", config=_CONFIG_OFF)
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is True


_CONFIG_DOCS_ONLY = (
    "gates:\n"
    "  enforce_hook: true\n"
    "  template_mode: false\n"
    "  guard_paths:\n"
    "    docs/20-design.md: requirements\n"
    "    docs/decisions/: requirements\n"
    "    docs/tasks/: design\n"
    "    docs/test/: build\n"
)

_CONFIG_SRC_LAYOUT = (
    "gates:\n"
    "  enforce_hook: true\n"
    "  template_mode: false\n"
    "  guard_paths:\n"
    "    docs/20-design.md: requirements\n"
    "    src/: tasks\n"
    "    src/design-notes/: design\n"
)


def test_guard_paths_docs_only_lets_existing_code_flow(chdir_tmp: Path) -> None:
    # The brownfield default: only docs deliverables are guarded, so normal development on the
    # existing codebase is not frozen by pending gates right after adoption.
    _setup(chdir_tmp, tasks="pending", config=_CONFIG_DOCS_ONLY)
    assert gate_guard.evaluate("backend/app/main.py") == (True, "")
    assert gate_guard.evaluate("src/anything.py") == (True, "")
    allowed, reason = gate_guard.evaluate("docs/20-design.md")
    assert allowed is False
    assert "requirements" in reason


def test_guard_paths_maps_custom_layout(chdir_tmp: Path) -> None:
    _setup(chdir_tmp, tasks="pending", config=_CONFIG_SRC_LAYOUT)
    allowed, reason = gate_guard.evaluate("src/feature/api.py")
    assert allowed is False
    assert "tasks" in reason
    # The longest matching prefix wins over the shorter one, deterministically.
    allowed, reason = gate_guard.evaluate("src/design-notes/plan.md")
    assert allowed is False
    assert "design" in reason
    # Paths outside the configured rules are unguarded (the built-in backend/ rule is replaced).
    assert gate_guard.evaluate("backend/app/main.py") == (True, "")


def test_guard_paths_absent_falls_back_to_defaults(chdir_tmp: Path) -> None:
    # _CONFIG_ON has no guard_paths key → the built-in default rules apply (backward compat).
    _setup(chdir_tmp, tasks="pending", config=_CONFIG_ON)
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is False


def test_template_mode_allows_everything(chdir_tmp: Path) -> None:
    # The template repo itself: scaffold originals share deliverable paths, so the guard steps aside.
    _setup(chdir_tmp, tasks="pending", config=_CONFIG_TEMPLATE)
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is True
    allowed, _ = gate_guard.evaluate("docs/20-design.md")
    assert allowed is True


def test_template_mode_defaults_off(chdir_tmp: Path) -> None:
    # A config without the key behaves as product mode (guard live).
    _setup(chdir_tmp, tasks="pending", config="gates:\n  enforce_hook: true\n")
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is False


def test_fail_closed_when_no_state(chdir_tmp: Path) -> None:
    # If state.md is absent, guarded paths are denied (fail closed): the guard is the only
    # mechanism for the design/tasks phases, so an unknown state must not open every gate.
    (chdir_tmp / ".agentloop").mkdir(parents=True, exist_ok=True)
    (chdir_tmp / ".agentloop" / "config.yaml").write_text(_CONFIG_ON, encoding="utf-8")
    allowed, reason = gate_guard.evaluate("backend/app/main.py")
    assert allowed is False
    assert "enforce_hook" in reason  # the escape hatch is pointed out
    # Unguarded paths stay allowed even with unreadable state.
    assert gate_guard.evaluate("README.md") == (True, "")


def test_fail_closed_when_gates_malformed(chdir_tmp: Path) -> None:
    (chdir_tmp / ".agentloop").mkdir(parents=True, exist_ok=True)
    (chdir_tmp / ".agentloop" / "config.yaml").write_text(_CONFIG_ON, encoding="utf-8")
    (chdir_tmp / ".agentloop" / "state.md").write_text("no front matter here", encoding="utf-8")
    allowed, _ = gate_guard.evaluate("docs/tasks/T-001.md")
    assert allowed is False


# --- main(): the hook's real stdin→stdout I/O path -----------------------------


def _run_main(payload: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = gate_guard.main()
    return rc, capsys.readouterr().out


def test_main_emits_deny_decision_for_guarded_path(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(chdir_tmp, tasks="pending")
    payload = json.dumps({"tool_input": {"file_path": "backend/app/main.py"}})
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert rc == 0  # the hook communicates via the JSON decision, not the exit code
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "tasks" in decision["permissionDecisionReason"]


def test_main_stays_silent_for_allowed_path(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(chdir_tmp, tasks="approved")
    payload = json.dumps({"tool_input": {"file_path": "backend/app/main.py"}})
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert rc == 0
    assert out == ""  # no decision printed = the tool call proceeds


def test_main_does_not_intervene_on_malformed_input(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for payload in ("not json", "{}", json.dumps({"tool_input": {"file_path": ""}})):
        rc, out = _run_main(payload, monkeypatch, capsys)
        assert (rc, out) == (0, "")


# --- VS Code Copilot dialect (camelCase filePath; hook fires on every tool) -----


def test_main_denies_camelcase_file_path(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(chdir_tmp, tasks="pending")
    payload = json.dumps({"tool_name": "create_file", "tool_input": {"filePath": "backend/app/main.py"}})
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert rc == 0
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "tasks" in decision["permissionDecisionReason"]


def test_main_allows_camelcase_file_path_when_gate_approved(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(chdir_tmp, tasks="approved")
    payload = json.dumps({"tool_name": "replace_string_in_file", "tool_input": {"filePath": "backend/app/main.py"}})
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert (rc, out) == (0, "")


def test_main_passes_pathless_tools_even_when_state_is_unreadable(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # VS Code ignores matchers, so the hook sees reads/terminal too. Fail-closed applies
    # only to guarded-path writes — a path-less tool must pass even with state.md broken.
    (chdir_tmp / ".agentloop").mkdir(parents=True, exist_ok=True)
    (chdir_tmp / ".agentloop" / "config.yaml").write_text(_CONFIG_ON, encoding="utf-8")
    payload = json.dumps({"tool_name": "run_in_terminal", "tool_input": {"command": "ls"}})
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert (rc, out) == (0, "")


# --- gate-approval write protection (edit-time): only `make approve` may flip ----


def _deny_reason(out: str) -> str:
    return str(json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])


def test_write_flipping_a_gate_to_approved_is_denied(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(chdir_tmp)
    flipped = make_state(gates={"requirements": "approved", "design": "pending", "tasks": "pending"})
    payload = json.dumps({"tool_input": {"file_path": ".agentloop/state.md", "content": flipped}})
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert rc == 0
    reason = _deny_reason(out)
    assert "gates.requirements" in reason and "agentloop approve" in reason


def test_edit_flipping_a_gate_to_approved_is_denied(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(chdir_tmp)
    payload = json.dumps(
        {
            "tool_input": {
                "file_path": ".agentloop/state.md",
                "old_string": "requirements: pending",
                "new_string": "requirements: approved   # 2026-07-13",
            }
        }
    )
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert rc == 0
    assert "agentloop approve" in _deny_reason(out)


def test_multiedit_and_camelcase_flips_are_denied(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(chdir_tmp)
    payload = json.dumps(
        {
            "tool_input": {
                "file_path": ".agentloop/state.md",
                "edits": [
                    {"old_string": "current_phase: build", "new_string": "current_phase: verify"},
                    {"old_string": "build: pending", "new_string": "build: approved"},
                ],
            }
        }
    )
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert "gates.build" in _deny_reason(out)
    # VS Code Copilot spelling (filePath / oldString / newString) is understood too.
    payload = json.dumps(
        {
            "tool_input": {
                "filePath": ".agentloop/state.md",
                "oldString": "requirements: pending",
                "newString": "requirements: approved",
            }
        }
    )
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert "gates.requirements" in _deny_reason(out)


def test_state_edits_that_do_not_flip_are_allowed(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Ordinary state.md maintenance (progress board, logs, already-approved gate untouched or
    # reset to pending) must keep flowing — only the pending→approved direction is protected.
    _setup(chdir_tmp, requirements="approved")
    for old, new in (
        ("# board", "# board\n- note"),
        ("requirements: approved", "requirements: approved   # 2026-07-13 alice"),
        ("requirements: approved", "requirements: pending"),
    ):
        payload = json.dumps({"tool_input": {"file_path": ".agentloop/state.md", "old_string": old, "new_string": new}})
        rc, out = _run_main(payload, monkeypatch, capsys)
        assert (rc, out) == (0, ""), (old, new)


def test_creating_state_with_approved_gates_is_denied(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No on-disk state.md = nothing is approved yet; a Write that starts life approved is a flip.
    (chdir_tmp / ".agentloop").mkdir(parents=True, exist_ok=True)
    (chdir_tmp / ".agentloop" / "config.yaml").write_text(_CONFIG_ON, encoding="utf-8")
    content = make_state(gates={"requirements": "approved", "design": "pending", "tasks": "pending"})
    payload = json.dumps({"tool_input": {"file_path": ".agentloop/state.md", "content": content}})
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert "gates.requirements" in _deny_reason(out)


def test_flip_denial_ignores_template_mode_but_respects_enforce_hook(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # template_mode relaxes the deliverable-path rules, not gate rule 2 (the scaffold state.md
    # has no legitimate approved-flip either); enforce_hook: false stays the global escape hatch.
    payload = json.dumps(
        {
            "tool_input": {
                "file_path": ".agentloop/state.md",
                "old_string": "requirements: pending",
                "new_string": "requirements: approved",
            }
        }
    )
    _setup(chdir_tmp, config=_CONFIG_TEMPLATE)
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert "agentloop approve" in _deny_reason(out)
    _setup(chdir_tmp, config=_CONFIG_OFF)
    rc, out = _run_main(payload, monkeypatch, capsys)
    assert (rc, out) == (0, "")


def test_unrecognized_state_write_shape_is_allowed_with_a_trace(
    chdir_tmp: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A host tool shape we cannot simulate must not block path-carrying non-edits; the
    # commit-stage flip check still covers whatever it wrote.
    _setup(chdir_tmp)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_input": {"file_path": ".agentloop/state.md"}})))
    rc = gate_guard.main()
    captured = capsys.readouterr()
    assert (rc, captured.out) == (0, "")
    assert "unrecognized payload shape" in captured.err


# --- --check-diff: the agent-agnostic commit-stage mode --------------------------
# Every test below shells out to real git, so each carries @pytest.mark.integration.


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


@pytest.mark.integration
def test_check_diff_denies_untracked_guarded_path(chdir_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A brand-new deliverable is exactly what lands at a gate — untracked files must be covered.
    _git_init(chdir_tmp)
    _setup(chdir_tmp, design="pending")
    (chdir_tmp / "docs" / "tasks").mkdir(parents=True)
    (chdir_tmp / "docs" / "tasks" / "T-001.md").write_text("ticket", encoding="utf-8")
    assert gate_guard.main(["--check-diff"]) == 1
    err = capsys.readouterr().err
    assert "docs/tasks/T-001.md" in err
    assert "design" in err


@pytest.mark.integration
def test_check_diff_passes_when_gate_approved(chdir_tmp: Path) -> None:
    _git_init(chdir_tmp)
    _setup(chdir_tmp, design="approved")
    (chdir_tmp / "docs" / "tasks").mkdir(parents=True)
    (chdir_tmp / "docs" / "tasks" / "T-001.md").write_text("ticket", encoding="utf-8")
    assert gate_guard.main(["--check-diff"]) == 0


@pytest.mark.integration
def test_check_diff_passes_unguarded_changes_only(chdir_tmp: Path) -> None:
    # .agentloop/** and README-like paths are unguarded; a diff of only those must not fail.
    _git_init(chdir_tmp)
    _setup(chdir_tmp, design="pending")
    (chdir_tmp / "README.md").write_text("readme", encoding="utf-8")
    assert gate_guard.main(["--check-diff"]) == 0


@pytest.mark.integration
def test_check_diff_respects_template_mode(chdir_tmp: Path) -> None:
    _git_init(chdir_tmp)
    _setup(chdir_tmp, design="pending", config=_CONFIG_TEMPLATE)
    (chdir_tmp / "docs" / "tasks").mkdir(parents=True)
    (chdir_tmp / "docs" / "tasks" / "T-001.md").write_text("ticket", encoding="utf-8")
    assert gate_guard.main(["--check-diff"]) == 0


@pytest.mark.integration
def test_check_diff_denies_modified_tracked_guarded_path(chdir_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git_init(chdir_tmp)
    _setup(chdir_tmp, requirements="pending")
    (chdir_tmp / "docs").mkdir()
    (chdir_tmp / "docs" / "20-design.md").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=chdir_tmp, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=chdir_tmp,
        check=True,
    )
    assert gate_guard.main(["--check-diff"]) == 0  # clean tree: nothing to flag
    (chdir_tmp / "docs" / "20-design.md").write_text("v2", encoding="utf-8")
    assert gate_guard.main(["--check-diff"]) == 1
    assert "docs/20-design.md" in capsys.readouterr().err


@pytest.mark.integration
def test_check_diff_skips_outside_a_git_repo(
    chdir_tmp: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without git there is no diff to enforce against; skip with a note instead of blocking make check.
    monkeypatch.setenv("GIT_DIR", str(chdir_tmp / "nonexistent"))  # make git status fail even under a parent repo
    _setup(chdir_tmp, design="pending")
    assert gate_guard.main(["--check-diff"]) == 0
    assert "skipping" in capsys.readouterr().err


# --- --check-diff: the commit-stage gate-flip check -------------------------------


def _commit_all(path: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-qm", "seed"],
        cwd=path,
        check=True,
    )


@pytest.mark.integration
def test_check_diff_denies_gate_flip_without_event(chdir_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A flip smuggled past the tool hook (shell redirect / sed) has no gate_approved event.
    _git_init(chdir_tmp)
    _setup(chdir_tmp)
    _commit_all(chdir_tmp)
    flipped = make_state(gates={"requirements": "approved", "design": "pending", "tasks": "pending"})
    (chdir_tmp / ".agentloop" / "state.md").write_text(flipped, encoding="utf-8")
    assert gate_guard.main(["--check-diff"]) == 1
    err = capsys.readouterr().err
    assert "gates.requirements" in err and "agentloop approve" in err


@pytest.mark.integration
def test_check_diff_passes_gate_flip_with_event(chdir_tmp: Path) -> None:
    # approve.py writes the flip and the event in one operation — the sanctioned path passes.
    from agentloop import approve

    _git_init(chdir_tmp)
    _setup(chdir_tmp)
    _commit_all(chdir_tmp)
    approve.record_approval("requirements", "alice")
    assert gate_guard.main(["--check-diff"]) == 0


@pytest.mark.integration
def test_check_diff_flip_check_ignores_template_mode(chdir_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The deliverable-path rules relax under template_mode; gate rule 2's protection does not.
    _git_init(chdir_tmp)
    _setup(chdir_tmp, config=_CONFIG_TEMPLATE)
    _commit_all(chdir_tmp)
    flipped = make_state(gates={"requirements": "approved", "design": "pending", "tasks": "pending"})
    (chdir_tmp / ".agentloop" / "state.md").write_text(flipped, encoding="utf-8")
    assert gate_guard.main(["--check-diff"]) == 1
    assert "gates.requirements" in capsys.readouterr().err
