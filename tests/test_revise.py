"""Verify revise.py's gate chaining and surgical state.md update (deterministic, offline)."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentloop import revise

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: build          # brief | requirements | ... | done
gates:
  requirements: approved      # c1
  design: approved            # c2
  tasks: approved             # c3
  build: approved             # c4
  release: pending            # c5
updated_at: "2026-06-26"
---
# board

## Roll-back (revision) log

| Date | Target (phase) | Gates reset to pending in chain | Reason |
|------|---------------|-------------------------------|------|
<!-- REVISE-LOG -->
"""


def test_cascade_gates_per_phase() -> None:
    assert revise.cascade_gates("requirements") == ["requirements", "design", "tasks", "build", "release"]
    assert revise.cascade_gates("design") == ["design", "tasks", "build", "release"]
    assert revise.cascade_gates("tasks") == ["tasks", "build", "release"]
    assert revise.cascade_gates("build") == ["build", "release"]


def test_cascade_gates_rejects_invalid() -> None:
    with pytest.raises(revise.ReviseError):
        revise.cascade_gates("verify")


def test_apply_revision_to_design() -> None:
    out = revise.apply_revision(_STATE, "design", "rethink the auth method", "2026-07-01")
    # Upstream (requirements) stays approved; design onward is pending.
    assert re.search(r"requirements: approved\s+# c1", out)
    assert re.search(r"design: pending\s+# c2", out)  # comment preserved
    assert re.search(r"tasks: pending\s+# c3", out)
    assert re.search(r"build: pending\s+# c4", out)
    assert re.search(r"release: pending\s+# c5", out)
    # current_phase / updated_at (comment preserved).
    assert re.search(r"current_phase: design\s+# brief", out)
    assert 'updated_at: "2026-07-01"' in out
    # The log row is inserted right before the marker.
    assert "| 2026-07-01 | design | design, tasks, build, release | rethink the auth method |" in out
    row_idx = out.index("| 2026-07-01 | design")
    assert row_idx < out.index(revise.REVISE_MARKER)


def test_apply_revision_to_requirements_reverts_all() -> None:
    out = revise.apply_revision(_STATE, "requirements", "", "2026-07-01")
    for gate in ("requirements", "design", "tasks", "build", "release"):
        assert re.search(rf"{gate}: pending", out)
    assert re.search(r"current_phase: requirements\s+# brief", out)
    # An empty reason becomes "-".
    assert "| 2026-07-01 | requirements |" in out and "| - |" in out


def test_apply_revision_without_marker_is_safe() -> None:
    # Even a state.md with no marker is not broken; only the gates are updated.
    text = _STATE.replace(revise.REVISE_MARKER, "")
    out = revise.apply_revision(text, "build", "x", "2026-07-01")
    assert re.search(r"build: pending\s+# c4", out)
    assert re.search(r"release: pending\s+# c5", out)
    assert re.search(r"requirements: approved\s+# c1", out)  # what is not in the chain is unchanged


# --- main(): the CLI entry (dry-run plans only; the real run rewrites state.md) ---


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".agentloop" / "state.md").write_text(_STATE, encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_main_dry_run_plans_without_writing(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert revise.main(["--to", "design", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "design, tasks, build, release" in out
    assert (project / ".agentloop" / "state.md").read_text(encoding="utf-8") == _STATE  # untouched


def test_main_rewrites_state_and_logs(project: Path) -> None:
    assert revise.main(["--to", "build", "--reason", "verify found a defect"]) == 0
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert re.search(r"build: pending\s+# c4", state) and re.search(r"release: pending\s+# c5", state)
    assert re.search(r"tasks: approved\s+# c3", state)  # upstream untouched
    assert "| build | build, release | verify found a defect |" in state


def test_main_errors_when_state_missing(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / ".agentloop" / "state.md").unlink()
    assert revise.main(["--to", "design"]) == 1
    assert "cannot read state.md" in capsys.readouterr().err


# --- --impacted: deterministic impact marking in tasks.yaml -------------------

_TASKS = """tasks:
  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: done, test: make test}
  - {id: T-002, title: leaf A, kind: parallel, blockedBy: [T-001], status: done, test: make test}
  - {id: T-003, title: leaf B, kind: parallel, blockedBy: [T-001], status: todo, test: make test}
  - {id: T-004, title: join, kind: integration, blockedBy: [T-002, T-003], status: todo, test: make test}
"""


def _statuses() -> dict[str, str]:
    from agentloop import dag

    return {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}


def test_main_impacted_marks_seed_and_transitive_dependents(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Missing an impacted task is the dangerous direction: the whole closure is marked in code;
    # "keep" is a deliberate reclassification at the /tasks reconcile, never a silent default.
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    assert revise.main(["--impacted", "T-002"]) == 0
    statuses = _statuses()
    assert statuses["T-002"] == "needs-revision"  # seed
    assert statuses["T-004"] == "needs-revision"  # transitive dependent
    assert statuses["T-001"] == "done" and statuses["T-003"] == "todo"  # outside the closure: untouched
    out = capsys.readouterr().out
    assert "T-002 → needs-revision (was done)" in out  # former statuses feed the reconcile
    assert "1 seed(s) + 1 transitive dependent(s)" in out


def test_main_impacted_dry_run_lists_without_writing(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    assert revise.main(["--impacted", "T-002", "--dry-run"]) == 0
    assert _statuses()["T-002"] == "done"  # untouched
    assert "[dry-run] would mark T-002" in capsys.readouterr().out


def test_main_impacted_unknown_id_errors(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    assert revise.main(["--impacted", "T-999"]) == 1
    assert "unknown task id" in capsys.readouterr().err
    assert _statuses() == {"T-001": "done", "T-002": "done", "T-003": "todo", "T-004": "todo"}


def test_main_impacted_broken_tasks_yaml_names_the_file_and_next_step(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".agentloop" / "tasks.yaml").write_text("tasks: [broken\n", encoding="utf-8")
    assert revise.main(["--impacted", "T-001"]) == 1  # a YAML parse error must not traceback
    err = capsys.readouterr().err
    assert ".agentloop/tasks.yaml" in err and "agentloop doctor" in err  # cause + the next step


def test_main_combines_to_and_impacted(project: Path) -> None:
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    assert revise.main(["--to", "design", "--impacted", "T-002,T-003"]) == 0
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert re.search(r"design: pending\s+# c2", state)  # gates reset...
    statuses = _statuses()
    assert statuses["T-002"] == statuses["T-003"] == statuses["T-004"] == "needs-revision"  # ...and impact marked


def test_main_requires_to_or_impacted(project: Path) -> None:
    with pytest.raises(SystemExit):
        revise.main([])
