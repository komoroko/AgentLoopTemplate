"""build_loop.py のスケジューリングと dry-run 制御フローを検証する。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import build_loop
import dag
import pytest


def _graph(done: tuple[str, ...] = ()) -> dag.Graph:
    def st(tid: str) -> str:
        return "done" if tid in done else "todo"

    return dag.Graph.from_tasks(
        [
            dag.Task(id="T-001", title="基盤", kind="foundation", blocked_by=(), status=st("T-001")),
            dag.Task(id="T-002", title="葉A", kind="parallel", blocked_by=("T-001",), status=st("T-002")),
            dag.Task(id="T-003", title="葉B", kind="parallel", blocked_by=("T-001",), status=st("T-003")),
            dag.Task(id="T-004", title="葉C", kind="parallel", blocked_by=("T-001",), status=st("T-004")),
            dag.Task(id="T-005", title="葉D", kind="parallel", blocked_by=("T-001",), status=st("T-005")),
        ]
    )


def test_plan_batch_foundation_first_serial() -> None:
    batch = build_loop.plan_batch(_graph(), max_parallel=3)
    assert batch is not None
    mode, tasks = batch
    assert mode == "serial"
    assert [t.id for t in tasks] == ["T-001"]


def test_plan_batch_parallel_capped_at_max() -> None:
    batch = build_loop.plan_batch(_graph(done=("T-001",)), max_parallel=3)
    assert batch is not None
    mode, tasks = batch
    assert mode == "parallel"
    assert [t.id for t in tasks] == ["T-002", "T-003", "T-004"]  # max_parallel=3 で T-005 は次周


def test_plan_batch_none_when_no_frontier() -> None:
    full = _graph(done=("T-001", "T-002", "T-003", "T-004", "T-005"))
    assert build_loop.plan_batch(full, max_parallel=3) is None


_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: build
gates:
  requirements: approved
  design: approved
  tasks: {tasks}
  build: pending
  release: pending
updated_at: "2026-06-26"
---
# board
"""

_CONFIG = (
    "build:\n"
    "  max_parallel: 3\n"
    "  worktree: {enabled: true, dir: .worktrees, branch_pattern: '{branch}/{task_id}'}\n"
    "  retries: {test_fix: 2, check_fix: 2}\n"
    "  quality_gate: {test_cmd: 'make test', check_cmd: 'make check'}\n"
    "  merge: {strategy: sequential}\n"
    "gates:\n  enforce_hook: true\n"
)

_TASKS = """tasks:
  - {id: T-001, title: 基盤, kind: foundation, blockedBy: [], status: todo, test: make test}
  - {id: T-002, title: 葉A, kind: parallel, blockedBy: [T-001], status: todo, test: make test}
  - {id: T-003, title: 葉B, kind: parallel, blockedBy: [T-001], status: todo, test: make test}
"""


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".agentloop" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_dry_run_blocks_when_tasks_not_approved(project: Path) -> None:
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="pending"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    assert build_loop.main(["--dry-run"]) == 2


def test_dry_run_completes_all_tasks(project: Path) -> None:
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    rc = build_loop.main(["--dry-run"])
    assert rc == 0
    graph = dag.load(".agentloop/tasks.yaml")
    assert graph.counts()["done"] == 3
