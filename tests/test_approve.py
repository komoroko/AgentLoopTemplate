"""Verify approve.py: the single sanctioned pending→approved write path (gate rule 2's operation)."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentloop import approve, events

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: requirements
gates:
  requirements: pending       # c1
  design: pending             # c2
  tasks: pending
  build: pending
  release: pending
updated_at: "2026-07-01"
---
# board

Body example that must never be rewritten: `tasks: pending`.
"""


# --- apply_approval: the pure rewrite -------------------------------------------


def test_apply_rewrites_gate_line_and_advances_phase() -> None:
    out = approve.apply_approval(_STATE, "requirements", "2026-07-12", "alice")
    assert re.search(r"requirements: approved\s+# 2026-07-12 alice", out)
    assert re.search(r"design: pending\s+# c2", out)  # downstream untouched
    assert "current_phase: design" in out
    assert 'updated_at: "2026-07-12"' in out
    assert "Body example that must never be rewritten: `tasks: pending`." in out  # body untouched


def test_apply_without_approver_stamps_date_only() -> None:
    out = approve.apply_approval(_STATE, "requirements", "2026-07-12")
    assert re.search(r"requirements: approved\s+# 2026-07-12$", out, re.MULTILINE)


def test_apply_enforces_chain_order() -> None:
    with pytest.raises(approve.ApproveError) as exc:
        approve.apply_approval(_STATE, "design", "2026-07-12")  # requirements still pending
    assert exc.value.status == 409
    approved = approve.apply_approval(_STATE, "requirements", "2026-07-12")
    out = approve.apply_approval(approved, "design", "2026-07-12")  # now legal
    assert re.search(r"design: approved\s+# 2026-07-12", out)
    assert "current_phase: tasks" in out


def test_apply_rejects_already_approved_and_unknown() -> None:
    approved = approve.apply_approval(_STATE, "requirements", "2026-07-12")
    with pytest.raises(approve.AlreadyApproved) as exc:
        approve.apply_approval(approved, "requirements", "2026-07-12")
    assert exc.value.status == 409
    with pytest.raises(approve.ApproveError) as exc2:
        approve.apply_approval(_STATE, "verify", "2026-07-12")  # a phase name, not a gate
    assert exc2.value.status == 400


def test_apply_requires_frontmatter() -> None:
    with pytest.raises(approve.ApproveError) as exc:
        approve.apply_approval("# no front-matter here", "requirements", "2026-07-12")
    assert exc.value.status == 500


def test_release_approval_reaches_done() -> None:
    text = _STATE
    for gate in ("requirements", "design", "tasks", "build", "release"):
        text = approve.apply_approval(text, gate, "2026-07-12")
    assert "current_phase: done" in text


# --- record_approval / CLI: the write path + the event record --------------------


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".agentloop" / "state.md").write_text(_STATE, encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_record_approval_writes_state_and_event(repo: Path) -> None:
    approve.record_approval("requirements", "alice")
    state = (repo / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert re.search(r"requirements: approved\s+# \d{4}-\d{2}-\d{2} alice", state)
    assert "current_phase: design" in state
    recorded = events.load_events(str(repo / ".agentloop" / "events.ndjson"))
    assert [(e.event, e.gate, e.detail) for e in recorded] == [("gate_approved", "requirements", "alice")]


def test_main_success_and_chain_refusal(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert approve.main(["requirements", "--by", "alice"]) == 0
    assert "gate 'requirements' approved" in capsys.readouterr().out
    assert approve.main(["tasks"]) == 1  # design still pending — chain refusal
    assert "upstream gate 'design' is still pending" in capsys.readouterr().err


def test_main_already_approved_is_a_noop(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert approve.main(["requirements"]) == 0
    before = (repo / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert approve.main(["requirements"]) == 0  # second run: no-op, still exit 0
    assert "already approved" in capsys.readouterr().out
    assert (repo / ".agentloop" / "state.md").read_text(encoding="utf-8") == before
    # no second gate_approved event was appended
    recorded = events.load_events(str(repo / ".agentloop" / "events.ndjson"))
    assert sum(1 for e in recorded if e.event == "gate_approved") == 1


def test_main_missing_state_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".agentloop" / "state.md").unlink()
    assert approve.main(["requirements"]) == 1
    assert "cannot read" in capsys.readouterr().err
