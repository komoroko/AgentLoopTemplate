"""Verify dag.py's deterministic derivation."""

from __future__ import annotations

from pathlib import Path

import dag
import pytest


def _graph() -> dag.Graph:
    return dag.Graph.from_tasks(
        [
            dag.Task(id="T-001", title="base", kind="foundation", blocked_by=()),
            dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",)),
            dag.Task(id="T-003", title="leaf B", kind="parallel", blocked_by=("T-001",)),
            dag.Task(id="T-004", title="integration", kind="integration", blocked_by=("T-002", "T-003")),
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


def test_dependents_closure() -> None:
    g = _graph()  # T-001 →(T-002,T-003)→ T-004
    assert g.dependents_closure(["T-001"]) == {"T-002", "T-003", "T-004"}  # transitive; excludes self
    assert g.dependents_closure(["T-002"]) == {"T-004"}
    assert g.dependents_closure(["T-004"]) == set()  # a leaf has no dependents
    assert g.dependents_closure(["T-999"]) == set()  # unknown seed is ignored
    assert g.dependents_closure(["T-002", "T-003"]) == {"T-004"}  # duplicates counted once
    # A mutually-dependent seed (T-002 is downstream of T-001) is also excluded = seed and ripple are disjoint.
    assert g.dependents_closure(["T-001", "T-002"]) == {"T-003", "T-004"}


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
    # After the foundation is done, both leaves have fan-out=1. T-002 on the critical path comes first.
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
        "  - id: T-001\n    title: base\n    kind: foundation\n    blockedBy: []\n    status: done\n"
        "  - id: T-002\n    title: leaf\n    kind: parallel\n    blockedBy: [T-001]\n    status: todo\n",
        encoding="utf-8",
    )
    graph = dag.load(str(p))
    assert [t.id for t in graph.frontier()] == ["T-002"]


def test_load_rejects_non_mapping_task(tmp_path: object) -> None:
    # When a task element is a scalar (not a mapping), raise DagError rather than AttributeError.
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text("tasks:\n  - T-001\n", encoding="utf-8")
    with pytest.raises(dag.DagError):
        dag.load(str(p))


def test_load_reads_req_and_phase(tmp_path: object) -> None:
    # req/phase are optional metadata. Unspecified defaults to ("" / build).
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text(
        "tasks:\n"
        "  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n"
        "    status: todo\n    req: R-1\n    phase: verify\n"
        "  - id: T-002\n    title: y\n    kind: parallel\n"
        "    blockedBy: [T-001]\n    status: todo\n",
        encoding="utf-8",
    )
    g = dag.load(str(p))
    assert (g.get("T-001").req, g.get("T-001").phase) == ("R-1", "verify")
    assert (g.get("T-002").req, g.get("T-002").phase) == ("", "build")


def test_load_req_phase_empty_value_defaults(tmp_path: object) -> None:
    # Even if the key has an empty value (YAML null), do not make it the 'None' string; use the defaults ("" / build).
    p = tmp_path / "tasks.yaml"  # type: ignore[operator]
    p.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n"
        "    status: todo\n    req:\n    phase:\n",
        encoding="utf-8",
    )
    t = dag.load(str(p)).get("T-001")
    assert (t.req, t.phase) == ("", "build")


def test_mermaid_structure() -> None:
    out = dag.mermaid(_done(_graph(), "T-001"))
    lines = out.splitlines()
    assert lines[0] == "```mermaid"
    assert lines[1] == "graph TD"
    assert out.endswith("```")
    # Nodes are sanitized `-`→`_`; the label is the original id: title.
    assert '  T_001["T-001: base"]' in lines
    assert '  T_004["T-004: integration"]' in lines
    # An edge per blockedBy (dependency → dependent).
    assert "  T_001 --> T_002" in lines
    assert "  T_002 --> T_004" in lines
    assert "  T_003 --> T_004" in lines
    # status color-coding (done and todo assigned to different classes).
    assert "  classDef done fill:#d7f5dd,stroke:#22a04b,color:#0b3d1d;" in lines
    assert "  class T_001 done;" in lines
    # Critical-path emphasis (T-001→T-002→T-004, etc. in the critical class).
    assert any(line.startswith("  class ") and line.endswith(" critical;") for line in lines)


def test_mermaid_is_deterministic() -> None:
    g = _done(_graph(), "T-001")
    assert dag.mermaid(g) == dag.mermaid(g)


def test_mermaid_empty_graph() -> None:
    out = dag.mermaid(dag.Graph.from_tasks([]))
    assert "graph TD" in out
    assert 'empty["(no tasks)"]' in out
    assert out.strip().endswith("```")


# ---- consistency trace (requirements → design → tasks) -------------------------


def test_parse_requirement_ids_from_headings() -> None:
    text = (
        "# Requirements\n\n## List\n\n### R-1: login\nbody\n### R-2: search\n"
        "<!-- R-3 is in a comment so it is not picked up -->\nR-9 in body text is not picked up\n"
    )
    # Headings only; order of appearance; deduplicated. R-x in comments/body is not picked up.
    assert dag.parse_requirement_ids(text) == ["R-1", "R-2"]


def test_parse_requirement_ids_dedupes() -> None:
    assert dag.parse_requirement_ids("### R-1 → design\n### R-1 note\n### R-2 → design\n") == ["R-1", "R-2"]


def test_parse_requirement_ids_multiple_per_heading() -> None:
    # When one heading bundles multiple requirements (a shared design section), all IDs are picked up.
    assert dag.parse_requirement_ids("### R-1, R-2 → shared design\n") == ["R-1", "R-2"]


def test_parse_requirement_ids_any_heading_level() -> None:
    # Not tied to heading depth (number of #); picked from any of H1–H6.
    assert dag.parse_requirement_ids("# R-1\n###### R-2\n") == ["R-1", "R-2"]


def test_parse_requirement_ids_ignores_code_fences() -> None:
    # Example headings inside code fences are not mistaken for real IDs.
    text = "### R-1: real\n\n```\n### R-99 → design (document example)\n```\n"
    assert dag.parse_requirement_ids(text) == ["R-1"]


def test_parse_requirement_ids_matches_before_cjk() -> None:
    # Still matches when CJK follows with no separator (does not rely on a trailing \b). Does not mistake R-12 for R-1.
    # (A Japanese-language requirements document writes headings like this, so this capability must hold.)
    assert dag.parse_requirement_ids("### R-1ログイン\n### R-12 検索\n") == ["R-1", "R-12"]


def test_task_req_ids_splits_field() -> None:
    # Accepts either comma or whitespace separators and dedupes.
    assert dag.task_req_ids(dag.Task("T-1", "x", "parallel", req="R-1, R-3")) == ["R-1", "R-3"]
    assert dag.task_req_ids(dag.Task("T-2", "y", "parallel", req="R-1 R-3")) == ["R-1", "R-3"]
    assert dag.task_req_ids(dag.Task("T-3", "z", "parallel", req="R-1, R-1")) == ["R-1"]
    assert dag.task_req_ids(dag.Task("T-4", "w", "parallel", req="")) == []


def test_from_tasks_rejects_malformed_req() -> None:
    # The req token's R-<number> form is validated at load time (typos are not missed).
    for bad in ("R1", "Req-1", "R-", "R-1x"):
        with pytest.raises(dag.DagError, match="req"):
            dag.Graph.from_tasks([dag.Task("T-001", "x", "foundation", req=bad)])


def _trace_graph() -> dag.Graph:
    return dag.Graph.from_tasks(
        [
            dag.Task("T-001", "base", "foundation", req="R-1"),
            dag.Task("T-002", "leaf", "parallel", blocked_by=("T-001",), req="R-2"),
        ]
    )


def test_trace_all_connected() -> None:
    report = dag.trace(_trace_graph(), ["R-1", "R-2"], ["R-1", "R-2"])
    assert report.ok
    assert report.req_to_tasks == {"R-1": ["T-001"], "R-2": ["T-002"]}
    assert report.uncovered_requirements == ()


def test_trace_detects_uncovered_requirement() -> None:
    # R-3 is covered by no task (requirements → tasks is broken).
    report = dag.trace(_trace_graph(), ["R-1", "R-2", "R-3"], ["R-1", "R-2", "R-3"])
    assert not report.ok
    assert report.uncovered_requirements == ("R-3",)


def test_trace_detects_requirement_missing_design() -> None:
    # R-2 is not in the design (requirements → design is broken).
    report = dag.trace(_trace_graph(), ["R-1", "R-2"], ["R-1"])
    assert not report.ok
    assert report.requirements_missing_design == ("R-2",)


def test_trace_detects_unknown_refs() -> None:
    g = dag.Graph.from_tasks([dag.Task("T-001", "x", "foundation", req="R-9")])
    report = dag.trace(g, ["R-1"], ["R-1", "R-7"])
    assert not report.ok
    assert report.unknown_in_tasks == (("T-001", "R-9"),)
    assert report.unknown_in_design == ("R-7",)


def test_trace_warns_build_task_without_req_but_stays_ok() -> None:
    # A build task with no req is a WARN (does not break ok). The verify phase is excluded.
    g = dag.Graph.from_tasks(
        [
            dag.Task("T-001", "x", "foundation", req="R-1"),
            dag.Task("T-002", "build no req", "parallel", blocked_by=("T-001",)),
            dag.Task("T-003", "verify no req", "parallel", blocked_by=("T-001",), phase="verify"),
        ]
    )
    report = dag.trace(g, ["R-1"], ["R-1"])
    assert report.ok  # ok if only a WARN
    assert report.tasks_without_req == ("T-002",)


def test_trace_skips_design_dimension_when_absent() -> None:
    report = dag.trace(_trace_graph(), ["R-1", "R-2"], None)
    assert report.design_checked is False
    assert report.requirements_missing_design == ()
    assert report.ok


def test_trace_verify_task_does_not_cover_requirement() -> None:
    # A requirement whose only req-bearing task is a verify-phase task is "not in the implementation plan" = uncovered.
    g = dag.Graph.from_tasks(
        [
            dag.Task("T-001", "build", "foundation", req="R-1"),
            dag.Task("T-002", "verify only", "parallel", blocked_by=("T-001",), req="R-2", phase="verify"),
        ]
    )
    report = dag.trace(g, ["R-1", "R-2"], ["R-1", "R-2"])
    assert not report.ok
    assert report.uncovered_requirements == ("R-2",)  # a verify task does not count toward coverage
    assert report.req_to_tasks == {"R-1": ["T-001"], "R-2": []}


def test_trace_unknown_ref_flagged_regardless_of_phase() -> None:
    # A dangling reference (an R not in the requirements) is an ERROR regardless of phase.
    g = dag.Graph.from_tasks([dag.Task("T-001", "verify", "foundation", req="R-9", phase="verify")])
    report = dag.trace(g, ["R-1"], ["R-1"])
    assert report.unknown_in_tasks == (("T-001", "R-9"),)


def test_trace_cli_returns_nonzero_on_gap(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n    req: R-1\n",
        encoding="utf-8",
    )
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n### R-2: b\n", encoding="utf-8")  # R-2 is uncovered
    design = tmp_path / "design.md"
    design.write_text("### R-1 → design\n### R-2 → design\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs), "--design", str(design)])
    assert rc == 1


def test_trace_cli_ok_when_connected(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n    req: R-1\n",
        encoding="utf-8",
    )
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    design = tmp_path / "design.md"
    design.write_text("### R-1 → design\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs), "--design", str(design)])
    assert rc == 0


def _trace_tasks_file(tmp_path: Path) -> Path:
    tasks = tmp_path / "tasks.yaml"
    tasks.write_text(
        "tasks:\n  - id: T-001\n    title: x\n    kind: foundation\n    blockedBy: []\n    req: R-1\n",
        encoding="utf-8",
    )
    return tasks


def test_trace_cli_exit2_when_requirements_missing(tmp_path: Path) -> None:
    # An absent requirements document is "cannot check" = exit 2 (distinct from missing's 1).
    tasks = _trace_tasks_file(tmp_path)
    rc = dag.main([str(tasks), "--trace", "--requirements", str(tmp_path / "missing.md")])
    assert rc == 2


def test_trace_cli_exit2_when_no_requirement_ids(tmp_path: Path) -> None:
    # The requirements file exists but no requirement ID can be extracted = cannot check = exit 2.
    tasks = _trace_tasks_file(tmp_path)
    reqs = tmp_path / "req.md"
    reqs.write_text("# Requirements\nbody only, no R heading\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs)])
    assert rc == 2


def test_trace_cli_design_absent_skips_with_exit0(tmp_path: Path) -> None:
    # An absent design is skipped by default (exit 0 if requirement coverage is OK).
    tasks = _trace_tasks_file(tmp_path)
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    rc = dag.main([str(tasks), "--trace", "--requirements", str(reqs), "--design", str(tmp_path / "none.md")])
    assert rc == 0


def test_trace_cli_require_design_exit2_when_design_missing(tmp_path: Path) -> None:
    # With --require-design, an absent design is not allowed and exits 2 (for the gate of the design-approved phase).
    tasks = _trace_tasks_file(tmp_path)
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    rc = dag.main(
        [str(tasks), "--trace", "--requirements", str(reqs), "--design", str(tmp_path / "none.md"), "--require-design"]
    )
    assert rc == 2


def test_trace_cli_exit2_when_tasks_yaml_missing(tmp_path: Path) -> None:
    # tasks.yaml unreadable = trace not established = exit 2 (distinct from render's generic 1).
    reqs = tmp_path / "req.md"
    reqs.write_text("### R-1: a\n", encoding="utf-8")
    rc = dag.main([str(tmp_path / "none.yaml"), "--trace", "--requirements", str(reqs)])
    assert rc == 2
