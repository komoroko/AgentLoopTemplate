"""dag.py の決定的導出を検証する。"""

from __future__ import annotations

import dag
import pytest


def _graph() -> dag.Graph:
    return dag.Graph.from_tasks(
        [
            dag.Task(id="T-001", title="基盤", kind="foundation", blocked_by=()),
            dag.Task(id="T-002", title="葉A", kind="parallel", blocked_by=("T-001",)),
            dag.Task(id="T-003", title="葉B", kind="parallel", blocked_by=("T-001",)),
            dag.Task(id="T-004", title="統合", kind="integration", blocked_by=("T-002", "T-003")),
        ]
    )


def _done(graph: dag.Graph, *done_ids: str) -> dag.Graph:
    tasks = [
        dag.Task(t.id, t.title, t.kind, t.blocked_by, "done" if t.id in done_ids else t.status, t.test)
        for t in graph.tasks
    ]
    return dag.Graph.from_tasks(tasks)


def test_fan_out() -> None:
    assert _graph().fan_out() == {"T-001": 2, "T-002": 1, "T-003": 1, "T-004": 0}


def test_frontier_only_roots_when_nothing_done() -> None:
    assert [t.id for t in _graph().frontier()] == ["T-001"]


def test_frontier_opens_after_foundation_done() -> None:
    assert [t.id for t in _done(_graph(), "T-001").frontier()] == ["T-002", "T-003"]


def test_layers() -> None:
    assert _graph().layers() == [["T-001"], ["T-002", "T-003"], ["T-004"]]


def test_critical_path_is_deterministic() -> None:
    assert _graph().critical_path() == ["T-001", "T-002", "T-004"]


def test_order_frontier_prefers_foundation() -> None:
    assert [t.id for t in _graph().order_frontier()] == ["T-001"]


def test_order_frontier_prefers_critical_then_id() -> None:
    # 基盤 done 後、両葉とも fan-out=1。クリティカルパス上の T-002 が先。
    assert [t.id for t in _done(_graph(), "T-001").order_frontier()] == ["T-002", "T-003"]


def test_counts() -> None:
    counts = _done(_graph(), "T-001").counts()
    assert counts["done"] == 1
    assert counts["todo"] == 3


def test_cycle_detected() -> None:
    with pytest.raises(dag.DagError):
        dag.Graph.from_tasks(
            [
                dag.Task(id="A", title="a", kind="parallel", blocked_by=("B",)),
                dag.Task(id="B", title="b", kind="parallel", blocked_by=("A",)),
            ]
        )


def test_unknown_dependency_detected() -> None:
    with pytest.raises(dag.DagError):
        dag.Graph.from_tasks([dag.Task(id="A", title="a", kind="parallel", blocked_by=("X",))])


def test_duplicate_id_detected() -> None:
    with pytest.raises(dag.DagError):
        dag.Graph.from_tasks(
            [
                dag.Task(id="A", title="a", kind="parallel"),
                dag.Task(id="A", title="a2", kind="parallel"),
            ]
        )


def test_load_from_yaml(tmp_path: object) -> None:
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text(
        "tasks:\n"
        "  - id: T-001\n    title: 基盤\n    kind: foundation\n    blockedBy: []\n    status: done\n"
        "  - id: T-002\n    title: 葉\n    kind: parallel\n    blockedBy: [T-001]\n    status: todo\n",
        encoding="utf-8",
    )
    graph = dag.load(str(p))
    assert [t.id for t in graph.frontier()] == ["T-002"]


def test_load_rejects_non_mapping_task(tmp_path: object) -> None:
    # task 要素がスカラ（マッピングでない）場合は AttributeError ではなく DagError を投げる。
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text("tasks:\n  - T-001\n", encoding="utf-8")
    with pytest.raises(dag.DagError):
        dag.load(str(p))
