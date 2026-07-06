"""Verify cycle.py's snapshot, archive plan, and state reset."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import cycle
import pytest

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: verify
gates:
  requirements: approved
  design: approved
  tasks: approved
  build: approved
  release: approved
updated_at: "2026-06-26"
---
# board

## Roll-back (revision) log

| Date | Target (phase) | Gates reset to pending in chain | Reason |
|------|---------------|-------------------------------|------|
<!-- REVISE-LOG -->
"""


# A pristine state snapshot (fresh body, empty logs) and a stale live state (checked boxes, a task
# row, an accumulated roll-back row) — the inputs to the body-refresh path.
_PRISTINE = """---
project: "<enter the product name>"
branch: "<enter the work branch name>"
current_phase: brief
gates:
  requirements: pending
  design: pending
  tasks: pending
  build: pending
  release: pending
updated_at: "<YYYY-MM-DD>"
---

# Progress board

## Phase progress
- [ ] brief
- [ ] requirements

## Roll-back (revision) log

| Date | Target (phase) | Gates reset to pending in chain | Reason |
|------|---------------|-------------------------------|------|
<!-- REVISE-LOG -->
"""

_LIVE_STALE = """---
project: "demo"
branch: "build/demo"
current_phase: verify
gates:
  requirements: approved
  design: approved
  tasks: approved
  build: approved
  release: approved
updated_at: "2026-06-26"
---

# Progress board

## Phase progress
- [x] brief
- [x] requirements

## Roll-back (revision) log

| Date | Target (phase) | Gates reset to pending in chain | Reason |
|------|---------------|-------------------------------|------|
| 2026-06-01 | requirements | requirements, design, tasks, build, release | earlier revise |
<!-- REVISE-LOG -->
"""


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    docs = tmp_path / "docs"
    (docs / "decisions").mkdir(parents=True)
    (docs / "tasks").mkdir()
    (docs / "test").mkdir()
    for name in ("00-product-brief.md", "10-requirements.md", "20-design.md", "retrospective.md"):
        (docs / name).write_text(f"scaffold: {name}\n", encoding="utf-8")
    (docs / "decisions" / "ADR-template.md").write_text("scaffold: adr\n", encoding="utf-8")
    (docs / "tasks" / "T-template.md").write_text("scaffold: task\n", encoding="utf-8")
    (docs / "test" / "test-plan.md").write_text("scaffold: test-plan\n", encoding="utf-8")
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".agentloop" / "state.md").write_text(_STATE, encoding="utf-8")
    (tmp_path / ".agentloop" / "tasks.yaml").write_text("tasks:\n  - {id: T-001}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_snapshot_scaffold_copies_docs_once(project: Path) -> None:
    assert cycle.snapshot_scaffold() is True
    snap = project / ".agentloop" / "scaffold" / "docs"
    assert (snap / "10-requirements.md").read_text(encoding="utf-8") == "scaffold: 10-requirements.md\n"
    assert (snap / "decisions" / "ADR-template.md").exists()
    # A re-run after docs were filled must not overwrite the pristine copy.
    (project / "docs" / "10-requirements.md").write_text("FILLED\n", encoding="utf-8")
    assert cycle.snapshot_scaffold() is False
    assert (snap / "10-requirements.md").read_text(encoding="utf-8") == "scaffold: 10-requirements.md\n"


def test_snapshot_skips_archive_dir(project: Path) -> None:
    (project / "docs" / "archive" / "2026-01-01-old").mkdir(parents=True)
    assert cycle.snapshot_scaffold() is True
    assert not (project / ".agentloop" / "scaffold" / "docs" / "archive").exists()


def test_plan_close_marks_missing_items_as_skip(project: Path) -> None:
    (project / "docs" / "retrospective.md").unlink()
    rows = {src: action for action, src, _ in cycle.plan_close("demo", "2026-07-03")}
    assert rows["docs/10-requirements.md"] == "archive"
    assert rows["docs/retrospective.md"] == "skip"


def test_reset_state_text_resets_gates_phase_and_logs(project: Path) -> None:
    out = cycle.reset_state_text(_STATE, "demo", "2026-07-03", "docs/archive/2026-07-03-demo")
    assert out.count(": pending") == 5  # all five gates
    assert "approved" not in out.split("---")[1]  # none left in the front-matter
    assert "current_phase: brief" in out
    assert 'updated_at: "2026-07-03"' in out
    assert "| 2026-07-03 | cycle-close (demo) |" in out  # logged in the roll-back table


def test_main_close_archives_restores_and_resets(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cycle.snapshot_scaffold()
    (project / "docs" / "10-requirements.md").write_text("FILLED requirements\n", encoding="utf-8")
    assert cycle.main(["--name", "first"]) == 0
    out = capsys.readouterr().out
    # The filled deliverable is archived and a fresh scaffold restored in its place.
    archives = list((project / "docs" / "archive").iterdir())
    assert len(archives) == 1 and archives[0].name.endswith("-first")
    assert (archives[0] / "10-requirements.md").read_text(encoding="utf-8") == "FILLED requirements\n"
    assert (project / "docs" / "10-requirements.md").read_text(encoding="utf-8") == "scaffold: 10-requirements.md\n"
    # The persistent files stay put.
    assert (project / "docs" / "00-product-brief.md").exists()
    # tasks.yaml is reset to an empty list with the pointer header.
    tasks = (project / ".agentloop" / "tasks.yaml").read_text(encoding="utf-8")
    assert "tasks: []" in tasks and tasks.startswith("#")
    # Gates are back to pending and the close is logged.
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert "release: approved" not in state
    assert "current_phase: brief" in state
    assert "cycle-close (first)" in state
    assert "restore docs/10-requirements.md" in out


def test_main_close_is_idempotent(project: Path) -> None:
    cycle.snapshot_scaffold()
    assert cycle.main(["--name", "first"]) == 0
    # A re-run archives nothing new (fresh scaffolds are today's deliverables-to-be, but a second
    # close on the same day just re-archives the pristine scaffolds — the state stays consistent).
    assert cycle.main(["--name", "second"]) == 0


def test_main_requires_snapshot(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cycle.main(["--name", "first"]) == 1
    assert "scaffold snapshot" in capsys.readouterr().err


def test_main_dry_run_changes_nothing(project: Path) -> None:
    cycle.snapshot_scaffold()
    assert cycle.main(["--name", "first", "--dry-run"]) == 0
    assert (project / "docs" / "10-requirements.md").exists()
    assert not (project / "docs" / "archive").exists()
    assert "approved" in (project / ".agentloop" / "state.md").read_text(encoding="utf-8")


def test_snapshot_scaffold_copies_state(project: Path) -> None:
    assert cycle.snapshot_scaffold() is True
    snap = project / ".agentloop" / "scaffold" / "state.md"
    assert snap.read_text(encoding="utf-8") == _STATE
    # Once taken, a re-run must not overwrite the pristine copy.
    (project / ".agentloop" / "state.md").write_text("FILLED\n", encoding="utf-8")
    assert cycle.snapshot_scaffold() is False
    assert snap.read_text(encoding="utf-8") == _STATE


def test_reset_state_text_with_pristine_refreshes_body(project: Path) -> None:
    out = cycle.reset_state_text(_LIVE_STALE, "demo", "2026-07-03", "docs/archive/2026-07-03-demo", _PRISTINE)
    # Body is restored from the pristine snapshot: checked boxes gone, fresh ones back.
    assert "- [x]" not in out
    assert "- [ ] brief" in out
    # Identity (project/branch) is carried over from the live repo, not the placeholder.
    assert 'project: "demo"' in out and 'branch: "build/demo"' in out
    assert "<enter the product name>" not in out
    # Front-matter reset as usual.
    assert out.count(": pending") == 5
    assert "approved" not in out.split("---")[1]
    assert "current_phase: brief" in out and 'updated_at: "2026-07-03"' in out
    # The accumulated roll-back row is carried, and the close is appended below it.
    assert "| 2026-06-01 | requirements |" in out
    assert "| 2026-07-03 | cycle-close (demo) |" in out


def test_reset_state_text_without_pristine_is_frontmatter_only(project: Path) -> None:
    out = cycle.reset_state_text(_LIVE_STALE, "demo", "2026-07-03", "docs/archive/2026-07-03-demo")
    # Fallback: gates reset and the close logged, but the body is left untouched (stale boxes stay).
    assert out.count(": pending") == 5
    assert "current_phase: brief" in out
    assert "| 2026-07-03 | cycle-close (demo) |" in out
    assert "- [x] brief" in out  # body not refreshed without a snapshot


def test_main_close_refreshes_body_from_snapshot(project: Path) -> None:
    # Snapshot a pristine state, then dirty the live state's body, then close.
    (project / ".agentloop" / "state.md").write_text(_PRISTINE, encoding="utf-8")
    cycle.snapshot_scaffold()
    (project / ".agentloop" / "state.md").write_text(_LIVE_STALE, encoding="utf-8")
    assert cycle.main(["--name", "first"]) == 0
    state = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert "- [x]" not in state and "- [ ] brief" in state
    assert 'project: "demo"' in state
    assert "current_phase: brief" in state and "release: approved" not in state
    assert "cycle-close (first)" in state
