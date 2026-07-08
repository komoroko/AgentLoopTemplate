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
    "  worktree: {enabled: true, dir: .worktrees, branch_pattern: '{branch}/{task_id}'}\n"
    "  retries: {test_fix: 2, check_fix: 2}\n"
    "  quality_gate: {test_cmd: 'make test', check_cmd: 'make check'}\n"
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


# --- quality-gate pipeline (config-declared DoD) -----------------------------

_CONFIG_STEPS = (
    "build:\n"
    "  max_parallel: 3\n"
    "  worktree: {enabled: true, dir: .worktrees, branch_pattern: '{branch}/{task_id}'}\n"
    "  quality_gate:\n"
    "    agent_steps: true\n"
    "    steps:\n"
    "      - {name: test, kind: cmd, run: 'make test', retries: 1}\n"
    "      - {name: check, kind: cmd, run: 'make check', retries: 1}\n"
    "      - {name: review, kind: agent}\n"
    "      - {name: smoke, kind: cmd, run: '', retries: 1}\n"
    "gates:\n  enforce_hook: true\n"
)


def test_config_legacy_form_maps_to_two_cmd_steps(project: Path) -> None:
    # The pre-pipeline config form (test_cmd/check_cmd + retries) keeps its exact old behavior.
    config = build_loop.Config.load()
    assert [(s.name, s.kind, s.run, s.retries) for s in config.steps] == [
        ("test", "cmd", "make test", 2),
        ("check", "cmd", "make check", 2),
    ]
    assert config.gate_cmds == ["make test", "make check"]


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
    assert build_loop.main(["--dry-run"]) == 2


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
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    monkeypatch.setattr(orch, "_run_task_to_done", lambda task, cwd: (True, ""))
    foundation = dag.Task(id="T-001", title="base", kind="foundation")
    orch._consume_serial([foundation])
    assert ["git", "add", "-A", "--", ".", ":(exclude).agentloop"] in calls  # one commit = one task
    assert ["git", "commit", "-m", "T-001: base"] in calls
    assert {t.id: t.status for t in dag.load(".agentloop/tasks.yaml").tasks}["T-001"] == "done"


def test_merge_leaf_success_removes_worktree(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _provision(project)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch = build_loop.Orchestrator(build_loop.Config.load(), dry_run=False)
    assert orch.merge_leaf(_leaf("T-002", "leaf A"), "build/demo/T-002") is True
    assert ["git", "merge", "--no-ff", "--no-edit", "build/demo/T-002"] in calls
    assert ["git", "worktree", "remove", "--force", str(Path(".worktrees") / "T-002")] in calls


def test_run_escalates_when_all_unfinished_are_blocked(project: Path) -> None:
    blocked = _TASKS.replace("status: todo", "status: blocked")
    (project / ".agentloop" / "state.md").write_text(_STATE.format(tasks="approved"), encoding="utf-8")
    (project / ".agentloop" / "tasks.yaml").write_text(blocked, encoding="utf-8")
    assert build_loop.main(["--dry-run"]) == 1  # frontier empty + unfinished → escalate, stop
    recorded = events.load_events()  # the escalation lands as a structured event, not free text
    assert [e.event for e in recorded] == ["no_runnable"]
    assert "Help needed" in recorded[0].detail


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
