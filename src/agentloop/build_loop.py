"""The deterministic orchestrator for the implementation phase (the engine driving /build).

It runs the scheduling control flow (which tasks, at what parallelism, in what merge order, and when to stop)
deterministically **in code, not a prompt**. Each task's implementation code content itself is non-deterministic
because an LLM writes it (the implementer launched via the configured headless agent CLI,
`build.headless.cmd` — `claude -p` by default), but

  - frontier computation / consumption order / max parallelism / worktree isolation / merge order
  - the pass/fail decision of each quality-gate step (config `quality_gate.steps`) by exit code
  - the per-step retry budget / blocked decision / stop condition / prerequisite-gate check

are all decided deterministically by this script. `.agentloop/config.yaml` is the single source of knobs,
and its `quality_gate.steps` is the single definition of the DoD (AGENTS.md and /build refer here).

The determinism boundary:
  - Deterministic (here): control flow, parallelism, merge, cmd-step gate decisions, stopping.
  - Non-deterministic (LLM): implementation code, and the review/simplify agent step's fixes
    → absorbed by "re-run the preceding cmd steps after an agent step; retry until green, else blocked".

This script **does not set gates.build to approved** (only the human opens a gate).
After all tasks are done it prints a summary and stops, leaving it to the human's approval (/build's gate ④).

Usage:
  agentloop build            # run
  agentloop build --dry-run  # check the control flow without calling the agent CLI/git

--dry-run is strictly read-only: task statuses advance only in an in-memory overlay, and no SSOT
file, event log, or lock file is written — running it never changes what a later real run sees.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from agentloop import build_git, build_prompts, common, dag, events, gate_guard
from agentloop import repo as repo_mod

logger = logging.getLogger(__name__)

# Single definitions live in common.py; the old names stay importable from here.
STATE_PATH = common.STATE_PATH
CONFIG_PATH = common.CONFIG_PATH
TASKS_PATH = common.TASKS_PATH
LOCK_PATH = ".agentloop/build-loop.lock"
# The post-build security-review report. Under .agentloop/ (not docs/test/) deliberately: the
# review runs BEFORE gate ④ is approved, and gate_guard denies docs/test/** writes until then.
SECURITY_REVIEW_PATH = ".agentloop/security-review.md"


StopLoop = common.StopLoop


@dataclass(frozen=True)
class GateStep:
    """One quality-gate step.

    kind="cmd"   — run `run` and decide deterministically by exit code. `retries` is that step's
                   own budget for sending a failure back to the implementer (empty `run` = skip).
    kind="agent" — a headless review+simplify pass (the configured headless CLI,
                   build.headless.cmd) that fixes findings in place.
                   Its content is non-deterministic, so the pipeline re-runs the cmd steps that
                   already passed whenever it changed the tree.

    `required` (cmd only): an empty `run` is normally a silent skip — fine for a library, but for
    a runnable deliverable a forgotten smoke command lets the whole build finish without ever
    launching the thing. Marking the step required makes the loop refuse to start until `run` is
    filled (fail-fast, before any implementer is paid for).
    """

    name: str
    kind: str
    run: str = ""
    retries: int = 2
    required: bool = False


def _parse_steps(qg: Any) -> tuple[GateStep, ...]:
    """Parse quality_gate.steps — the required, single definition of the DoD.

    (The pre-0.3.0 legacy form — quality_gate.test_cmd/check_cmd + build.retries — was removed;
    a config still carrying it fails here with a migration hint, and doctor flags the stale keys.)
    """
    raw = qg.get("steps")
    if not raw:
        raise ValueError(
            "quality_gate.steps is missing — define the DoD as a steps list in .agentloop/config.yaml "
            "(the legacy test_cmd/check_cmd + retries form was removed in 0.3.0; "
            "see the template config.yaml or .agentloop/schema/config.schema.json)"
        )
    if not isinstance(raw, list):
        raise ValueError("quality_gate.steps must be a list")
    steps: list[GateStep] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"quality_gate.steps[{i}] must be a mapping")
        kind = str(entry.get("kind", "cmd"))
        if kind not in ("cmd", "agent"):
            raise ValueError(f"quality_gate.steps[{i}]: unknown kind {kind!r} (expected cmd | agent)")
        steps.append(
            GateStep(
                name=str(entry.get("name", f"step{i}")),
                kind=kind,
                run=str(entry.get("run") or ""),
                retries=max(0, int(entry.get("retries", 2))),
                required=bool(entry.get("required", False)),
            )
        )
    return tuple(steps)


def _timeout_sec(value: Any, default: int) -> float | None:
    """Normalize a timeouts knob: seconds as a positive float, or None (= no timeout) for 0/negative."""
    sec = float(default if value is None else value)
    return sec if sec > 0 else None


def _parse_headless(build: Any) -> tuple[str, ...]:
    """build.headless.cmd — the headless agent CLI; the prompt is appended as the last argument.

    Mode A is agent-CLI-pluggable through this one knob: ["claude", "-p"] (the default),
    ["codex", "exec"], ["gemini", "-p"], … all launch the same prompts.
    """
    raw = (build.get("headless") or {}).get("cmd")
    if raw is None:
        return ("claude", "-p")
    if not isinstance(raw, list) or not raw or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ValueError('build.headless.cmd must be a non-empty list of strings, e.g. ["claude", "-p"]')
    return tuple(x.strip() for x in raw)


@dataclass
class Config:
    max_parallel: int
    worktree_enabled: bool
    worktree_dir: str
    branch_pattern: str
    steps: tuple[GateStep, ...]
    agent_steps: bool
    integration_gate: bool = True
    security_review: bool = True
    timeout_cmd: float | None = 1800.0
    timeout_agent: float | None = 3600.0
    headless_cmd: tuple[str, ...] = ("claude", "-p")

    @property
    def gate_cmds(self) -> list[str]:
        """The deterministic commands of the gate (for prompts / display)."""
        return [s.run for s in self.steps if s.kind == "cmd" and s.run]

    # The config.yaml schema version this parser understands (see data/schema/config.schema.json).
    SCHEMA_VERSION = 1

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> Config:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        declared = data.get("schema_version")
        if isinstance(declared, int) and declared > cls.SCHEMA_VERSION:
            raise ValueError(
                f"config.yaml declares schema_version {declared} but this agentloop understands "
                f"{cls.SCHEMA_VERSION} — upgrade the tool (`uv tool upgrade agentloop`)"
            )
        build = data.get("build") or {}
        wt = build.get("worktree") or {}
        qg = build.get("quality_gate") or {}
        tm = build.get("timeouts") or {}
        pb = build.get("post_build") or {}
        return cls(
            max_parallel=max(1, int(build.get("max_parallel", 3))),
            worktree_enabled=bool(wt.get("enabled", True)),
            worktree_dir=str(wt.get("dir", ".worktrees")),
            # `-` (not `/`) between branch and task: git forbids a branch that is a path-prefix of
            # another ref ("work" + "work/T-001" cannot coexist), so a slash pattern always fails.
            branch_pattern=str(wt.get("branch_pattern", "{branch}-{task_id}")),
            steps=_parse_steps(qg),
            agent_steps=bool(qg.get("agent_steps", True)),
            integration_gate=bool(qg.get("integration_gate", True)),
            security_review=bool(pb.get("security_review", True)),
            timeout_cmd=_timeout_sec(tm.get("cmd_sec"), 1800),
            timeout_agent=_timeout_sec(tm.get("agent_sec"), 3600),
            headless_cmd=_parse_headless(build),
        )


# --- reading/writing state.md / tasks.yaml ---------------------------------


# The single parser lives in common.py (fail-open posture: {} on a structurally absent block).
read_frontmatter = common.read_frontmatter


def work_branch(front: dict[str, object], root: str = ".") -> str:
    branch = front.get("branch")
    if isinstance(branch, str) and branch and not branch.startswith("<"):
        return branch
    # If state.md is not filled in, use the current branch.
    rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    return out.strip() if rc == 0 else "HEAD"


# The pointer header of tasks.yaml. The shipped scaffold starts with exactly these lines, so the
# round-trip rewrite below is lossless — keep the file pure data + this pointer (schema detail
# lives in .agentloop/prompts/commands/tasks.md, not in comments a rewrite would destroy).
TASKS_HEADER = (
    "# yaml-language-server: $schema=schema/tasks.schema.json\n"
    "# .agentloop/tasks.yaml — machine-readable SSOT of the task graph (DAG) (build_loop updates status)\n"
    "# schema (id/title/kind/blockedBy/status/test/req/phase): see .agentloop/prompts/commands/tasks.md / AGENTS.md\n"
)


def set_task_status(task_id: str, status: str, tasks_path: str = TASKS_PATH) -> None:
    """Update one task's status in tasks.yaml and write it back (pure data + pointer header)."""
    data = yaml.safe_load(Path(tasks_path).read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") or []
    for t in tasks:
        if str(t.get("id")) == task_id:
            t["status"] = status
            break
    Path(tasks_path).write_text(
        TASKS_HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 = existence probe only
    except ProcessLookupError:
        return False
    except OSError:  # e.g. EPERM: exists but owned by someone else
        return True
    return True


def acquire_lock(path: str = LOCK_PATH) -> bool:
    """Take the single-run lock (a PID file). False = another live run holds it.

    Two concurrent loops would race the whole-file tasks.yaml rewrites and collide on the same
    worktree paths. A lock whose PID is no longer alive (a crashed run) is reclaimed automatically,
    so no manual cleanup is needed after an interruption.
    """
    p = Path(path)
    try:
        pid = int(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if pid > 0 and pid != os.getpid() and _pid_alive(pid):
        return False
    p.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock(path: str = LOCK_PATH) -> None:
    Path(path).unlink(missing_ok=True)


DAG_VIEW_BEGIN = "<!-- DAG-VIEW:BEGIN -->"
DAG_VIEW_END = "<!-- DAG-VIEW:END -->"


def update_state_view(graph: dag.Graph, path: str = STATE_PATH) -> bool:
    """Refresh state.md's generated DAG view block (between the DAG-VIEW markers) and bump updated_at.

    tasks.yaml stays the SSOT; this only re-renders the human-facing view so the board does not go
    stale while the deterministic loop runs (a human pastes the same render output in mode B).
    No markers (a hand-restructured state.md) or unreadable file = no-op, never an abort.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    begin = text.find(DAG_VIEW_BEGIN)
    end = text.find(DAG_VIEW_END)
    if begin == -1 or end == -1 or end < begin:
        return False
    new = text[: begin + len(DAG_VIEW_BEGIN)] + "\n" + dag.render(graph) + "\n" + text[end:]
    new = re.sub(r"^(\s*updated_at:\s*).*$", rf'\g<1>"{date.today().isoformat()}"', new, count=1, flags=re.MULTILINE)
    Path(path).write_text(new, encoding="utf-8")
    return True


# Single definitions live in events.py / common.py; the old names stay importable from here.
log_escalation = events.log_escalation
summarize_failure = common.summarize_failure
_FAILURE_MAX_LINES = common._FAILURE_MAX_LINES


# --- subprocess -------------------------------------------------------------


# The implementation lives in common.run; the `_run` name stays because doctor/pr_draft call
# through it and the tests monkeypatch it here to fake git/agent-CLI results.
_run = common.run


def _late_run(cmd: list[str], cwd: str | None = None, timeout: float | None = None) -> tuple[int, str]:
    """Late-binding indirection to `_run`: resolved from this module's globals at call time, so a
    test patching build_loop._run reaches the injected GitWorkspace runner too — regardless of
    whether the patch lands before or after the Orchestrator is constructed."""
    return _run(cmd, cwd, timeout)


# --- scheduling (pure, under test) ------------------------------------------


def plan_batch(graph: dag.Graph, max_parallel: int) -> tuple[str, list[dag.Task]] | None:
    """Deterministically decide the next batch to start.

    Returns:
      ("serial", [one foundation task])       — foundation / high fan-out is finalized serially
      ("parallel", [leaf tasks, ≤max_parallel]) — independent leaves are launched in parallel in isolation
      None                                    — the frontier is empty
    """
    ordered = graph.order_frontier()
    if not ordered:
        return None
    foundations = [t for t in ordered if t.kind == "foundation"]
    if foundations:
        return ("serial", [foundations[0]])
    return ("parallel", ordered[:max_parallel])


# --- orchestrator body ------------------------------------------------------


class Orchestrator:
    def __init__(self, config: Config, dry_run: bool, repo: repo_mod.Repo | None = None) -> None:
        self.config = config
        self.dry_run = dry_run
        # The discovered repository anchors every path and git call below — the orchestrator
        # behaves identically no matter which directory it was launched from.
        self.repo = repo or repo_mod.get()
        self.root = str(self.repo.root)
        self.front = read_frontmatter(str(self.repo.state))
        self.branch = work_branch(self.front, self.root)
        # The git/worktree layer (build_git.py); the runner is late-bound through _run above.
        self.ws = build_git.GitWorkspace(
            self.repo,
            self.branch,
            dry_run=dry_run,
            worktree_dir=config.worktree_dir,
            branch_pattern=config.branch_pattern,
            run=_late_run,
        )
        # Dry-run status overlay: the simulated statuses live here instead of tasks.yaml, so the
        # loop can progress to completion while the run stays strictly read-only.
        self._sim_status: dict[str, str] = {}

    def _set_status(self, task_id: str, status: str) -> None:
        if self.dry_run:
            self._sim_status[task_id] = status
            print(f"    [dry-run] {task_id} → {status}")
            return
        set_task_status(task_id, status, tasks_path=str(self.repo.tasks))

    def _escalate(self, event: str, message: str, *, task: str = "") -> None:
        if self.dry_run:  # read-only: surface it on the console without touching the event log
            logger.warning(f"[escalation] {message}")
            return
        log_escalation(event, message, task=task, events_path=str(self.repo.events), state_path=str(self.repo.state))

    def _load_graph(self) -> dag.Graph:
        graph = dag.load(self.repo.tasks)
        if self.dry_run and self._sim_status:
            graph = dag.Graph.from_tasks([replace(t, status=self._sim_status.get(t.id, t.status)) for t in graph.tasks])
        return graph

    # -- implementer launch and quality gate --

    def _implementer_prompt(self, task: dag.Task, failure_log: str) -> str:
        return build_prompts.implementer_prompt(
            task,
            failure_log,
            gate_cmds=self.config.gate_cmds,
            has_baseline=self.repo.path("docs/05-current-state.md").exists(),
        )

    def _invoke_implementer(self, task: dag.Task, cwd: str, failure_log: str) -> None:
        if self.dry_run:
            print(f"    [dry-run] launch implementer (cwd={cwd}) task={task.id}")
            return
        rc, out = _run(
            [*self.config.headless_cmd, self._implementer_prompt(task, failure_log)],
            cwd=cwd,
            timeout=self.config.timeout_agent,
        )
        if rc != 0:
            raise StopLoop(f"{task.id}: failed to launch implementer (rc={rc})\n{out[-1000:]}")

    @property
    def _steps_effective(self) -> tuple[GateStep, ...]:
        """The gate steps actually run (agent steps drop out when quality_gate.agent_steps is false)."""
        if self.config.agent_steps:
            return self.config.steps
        return tuple(s for s in self.config.steps if s.kind == "cmd")

    def _steps_for(self, task: dag.Task) -> tuple[GateStep, ...]:
        """The gate steps for one task: the task's own `test` command first, then the configured DoD.

        tasks.yaml's per-task `test` (the ticket's automated-test approach — what /tasks recorded
        as this task's green decision) runs as a focused cmd step ahead of the shared pipeline:
        it fails faster and summarizes tighter than the whole suite. It is skipped when it
        duplicates a configured cmd step's `run` (the default "make test" case), so nothing runs
        twice. Budget: the `test` step's retries (the task command is its focused stand-in)."""
        steps = self._steps_effective
        run = task.test.strip()
        if not run or any(s.kind == "cmd" and s.run.strip() == run for s in self.config.steps):
            return steps
        retries = next((s.retries for s in self.config.steps if s.kind == "cmd" and s.name == "test"), 2)
        return (GateStep(name="task-test", kind="cmd", run=run, retries=retries), *steps)

    def _review_scope(self, task: dag.Task, cwd: str, base: str) -> tuple[list[str], str]:
        """The changed-path list + exact diff command that scope the review step's read.

        Computed fresh at review time (the tree moves between retries). A leaf worktree's scope
        is everything since it forked off the work branch; a serial task's is the commits since
        `base` (the pre-task HEAD) plus the dirty tree. No base on the work branch (dry-run,
        or a caller without one) degrades to the unscoped prompt.
        """
        if self.dry_run:
            return [], ""
        if cwd != self.root:
            return self.ws.branch_changed_paths(self.ws.branch_for(task.id)), f"git diff {self.branch}...HEAD"
        if base:
            return self.ws.changed_since(base), f"git diff {base[:12]}..HEAD"
        return [], ""

    def _review_prompt(self, task: dag.Task, cwd: str, base: str) -> str:
        changed, diff_cmd = self._review_scope(task, cwd, base)
        return build_prompts.review_prompt(
            task, gate_cmds=self.config.gate_cmds, changed_paths=changed, diff_cmd=diff_cmd
        )

    def _tree_state(self, cwd: str) -> tuple[str, str]:
        return self.ws.tree_state(cwd)

    def _run_agent_step(self, task: dag.Task, cwd: str, base: str) -> bool:
        """Run the review+simplify agent step headless. Returns True if it changed the tree."""
        before = self._tree_state(cwd)
        rc, out = _run(
            [*self.config.headless_cmd, self._review_prompt(task, cwd, base)],
            cwd=cwd,
            timeout=self.config.timeout_agent,
        )
        if rc != 0:
            raise StopLoop(f"{task.id}: failed to launch the review agent step (rc={rc})\n{out[-1000:]}")
        return self._tree_state(cwd) != before

    def _run_cmd_step(self, step: GateStep, cwd: str) -> str:
        """Run one cmd step. Returns "" on pass, a compact failure summary otherwise.

        shlex-split so quoted arguments work (e.g. `pytest -k 'a b'`). Still no shell: pipes and
        redirections don't work in a step's `run` — wrap those in a make target or script.
        """
        rc, out = _run(shlex.split(step.run), cwd=cwd, timeout=self.config.timeout_cmd)
        return "" if rc == 0 else summarize_failure(step.run, rc, out)

    def _run_pipeline(self, task: dag.Task, cwd: str, base: str = "") -> tuple[str | None, str]:
        """Run the quality-gate steps (config quality_gate.steps = the DoD) in order.

        Returns (failed_step_name, failure_summary), or (None, "") when every step passed.
        An agent step's fixes invalidate the evidence of the cmd steps that already passed,
        so those are re-run whenever it changed the tree (deterministic re-verification).
        """
        steps = self._steps_for(task)
        if self.dry_run:
            shown = " → ".join(f"{s.name}({s.kind})" for s in steps)
            print(f"    [dry-run] quality gate: {shown} (cwd={cwd})")
            return None, ""
        passed: list[GateStep] = []
        for step in steps:
            if step.kind == "agent":
                if self._run_agent_step(task, cwd, base):
                    for prev in passed:
                        failure = self._run_cmd_step(prev, cwd)
                        if failure:
                            return prev.name, failure
                continue
            if not step.run:
                print(f"    [gate] skip {step.name}: no command configured")
                continue
            failure = self._run_cmd_step(step, cwd)
            if failure:
                return step.name, failure
            passed.append(step)
        return None, ""

    def _run_task_to_done(self, task: dag.Task, cwd: str, base: str = "") -> tuple[bool, str]:
        """Take one task to done via implementer implementation + the quality-gate pipeline.

        Each cmd step carries its own send-back budget (step.retries); a failure consumes only
        that step's budget. Returns (ok, log); ok=False means some step's budget ran out
        (the caller marks the task blocked).
        """
        budgets = {s.name: s.retries for s in self._steps_for(task) if s.kind == "cmd"}
        failure_log = ""
        while True:
            self._invoke_implementer(task, cwd, failure_log)
            failed, failure_log = self._run_pipeline(task, cwd, base)
            if failed is None:
                return True, ""
            left = budgets.get(failed, 0)
            print(f"    quality gate fail at step '{failed}' (retries left: {left}): {task.id}")
            if not self.dry_run:  # unreachable in dry-run today (the dry pipeline always passes); keep read-only anyway
                events.append_event("step_fail", task=task.id, step=failed, detail=f"retries left: {left}")
            if left <= 0:
                return False, failure_log
            budgets[failed] = left - 1

    # -- post-merge integration gate --

    def _integration_fix_prompt(self, ids: str, failure_log: str) -> str:
        return build_prompts.integration_fix_prompt(ids, failure_log, gate_cmds=self.config.gate_cmds)

    def _invoke_integration_fixer(self, ids: str, failure_log: str) -> None:
        rc, out = _run(
            [*self.config.headless_cmd, self._integration_fix_prompt(ids, failure_log)],
            cwd=self.root,
            timeout=self.config.timeout_agent,
        )
        if rc != 0:
            raise StopLoop(f"{ids}: failed to launch the integration fixer (rc={rc})\n{out[-1000:]}")

    def _integration_gate(self, tasks: list[dag.Task]) -> tuple[bool, str]:
        """Re-verify the merged/integrated state of the work branch after a multi-leaf join.

        Each leaf passed the gate only in its own isolated worktree; the *combined* file set can
        still be red (a lint/type error only the whole tree surfaces, a format reflow another
        task's change triggers). One batch-level re-run of the deterministic cmd steps catches
        that before the merged tasks are marked done. Cost control: the caller runs this only
        when 2+ leaves merged — a single-leaf join leaves the work tree identical to the one
        already verified in that leaf's worktree (leaves branch from the batch's common base and
        work advances only by this batch's merges), so re-running would prove nothing new.

        On red, a headless fixer runs on the work branch within each step's own retries budget
        (the same deterministic pattern as _run_task_to_done). Returns (ok, last_failure).
        """
        ids = ",".join(t.id for t in tasks)
        if self.dry_run:
            print(f"    [dry-run] integration gate on work after merging {ids}")
            return True, ""
        budgets = {s.name: s.retries for s in self.config.steps if s.kind == "cmd"}
        while True:
            failed, failure_log = None, ""
            for step in self._steps_effective:
                if step.kind != "cmd" or not step.run:
                    continue
                failure = self._run_cmd_step(step, cwd=self.root)
                if failure:
                    failed, failure_log = step.name, failure
                    break
            if failed is None:
                return True, ""
            left = budgets.get(failed, 0)
            print(f"    integration gate fail at step '{failed}' (retries left: {left}): {ids}")
            events.append_event("step_fail", task=ids, step=failed, detail=f"integration; retries left: {left}")
            if left <= 0:
                return False, failure_log
            budgets[failed] = left - 1
            self._invoke_integration_fixer(ids, failure_log)

    # -- worktree / merge --

    def _git(self, args: list[str], cwd: str | None = None) -> None:
        self.ws.git(args, cwd)

    def _branch_for(self, task: dag.Task) -> str:
        return self.ws.branch_for(task.id)

    def _worktree_path(self, task: dag.Task) -> str:
        return self.ws.worktree_path(task.id)

    def _add_worktree(self, task: dag.Task) -> str:
        return self.ws.add_worktree(task.id)

    def _safe_run_task(self, task: dag.Task, cwd: str) -> tuple[bool, str]:
        """Call _run_task_to_done safely from a thread. Convert exceptions to (False, log).

        So that one leaf's failure (e.g. implementer launch failure) does not drag down the whole parallel batch and
        leave other tasks stuck in in_progress, deadlocking.
        """
        try:
            return self._run_task_to_done(task, cwd=cwd)
        except StopLoop as exc:
            return False, str(exc)

    def _finalize_commit(self, cwd: str, message: str) -> bool:
        return self.ws.finalize_commit(cwd, message)

    def _gate_violations(self, paths: list[str]) -> list[tuple[str, str]]:
        """Gate-guard verdict for each path; [(path, deny reason)] for the denied ones.

        The merge/finalize-stage twin of gate_guard's edit-time and commit-stage checkpoints.
        Preservation commits run --no-verify and an implementer may commit with hooks absent or
        bypassed, and once a commit reaches the work branch the commit-stage `--check-diff`
        (a diff vs HEAD) can never see it again — so what a task actually changed is re-checked
        in code here, before it lands. template_mode / enforce_hook short-circuit inside
        evaluate() exactly as they do for the other checkpoints.
        """
        verdicts = ((p, gate_guard.evaluate(str(self.repo.path(p)), self.repo)) for p in paths)
        return [(p, reason) for p, (ok, reason) in verdicts if not ok]

    def _branch_changed_paths(self, branch: str) -> list[str]:
        return self.ws.branch_changed_paths(branch)

    def _changed_since(self, base: str) -> list[str]:
        return self.ws.changed_since(base)

    def _escalate_gate_violation(self, task_id: str, where: str, violations: list[tuple[str, str]]) -> None:
        listing = "\n".join(f"  {p} — {reason}" for p, reason in violations)
        self._escalate(
            "gate_violation",
            f"{task_id}: {where} touches gate-guarded paths whose prerequisite gate is pending — "
            f"the task is blocked for human review (gate rule 3: never land next-phase edits silently).\n{listing}",
            task=task_id,
        )

    def _cleanup_worktree(self, task: dag.Task) -> None:
        self.ws.cleanup_worktree(task.id)

    def merge_leaf(self, task: dag.Task, branch: str) -> bool:
        return self.ws.merge_leaf(task.id, branch)

    def _log_task_done(self, task: dag.Task) -> None:
        """Record a task_done event carrying the work-branch commit that finalized the task.

        The commit hash is what lets the log answer "which commit closed T-NNN" later (and, for a
        resolved escalation, "which commit fixed it") without digging through git history by hand.
        """
        if self.dry_run:
            return
        events.append_event("task_done", task=task.id, commit=self.ws.head(), path=str(self.repo.events))

    # -- main loop --

    def _recover_in_progress(self) -> None:
        """Reset tasks left in in_progress from a previous interruption back to todo (crash recovery).

        Since the frontier only picks status==todo, re-running with in_progress left over would mean
        that task is never started and the loop deadlocks. Roll back once at startup.
        """
        try:
            graph = dag.load(self.repo.tasks)
        except (OSError, dag.DagError, yaml.YAMLError):
            return
        for t in graph.tasks:
            if t.status == "in_progress":
                self._set_status(t.id, "todo")
                print(f"  [recover] {t.id}: reset in_progress → todo (resuming from a previous interruption)")

    def run(self) -> int:
        project = self.front.get("project")
        if isinstance(project, str) and project.startswith("<"):
            logger.error(
                "state.md still carries the template placeholders. Run `agentloop init --name <product>` first."
            )
            return 2
        gates = self.front.get("gates") or {}
        if not (isinstance(gates, dict) and gates.get("tasks") == "approved"):
            logger.error("gates.tasks is not approved. Approve /tasks first.")
            return 2
        if not self.dry_run and self.branch in ("", "HEAD"):
            # work_branch falls back to "HEAD" when git is unavailable/detached; creating worktrees
            # or committing against that would land the work on an arbitrary base.
            logger.error(
                "cannot determine the work branch (git unavailable or detached HEAD) — "
                "fill `branch:` in state.md or check out the work branch first."
            )
            return 2
        if self.dry_run:
            return self._run_loop()  # read-only: no lock file either (and no contention to guard against)
        if not acquire_lock(str(self.repo.path(LOCK_PATH))):
            logger.error(
                f"another build-loop run appears to be active ({LOCK_PATH} holds a live PID). "
                "Wait for it to finish, or remove the lock file if you are sure it is gone."
            )
            return 2
        try:
            return self._run_loop()
        finally:
            release_lock(str(self.repo.path(LOCK_PATH)))

    def _run_loop(self) -> int:
        # Fail fast on a contradictory DoD: a step marked required with no command would otherwise
        # be silently skipped every task and only be noticed (if at all) at gate ④ — after the
        # whole build was paid for. Refuse before consuming anything.
        unrunnable = [s.name for s in self.config.steps if s.kind == "cmd" and s.required and not s.run.strip()]
        if unrunnable:
            logger.error(
                f"quality_gate step(s) marked `required: true` have no command: {', '.join(unrunnable)}. "
                "Fill `run` in .agentloop/config.yaml (or drop `required`) before running the build loop."
            )
            return 2
        if not self.dry_run:
            # keep the append-only event log lean before appending this run's entries
            events.rotate_if_large(str(self.repo.events))
        self._recover_in_progress()
        while True:
            graph = self._load_graph()
            if not self.dry_run:
                # keep state.md's human-facing board fresh each iteration
                update_state_view(graph, str(self.repo.state))
                events.refresh_state_view(str(self.repo.events), str(self.repo.state))
            counts = graph.counts()
            unfinished = len(graph.tasks) - counts["done"]
            if unfinished == 0:
                return self._present_gate4(graph, self._post_build_security_review())

            batch = plan_batch(graph, self.config.max_parallel)
            if batch is None:
                # frontier empty & there are unfinished ones = all blocked/needs-revision. To the human.
                blocked = [t.id for t in graph.tasks if t.status in ("blocked", "needs-revision")]
                self._escalate(
                    "no_runnable",
                    f"No runnable tasks and {unfinished} unfinished ({', '.join(blocked)}). Help needed.",
                )
                return 1

            mode, tasks = batch
            print(f"[batch] mode={mode} tasks={[t.id for t in tasks]}")
            try:
                if mode == "serial" or not self.config.worktree_enabled:
                    self._consume_serial(tasks)
                else:
                    self._consume_parallel(tasks)
            except StopLoop as exc:
                if not self.dry_run:
                    try:  # leave the board reflecting the batch's blocked/done statuses before stopping
                        update_state_view(dag.load(self.repo.tasks), str(self.repo.state))
                    except (OSError, dag.DagError, yaml.YAMLError) as view_exc:
                        # Cosmetic only (tasks.yaml stays the truth), but say so — a silently
                        # stale board would misdirect the human the escalation is aimed at.
                        logger.warning(f"[warn] could not refresh the state.md board: {view_exc}")
                logger.error(str(exc))
                return exc.code
            # Recompute at the top of the loop after each batch (reassemble the chain).

    def _consume_serial(self, tasks: list[dag.Task]) -> None:
        """Finalize foundation tasks etc. serially on the work branch."""
        for task in tasks:
            self._set_status(task.id, "in_progress")
            print(f"  [serial] {task.id} {task.title}")
            pre_head = "" if self.dry_run else self.ws.head()
            ok, log = self._run_task_to_done(task, cwd=self.root, base=pre_head)
            if not ok:
                self._set_status(task.id, "blocked")
                self._escalate(
                    "blocked",
                    f"{task.id}: could not pass the quality gate within the limit; blocked.\n{log}",
                    task=task.id,
                )
                raise StopLoop(f"{task.id} is blocked. Human intervention needed.", code=1)
            # A serial task lands directly on the work branch (its own commits plus the finalize
            # below), where --no-verify and already-in-HEAD commits both escape the commit-stage
            # guard — so re-check everything the task changed before accepting it as done.
            if not self.dry_run and pre_head:
                violations = self._gate_violations(self._changed_since(pre_head))
                if violations:
                    self._set_status(task.id, "blocked")
                    self._escalate_gate_violation(task.id, "its work-branch changes", violations)
                    raise StopLoop(
                        f"{task.id}: changed gate-guarded paths while their gate is pending "
                        f"(commits since {pre_head[:12]} stay on the branch for review). "
                        "Human intervention needed.",
                        code=1,
                    )
            # Finalize the task diff only. The .agentloop/ orchestration state (tasks.yaml status, etc.)
            # is not included in the per-task commit (keeping one commit = one task). If the
            # implementer has not committed, this finalizes the diff (no-op otherwise).
            if not self._finalize_commit(self.root, f"{task.id}: {task.title}"):
                # The tree on the work branch keeps the diff, but the task must not be marked done
                # without its commit (one commit = one task is the record gate ④ reviews).
                self._set_status(task.id, "blocked")
                raise StopLoop(f"{task.id}: finalize commit failed on the work branch. Human intervention needed.")
            self._log_task_done(task)
            self._set_status(task.id, "done")

    def _consume_parallel(self, tasks: list[dag.Task]) -> None:
        """Implement independent leaves worktree-isolated up to max_parallel, then merge in ascending id order.

        Worktree creation is done serially on the main thread (avoiding .git index.lock contention);
        only the implementation is parallelized.
        """
        for task in tasks:
            self._set_status(task.id, "in_progress")
        # Worktree creation is serial (avoid git lock contention). The implementation is run in parallel after.
        branches = {task.id: self._add_worktree(task) for task in tasks}
        results: dict[str, tuple[bool, str]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.max_parallel)) as pool:
            futures = {pool.submit(self._safe_run_task, t, self._worktree_path(t)): t for t in tasks}
            for future, task in futures.items():
                results[task.id] = future.result()

        blocked_any = False
        merged: list[dag.Task] = []
        # Merge deterministically in ascending id order (sequential join).
        for task in sorted(tasks, key=lambda t: t.id):
            ok, log = results[task.id]
            if not ok:
                self._set_status(task.id, "blocked")
                self._escalate(
                    "blocked",
                    f"{task.id}: could not pass the quality gate within the limit; blocked.\n{log}",
                    task=task.id,
                )
                self._cleanup_worktree(task)  # the branch keeps the diff for inspection
                blocked_any = True
                continue
            # The leaf's full diff must be on its branch before the merge — an implementer that
            # forgot to commit would otherwise lose that work when the worktree is removed.
            if not self._finalize_commit(self._worktree_path(task), f"{task.id}: {task.title}"):
                # Keep the worktree (it may hold the only copy) and let the rest of the batch merge.
                self._set_status(task.id, "blocked")
                blocked_any = True
                continue
            # The leaf's commits were made in its worktree, where --no-verify (finalize) or a
            # bypassed hook can carry a gate violation; merging would bury it in the work branch's
            # HEAD where --check-diff never looks again. Check the branch's full diff first.
            if not self.dry_run:
                violations = self._gate_violations(self._branch_changed_paths(branches[task.id]))
                if violations:
                    self._set_status(task.id, "blocked")
                    self._escalate_gate_violation(task.id, f"leaf branch {branches[task.id]}", violations)
                    self._cleanup_worktree(task)  # not merged; the branch keeps the diff for review
                    blocked_any = True
                    continue
            if self.merge_leaf(task, branches[task.id]):
                merged.append(task)  # done is decided after the integration gate below
            else:
                self._set_status(task.id, "blocked")
                self._cleanup_worktree(task)  # conflict: aborted merge, worktree no longer needed
                blocked_any = True
        # Integration gate: only a join of 2+ leaves creates a combined tree nobody has verified
        # (a single-leaf join is byte-identical to that leaf's already-gated worktree state).
        if len(merged) >= 2 and self.config.integration_gate:
            ok, log = self._integration_gate(merged)
        else:
            ok, log = True, ""
        ids = ",".join(t.id for t in merged)
        if ok:
            for task in merged:
                self._set_status(task.id, "done")
                self._log_task_done(task)
        else:
            for task in merged:
                self._set_status(task.id, "blocked")
            self._escalate(
                "integration_red",
                f"{ids}: merged into work, but the integrated state fails the quality gate within the "
                f"limit. Fix the work branch, then set these tasks back to done.\n{log}",
                task=ids,
            )
            blocked_any = True
        if blocked_any:
            raise StopLoop("A blocked task occurred. Human intervention needed.", code=1)

    # -- post-build security review (binds the review to this build's deliverable) --

    def _reviewed_head(self) -> str:
        """The `Reviewed-HEAD:` hash recorded in the last security-review report ("" if none).

        This is the freshness/idempotence key: a re-invoked loop at the same HEAD must not pay
        for a second headless review, and a stale report (HEAD moved on) must not pass for a
        current one at gate ④.
        """
        try:
            text = self.repo.path(SECURITY_REVIEW_PATH).read_text(encoding="utf-8")
        except OSError:
            return ""
        m = re.search(r"^Reviewed-HEAD:\s*([0-9a-fA-F]+)", text, re.MULTILINE)
        return m.group(1) if m else ""

    def _security_review_prompt(self, head: str) -> str:
        return build_prompts.security_review_prompt(head, report_path=SECURITY_REVIEW_PATH)

    def _post_build_security_review(self) -> str:
        """Run the headless security review once per work-branch HEAD; return a gate-④ status line.

        Report-only by design (the reviewer must not fix code), written to SECURITY_REVIEW_PATH with
        the reviewed HEAD embedded, and recorded as a security_review event carrying that hash —
        binding "which state was reviewed" to the build's deliverable instead of leaving the review
        as an unrecorded conversational step. /verify still runs its own full review later.
        """
        if self.dry_run:
            return "[dry-run] security review not launched"
        if not self.config.security_review:
            return (
                "post-build security review is OFF (build.post_build.security_review: false) — "
                "run /security-review by hand before approving gate ④."
            )
        head = self.ws.head()
        if head and self._reviewed_head() == head:
            return f"already reviewed at current HEAD — report: {SECURITY_REVIEW_PATH} (Reviewed-HEAD {head[:12]})"
        rc, out = _run(
            [*self.config.headless_cmd, self._security_review_prompt(head)],
            cwd=self.root,
            timeout=self.config.timeout_agent,
        )
        if rc != 0:
            return (
                f"security-review launch FAILED (rc={rc}) — run /security-review by hand before gate ④.\n{out[-500:]}"
            )
        if self._reviewed_head() != head:
            return (
                f"security review ran but {SECURITY_REVIEW_PATH} does not record Reviewed-HEAD {head[:12]} — "
                "treat it as not done; run /security-review by hand before gate ④."
            )
        events.append_event("security_review", commit=head, detail=SECURITY_REVIEW_PATH, path=str(self.repo.events))
        # this event lands after the loop-top refresh; keep the view current
        events.refresh_state_view(str(self.repo.events), str(self.repo.state))
        return f"report written: {SECURITY_REVIEW_PATH} (Reviewed-HEAD {head[:12]})"

    def _present_gate4(self, graph: dag.Graph, security_note: str) -> int:
        print("\n========== all tasks done (gate ④) ==========")
        print(dag.render(graph))
        skipped = [s.name for s in self.config.steps if s.kind == "cmd" and not s.run.strip()]
        if skipped:
            # The empty-step nudge must reach the gate reviewer mechanically, not depend on the
            # lead remembering it: a silently skipped smoke means the DoD never launched the thing.
            print(
                f"\n  ! The DoD ran WITHOUT: {', '.join(skipped)} (no command configured). If the deliverable "
                "is runnable, fill `run` (and set `required: true`) in .agentloop/config.yaml."
            )
        print(
            "\nNext steps (human approval needed):\n"
            f"  1. Security review: {security_note}\n"
            "     Triage the report's findings; must-fix items go back to the implementer before gate ④.\n"
            "  2. Review the implementation summary and, if fine, approve at /build's gate ④.\n"
            "  * This script does not set gates.build to approved (only the human opens a gate)."
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the deterministic orchestrator for the implementation phase")
    parser.add_argument(
        "--dry-run", action="store_true", help="run only the control flow without calling the agent CLI/git"
    )
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        config = Config.load(str(repo.config))
    except (OSError, yaml.YAMLError, ValueError) as exc:
        logger.error(
            f"cannot load .agentloop/config.yaml: {exc} — fix it (`agentloop doctor` validates it against the schema)"
        )
        return 1
    if not args.dry_run and config.headless_cmd == ("claude", "-p"):
        print(
            '(hint) headless CLI = claude -p (the default). Switch it with `agentloop agent <codex|gemini|"custom">`.'
        )
    return Orchestrator(config, dry_run=args.dry_run, repo=repo).run()


if __name__ == "__main__":
    raise SystemExit(main())
