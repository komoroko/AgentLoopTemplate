"""gate_guard.py のゲート判定を検証する。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import gate_guard
import pytest

_STATE_TMPL = """---
project: "demo"
branch: "build/demo"
current_phase: build
gates:
  requirements: {requirements}
  design: {design}
  tasks: {tasks}
  build: {build}
  release: pending
updated_at: "2026-06-26"
---
# board
"""

_CONFIG_ON = "build:\n  max_parallel: 3\ngates:\n  enforce_hook: true\n"
_CONFIG_OFF = "build:\n  max_parallel: 3\ngates:\n  enforce_hook: false\n"


def _setup(
    tmp_path: Path,
    *,
    requirements: str = "pending",
    design: str = "pending",
    tasks: str = "pending",
    build: str = "pending",
    config: str = _CONFIG_ON,
) -> None:
    (tmp_path / ".agentloop").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agentloop" / "state.md").write_text(
        _STATE_TMPL.format(requirements=requirements, design=design, tasks=tasks, build=build),
        encoding="utf-8",
    )
    (tmp_path / ".agentloop" / "config.yaml").write_text(config, encoding="utf-8")


@pytest.fixture
def in_tmp(tmp_path: Path) -> Iterator[Path]:
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_required_gate_mapping(in_tmp: Path) -> None:
    assert gate_guard.required_gate("docs/20-design.md") == "requirements"
    assert gate_guard.required_gate("docs/decisions/ADR-001.md") == "requirements"
    assert gate_guard.required_gate("docs/tasks/T-001.md") == "design"
    assert gate_guard.required_gate("backend/app/main.py") == "tasks"
    assert gate_guard.required_gate("frontend/src/index.ts") == "tasks"
    assert gate_guard.required_gate("scripts/my_product_tool.py") == "tasks"  # プロダクト用スクリプト
    assert gate_guard.required_gate("docs/test/test-plan.md") == "build"
    # ガード対象外
    assert gate_guard.required_gate("scripts/agentloop/dag.py") is None  # 基盤ツールは除外
    assert gate_guard.required_gate("docs/10-requirements.md") is None
    assert gate_guard.required_gate("README.md") is None


def test_blocks_impl_when_tasks_pending(in_tmp: Path) -> None:
    _setup(in_tmp, tasks="pending")
    allowed, reason = gate_guard.evaluate("backend/app/main.py")
    assert allowed is False
    assert "tasks" in reason


def test_allows_impl_when_tasks_approved(in_tmp: Path) -> None:
    _setup(in_tmp, tasks="approved")
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is True


def test_blocks_product_script_when_tasks_pending(in_tmp: Path) -> None:
    _setup(in_tmp, tasks="pending")
    allowed, reason = gate_guard.evaluate("scripts/my_product_tool.py")
    assert allowed is False
    assert "tasks" in reason


def test_allows_agentloop_tooling_even_when_pending(in_tmp: Path) -> None:
    # 基盤ツールはゲートに関わらず常に許可（フック自身の保守を妨げない）。
    _setup(in_tmp, tasks="pending")
    assert gate_guard.evaluate("scripts/agentloop/build_loop.py") == (True, "")


def test_allows_unguarded_path(in_tmp: Path) -> None:
    _setup(in_tmp)
    assert gate_guard.evaluate("scripts/agentloop/gate_guard.py") == (True, "")


def test_enforce_hook_false_allows_everything(in_tmp: Path) -> None:
    _setup(in_tmp, tasks="pending", config=_CONFIG_OFF)
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is True


def test_fail_open_when_no_state(in_tmp: Path) -> None:
    # state.md が無ければ介入しない（fail-open）。
    (in_tmp / ".agentloop").mkdir(parents=True, exist_ok=True)
    (in_tmp / ".agentloop" / "config.yaml").write_text(_CONFIG_ON, encoding="utf-8")
    allowed, _ = gate_guard.evaluate("backend/app/main.py")
    assert allowed is True
