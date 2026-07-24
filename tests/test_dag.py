"""Tests for dag.py / dag_render.py / dag_trace.py — scheduling and the traceability thread.

The scheduling assertions are all about **determinism**: the same plan must always produce
the same layers, the same critical path, and the same consumption order. That is what lets a
human predict `/build` instead of interviewing it, so a test here failing means the loop
became something you have to watch rather than something you can reason about.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import dag, dag_render, dag_trace, models
from agentloop import repo as repo_mod
from tests._support import make_claim, make_obligation, make_plan, make_state, make_task, seed_repo


def graph_of(*specs: tuple[str, str, list[str]]) -> dag.Graph:
    """A graph from (id, kind, blocked_by) triples — every task todo."""
    return dag.Graph.from_tasks(
        [dag.Task(id=tid, title=tid, kind=kind, blocked_by=tuple(blocked)) for tid, kind, blocked in specs]
    )


# --- validation ---------------------------------------------------------------


def test_duplicate_id_is_refused() -> None:
    with pytest.raises(dag.DagError, match="duplicate task ID"):
        graph_of(("T-001", "foundation", []), ("T-001", "parallel", []))


def test_unknown_dependency_is_refused() -> None:
    with pytest.raises(dag.DagError, match="unknown dependency 'T-999'"):
        graph_of(("T-001", "parallel", ["T-999"]))


def test_self_dependency_is_refused() -> None:
    with pytest.raises(dag.DagError, match="depends on itself"):
        graph_of(("T-001", "parallel", ["T-001"]))


def test_a_cycle_is_refused() -> None:
    with pytest.raises(dag.DagError, match="cycle"):
        graph_of(("T-001", "parallel", ["T-002"]), ("T-002", "parallel", ["T-001"]))


@pytest.mark.parametrize(
    ("field", "bad"),
    [("kind", "whatever"), ("status", "nearly-done"), ("risk", "spicy")],
)
def test_off_vocabulary_values_are_refused(field: str, bad: str) -> None:
    fields: dict[str, str] = {"kind": "parallel", "status": "todo", "risk": "low", field: bad}
    task = dag.Task(id="T-001", title="t", **fields)  # type: ignore[arg-type]
    with pytest.raises(dag.DagError, match=f"invalid {field}"):
        dag.Graph.from_tasks([task])


# --- derivation ---------------------------------------------------------------


def test_layers_and_critical_path_are_deterministic() -> None:
    graph = graph_of(
        ("T-001", "foundation", []),
        ("T-002", "parallel", ["T-001"]),
        ("T-003", "parallel", ["T-001"]),
        ("T-004", "integration", ["T-002", "T-003"]),
    )
    assert graph.layers() == [["T-001"], ["T-002", "T-003"], ["T-004"]]
    assert graph.critical_path() == ["T-001", "T-002", "T-004"]
    # Same graph, tasks declared in a different order: the schedule must not move.
    shuffled = graph_of(
        ("T-004", "integration", ["T-002", "T-003"]),
        ("T-003", "parallel", ["T-001"]),
        ("T-002", "parallel", ["T-001"]),
        ("T-001", "foundation", []),
    )
    assert shuffled.layers() == graph.layers()
    assert shuffled.critical_path() == graph.critical_path()


def test_fan_out_counts_direct_dependents() -> None:
    graph = graph_of(
        ("T-001", "foundation", []),
        ("T-002", "parallel", ["T-001"]),
        ("T-003", "parallel", ["T-001"]),
    )
    assert graph.fan_out() == {"T-001": 2, "T-002": 0, "T-003": 0}


def test_frontier_holds_only_startable_todo_tasks() -> None:
    tasks = [
        dag.Task(id="T-001", title="a", kind="foundation", status="done"),
        dag.Task(id="T-002", title="b", kind="parallel", blocked_by=("T-001",)),
        dag.Task(id="T-003", title="c", kind="parallel", blocked_by=("T-002",)),
        dag.Task(id="T-004", title="d", kind="parallel", status="blocked"),
    ]
    assert [t.id for t in dag.Graph.from_tasks(tasks).frontier()] == ["T-002"]


def test_order_frontier_puts_foundation_first_then_fan_out() -> None:
    tasks = [
        dag.Task(id="T-001", title="lonely", kind="parallel"),
        dag.Task(id="T-002", title="hub", kind="parallel"),
        dag.Task(id="T-003", title="base", kind="foundation"),
        dag.Task(id="T-004", title="child", kind="parallel", blocked_by=("T-002",)),
        dag.Task(id="T-005", title="child2", kind="parallel", blocked_by=("T-002",)),
    ]
    ordered = [t.id for t in dag.Graph.from_tasks(tasks).order_frontier()]
    assert ordered[0] == "T-003"  # foundation is finalized before anything forks off it
    assert ordered[1] == "T-002"  # then the highest fan-out


def test_dependents_closure_excludes_the_seeds() -> None:
    graph = graph_of(
        ("T-001", "foundation", []),
        ("T-002", "parallel", ["T-001"]),
        ("T-003", "parallel", ["T-002"]),
        ("T-004", "parallel", []),
    )
    assert graph.dependents_closure(["T-001"]) == {"T-002", "T-003"}
    assert graph.dependents_closure(["T-999"]) == set()


def test_counts_cover_every_status_in_vocabulary_order() -> None:
    graph = graph_of(("T-001", "foundation", []))
    assert list(graph.counts()) == list(models.TASK_STATUS_ORDER)


# --- joining plan structure with state status ---------------------------------


def test_join_takes_structure_from_the_plan_and_status_from_the_state() -> None:
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001")],
            tasks=[make_task("T-001", claim_ids=["C-001"]), make_task("T-002", kind="parallel", blocked_by=["T-001"])],
        )
    )
    state = models.State(make_state(tasks={"T-001": "done"}))
    graph = dag.join(plan, state)
    assert graph.get("T-001").status == "done"
    assert graph.get("T-002").status == "todo"  # absent from state = not started
    assert graph.get("T-001").claim_ids == ("C-001",)


def test_status_for_a_task_the_plan_does_not_declare_is_an_error() -> None:
    """The plan was rewound and the state did not follow. Scheduling against that mismatch
    would run work nobody approved."""
    plan = models.Plan(make_plan(tasks=[make_task("T-001", claim_ids=["C-001"])]))
    state = models.State(make_state(tasks={"T-001": "done", "T-777": "done"}))
    with pytest.raises(dag.DagError, match="T-777"):
        dag.join(plan, state)


def test_join_without_a_state_treats_everything_as_todo() -> None:
    plan = models.Plan(make_plan(tasks=[make_task("T-001", claim_ids=["C-001"])]))
    assert dag.join(plan, None).get("T-001").status == "todo"


def test_claims_without_a_task_are_reported() -> None:
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001"), make_claim("C-002", obligation_ids=["EO-001"])],
            obligations=[make_obligation("EO-001", subject_ids=["C-001", "C-002"])],
            tasks=[make_task("T-001", claim_ids=["C-001"])],
        )
    )
    assert dag.join(plan, None).claims_without_a_task(plan) == ["C-002"]


def test_load_reads_the_repo(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert [t.id for t in dag.load(repo_mod.Repo(tmp_path)).tasks] == ["T-001"]


def test_load_without_a_plan_says_so(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=None)
    with pytest.raises(dag.DagError, match="no plan"):
        dag.load(repo_mod.Repo(tmp_path))


# --- rendering ----------------------------------------------------------------


def test_render_shows_claims_and_risk_not_a_free_text_req() -> None:
    graph = dag.Graph.from_tasks(
        [
            dag.Task(
                id="T-001",
                title="base",
                kind="foundation",
                risk="critical",
                claim_ids=("C-002",),
                oracle_ids=("O-002",),
            )
        ]
    )
    rendered = dag_render.render(graph)
    assert "C-002" in rendered and "O-002" in rendered and "critical" in rendered
    assert "req" not in rendered  # 0.8.x's free-text requirement column is gone


def test_mermaid_is_deterministic_and_fenced() -> None:
    graph = graph_of(("T-001", "foundation", []), ("T-002", "parallel", ["T-001"]))
    first = dag_render.mermaid(graph)
    assert first.startswith("```mermaid") and first.rstrip().endswith("```")
    assert "T_001 --> T_002" in first
    assert dag_render.mermaid(graph) == first


def test_mermaid_of_an_empty_graph_says_so() -> None:
    assert "(no tasks)" in dag_render.mermaid(dag.Graph.from_tasks([]))


# --- the traceability thread --------------------------------------------------


def _plan_with(**kwargs: object) -> models.Plan:
    return models.Plan(make_plan(**kwargs))  # type: ignore[arg-type]


def test_a_whole_thread_reports_no_errors() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001", requirement_ids=["R-1"])],
        tasks=[make_task("T-001", claim_ids=["C-001"])],
    )
    report = dag_trace.trace(plan, dag.join(plan, None))
    assert report.ok
    assert report.errors == [] and report.warnings == []
    assert report.requirements == ["R-1"]


def test_an_ungrounded_high_risk_claim_blocks() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001", risk="high", epistemic_status="unknown", oracle_ids=None)],
        tasks=[make_task("T-001", claim_ids=["C-001"])],
    )
    errors = dag_trace.trace(plan).errors
    assert any("may not stay ungrounded" in e for e in errors)


def test_a_low_risk_unknown_is_not_an_error() -> None:
    # An honest `unknown` at low risk is allowed to exist; forcing it to be grounded is what
    # makes people write prose instead.
    plan = _plan_with(claims=[make_claim("C-001", risk="low", epistemic_status="unknown")])
    assert dag_trace.trace(plan).ok


def test_a_high_risk_claim_without_an_oracle_blocks() -> None:
    plan = _plan_with(claims=[make_claim("C-001", risk="high")])
    assert any("needs a judgement boundary" in e for e in dag_trace.trace(plan).errors)


def test_an_unsatisfied_obligation_blocks() -> None:
    plan = _plan_with(obligations=[make_obligation("EO-001", satisfied=False)])
    assert any("obligation unsatisfied" in e for e in dag_trace.trace(plan).errors)


def test_a_task_answering_for_no_claim_blocks() -> None:
    plan = _plan_with(tasks=[make_task("T-001", claim_ids=[])])
    report = dag_trace.trace(plan, dag.join(plan, None))
    assert any("answers for no claim" in e for e in report.errors)


def test_a_claim_with_no_task_is_a_warning_not_a_block() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001"), make_claim("C-002", obligation_ids=["EO-001"])],
        obligations=[make_obligation("EO-001", subject_ids=["C-001", "C-002"])],
        tasks=[make_task("T-001", claim_ids=["C-001"])],
    )
    report = dag_trace.trace(plan, dag.join(plan, None))
    assert report.ok
    assert any("no task is answerable" in w for w in report.warnings)


def test_nfr_coverage_is_softer_but_grounding_is_not() -> None:
    assert dag_trace.is_nfr("NFR-1")
    assert not dag_trace.is_nfr("R-1")
    # A high-risk NFR claim still has to be grounded — "we could not find out" does not become
    # acceptable because the requirement is non-functional.
    plan = _plan_with(claims=[make_claim("C-001", requirement_ids=["NFR-1"], risk="high", epistemic_status="unknown")])
    assert not dag_trace.trace(plan).ok


def test_render_trace_names_the_blocking_findings() -> None:
    plan = _plan_with(claims=[make_claim("C-001", risk="high", epistemic_status="conflicted")])
    rendered = dag_trace.render_trace(dag_trace.trace(plan))
    assert "Blocking" in rendered and "C-001" in rendered


def test_render_trace_of_a_whole_thread_says_so() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001", requirement_ids=["R-1"])], tasks=[make_task("T-001", claim_ids=["C-001"])]
    )
    rendered = dag_trace.render_trace(dag_trace.trace(plan, dag.join(plan, None)))
    assert "The thread is whole" in rendered


# --- CLI ----------------------------------------------------------------------


def test_cli_validate_is_silent_on_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    assert dag.main(["--validate", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_cli_impacted_lists_the_ripple(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(
        tmp_path,
        plan=make_plan(
            tasks=[
                make_task("T-001", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
            ]
        ),
    )
    assert dag.main(["--impacted", "T-001", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "T-002"


def test_cli_impacted_rejects_an_unknown_task(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert dag.main(["--impacted", "T-999", "--repo", str(tmp_path)]) == 2


def test_cli_trace_exits_2_when_there_is_no_plan_yet(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=None)
    assert dag.main(["--trace", "--repo", str(tmp_path)]) == 2


def test_cli_trace_exits_1_on_a_broken_thread(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=make_plan(claims=[make_claim("C-001", risk="critical", epistemic_status="unknown")]))
    assert dag.main(["--trace", "--repo", str(tmp_path)]) == 1
