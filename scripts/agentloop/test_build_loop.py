"""Verify build_loop.py's scheduling and dry-run control flow."""

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
            dag.Task(id="T-001", title="base", kind="foundation", blocked_by=(), status=st("T-001")),
            dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",), status=st("T-002")),
            dag.Task(id="T-003", title="leaf B", kind="parallel", blocked_by=("T-001",), status=st("T-003")),
            dag.Task(id="T-004", title="leaf C", kind="parallel", blocked_by=("T-001",), status=st("T-004")),
            dag.Task(id="T-005", title="leaf D", kind="parallel", blocked_by=("T-001",), status=st("T-005")),
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
    assert [t.id for t in tasks] == ["T-002", "T-003", "T-004"]  # with max_parallel=3, T-005 is next iteration


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
  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: todo, test: make test}
  - {id: T-002, title: leaf A, kind: parallel, blockedBy: [T-001], status: todo, test: make test}
  - {id: T-003, title: leaf B, kind: parallel, blockedBy: [T-001], status: todo, test: make test}
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


def test_recovers_stale_in_progress(project: Path) -> None:
    # A task left in in_progress from a previous interruption is reset to todo at startup and re-consumed.
    # Without recovery it falls out of the frontier (todo-only) and is never started, deadlocking.
    stale = _TASKS.replace(
        "{id: T-002, title: leaf A, kind: parallel, blockedBy: [T-001], status: todo, test: make test}",
        "{id: T-002, title: leaf A, kind: parallel, blockedBy: [T-001], status: in_progress, test: make test}",
    )
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(stale, encoding="utf-8")
    rc = build_loop.main(["--dry-run"])
    assert rc == 0
    graph = dag.load(".agentloop/tasks.yaml")
    assert graph.counts()["done"] == 3  # the previously in_progress T-002 also reaches done


# --- non-dry-run failure handling (git monkeypatched to simulate) -----------------


def _provision(project: Path) -> None:
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")


def _leaf(task_id: str, title: str) -> dag.Task:
    return dag.Task(id=task_id, title=title, kind="parallel", blocked_by=("T-001",))


def test_merge_leaf_conflict_aborts_and_returns_false(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # If the merge into work conflicts, run merge --abort and return False (do not mark done).
    _provision(project)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str) -> tuple[int, str]:
        calls.append(cmd)
        if cmd[:2] == ["git", "merge"] and "--abort" not in cmd:
            return 1, "CONFLICT (content): Merge conflict in foo.py"
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    assert orch.merge_leaf(_leaf("T-002", "leaf A"), "build/demo/T-002") is False
    assert ["git", "merge", "--abort"] in calls  # a conflict is rolled back with abort


def test_add_worktree_failure_raises_stoploop(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # If worktree add fails, raise StopLoop via _git, stopping the loop and raising it to the human.
    _provision(project)

    def fake_run(cmd: list[str], cwd: str) -> tuple[int, str]:
        if cmd[:3] == ["git", "worktree", "add"]:
            return 128, "fatal: '.worktrees/T-002' already exists"
        return 0, ""  # pre-cleanup (remove/branch -D/prune) is a no-op ignoring rc

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    with pytest.raises(build_loop.StopLoop):
        orch._add_worktree(_leaf("T-002", "leaf A"))


def test_consume_parallel_partial_failure_blocks_only_failed(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Even if one leaf in a parallel batch cannot pass the quality gate, the successful leaves are merged to done
    # and only the failed leaf becomes blocked (one leaf's failure does not leave other leaves stuck in in_progress).
    _provision(project)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd: (0, ""))  # treat all git calls as success

    def fake_run_task(task: dag.Task, cwd: str) -> tuple[bool, str]:
        return (task.id != "T-003"), ("" if task.id != "T-003" else "$ make test (rc=1)")

    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", fake_run_task)
    with pytest.raises(build_loop.StopLoop):
        orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])

    by_id = {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}
    assert by_id["T-002"] == "done"
    assert by_id["T-003"] == "blocked"
