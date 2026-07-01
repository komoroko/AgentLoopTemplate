"""Verify revise.py's gate chaining and surgical state.md update (deterministic, offline)."""

from __future__ import annotations

import re

import pytest
import revise

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
