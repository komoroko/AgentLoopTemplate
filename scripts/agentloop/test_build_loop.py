"""Verify build_loop.py's scheduling and dry-run control flow."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import build_loop
import dag
import events
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
    "  worktree: {enabled: true, dir: .worktrees, branch_pattern: '{branch}-{task_id}'}\n"
    "  quality_gate:\n"
    "    steps:\n"
    "      - {name: test, kind: cmd, run: 'make test', retries: 2}\n"
    "      - {name: check, kind: cmd, run: 'make check', retries: 2}\n"
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


def test_dry_run_blocks_when_placeholders_remain(project: Path) -> None:
    # A template that was never `make init`-ed must not start consuming tasks.
    state = _STATE.format(tasks="approved").replace('project: "demo"', 'project: "<enter the product name>"')
    (project / ".agentloop" / "state.md").write_text(state, encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    assert build_loop.main(["--dry-run"]) == 2


def test_dry_run_blocks_when_tasks_not_approved(project: Path) -> None:
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="pending"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    assert build_loop.main(["--dry-run"]) == 2


def _snapshot(project: Path) -> dict[str, bytes | None]:
    """Byte-level snapshot of every SSOT/log file a run could touch (None = absent)."""
    out: dict[str, bytes | None] = {}
    files = (".agentloop/tasks.yaml", ".agentloop/state.md", ".agentloop/events.ndjson", ".agentloop/build-loop.lock")
    for rel in files:
        p = project / rel
        out[rel] = p.read_bytes() if p.exists() else None
    return out


def test_dry_run_completes_all_tasks_without_writing(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --dry-run simulates the whole loop to gate ④ in memory; the SSOT files stay byte-identical
    # (a dry run that marks tasks done would corrupt the next real run's starting state).
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    before = _snapshot(project)
    rc = build_loop.main(["--dry-run"])
    assert rc == 0
    assert "all tasks done (gate ④)" in capsys.readouterr().out  # the simulation reached completion
    assert _snapshot(project) == before
    assert dag.load(".agentloop/tasks.yaml").counts()["done"] == 0


def test_recovers_stale_in_progress(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A task left in in_progress from a previous interruption is reset to todo at startup and re-consumed.
    # Without recovery it falls out of the frontier (todo-only) and is never started, deadlocking.
    stale = _TASKS.replace(
        "{id: T-002, title: leaf A, kind: parallel, blockedBy: [T-001], status: todo, test: make test}",
        "{id: T-002, title: leaf A, kind: parallel, blockedBy: [T-001], status: in_progress, test: make test}",
    )
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(stale, encoding="utf-8")
    before = _snapshot(project)
    rc = build_loop.main(["--dry-run"])
    assert rc == 0
    # The previously in_progress T-002 also reaches done — in the simulation only; the files stay put.
    assert "all tasks done (gate ④)" in capsys.readouterr().out
    assert _snapshot(project) == before


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
    assert orch.merge_leaf(_leaf("T-002", "leaf A"), "build/demo-T-002") is False
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


# --- quality-gate pipeline (config-declared DoD) -----------------------------

_CONFIG_STEPS = (
    "build:\n"
    "  max_parallel: 3\n"
    "  worktree: {enabled: true, dir: .worktrees, branch_pattern: '{branch}-{task_id}'}\n"
    "  quality_gate:\n"
    "    agent_steps: true\n"
    "    steps:\n"
    "      - {name: test, kind: cmd, run: 'make test', retries: 1}\n"
    "      - {name: check, kind: cmd, run: 'make check', retries: 1}\n"
    "      - {name: review, kind: agent}\n"
    "      - {name: smoke, kind: cmd, run: '', retries: 1}\n"
    "gates:\n  enforce_hook: true\n"
)


def test_config_steps_are_required_with_migration_hint(project: Path) -> None:
    # The legacy test_cmd/check_cmd + retries form was removed in 0.3.0: a config without a
    # steps list must fail loudly with the migration pointer, not fall back silently.
    (project / ".agentloop" / "config.yaml").write_text(
        "build:\n  quality_gate: {test_cmd: 'make test', check_cmd: 'make check'}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="quality_gate.steps is missing.*removed in 0.3.0"):
        build_loop.Config.load()


def test_config_steps_form_parses_kinds_and_retries(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text(_CONFIG_STEPS, encoding="utf-8")
    config = build_loop.Config.load()
    assert [(s.name, s.kind) for s in config.steps] == [
        ("test", "cmd"),
        ("check", "cmd"),
        ("review", "agent"),
        ("smoke", "cmd"),
    ]
    assert config.steps[0].retries == 1
    assert config.gate_cmds == ["make test", "make check"]  # empty-run smoke is not a gate command


def test_config_rejects_unknown_step_kind(project: Path) -> None:
    bad = _CONFIG_STEPS.replace("kind: agent", "kind: llm")
    (project / ".agentloop" / "config.yaml").write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind"):
        build_loop.Config.load()


def _steps_orch(project: Path, monkeypatch: pytest.MonkeyPatch) -> build_loop.Orchestrator:
    (project / ".agentloop" / "config.yaml").write_text(_CONFIG_STEPS, encoding="utf-8")
    _provision(project)
    return build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)


def test_pipeline_stops_at_first_failing_cmd_step(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _steps_orch(project, monkeypatch)
    monkeypatch.setattr(orch, "_run_cmd_step", lambda step, cwd: "boom" if step.name == "check" else "")
    monkeypatch.setattr(orch, "_run_agent_step", lambda task, cwd: pytest.fail("agent step must not run"))
    failed, log = orch._run_pipeline(_leaf("T-002", "leaf A"), cwd=".")
    assert failed == "check"
    assert log == "boom"


def test_pipeline_reruns_passed_cmd_steps_after_agent_change(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The agent step's fixes invalidate the earlier green evidence: test/check run again after it.
    orch = _steps_orch(project, monkeypatch)
    ran: list[str] = []

    def record(step: build_loop.GateStep, cwd: str) -> str:
        ran.append(step.name)
        return ""

    monkeypatch.setattr(orch, "_run_cmd_step", record)
    monkeypatch.setattr(orch, "_run_agent_step", lambda task, cwd: True)  # it changed the tree
    failed, _ = orch._run_pipeline(_leaf("T-002", "leaf A"), cwd=".")
    assert failed is None
    assert ran == ["test", "check", "test", "check"]  # smoke (empty run) is skipped, never executed


def test_pipeline_skips_rerun_when_agent_changed_nothing(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _steps_orch(project, monkeypatch)
    ran: list[str] = []

    def record(step: build_loop.GateStep, cwd: str) -> str:
        ran.append(step.name)
        return ""

    monkeypatch.setattr(orch, "_run_cmd_step", record)
    monkeypatch.setattr(orch, "_run_agent_step", lambda task, cwd: False)
    failed, _ = orch._run_pipeline(_leaf("T-002", "leaf A"), cwd=".")
    assert failed is None
    assert ran == ["test", "check"]


def test_pipeline_agent_steps_off_drops_agent_step(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _steps_orch(project, monkeypatch)
    orch.config.agent_steps = False
    monkeypatch.setattr(orch, "_run_agent_step", lambda task, cwd: pytest.fail("agent step must not run"))
    monkeypatch.setattr(orch, "_run_cmd_step", lambda step, cwd: "")
    failed, _ = orch._run_pipeline(_leaf("T-002", "leaf A"), cwd=".")
    assert failed is None


def test_run_task_to_done_budget_is_per_step(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One failure of test and one of check each consume their own budget (retries: 1 each) —
    # under the old max()-collapsed single counter this sequence would have been blocked.
    orch = _steps_orch(project, monkeypatch)
    outcomes = iter([("test", "t red"), ("check", "c red"), (None, "")])
    implementer_calls: list[str] = []
    monkeypatch.setattr(orch, "_invoke_implementer", lambda task, cwd, log: implementer_calls.append(log))
    monkeypatch.setattr(orch, "_run_pipeline", lambda task, cwd: next(outcomes))
    ok, log = orch._run_task_to_done(_leaf("T-002", "leaf A"), cwd=".")
    assert ok is True
    assert implementer_calls == ["", "t red", "c red"]  # each failure went back to the implementer


def test_run_task_to_done_blocks_when_one_step_budget_runs_out(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _steps_orch(project, monkeypatch)
    monkeypatch.setattr(orch, "_invoke_implementer", lambda task, cwd, log: None)
    monkeypatch.setattr(orch, "_run_pipeline", lambda task, cwd: ("test", "still red"))
    ok, log = orch._run_task_to_done(_leaf("T-002", "leaf A"), cwd=".")
    assert ok is False  # retries: 1 → initial attempt + 1 send-back, then blocked
    assert log == "still red"


# --- per-task test command (tasks.yaml `test` = the ticket's own green decision)


def _leaf_with_test(run: str) -> dag.Task:
    return dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",), test=run)


def test_task_test_runs_first_as_focused_step(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # tasks.yaml documents `test` as the task's green decision — it must actually run, ahead of
    # the shared pipeline (fastest, most focused failure first).
    orch = _steps_orch(project, monkeypatch)
    ran: list[str] = []

    def record(step: build_loop.GateStep, cwd: str) -> str:
        ran.append(step.name)
        return ""

    monkeypatch.setattr(orch, "_run_cmd_step", record)
    monkeypatch.setattr(orch, "_run_agent_step", lambda task, cwd: False)
    failed, _ = orch._run_pipeline(_leaf_with_test("pytest tests/test_a.py -q"), cwd=".")
    assert failed is None
    assert ran == ["task-test", "test", "check"]


def test_task_test_duplicating_a_config_step_runs_once(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The default `test: make test` would double the most expensive step — dedup keeps it single.
    orch = _steps_orch(project, monkeypatch)
    ran: list[str] = []

    def record(step: build_loop.GateStep, cwd: str) -> str:
        ran.append(step.name)
        return ""

    monkeypatch.setattr(orch, "_run_cmd_step", record)
    monkeypatch.setattr(orch, "_run_agent_step", lambda task, cwd: False)
    failed, _ = orch._run_pipeline(_leaf_with_test("make test"), cwd=".")
    assert failed is None
    assert ran == ["test", "check"]


def test_task_test_failure_consumes_budget_and_blocks(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The focused step carries the test step's send-back budget; running out blocks like any step.
    orch = _steps_orch(project, monkeypatch)
    monkeypatch.setattr(orch, "_invoke_implementer", lambda task, cwd, log: None)
    monkeypatch.setattr(orch, "_run_pipeline", lambda task, cwd: ("task-test", "focused red"))
    ok, log = orch._run_task_to_done(_leaf_with_test("pytest tests/test_a.py -q"), cwd=".")
    assert ok is False  # test retries: 1 → initial attempt + 1 send-back, then blocked
    assert log == "focused red"
    fails = [e for e in events.load_events() if e.event == "step_fail"]
    assert fails and all(e.step == "task-test" for e in fails)


# --- smoke `required` knob (a runnable deliverable must not skip its launch check)


def test_config_parses_step_required_flag(project: Path) -> None:
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG_STEPS.replace("run: '', retries: 1", "run: '', retries: 1, required: true"), encoding="utf-8"
    )
    steps = {s.name: s.required for s in build_loop.Config.load().steps}
    assert steps["smoke"] is True
    assert steps["test"] is False  # default stays off


def test_required_step_without_command_refuses_to_start(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail-fast: the contradiction must stop the loop before any implementer is paid for —
    # not surface as a silent skip at gate ④ after the whole build ran.
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG_STEPS.replace("run: '', retries: 1", "run: '', retries: 1, required: true"), encoding="utf-8"
    )
    _provision(project)
    monkeypatch.setattr(
        build_loop, "_run", lambda cmd, cwd, timeout=None: pytest.fail("must stop before any git/claude call")
    )
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    assert orch._run_loop() == 2
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-001"] == "todo"  # nothing consumed


def test_present_gate4_flags_empty_cmd_steps(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The empty-smoke nudge is machine output at the gate, not a lead's memory item.
    (project / ".agentloop" / "config.yaml").write_text(_CONFIG_STEPS, encoding="utf-8")
    _provision(project)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=True)
    orch._present_gate4(dag.load(".agentloop/tasks.yaml"), "n/a")
    out = capsys.readouterr().out
    assert "DoD ran WITHOUT: smoke" in out and "required: true" in out


def test_task_test_appears_in_implementer_prompt(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Instruction and execution must not diverge: the implementer is told the exact command the
    # gate will judge it by first.
    orch = _steps_orch(project, monkeypatch)
    assert "pytest tests/test_a.py -q" in orch._implementer_prompt(_leaf_with_test("pytest tests/test_a.py -q"), "")
    assert "own test command" not in orch._implementer_prompt(_leaf("T-002", "leaf A"), "")


# --- robustness: orphan cleanup / single-run lock / quoting / branch guard ----


def test_consume_parallel_cleans_up_blocked_worktree(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A blocked leaf never reaches merge_leaf, so without explicit cleanup its worktree orphans
    # under .worktrees/ (blocked tasks leave the frontier and startup cleanup never sees them).
    _provision(project)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)

    def fake_run_task(task: dag.Task, cwd: str) -> tuple[bool, str]:
        return (task.id != "T-003"), ("" if task.id != "T-003" else "$ make test (rc=1)")

    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", fake_run_task)
    with pytest.raises(build_loop.StopLoop):
        orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    assert ["git", "worktree", "remove", "--force", str(Path(".worktrees") / "T-003")] in calls
    assert ["git", "worktree", "remove", "--force", str(Path(".worktrees") / "T-002")] in calls  # via merge_leaf


def test_acquire_lock_blocks_live_pid_and_reclaims_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "build-loop.lock"
    lock.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(build_loop, "_pid_alive", lambda pid: True)
    assert build_loop.acquire_lock(str(lock)) is False  # another live run holds it
    monkeypatch.setattr(build_loop, "_pid_alive", lambda pid: False)
    assert build_loop.acquire_lock(str(lock)) is True  # a crashed run's lock is reclaimed
    assert lock.read_text(encoding="utf-8") == str(os.getpid())
    build_loop.release_lock(str(lock))
    assert not lock.exists()


def test_run_refuses_concurrent_loop(project: Path) -> None:
    _provision(project)
    (project / ".agentloop" / "build-loop.lock").write_text("1", encoding="utf-8")  # PID 1 is always alive
    assert build_loop.main([]) == 2  # a real run refuses while another live run holds the lock
    # A dry run is read-only, so it neither takes nor honors the lock — it may run alongside.
    assert build_loop.main(["--dry-run"]) == 0
    assert (project / ".agentloop" / "build-loop.lock").read_text(encoding="utf-8") == "1"


def test_run_refuses_undetermined_work_branch(project: Path) -> None:
    # Placeholder branch + no git repo → work_branch falls back to "HEAD"; a non-dry run must stop
    # instead of creating worktrees/commits against an arbitrary base.
    _provision(project)
    state = _STATE.format(tasks="approved").replace('branch: "build/demo"', 'branch: "<enter the work branch>"')
    (project / ".agentloop" / "state.md").write_text(state, encoding="utf-8")
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    assert orch.run() == 2


def test_cmd_step_shlex_splits_quoted_args(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision(project)
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        seen.append(cmd)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    orch._run_cmd_step(build_loop.GateStep("test", "cmd", "pytest -k 'a b'"), cwd=".")
    assert seen == [["pytest", "-k", "a b"]]


# --- non-dry-run real paths (git monkeypatched) -------------------------------


def test_consume_serial_commits_task_diff_excluding_agentloop(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision(project)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return 0, " M app.py"  # dirty tree: the implementer forgot to commit
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    foundation = dag.Task(id="T-001", title="base", kind="foundation")
    orch._consume_serial([foundation])
    assert ["git", "add", "-A", "--", ".", ":(exclude).agentloop"] in calls  # one commit = one task
    assert ["git", "commit", "--no-verify", "-m", "T-001: base"] in calls
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-001"] == "done"


def test_merge_leaf_success_removes_worktree(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision(project)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    assert orch.merge_leaf(_leaf("T-002", "leaf A"), "build/demo-T-002") is True
    assert ["git", "merge", "--no-ff", "--no-edit", "build/demo-T-002"] in calls
    assert ["git", "worktree", "remove", "--force", str(Path(".worktrees") / "T-002")] in calls


def test_run_escalates_when_all_unfinished_are_blocked(project: Path) -> None:
    blocked = _TASKS.replace("status: todo", "status: blocked")
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(blocked, encoding="utf-8")
    assert build_loop.main([]) == 1  # frontier empty + unfinished → escalate, stop (before any git/claude call)
    recorded = events.load_events()  # the escalation lands as a structured event, not free text
    assert [e.event for e in recorded] == ["no_runnable"]
    assert "Help needed" in recorded[0].detail


def test_run_escalation_in_dry_run_stays_off_the_event_log(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocked = _TASKS.replace("status: todo", "status: blocked")
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(blocked, encoding="utf-8")
    assert build_loop.main(["--dry-run"]) == 1  # same decision as a real run...
    assert events.load_events() == []  # ...but read-only: nothing appended
    assert "Help needed" in capsys.readouterr().err  # the human still sees the escalation on the console


# --- state.md generated-view refresh (mode A keeps the human-facing board fresh) ----

_STATE_WITH_VIEW = _STATE.replace(
    "# board",
    "# board\n\n<!-- DAG-VIEW:BEGIN -->\n_(stale view)_\n<!-- DAG-VIEW:END -->\n",
)


def test_update_state_view_replaces_block_and_bumps_date(project: Path) -> None:
    (project / ".agentloop" / "state.md").write_text(_STATE_WITH_VIEW.format(tasks="approved"), encoding="utf-8")
    assert build_loop.update_state_view(_graph(done=("T-001",))) is True
    text = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert "_(stale view)_" not in text  # the old view was replaced
    assert "| T-001 | base | foundation |" in text  # the rendered task table landed between the markers
    assert build_loop.DAG_VIEW_BEGIN in text and build_loop.DAG_VIEW_END in text  # markers survive re-runs
    assert '"2026-06-26"' not in text  # updated_at was bumped off the fixture date


def test_update_state_view_noop_without_markers(project: Path) -> None:
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    before = (project / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert build_loop.update_state_view(_graph()) is False
    assert (project / ".agentloop" / "state.md").read_text(encoding="utf-8") == before


# --- subprocess timeouts (a hung process must not stall the loop forever) ----


def test_run_kills_hung_process_with_rc_124() -> None:
    rc, out = build_loop._run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=".", timeout=0.2)
    assert rc == 124  # the coreutils timeout convention
    assert "timed out after 0s (process killed)" in out


def test_run_no_timeout_by_default() -> None:
    rc, out = build_loop._run([sys.executable, "-c", "print('ok')"], cwd=".")
    assert rc == 0
    assert "ok" in out


def test_config_parses_timeouts_and_zero_disables(project: Path) -> None:
    config = build_loop.Config.load()
    assert config.timeout_cmd == 1800.0  # defaults apply when the knob is absent
    assert config.timeout_agent == 3600.0
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("build:\n", "build:\n  timeouts: {cmd_sec: 60, agent_sec: 0}\n"), encoding="utf-8"
    )
    config = build_loop.Config.load()
    assert config.timeout_cmd == 60.0
    assert config.timeout_agent is None  # 0 = no timeout


def test_cmd_step_passes_cmd_timeout(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision(project)
    seen: list[float | None] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        seen.append(timeout)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    assert orch._run_cmd_step(build_loop.GateStep("test", "cmd", "make test"), cwd=".") == ""
    assert seen == [orch.config.timeout_cmd]


# --- failure summarization (retry-friendly, token-lean) ---------------------


def test_summarize_failure_keeps_pytest_salient_and_drops_noise() -> None:
    # A pytest run buried in passing-test noise: keep FAILED/assertion/summary lines, drop the noise.
    out = "\n".join(
        ["test_a.py::test_ok PASSED"] * 200
        + [
            "test_a.py::test_bad FAILED",
            "    def test_bad():",
            ">       assert add(1, 2) == 4",
            "E       assert 3 == 4",
            "=================== 1 failed, 200 passed in 0.42s ===================",
            "FAILED test_a.py::test_bad - assert 3 == 4",
        ]
    )
    summary = build_loop.summarize_failure("make test", 1, out)
    assert summary.startswith("$ make test (rc=1)")
    assert "E       assert 3 == 4" in summary  # the actionable assertion survives
    assert "1 failed, 200 passed" in summary  # the summary rule line survives
    assert "PASSED" not in summary  # passing-test noise is dropped
    assert "omitted" in summary  # the omission is disclosed


def test_summarize_failure_keeps_ruff_and_mypy_locations() -> None:
    out = "\n".join(
        [
            "backend/foo.py:12:5: F401 `os` imported but unused",
            "backend/bar.py:3:1: E402 module level import not at top of file",
            'backend/foo.py:20: error: Incompatible return value type (got "int", expected "str")',
            "Found 1 error in 1 file (checked 12 source files)",
        ]
    )
    summary = build_loop.summarize_failure("make check", 1, out)
    assert "F401" in summary
    assert "error: Incompatible return value type" in summary


def test_summarize_failure_falls_back_to_tail_and_caps_budget() -> None:
    # No recognizable markers: keep the non-empty tail, capped to the line budget.
    out = "\n".join(f"line {i}" for i in range(500))
    summary = build_loop.summarize_failure("make test", 2, out)
    assert summary.startswith("$ make test (rc=2)")
    assert "line 499" in summary  # the tail is kept
    assert "line 0" not in summary  # the head is dropped
    assert len(summary.splitlines()) <= build_loop._FAILURE_MAX_LINES + 3  # header + note + budget


def test_summarize_failure_empty_output_is_just_header() -> None:
    assert build_loop.summarize_failure("make test", 1, "") == "$ make test (rc=1)"


def test_summarize_failure_whitespace_only_is_just_header() -> None:
    # Whitespace-only output must not emit a bare "N line(s) omitted" note with no body.
    assert build_loop.summarize_failure("make test", 1, "\n \n\t\n") == "$ make test (rc=1)"


def test_summarize_failure_keeps_frontend_and_verbose_diagnostics() -> None:
    # eslint/tsc/mypy lowercase "error" and inline verbose pytest FAILED/ERROR must survive — the quality
    # gate (CLAUDE.md) covers eslint/tsc, and a line-start-uppercase-only regex would silently drop these.
    out = "\n".join(
        [
            "src/app.ts(10,3): error TS2322: Type 'number' is not assignable to type 'string'.",
            "  12:5  error  'x' is assigned a value but never used  no-unused-vars",
            "backend/foo.py:20: error: Incompatible return value type",
            "tests/test_x.py::test_login FAILED",
            "tests/test_x.py::test_db ERROR",
        ]
    )
    s = build_loop.summarize_failure("make check", 1, out)
    assert "TS2322" in s
    assert "no-unused-vars" in s
    assert "Incompatible return value type" in s
    assert "test_login FAILED" in s
    assert "test_db ERROR" in s


def test_summarize_failure_drops_passing_tests_named_like_exceptions() -> None:
    # Passing tests whose names contain "error"/"…Error" must not be treated as salient, or they can
    # evict the real failure under the line cap and surface passing-test noise instead.
    out = "\n".join(
        ["tests/test_x.py::test_raises_ValueError PASSED", "tests/test_error_handling.py::test_ok PASSED"] * 60
        + ["tests/test_x.py::test_add FAILED", "E       assert 3 == 4"]
    )
    s = build_loop.summarize_failure("make test", 1, out)
    assert "PASSED" not in s  # no passing-test noise leaked in
    assert "test_add FAILED" in s and "assert 3 == 4" in s  # the real failure survived the cap


def test_summarize_failure_keeps_exception_line_not_named_test() -> None:
    out = "raise happened\nValueError: bad input\ntests/x.py::test_uses_ValueError PASSED"
    s = build_loop.summarize_failure("make test", 1, out)
    assert "ValueError: bad input" in s  # the real exception line is kept (colon-anchored branch)
    assert "PASSED" not in s  # the passing test merely naming ValueError is not


def test_summarize_failure_long_single_line_keeps_head_with_location() -> None:
    # A single salient line over the char budget keeps its head (the file:line:col prefix), not the tail.
    out = "backend/foo.py:20:5: error: " + ("x" * 3000)
    s = build_loop.summarize_failure("make check", 1, out)
    assert "backend/foo.py:20:5: error:" in s


# --- post-merge integration gate (the merged state is re-verified in code) ---


def test_config_parses_integration_gate_default_and_off(project: Path) -> None:
    assert build_loop.Config.load().integration_gate is True  # default on
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG_STEPS.replace("    agent_steps: true\n", "    agent_steps: true\n    integration_gate: false\n"),
        encoding="utf-8",
    )
    assert build_loop.Config.load().integration_gate is False


def _parallel_orch(project: Path, monkeypatch: pytest.MonkeyPatch) -> build_loop.Orchestrator:
    _provision(project)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd, timeout=None: (0, ""))
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    return orch


def test_multi_leaf_batch_runs_integration_gate_then_done(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _parallel_orch(project, monkeypatch)
    gated: list[list[str]] = []

    def fake_gate(tasks: list[dag.Task]) -> tuple[bool, str]:
        gated.append([t.id for t in tasks])
        return True, ""

    monkeypatch.setattr(orch, "_integration_gate", fake_gate)
    orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    assert gated == [["T-002", "T-003"]]
    by_id = {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}
    assert by_id["T-002"] == "done" and by_id["T-003"] == "done"


def test_single_leaf_merge_skips_integration_gate(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One merged leaf leaves work byte-identical to the already-gated worktree tree: re-running
    # the gate would prove nothing new, so the cost is skipped.
    orch = _parallel_orch(project, monkeypatch)
    monkeypatch.setattr(orch, "_integration_gate", lambda tasks: pytest.fail("gate must not run for 1 leaf"))
    orch._consume_parallel([_leaf("T-002", "leaf A")])
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-002"] == "done"


def test_integration_gate_off_skips_reverification(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _parallel_orch(project, monkeypatch)
    orch.config.integration_gate = False
    monkeypatch.setattr(orch, "_integration_gate", lambda tasks: pytest.fail("gate is configured off"))
    orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])


def test_integration_red_blocks_whole_batch_with_event(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _parallel_orch(project, monkeypatch)
    monkeypatch.setattr(orch, "_integration_gate", lambda tasks: (False, "$ make check (rc=1)"))
    with pytest.raises(build_loop.StopLoop):
        orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    by_id = {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}
    assert by_id["T-002"] == "blocked" and by_id["T-003"] == "blocked"  # merged code stays on work
    recorded = [e for e in events.load_events() if e.event == "integration_red"]
    assert len(recorded) == 1 and recorded[0].task == "T-002,T-003"
    assert "$ make check (rc=1)" in recorded[0].detail


def test_integration_gate_retries_fixer_until_green(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _steps_orch(project, monkeypatch)  # steps config: test/check retries 1 each
    outcomes = iter(["boom", "", ""])  # test red once, then all green
    monkeypatch.setattr(orch, "_run_cmd_step", lambda step, cwd: next(outcomes) if step.name == "test" else "")
    fixer_calls: list[str] = []
    monkeypatch.setattr(orch, "_invoke_integration_fixer", lambda ids, log: fixer_calls.append(log))
    ok, log = orch._integration_gate([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    assert ok is True and log == ""
    assert fixer_calls == ["boom"]  # the failure summary went to the fixer once
    fails = [e for e in events.load_events() if e.event == "step_fail"]
    assert [(e.task, e.step) for e in fails] == [("T-002,T-003", "test")]


def test_integration_gate_blocks_when_budget_runs_out(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _steps_orch(project, monkeypatch)
    monkeypatch.setattr(orch, "_run_cmd_step", lambda step, cwd: "still red" if step.name == "check" else "")
    monkeypatch.setattr(orch, "_invoke_integration_fixer", lambda ids, log: None)
    ok, log = orch._integration_gate([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    assert ok is False and log == "still red"  # retries: 1 → initial + 1 fix attempt, then give up


def test_integration_gate_skips_agent_and_empty_steps(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The review agent step already ran per task; the integration gate re-runs only the
    # deterministic cmd steps (and skips smoke's empty run).
    orch = _steps_orch(project, monkeypatch)
    ran: list[str] = []

    def record(step: build_loop.GateStep, cwd: str) -> str:
        ran.append(step.name)
        assert cwd == "."  # on the merged work branch, not a worktree
        return ""

    monkeypatch.setattr(orch, "_run_cmd_step", record)
    monkeypatch.setattr(orch, "_run_agent_step", lambda task, cwd: pytest.fail("agent step must not run"))
    ok, _ = orch._integration_gate([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    assert ok is True
    assert ran == ["test", "check"]


# --- post-build security review (bound to the reviewed HEAD) -----------------


def _sec_orch(
    project: Path, monkeypatch: pytest.MonkeyPatch, head: str = "abc123", claude_rc: int = 0, writes_report: bool = True
) -> tuple[build_loop.Orchestrator, list[list[str]]]:
    _provision(project)
    (project / ".agentloop").mkdir(exist_ok=True)
    claude_calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"] and "HEAD" in cmd:
            return 0, head + "\n"
        if cmd[0] == "claude" and cmd[1] == "-p":
            claude_calls.append(cmd)
            if writes_report:
                Path(build_loop.SECURITY_REVIEW_PATH).write_text(
                    f"Reviewed-HEAD: {head}\n\nNo findings.\n", encoding="utf-8"
                )
            return claude_rc, "boom" if claude_rc else ""
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    return build_loop.Orchestrator(build_loop.Config.load(), dry_run=False), claude_calls


def test_security_review_writes_report_and_event(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch, claude_calls = _sec_orch(project, monkeypatch)
    note = orch._post_build_security_review()
    assert "report written" in note and "abc123" in note
    assert len(claude_calls) == 1
    recorded = [e for e in events.load_events() if e.event == "security_review"]
    assert len(recorded) == 1 and recorded[0].commit == "abc123"
    assert recorded[0].detail == build_loop.SECURITY_REVIEW_PATH


def test_security_review_skips_when_head_already_reviewed(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Idempotence: a loop re-invoked at the same HEAD (e.g. after gate presentation) must not pay
    # for a second headless review; a moved HEAD must re-review.
    orch, claude_calls = _sec_orch(project, monkeypatch)
    orch._post_build_security_review()
    note = orch._post_build_security_review()
    assert "already reviewed" in note
    assert len(claude_calls) == 1  # no second launch


def test_security_review_off_points_at_manual_run(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch, claude_calls = _sec_orch(project, monkeypatch)
    orch.config.security_review = False
    note = orch._post_build_security_review()
    assert "OFF" in note and "/security-review" in note
    assert claude_calls == []
    assert events.load_events() == []


def test_security_review_launch_failure_degrades_to_manual(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch, claude_calls = _sec_orch(project, monkeypatch, claude_rc=1, writes_report=False)
    note = orch._post_build_security_review()
    assert "FAILED" in note and "boom" in note
    assert events.load_events() == []  # a failed review is never recorded as done


def test_security_review_missing_head_line_is_not_done(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The agent exiting 0 is not evidence: the report must record the reviewed HEAD, or gate ④
    # would present a stale/absent report as current.
    orch, claude_calls = _sec_orch(project, monkeypatch, writes_report=False)
    note = orch._post_build_security_review()
    assert "does not record Reviewed-HEAD" in note
    assert events.load_events() == []


def test_config_parses_post_build_security_review(project: Path) -> None:
    assert build_loop.Config.load().security_review is True  # default on
    (project / ".agentloop" / "config.yaml").write_text(
        _CONFIG.replace("gates:\n", "  post_build: {security_review: false}\ngates:\n"), encoding="utf-8"
    )
    assert build_loop.Config.load().security_review is False


# --- uncommitted-work protection (nothing may be lost with the worktree) -----


def test_parallel_finalizes_worktree_before_merge_and_before_blocked_cleanup(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An implementer that forgot to commit must not lose work: the successful leaf's diff is
    # finalized on its branch BEFORE the merge, and a blocked leaf's diff BEFORE the forced
    # worktree removal (the branch is the only copy that survives `worktree remove --force`).
    _provision(project)
    calls: list[tuple[list[str], str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append((cmd, cwd))
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return 0, " M app.py"  # dirty tree: the implementer forgot to commit
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(
        orch, "_run_task_to_done", lambda task, cwd: ((task.id != "T-003"), "" if task.id != "T-003" else "red")
    )
    with pytest.raises(build_loop.StopLoop):
        orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])

    wt2, wt3 = str(Path(".worktrees") / "T-002"), str(Path(".worktrees") / "T-003")
    add = ["git", "add", "-A", "--", ".", ":(exclude).agentloop"]
    assert (add, wt2) in calls  # success path: finalize inside the worktree...
    commit_ok = calls.index((["git", "commit", "--no-verify", "-m", "T-002: leaf A"], wt2))
    merge = calls.index((["git", "merge", "--no-ff", "--no-edit", "build/demo-T-002"], "."))
    assert commit_ok < merge  # ...before the merge picks the branch up
    assert (add, wt3) in calls  # blocked path: finalize as WIP...
    commit_wip = calls.index((["git", "commit", "--no-verify", "-m", "T-003: WIP (blocked)"], wt3))
    # _add_worktree also pre-cleans worktrees at batch start; the removal that matters is the LAST one.
    removal = len(calls) - 1 - calls[::-1].index((["git", "worktree", "remove", "--force", wt3], "."))
    assert commit_wip < removal  # ...before the forced removal drops the tree


def test_consume_serial_finalize_is_noop_in_dry_run(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision(project)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd, timeout=None: pytest.fail("dry-run must not call git"))
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=True)
    orch._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])
    # The task completes only in the simulation overlay; tasks.yaml itself is never written.
    assert orch._sim_status["T-001"] == "done"
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-001"] == "todo"


def test_finalize_commit_clean_tree_issues_no_commit(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A clean tree (the implementer committed as instructed) must stay a strict no-op: an
    # unconditional `git commit` would fail on "nothing to commit", indistinguishable from a
    # real failure without the porcelain check.
    _provision(project)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append(cmd)
        return 0, ""  # `git status --porcelain` → empty = clean

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    assert orch._finalize_commit(".", "T-001: base") is True
    assert not [c for c in calls if c[:2] in (["git", "add"], ["git", "commit"])]


def _failing_commit_run(calls: list[tuple[list[str], str]], fail_cwd: str | None = None):  # type: ignore[no-untyped-def]
    """A fake _run: dirty porcelain everywhere; `git commit` fails (optionally only in fail_cwd)."""

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append((cmd, cwd))
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return 0, " M app.py"
        if cmd[:2] == ["git", "commit"] and (fail_cwd is None or cwd == fail_cwd):
            return 128, "fatal: unable to auto-detect email address"
        return 0, ""

    return fake_run


def test_finalize_commit_failure_blocks_serial_task_and_stops(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A dirty tree that cannot be committed is the precursor of data loss — the task must not be
    # marked done on a swallowed rc, and the failure must land in the escalation log.
    _provision(project)
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(build_loop, "_run", _failing_commit_run(calls))
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    with pytest.raises(build_loop.StopLoop, match="finalize commit failed"):
        orch._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-001"] == "blocked"
    blocked = [e for e in events.load_events() if e.event == "blocked"]
    assert blocked and "finalize commit failed" in blocked[0].detail


def test_finalize_commit_failure_keeps_blocked_worktree(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # When even the WIP preservation commit fails, the worktree is the only copy of the diff:
    # the forced removal must be skipped so a human can recover it.
    _provision(project)
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(build_loop, "_run", _failing_commit_run(calls))
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    orch._cleanup_worktree(_leaf("T-003", "leaf B"))
    assert not [c for c, _ in calls if c[:3] == ["git", "worktree", "remove"]]


def test_finalize_failure_before_merge_blocks_leaf_but_merges_the_rest(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One leaf's finalize failure must not abort the batch: its worktree is kept (the only copy),
    # the leaf blocks, and the other leaf still merges normally.
    _provision(project)
    calls: list[tuple[list[str], str]] = []
    wt3 = str(Path(".worktrees") / "T-003")
    monkeypatch.setattr(build_loop, "_run", _failing_commit_run(calls, fail_cwd=wt3))
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    with pytest.raises(build_loop.StopLoop):
        orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    statuses = {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}
    assert statuses["T-002"] == "done" and statuses["T-003"] == "blocked"
    merges = [c for c, _ in calls if c[:2] == ["git", "merge"]]
    assert merges == [["git", "merge", "--no-ff", "--no-edit", "build/demo-T-002"]]  # T-003 never merged
    # The startup pre-clean in _add_worktree may remove leftovers; after the failure there must be
    # no further removal of T-003's worktree.
    fail_at = calls.index((["git", "commit", "--no-verify", "-m", "T-003: leaf B"], wt3))
    assert not [c for c, _ in calls[fail_at:] if c[:3] == ["git", "worktree", "remove"] and c[3] == wt3]


# --- structured events emitted by the loop (the escalation log's truth) -----


def test_blocked_leaf_emits_event_and_refreshes_state_view(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A blocked leaf must land in events.ndjson (typed, aggregatable) and re-render the state.md view.
    state = _STATE.format(tasks="approved").replace(
        "# board", f"# board\n\n{events.VIEW_BEGIN}\n_(no events yet)_\n{events.VIEW_END}\n"
    )
    (project / ".agentloop" / "state.md").write_text(state, encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd, timeout=None: (0, ""))
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (False, "$ make test (rc=1)"))
    with pytest.raises(build_loop.StopLoop):
        orch._consume_parallel([_leaf("T-002", "leaf A")])
    recorded = events.load_events()
    assert [(e.event, e.task) for e in recorded] == [("blocked", "T-002")]
    assert "$ make test (rc=1)" in recorded[0].detail
    assert "T-002" in (project / ".agentloop" / "state.md").read_text(encoding="utf-8").split(events.VIEW_BEGIN)[1]


def test_done_tasks_emit_task_done_with_commit(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision(project)

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"] and "HEAD" in cmd:
            return 0, "abc123\n"
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    orch._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])
    recorded = events.load_events()
    assert [(e.event, e.task, e.commit) for e in recorded] == [("task_done", "T-001", "abc123")]


def test_step_fail_is_recorded_per_retry(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _steps_orch(project, monkeypatch)
    outcomes = iter([("test", "t red"), (None, "")])
    monkeypatch.setattr(orch, "_invoke_implementer", lambda task, cwd, log: None)
    monkeypatch.setattr(orch, "_run_pipeline", lambda task, cwd: next(outcomes))
    ok, _ = orch._run_task_to_done(_leaf("T-002", "leaf A"), cwd=".")
    assert ok is True
    recorded = events.load_events()
    assert [(e.event, e.task, e.step) for e in recorded] == [("step_fail", "T-002", "test")]


# --- implementer prompt scoping (Context: read only the relevant design section) --------


def test_implementer_prompt_scopes_to_task_requirement(project: Path) -> None:
    # When the task carries a req, the prompt points the implementer at that requirement's design
    # section (not the whole design doc) — keeping the subagent context lean.
    _provision(project)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=True)
    task = dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",), req="R-3")
    prompt = orch._implementer_prompt(task, failure_log="")
    assert "your requirement (R-3) in docs/20-design.md" in prompt


def test_implementer_prompt_points_at_baseline_when_present(project: Path) -> None:
    # An adopted (brownfield) repo has docs/05-current-state.md; the implementer must be told to
    # match the existing conventions and reuse the asset inventory.
    _provision(project)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=True)
    task = dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",))
    assert "05-current-state.md" not in orch._implementer_prompt(task, failure_log="")
    (project / "docs").mkdir()
    (project / "docs" / "05-current-state.md").write_text("# baseline\n", encoding="utf-8")
    assert "Consult docs/05-current-state.md" in orch._implementer_prompt(task, failure_log="")


def test_implementer_prompt_falls_back_to_whole_design_when_no_req(project: Path) -> None:
    _provision(project)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=True)
    task = dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",))  # req defaults to ""
    prompt = orch._implementer_prompt(task, failure_log="")
    assert "docs/tasks/T-002.md, docs/20-design.md, and the existing code" in prompt


# --- headless CLI configurability (build.headless.cmd) -----------------------


def _headless_config(cmd_yaml: str) -> str:
    return _CONFIG.replace("build:\n", f"build:\n  headless: {{cmd: {cmd_yaml}}}\n")


def test_config_headless_cmd_default_and_custom(project: Path) -> None:
    assert build_loop.Config.load().headless_cmd == ("claude", "-p")
    (project / ".agentloop" / "config.yaml").write_text(_headless_config('["codex", "exec"]'), encoding="utf-8")
    assert build_loop.Config.load().headless_cmd == ("codex", "exec")


def test_config_rejects_invalid_headless_cmd(project: Path) -> None:
    for bad in ("[]", '"claude -p"', "[claude, 3]", '["claude", ""]'):
        (project / ".agentloop" / "config.yaml").write_text(_headless_config(bad), encoding="utf-8")
        with pytest.raises(ValueError, match="headless.cmd"):
            build_loop.Config.load()


def test_invoke_implementer_launches_configured_headless_cmd(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / ".agentloop" / "config.yaml").write_text(_headless_config('["codex", "exec"]'), encoding="utf-8")
    _provision(project)
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        seen.append(cmd)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    orch._invoke_implementer(_leaf("T-002", "leaf A"), cwd=".", failure_log="")
    assert seen and seen[0][:2] == ["codex", "exec"]
    assert "T-002" in seen[0][-1]  # the prompt is appended as the last argument


# --- merge/finalize-stage gate check (out-of-scope edits must not land) ------


def test_parallel_gate_violation_blocks_leaf_and_skips_merge(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A leaf branch touching a pending-gate path (docs/test/** needs gates.build) must not merge:
    # in-worktree commits and the --no-verify finalize escape the commit-stage guard, and once
    # merged into work's HEAD the diff-vs-HEAD check could never see the violation again.
    _provision(project)
    calls: list[tuple[list[str], str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append((cmd, cwd))
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return (0, "docs/test/results.md\n") if cmd[3].endswith("T-003") else (0, "unguarded.py\n")
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    with pytest.raises(build_loop.StopLoop):
        orch._consume_parallel([_leaf("T-002", "leaf A"), _leaf("T-003", "leaf B")])
    statuses = {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}
    assert statuses["T-002"] == "done" and statuses["T-003"] == "blocked"
    merges = [c for c, _ in calls if c[:2] == ["git", "merge"]]
    assert merges == [["git", "merge", "--no-ff", "--no-edit", "build/demo-T-002"]]  # violator never merged
    violations = [e for e in events.load_events() if e.event == "gate_violation"]
    assert violations and "docs/test/results.md" in violations[0].detail
    # The cleanup keeps the branch for human review: no `git branch -D` after the violation
    # (the only -D is _add_worktree's pre-clean at batch start).
    fail_at = calls.index((["git", "diff", "--name-only", "build/demo...build/demo-T-003"], "."))
    assert not [c for c, _ in calls[fail_at:] if c[:3] == ["git", "branch", "-D"]]


def test_serial_gate_violation_blocks_task_and_stops(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A serial task commits straight to the work branch, where a commit already in HEAD escapes
    # the commit-stage guard — the loop itself must re-check what the task changed and stop loudly.
    _provision(project)

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "abc1234\n"
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return 0, "docs/test/results.md\n"
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    with pytest.raises(build_loop.StopLoop, match="gate-guarded"):
        orch._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-001"] == "blocked"
    violations = [e for e in events.load_events() if e.event == "gate_violation"]
    assert violations and "docs/test/results.md" in violations[0].detail


def test_serial_unguarded_changes_pass_the_gate_check(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The check must not false-positive on ordinary implementation paths.
    _provision(project)

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "abc1234\n"
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return 0, "unguarded.py\n"
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    orch._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-001"] == "done"
