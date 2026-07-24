"""Tests for gate_guard.py — the mechanism layer's four rules.

The most important assertions here are the *negative* ones: that there is no configuration
value, no template mode, and no unreadable state that turns a denial into an allow. A guard
with an off switch an agent can reach is a convention, and 0.8.x had exactly that
(`gates.enforce_hook: false`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentloop import gate_guard, models
from agentloop import repo as repo_mod
from tests._support import make_config, make_state, seed_repo


def decide(root: Path, rel: str) -> tuple[bool, str]:
    return gate_guard.evaluate(str(root / rel), repo_mod.Repo(root))


def hook(root: Path, rel: str, **tool_input: object) -> str:
    """Drive main() through the hook protocol; returns the deny reason ("" = allowed)."""
    import io
    import sys

    payload = {"cwd": str(root), "tool_input": {"file_path": str(root / rel), **tool_input}}
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), io.StringIO()
    try:
        assert gate_guard.main([]) == 0  # the decision travels as JSON; the exit code is always 0
        raw = sys.stdout.getvalue().strip()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return json.loads(raw)["hookSpecificOutput"]["permissionDecisionReason"] if raw else ""


# --- rule 1: machine-written artifacts ----------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".agentloop/state.yaml",
        ".agentloop/review.yaml",
        ".agentloop/events.ndjson",
        ".agentloop/attestations/ATT-BUILD-001.json",
        ".agentloop/agentloop.lock",
    ],
)
def test_machine_written_artifacts_are_never_hand_edited(tmp_path: Path, rel: str) -> None:
    seed_repo(tmp_path)
    allowed, reason = decide(tmp_path, rel)
    assert not allowed
    assert "Central Store transaction" in reason


def test_rule_one_is_not_relaxed_by_template_mode(tmp_path: Path) -> None:
    seed_repo(tmp_path, config=make_config(template_mode=True))
    allowed, _ = decide(tmp_path, ".agentloop/state.yaml")
    assert not allowed


def test_rule_one_holds_even_with_every_gate_approved(tmp_path: Path) -> None:
    seed_repo(tmp_path, state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "approved"), phase="done"))
    allowed, _ = decide(tmp_path, ".agentloop/events.ndjson")
    assert not allowed


# --- rule 2: a frozen plan is frozen ------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".agentloop/plan.yaml",
        ".agentloop/config.yaml",
        ".agentloop/oracles/O-001/oracle.yaml",
        ".agentloop/prompts/commands/build.md",
        ".agentloop/schema/plan.schema.json",
    ],
)
def test_a_frozen_plan_pins_its_artifacts(tmp_path: Path, rel: str) -> None:
    seed_repo(tmp_path, state=make_state(plan_status="frozen"))
    allowed, reason = decide(tmp_path, rel)
    assert not allowed
    assert "agentloop revise --to tasks" in reason


def test_a_draft_plan_is_editable(tmp_path: Path) -> None:
    seed_repo(tmp_path, state=make_state(plan_status="draft"))
    assert decide(tmp_path, ".agentloop/plan.yaml")[0]
    assert decide(tmp_path, ".agentloop/config.yaml")[0]


def test_an_unreadable_state_fails_closed_on_the_frozen_set(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".agentloop" / "state.yaml").write_text("a: [1, 2\n", encoding="utf-8")
    allowed, reason = decide(tmp_path, ".agentloop/plan.yaml")
    assert not allowed
    assert "fails closed" in reason


# --- rule 3: a deliverable waits for its prerequisite gate --------------------


@pytest.mark.parametrize(
    ("rel", "gate"),
    [
        ("docs/20-design.md", "requirements"),
        ("docs/decisions/ADR-001.md", "requirements"),
        ("docs/tasks/T-001.md", "design"),
        ("docs/test/test-plan.md", "build"),
        ("src/app.py", "tasks"),
        ("frontend/index.ts", "tasks"),
    ],
)
def test_a_guarded_path_names_its_prerequisite(tmp_path: Path, rel: str, gate: str) -> None:
    seed_repo(tmp_path, state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending")))
    allowed, reason = decide(tmp_path, rel)
    assert not allowed
    assert f"gate '{gate}' is not approved" in reason


def test_an_approved_gate_opens_its_paths(tmp_path: Path) -> None:
    seed_repo(tmp_path)  # approved through tasks
    assert decide(tmp_path, "src/app.py")[0]
    assert decide(tmp_path, "docs/20-design.md")[0]
    assert not decide(tmp_path, "docs/test/test-plan.md")[0]  # build is still pending


@pytest.mark.parametrize("rel", ["tests/test_app.py", "docs/00-product-brief.md", "README.md", "makefile"])
def test_unguarded_paths_stay_open(tmp_path: Path, rel: str) -> None:
    """tests/ is deliberately unguarded: preparing fixtures while a gate is pending is
    sanctioned speculative work, and freezing it would just push the work off the record."""
    seed_repo(tmp_path, state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending")))
    assert decide(tmp_path, rel)[0]


def test_template_mode_relaxes_only_rule_three(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        config=make_config(template_mode=True),
        state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending"), plan_status="draft"),
    )
    assert decide(tmp_path, "src/app.py")[0]  # rule 3 relaxed
    assert not decide(tmp_path, ".agentloop/state.yaml")[0]  # rule 1 is not


def test_an_unreadable_state_fails_closed_on_guarded_paths(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".agentloop" / "state.yaml").unlink()
    allowed, reason = decide(tmp_path, "src/app.py")
    assert not allowed
    assert "no flag that turns this guard off" in reason


def test_there_is_no_enforce_hook_escape_hatch(tmp_path: Path) -> None:
    """0.8.x's `gates.enforce_hook: false`. The schema rejects the key outright."""
    config = make_config()
    config["gates"] = {"enforce_hook": False}  # type: ignore[index]
    with pytest.raises(AssertionError, match="not schema-valid"):
        seed_repo(tmp_path, config=config)


def test_a_path_outside_the_repo_is_not_this_guard_s_business(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert gate_guard.evaluate("/etc/hosts", repo_mod.Repo(tmp_path))[0]


# --- rule matching: exact wins over prefix, longest prefix wins ---------------


def test_exact_rule_wins_over_a_prefix_rule() -> None:
    rules = {"docs/": "build", "docs/20-design.md": "requirements"}
    assert gate_guard.required_gate("docs/20-design.md", rules, repo_mod.Repo(Path.cwd())) == "requirements"


def test_the_longest_matching_prefix_wins() -> None:
    rules = {"src/": "tasks", "src/vendor/": "release"}
    repo = repo_mod.Repo(Path.cwd())
    assert gate_guard.required_gate("src/vendor/lib.py", rules, repo) == "release"
    assert gate_guard.required_gate("src/app.py", rules, repo) == "tasks"


def test_config_paths_replace_the_built_in_defaults(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        config=make_config(guard_paths=[{"path": "core/", "requires_gate": "tasks"}]),
        state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending")),
    )
    assert not decide(tmp_path, "core/thing.py")[0]
    assert decide(tmp_path, "src/app.py")[0]  # not in this repo's map


def test_defaults_apply_when_config_declares_no_paths(tmp_path: Path) -> None:
    config = make_config()
    config["guard"].pop("paths")  # type: ignore[union-attr]
    seed_repo(tmp_path, config=config, state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending")))
    assert not decide(tmp_path, "src/app.py")[0]


# --- rule 4: only humans open gates -------------------------------------------


def test_an_edit_that_would_approve_a_gate_names_the_reason(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    current = (tmp_path / ".agentloop" / "state.yaml").read_text(encoding="utf-8")
    proposed = current.replace("build:\n    status: pending", "build:\n    status: approved")
    reason = gate_guard.gate_flip_denial({"content": proposed}, repo_mod.Repo(tmp_path))
    assert "gates.build to approved" in reason
    assert "Trust Manifest" in reason


def test_an_edit_that_changes_no_gate_is_not_a_flip(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    current = (tmp_path / ".agentloop" / "state.yaml").read_text(encoding="utf-8")
    assert gate_guard.gate_flip_denial({"content": current}, repo_mod.Repo(tmp_path)) == ""


def test_the_hook_denies_a_state_write_whatever_the_payload_shape(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert "Central Store transaction" in hook(tmp_path, ".agentloop/state.yaml", content="anything")


# --- the hook protocol --------------------------------------------------------


def test_the_hook_allows_an_unguarded_path_silently(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert hook(tmp_path, "tests/test_x.py") == ""


def test_a_payload_with_no_file_path_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    seed_repo(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path), "tool_input": {}})))
    assert gate_guard.main([]) == 0


def test_the_camelcase_spelling_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    seed_repo(tmp_path, state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending")))
    payload = {"cwd": str(tmp_path), "tool_input": {"filePath": str(tmp_path / "src/app.py")}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    gate_guard.main([])
    assert "deny" in out.getvalue()


def test_an_unparseable_payload_allows_but_leaves_a_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Some hosts fire the hook for every tool; a malformed payload must not block path-less
    tools. The warning is what keeps a guard that stopped guarding visible."""
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))
    assert gate_guard.main([]) == 0
    assert "unparseable hook payload" in capsys.readouterr().err


# --- commit-stage check -------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.mark.integration
def test_check_diff_fails_on_a_guarded_path(tmp_path: Path) -> None:
    seed_repo(tmp_path, state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending")), git=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert gate_guard.check_diff(repo_mod.Repo(tmp_path)) == 1


@pytest.mark.integration
def test_check_diff_passes_when_the_gate_is_approved(tmp_path: Path) -> None:
    seed_repo(tmp_path, git=True)  # approved through tasks
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert gate_guard.check_diff(repo_mod.Repo(tmp_path)) == 0


@pytest.mark.integration
def test_check_diff_catches_a_gate_flip_with_no_event(tmp_path: Path) -> None:
    """A flip smuggled past the editor hook — a shell redirect, `sed -i` — has no
    gate_approved event, and fails here before it can be committed."""
    seed_repo(tmp_path, state=make_state(gates={"build": "pending"}), git=True)
    _git(tmp_path, "config", "user.email", "t@e.x")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")

    state = tmp_path / ".agentloop" / "state.yaml"
    state.write_text(
        state.read_text(encoding="utf-8").replace("build:\n    status: pending", "build:\n    status: approved"),
        encoding="utf-8",
    )
    assert gate_guard.check_diff(repo_mod.Repo(tmp_path)) == 1


@pytest.mark.integration
def test_check_diff_skips_when_git_is_unusable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)  # no git init
    assert gate_guard.check_diff(repo_mod.Repo(tmp_path)) == 0
    assert "git status unavailable" in capsys.readouterr().err
