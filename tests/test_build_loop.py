"""Tests for build_loop.py — the deterministic half of the implementation phase.

The line this file defends is the one in the module docstring: **scheduling is decided in
code, not in a prompt.** Which tasks run, at what parallelism, in what order they merge, and
when the loop stops are all pure functions of the graph, so two runs of the same plan schedule
identically. The LLM writes the code; it does not decide what happens next.

The other half is what the loop refuses to claim. When the tasks finish it says what it
established (the gate passed) and what it did *not* (that the code does what the plan says),
and hands over to the review pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop import build_loop, dag, models
from agentloop import repo as repo_mod
from agentloop import store as store_mod
from tests._support import fake_git, make_config, make_plan, make_state, make_task, seed_repo


def graph_of(done: tuple[str, ...] = ()) -> dag.Graph:
    def status(tid: str) -> str:
        return "done" if tid in done else "todo"

    return dag.Graph.from_tasks(
        [
            dag.Task(id="T-001", title="base", kind="foundation", status=status("T-001")),
            dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",), status=status("T-002")),
            dag.Task(id="T-003", title="leaf B", kind="parallel", blocked_by=("T-001",), status=status("T-003")),
            dag.Task(id="T-004", title="leaf C", kind="parallel", blocked_by=("T-001",), status=status("T-004")),
            dag.Task(id="T-005", title="leaf D", kind="parallel", blocked_by=("T-001",), status=status("T-005")),
        ]
    )


def build_repo(tmp_path: Path, **kwargs: object) -> Path:
    """A repo ready to build: gate 3 approved, plan frozen, four tasks."""
    kwargs.setdefault(
        "plan",
        make_plan(
            tasks=[
                make_task("T-001", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
            ]
        ),
    )
    kwargs.setdefault("state", make_state(phase="build", plan_status="frozen"))
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return tmp_path


# --- scheduling (pure, deterministic) -----------------------------------------


def test_a_foundation_task_is_finalized_serially() -> None:
    batch = build_loop.plan_batch(graph_of(), max_parallel=3)
    assert batch == ("serial", [graph_of().get("T-001")])


def test_leaves_run_in_parallel_capped_at_max() -> None:
    mode, tasks = build_loop.plan_batch(graph_of(done=("T-001",)), max_parallel=3)  # type: ignore[misc]
    assert mode == "parallel"
    assert [t.id for t in tasks] == ["T-002", "T-003", "T-004"]  # T-005 waits for the next iteration


def test_an_empty_frontier_returns_none() -> None:
    assert build_loop.plan_batch(graph_of(done=("T-001", "T-002", "T-003", "T-004", "T-005")), 3) is None


def test_the_batch_is_the_same_every_time() -> None:
    """Two runs of the same plan must schedule identically, or the loop is something you watch
    rather than something you can predict."""
    graph = graph_of(done=("T-001",))
    first = build_loop.plan_batch(graph, 2)
    for _ in range(5):
        assert build_loop.plan_batch(graph, 2) == first


# --- config ------------------------------------------------------------------


def test_config_normalizes_the_quality_gate(tmp_path: Path) -> None:
    config = build_loop.Config.from_models(models.Config(make_config()))
    assert [s.name for s in config.steps] == ["test", "check"]
    assert config.steps[0].command == ("make", "test")
    assert config.gate_cmds == ["make test", "make check"]


def test_an_unknown_adapter_is_refused_up_front() -> None:
    config = make_config()
    config["agents"]["implementer"]["adapter"] = "mystery"  # type: ignore[index]
    with pytest.raises(ValueError, match="does not know how to launch"):
        build_loop.Config.from_models(models.Config(config))


def test_worktree_isolation_is_not_optional() -> None:
    """Parallel leaves writing one tree is how two tasks' changes end up attributed to one
    review, so there is no knob that turns it off."""
    config = build_loop.Config.from_models(models.Config(make_config()))
    assert config.worktree_enabled is True


def test_the_integration_gate_is_not_a_knob() -> None:
    """Each leaf was green only in isolation, so a batch that merged two or more has never
    been verified as one tree — there is nothing to opt out of."""
    assert not hasattr(build_loop.Config, "integration_gate")
    assert "integration_gate" not in json.dumps(make_config())


def test_a_step_command_is_an_argv_list_not_a_shell_string() -> None:
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
    assert step.display == "make test"
    assert step.runnable
    assert not build_loop.GateStep(name="smoke", kind="command").runnable


# --- task status goes through the Central Store -------------------------------


def test_a_status_change_lands_with_the_event_that_explains_it(tmp_path: Path) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    build_loop.set_task_status(repo, "T-001", "done")

    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None and state.task_status["T-001"] == "done"
    assert [e.event for e in store.read_events()] == ["task_completed"]


def test_starting_a_task_counts_an_attempt(tmp_path: Path) -> None:
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.set_task_status(repo, "T-001", "in-progress")
    build_loop.set_task_status(repo, "T-001", "in-progress")
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None and raw["tasks"]["T-001"]["attempts"] == 2


def test_an_off_vocabulary_status_is_refused(tmp_path: Path) -> None:
    repo = repo_mod.Repo(build_repo(tmp_path))
    with pytest.raises(ValueError, match="unknown task status"):
        build_loop.set_task_status(repo, "T-001", "nearly")


# --- preconditions ------------------------------------------------------------


def test_the_loop_refuses_while_gate_three_is_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path, state=make_state(gates={"tasks": "pending"}, phase="tasks", plan_status="draft"))
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 2
    assert "no frozen plan to build against" in capsys.readouterr().err


def test_the_loop_refuses_to_build_against_a_draft_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Gate 3's approval is what freezes the plan; building against a draft would implement a
    plan nobody signed for."""
    root = build_repo(tmp_path, state=make_state(phase="build", plan_status="draft"))
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 2
    assert "not 'frozen'" in capsys.readouterr().err


def test_a_command_step_with_no_command_cannot_be_expressed() -> None:
    """0.8.x allowed an empty `run` and had to fail fast on it at build time. The schema now
    refuses the shape outright, so the contradictory DoD never reaches the loop — the scaffold
    ships an explicit placeholder command instead of a silent skip."""
    config = make_config(quality_gate=[{"name": "smoke", "kind": "command", "executor_profile": "oracle"}])
    assert any("'command' is a required property" in e for e in models.schema_errors(config, "config"))


def test_a_leaf_worktree_may_not_drive_a_build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    repo._cache["git_common_dir"] = tmp_path / "elsewhere" / ".git"
    monkey = repo_mod.get
    try:
        repo_mod.get = lambda *_a, **_k: repo  # type: ignore[assignment]
        assert build_loop.main(["--repo", str(root)]) == 2
    finally:
        repo_mod.get = monkey  # type: ignore[assignment]
    assert "linked worktree" in capsys.readouterr().err


# --- the dry run is strictly read-only ----------------------------------------


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    store = store_mod.Store(repo)
    before = store.document_digest("state")

    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 0

    assert store.document_digest("state") == before
    assert store.read_events() == []
    assert not store.build_lock.exists()


def test_a_dry_run_walks_the_whole_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 0
    out = capsys.readouterr().out
    assert "T-001" in out and "T-002" in out
    assert "all tasks done" in out


# --- what the loop hands over -------------------------------------------------


def test_the_handover_says_what_was_not_established(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Green tests plus an AI's summary is not evidence that the code does what the plan says,
    and the loop must not let the phrasing imply otherwise."""
    root = build_repo(tmp_path)
    build_loop.main(["--dry-run", "--repo", str(root)])
    out = capsys.readouterr().out
    assert "did NOT establish" in out
    assert "agentloop review generate" in out
    assert "cannot open gate 4" in out


def test_the_handover_does_not_offer_to_approve(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    build_loop.main(["--dry-run", "--repo", str(root)])
    out = capsys.readouterr().out
    assert "security review" not in out.lower()  # 0.8.x's gate-4 evidence is gone
    assert "signature" in out


# --- the build lock -----------------------------------------------------------


def test_two_runs_cannot_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    store_mod.ensure_private_dir(store_mod.Store(repo).runtime)
    with build_loop.build_lock(repo):
        config = build_loop.Config.load(repo)
        assert build_loop.Orchestrator(config, dry_run=False, repo=repo).run() == 2
    assert "holds the lock" in capsys.readouterr().err


def test_the_lock_lives_outside_the_worktree(tmp_path: Path) -> None:
    """A per-worktree lock inode meant two leaves could each hold "the" lock (plan §11.1)."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    assert not str(store_mod.Store(repo).build_lock).startswith(str(tmp_path / ".agentloop"))


# --- the quality-gate pipeline ------------------------------------------------


def orchestrator(tmp_path: Path, **kwargs: object) -> build_loop.Orchestrator:
    root = build_repo(tmp_path, **kwargs)
    repo = repo_mod.Repo(root)
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)


def test_a_command_step_passes_on_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_run", fake_git())
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
    assert loop._run_cmd_step(step, cwd=str(tmp_path)) == ""


def test_a_command_step_summarizes_its_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_run", fake_git({("make", "test"): (1, "tests/x.py::t FAILED")}))
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
    summary = loop._run_cmd_step(step, cwd=str(tmp_path))
    assert "make test (rc=1)" in summary and "FAILED" in summary


def test_a_step_runs_its_argv_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No shell, and no shlex-splitting of user text: an argument with a space stays one
    argument, and a pipe cannot appear by accident."""
    record: list[list[str]] = []
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_run", fake_git(record=record))
    step = build_loop.GateStep(name="test", kind="command", command=("pytest", "-k", "a b"))
    loop._run_cmd_step(step, cwd=str(tmp_path))
    assert record[-1] == ["pytest", "-k", "a b"]


def test_the_task_pipeline_is_the_configured_dod(tmp_path: Path) -> None:
    """0.8.x prepended the ticket's own `test:` command. A task's extra judgement boundary is
    now its frozen oracle, not a command the implementer could have chosen."""
    loop = orchestrator(tmp_path)
    task = dag.Task(id="T-001", title="t", kind="foundation")
    assert [s.name for s in loop._steps_for(task)] == ["test", "check"]


# --- the implementer prompt ---------------------------------------------------


def test_the_prompt_names_the_claims_the_task_answers_for(tmp_path: Path) -> None:
    from agentloop import build_prompts

    task = dag.Task(id="T-002", title="retry", kind="parallel", claim_ids=("C-002",), oracle_ids=("O-002",))
    prompt = build_prompts.implementer_prompt(task, "", gate_cmds=["make test"], has_baseline=False)
    assert "C-002" in prompt
    assert "O-002" in prompt
    assert "never edit .agentloop/oracles/" in prompt


def test_the_prompt_falls_back_to_the_whole_design_without_claims() -> None:
    from agentloop import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    prompt = build_prompts.implementer_prompt(task, "", gate_cmds=["make test"], has_baseline=False)
    assert "docs/20-design.md" in prompt


def test_a_previous_failure_is_passed_through_already_summarized() -> None:
    from agentloop import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    prompt = build_prompts.implementer_prompt(
        task, "$ make test (rc=1)\nE  assert 1 == 2", gate_cmds=["make test"], has_baseline=False
    )
    assert "assert 1 == 2" in prompt


# --- events -------------------------------------------------------------------


def test_the_loop_records_what_it_did(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    loop._event("task_started", "T-001", {"why": "test"})
    events = store_mod.Store(loop.repo).read_events()
    assert [e.event for e in events] == ["task_started"]
    assert events[0].subject_ids == ("T-001",)


def test_a_dry_run_records_nothing(tmp_path: Path) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=True, repo=repo)
    loop._event("task_started", "T-001", {})
    assert store_mod.Store(repo).read_events() == []


def test_an_escalation_is_recorded_and_announced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    loop = orchestrator(tmp_path)
    loop._escalate("task_failed", "everything is on fire", task="T-001")
    assert "everything is on fire" in capsys.readouterr().err
    assert [e.event for e in store_mod.Store(loop.repo).read_events()] == ["task_failed"]


def test_there_is_no_resolve_verb() -> None:
    """An escalation is closed by a signed disposition in the review, not by a flag somebody
    flips in a log."""
    source = Path(build_loop.__file__).read_text(encoding="utf-8")
    assert "log_escalation" not in source
    assert "rotate_if_large" not in source


# --- crash recovery -----------------------------------------------------------


def test_a_task_left_in_progress_is_reset_to_todo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The frontier only picks up `todo`, so an interrupted task would deadlock the loop."""
    root = build_repo(tmp_path, state=make_state(phase="build", tasks={"T-001": "in-progress"}))
    repo = repo_mod.Repo(root)
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    loop._recover_in_progress()

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.task_status["T-001"] == "todo"
    assert "reset in-progress" in capsys.readouterr().out


# --- the CLI ------------------------------------------------------------------


def test_an_unsupported_layout_stops_the_command(tmp_path: Path) -> None:
    root = build_repo(tmp_path)
    (root / ".agentloop" / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    assert build_loop.main(["--repo", str(root)]) == 1


def test_an_invalid_config_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    (root / ".agentloop" / "config.yaml").write_text("project: {}\n", encoding="utf-8")
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 1
    assert "cannot load .agentloop/config.yaml" in capsys.readouterr().err
