"""The deterministic orchestrator for the implementation phase (the engine driving /build).

It runs the scheduling control flow (which tasks, at what parallelism, in what merge order, and when to stop)
deterministically **in code, not a prompt**. Each task's implementation code content itself is non-deterministic
because an LLM writes it (the implementer launched headless with `claude -p`), but

  - frontier computation / consumption order / max parallelism / worktree isolation / merge order
  - the pass/fail decision of each quality-gate step (config `quality_gate.steps`) by exit code
  - the per-step retry budget / blocked decision / stop condition / prerequisite-gate check

are all decided deterministically by this script. `.agentloop/config.yaml` is the single source of knobs,
and its `quality_gate.steps` is the single definition of the DoD (CLAUDE.md and /build refer here).

The determinism boundary:
  - Deterministic (here): control flow, parallelism, merge, cmd-step gate decisions, stopping.
  - Non-deterministic (LLM): implementation code, and the review/simplify agent step's fixes
    → absorbed by "re-run the preceding cmd steps after an agent step; retry until green, else blocked".

This script **does not set gates.build to approved** (only the human opens a gate).
After all tasks are done it prints a summary and stops, leaving it to the human's approval (/build's gate ④).

Usage:
  uv run python scripts/agentloop/build_loop.py            # run
  uv run python scripts/agentloop/build_loop.py --dry-run  # check just the control flow without calling claude/git
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dag
import yaml

STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"
TASKS_PATH = ".agentloop/tasks.yaml"
LOG_PATH = ".agentloop/build-loop.log"
LOG_MAX_BYTES = 256 * 1024  # rotate the append-only build-loop.log past this (context hygiene; see rotate_log_if_large)


class StopLoop(Exception):
    """A cause to stop the loop and escalate to the human. `code` is the exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GateStep:
    """One quality-gate step.

    kind="cmd"   — run `run` and decide deterministically by exit code. `retries` is that step's
                   own budget for sending a failure back to the implementer (empty `run` = skip).
    kind="agent" — a headless review+simplify pass (`claude -p`) that fixes findings in place.
                   Its content is non-deterministic, so the pipeline re-runs the cmd steps that
                   already passed whenever it changed the tree.
    """

    name: str
    kind: str
    run: str = ""
    retries: int = 2


def _parse_steps(qg: Any, retries: Any) -> tuple[GateStep, ...]:
    """Parse quality_gate.steps; fall back to the legacy test_cmd/check_cmd + retries form.

    The legacy form maps to exactly the old behavior (two cmd steps, no agent step), so configs
    written before the pipeline keep working unchanged.
    """
    raw = qg.get("steps")
    if not raw:
        return (
            GateStep("test", "cmd", str(qg.get("test_cmd", "make test")), int(retries.get("test_fix", 2))),
            GateStep("check", "cmd", str(qg.get("check_cmd", "make check")), int(retries.get("check_fix", 2))),
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
            )
        )
    return tuple(steps)


@dataclass
class Config:
    max_parallel: int
    worktree_enabled: bool
    worktree_dir: str
    branch_pattern: str
    steps: tuple[GateStep, ...]
    agent_steps: bool

    @property
    def gate_cmds(self) -> list[str]:
        """The deterministic commands of the gate (for prompts / display)."""
        return [s.run for s in self.steps if s.kind == "cmd" and s.run]

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> Config:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        build = data.get("build") or {}
        wt = build.get("worktree") or {}
        qg = build.get("quality_gate") or {}
        return cls(
            max_parallel=max(1, int(build.get("max_parallel", 3))),
            worktree_enabled=bool(wt.get("enabled", True)),
            worktree_dir=str(wt.get("dir", ".worktrees")),
            branch_pattern=str(wt.get("branch_pattern", "{branch}/{task_id}")),
            steps=_parse_steps(qg, build.get("retries") or {}),
            agent_steps=bool(qg.get("agent_steps", True)),
        )


# --- reading/writing state.md / tasks.yaml ---------------------------------


def read_frontmatter(path: str = STATE_PATH) -> dict[str, object]:
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    loaded = yaml.safe_load(parts[1]) or {}
    return loaded if isinstance(loaded, dict) else {}


def work_branch(front: dict[str, object]) -> str:
    branch = front.get("branch")
    if isinstance(branch, str) and branch and not branch.startswith("<"):
        return branch
    # If state.md is not filled in, use the current branch.
    rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=".")
    return out.strip() if rc == 0 else "HEAD"


def set_task_status(task_id: str, status: str, tasks_path: str = TASKS_PATH) -> None:
    """Update one task's status in tasks.yaml and write it back (machine data, so round-trip is fine)."""
    data = yaml.safe_load(Path(tasks_path).read_text(encoding="utf-8")) or {}
    tasks = data.get("tasks") or []
    for t in tasks:
        if str(t.get("id")) == task_id:
            t["status"] = status
            break
    header = (
        "# .agentloop/tasks.yaml — machine-readable SSOT of the task graph (DAG) (build_loop updates status)\n"
        "# schema (id/title/kind/blockedBy/status/test/req/phase): see .claude/commands/tasks.md / CLAUDE.md\n"
    )
    Path(tasks_path).write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def log_escalation(message: str) -> None:
    with Path(LOG_PATH).open("a", encoding="utf-8") as fh:
        fh.write(message.rstrip() + "\n")
    print(f"[escalation] {message}", file=sys.stderr)


def rotate_log_if_large(path: str = LOG_PATH, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """Rotate an oversized append-only log to `<path>.1`, keeping a single generation.

    The escalation log is append-only across runs; left unbounded it bloats the context that both
    humans and agents re-read (Context Rot). Rotating one generation keeps the live log lean while
    preserving the immediately prior history. Returns True if a rotation happened.
    """
    p = Path(path)
    try:
        if p.stat().st_size <= max_bytes:
            return False
        p.replace(Path(f"{path}.1"))
    except OSError:
        return False  # no log yet / not statable / rename failed: best-effort, never abort the run
    return True


# --- subprocess -------------------------------------------------------------


def _run(cmd: list[str], cwd: str) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


# --- failure summarization (retry-friendly, token-lean) ---------------------
#
# make test / make check can emit huge output (full tracebacks, every passing test line). Feeding that
# raw into the implementer retry prompt / escalation log wastes tokens and buries the actionable lines,
# so we keep only the salient lines and cap the size — the retry and the human escalation both get a
# compact, actionable failure (retry-friendly error design) instead of a raw dump.

# Match genuine failure/error/diagnostic lines across the pytest/ruff/mypy default stack and the documented
# frontend (eslint/tsc), without pulling in passing-test noise. The markers are word-bounded so "error"/
# "…Error" inside an identifier (e.g. a passing "test_error_handling" or "test_raises_ValueError PASSED")
# is skipped, while a real exception line ("ValueError: msg") is kept via the colon-anchored branch.
_SALIENT_RE = re.compile(
    r"""
      ^E\s                                                  # pytest assertion / exception detail ("E   " prefixed)
    | ^=+.*\b(failed|error|passed|no\ tests\ ran)\b.*=+$    # pytest summary rule line
    | \bFAILED\b                                            # pytest failure marker (summary or verbose inline)
    | :\d+:\d+:\s                                           # ruff/mypy/eslint "file:line:col:" locations
    | \(\d+,\d+\):\s                                        # tsc "file(line,col):" locations
    | \berror\b                                             # error diagnostics (eslint/tsc/mypy), word-bounded
    | \b\w*(?:Error|Exception):                             # exception line "ValueError: ..." (colon skips test names)
    | ^Traceback\b                                          # traceback header
    """,
    re.IGNORECASE | re.VERBOSE,
)

_FAILURE_MAX_LINES = 40
_FAILURE_MAX_CHARS = 1500


def summarize_failure(cmd: str, rc: int, output: str) -> str:
    """Reduce a quality-gate command's raw output to a compact, salient failure summary.

    Keeps only the lines carrying the actionable signal (pytest FAILED / assertion lines, ruff/mypy
    error locations, exception markers); when nothing matches, falls back to the non-empty tail (the
    failure is usually last). Capped to a small line/char budget so retries and escalations stay
    token-lean. Pure and deterministic — unit-tested in test_build_loop.py.
    """
    header = f"$ {cmd} (rc={rc})"
    lines = output.splitlines()
    salient = [ln for ln in lines if _SALIENT_RE.search(ln)]
    if salient:
        kept, note = salient, "salient lines only"
    else:
        kept, note = [ln for ln in lines if ln.strip()], "tail"
    kept = kept[-_FAILURE_MAX_LINES:]
    # Char-budget guard for pathological long lines: drop whole leading lines first so the disclosed
    # omitted-count stays accurate, then keep the head of the remainder as a last resort (a single huge
    # line) — the head holds the actionable "file:line:col: error:" prefix, not the trailing message text.
    while len(kept) > 1 and len("\n".join(kept)) > _FAILURE_MAX_CHARS:
        kept = kept[1:]
    omitted = len(lines) - len(kept)
    body = "\n".join(kept)[:_FAILURE_MAX_CHARS]
    out_lines = [header]
    if body and omitted > 0:  # a bare "N omitted" with no body (e.g. whitespace-only output) would confuse
        out_lines.append(f"… ({omitted} line(s) omitted; kept {note})")
    if body:
        out_lines.append(body)
    return "\n".join(out_lines)


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
    def __init__(self, config: Config, dry_run: bool, claude_bin: str = "claude") -> None:
        self.config = config
        self.dry_run = dry_run
        self.claude_bin = claude_bin
        self.front = read_frontmatter()
        self.branch = work_branch(self.front)

    # -- implementer launch and quality gate --

    def _implementer_prompt(self, task: dag.Task, failure_log: str) -> str:
        # Point the implementer at the design section for this task's requirement rather than the whole
        # design doc: reading only the relevant slice keeps the subagent context lean and avoids
        # "Lost in the Middle" on a long design (see CLAUDE.md "Context budget"). Fall back to the whole
        # doc when the task has no req linkage.
        design_ref = (
            f"the design section(s) for your requirement ({task.req}) in docs/20-design.md"
            if task.req
            else "docs/20-design.md"
        )
        gate_list = " and ".join(f"`{c}`" for c in self.config.gate_cmds) or "the quality-gate commands"
        prompt = (
            f'You are the implementer subagent. Your only task is {task.id} "{task.title}".\n'
            f"Read docs/tasks/{task.id}.md, {design_ref}, and the existing code, and implement "
            "following the protocol in .claude/agents/implementer.md.\n"
            f"Write automated tests and get {gate_list} green.\n"
            "When done, commit your changes to this branch (excluding the orchestration state .agentloop/):\n"
            f"  git add -A -- . ':(exclude).agentloop' && git commit -m \"{task.id}: <summary>\"\n"
            "Do not reach outside scope (other tasks' territory). If you find a requirements/design defect, "
            "do not fix it on your own — report it."
        )
        if failure_log:
            # failure_log is already a compact summarize_failure() output (salient lines, budget-capped),
            # so it is passed through as-is — no crude tail-slicing that could cut the actionable lines.
            prompt += f"\n\nResolve the previous quality-gate failure:\n{failure_log}"
        return prompt

    def _invoke_implementer(self, task: dag.Task, cwd: str, failure_log: str) -> None:
        if self.dry_run:
            print(f"    [dry-run] launch implementer (cwd={cwd}) task={task.id}")
            return
        rc, out = _run([self.claude_bin, "-p", self._implementer_prompt(task, failure_log)], cwd=cwd)
        if rc != 0:
            raise StopLoop(f"{task.id}: failed to launch implementer (rc={rc})\n{out[-1000:]}")

    @property
    def _steps_effective(self) -> tuple[GateStep, ...]:
        """The gate steps actually run (agent steps drop out when quality_gate.agent_steps is false)."""
        if self.config.agent_steps:
            return self.config.steps
        return tuple(s for s in self.config.steps if s.kind == "cmd")

    def _review_prompt(self, task: dag.Task) -> str:
        cmds = ", ".join(f"`{c}`" for c in self.config.gate_cmds)
        return (
            f'You are the reviewer for task {task.id} "{task.title}" (the quality gate\'s agent step).\n'
            "Review this branch's changes for this task for correctness bugs (the /code-review discipline), "
            "then simplify: reuse existing code and remove needless complexity (the /simplify discipline). "
            "Apply the fixes directly.\n"
            "Stay within this task's scope; if you find a requirements/design defect, report it instead of fixing it.\n"
            f'If you change anything, commit with the "{task.id}: " prefix and keep {cmds} green.'
        )

    def _tree_state(self, cwd: str) -> tuple[str, str]:
        _, head = _run(["git", "rev-parse", "HEAD"], cwd=cwd)
        _, dirty = _run(["git", "status", "--porcelain"], cwd=cwd)
        return head.strip(), dirty.strip()

    def _run_agent_step(self, task: dag.Task, cwd: str) -> bool:
        """Run the review+simplify agent step headless. Returns True if it changed the tree."""
        before = self._tree_state(cwd)
        rc, out = _run([self.claude_bin, "-p", self._review_prompt(task)], cwd=cwd)
        if rc != 0:
            raise StopLoop(f"{task.id}: failed to launch the review agent step (rc={rc})\n{out[-1000:]}")
        return self._tree_state(cwd) != before

    def _run_cmd_step(self, step: GateStep, cwd: str) -> str:
        """Run one cmd step. Returns "" on pass, a compact failure summary otherwise."""
        rc, out = _run(step.run.split(), cwd=cwd)
        return "" if rc == 0 else summarize_failure(step.run, rc, out)

    def _run_pipeline(self, task: dag.Task, cwd: str) -> tuple[str | None, str]:
        """Run the quality-gate steps (config quality_gate.steps = the DoD) in order.

        Returns (failed_step_name, failure_summary), or (None, "") when every step passed.
        An agent step's fixes invalidate the evidence of the cmd steps that already passed,
        so those are re-run whenever it changed the tree (deterministic re-verification).
        """
        if self.dry_run:
            shown = " → ".join(f"{s.name}({s.kind})" for s in self._steps_effective)
            print(f"    [dry-run] quality gate: {shown} (cwd={cwd})")
            return None, ""
        passed: list[GateStep] = []
        for step in self._steps_effective:
            if step.kind == "agent":
                if self._run_agent_step(task, cwd):
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

    def _run_task_to_done(self, task: dag.Task, cwd: str) -> tuple[bool, str]:
        """Take one task to done via implementer implementation + the quality-gate pipeline.

        Each cmd step carries its own send-back budget (step.retries); a failure consumes only
        that step's budget. Returns (ok, log); ok=False means some step's budget ran out
        (the caller marks the task blocked).
        """
        budgets = {s.name: s.retries for s in self.config.steps if s.kind == "cmd"}
        failure_log = ""
        while True:
            self._invoke_implementer(task, cwd, failure_log)
            failed, failure_log = self._run_pipeline(task, cwd)
            if failed is None:
                return True, ""
            left = budgets.get(failed, 0)
            print(f"    quality gate fail at step '{failed}' (retries left: {left}): {task.id}")
            if left <= 0:
                return False, failure_log
            budgets[failed] = left - 1

    # -- worktree / merge --

    def _git(self, args: list[str], cwd: str = ".") -> None:
        if self.dry_run:
            print(f"    [dry-run] git {' '.join(args)} (cwd={cwd})")
            return
        rc, out = _run(["git", *args], cwd=cwd)
        if rc != 0:
            raise StopLoop(f"git {' '.join(args)} failed (rc={rc})\n{out[-1000:]}")

    def _branch_for(self, task: dag.Task) -> str:
        return self.config.branch_pattern.format(branch=self.branch, task_id=task.id)

    def _worktree_path(self, task: dag.Task) -> str:
        return str(Path(self.config.worktree_dir) / task.id)

    def _add_worktree(self, task: dag.Task) -> str:
        """Create a worktree for a leaf task and return the branch name. Clean up any existing one first.

        To avoid .git index.lock contention, worktree creation must be called **serially on the main thread**.
        """
        branch = self._branch_for(task)
        path = self._worktree_path(task)
        if not self.dry_run:
            # Clean up any worktree/branch left from a previous interruption (ignore if absent). Without this,
            # `git worktree add -b` would fail on a path/branch collision on re-run.
            _run(["git", "worktree", "remove", "--force", path], cwd=".")
            _run(["git", "branch", "-D", branch], cwd=".")
            _run(["git", "worktree", "prune"], cwd=".")
        self._git(["worktree", "add", "-b", branch, path, self.branch])
        return branch

    def _safe_run_task(self, task: dag.Task, cwd: str) -> tuple[bool, str]:
        """Call _run_task_to_done safely from a thread. Convert exceptions to (False, log).

        So that one leaf's failure (e.g. implementer launch failure) does not drag down the whole parallel batch and
        leave other tasks stuck in in_progress, deadlocking.
        """
        try:
            return self._run_task_to_done(task, cwd=cwd)
        except StopLoop as exc:
            return False, str(exc)

    def merge_leaf(self, task: dag.Task, branch: str) -> bool:
        """Merge a leaf branch into work and remove the worktree. On a conflict, abort and return False."""
        if self.dry_run:
            print(f"    [dry-run] git merge --no-ff {branch} → {self.branch}, remove worktree")
            return True
        rc, out = _run(["git", "merge", "--no-ff", "--no-edit", branch], cwd=".")
        if rc != 0:
            _run(["git", "merge", "--abort"], cwd=".")
            log_escalation(f"{task.id}: conflict merging into work. Manual resolution needed.\n{out[-500:]}")
            return False
        self._git(["worktree", "remove", "--force", self._worktree_path(task)])
        return True

    # -- main loop --

    def _recover_in_progress(self) -> None:
        """Reset tasks left in in_progress from a previous interruption back to todo (crash recovery).

        Since the frontier only picks status==todo, re-running with in_progress left over would mean
        that task is never started and the loop deadlocks. Roll back once at startup.
        """
        try:
            graph = dag.load(TASKS_PATH)
        except (OSError, dag.DagError, yaml.YAMLError):
            return
        for t in graph.tasks:
            if t.status == "in_progress":
                set_task_status(t.id, "todo")
                print(f"  [recover] {t.id}: reset in_progress → todo (resuming from a previous interruption)")

    def run(self) -> int:
        gates = self.front.get("gates") or {}
        if not (isinstance(gates, dict) and gates.get("tasks") == "approved"):
            print("gates.tasks is not approved. Approve /tasks first.", file=sys.stderr)
            return 2

        if not self.dry_run:
            rotate_log_if_large()  # keep the append-only escalation log lean before appending this run's entries
        self._recover_in_progress()
        while True:
            graph = dag.load(TASKS_PATH)
            counts = graph.counts()
            unfinished = len(graph.tasks) - counts["done"]
            if unfinished == 0:
                return self._present_gate4(graph)

            batch = plan_batch(graph, self.config.max_parallel)
            if batch is None:
                # frontier empty & there are unfinished ones = all blocked/needs-revision. To the human.
                blocked = [t.id for t in graph.tasks if t.status in ("blocked", "needs-revision")]
                log_escalation(f"No runnable tasks and {unfinished} unfinished ({', '.join(blocked)}). Help needed.")
                return 1

            mode, tasks = batch
            print(f"[batch] mode={mode} tasks={[t.id for t in tasks]}")
            try:
                if mode == "serial" or not self.config.worktree_enabled:
                    self._consume_serial(tasks)
                else:
                    self._consume_parallel(tasks)
            except StopLoop as exc:
                print(str(exc), file=sys.stderr)
                return exc.code
            # Recompute at the top of the loop after each batch (reassemble the chain).

    def _consume_serial(self, tasks: list[dag.Task]) -> None:
        """Finalize foundation tasks etc. serially on the work branch."""
        for task in tasks:
            set_task_status(task.id, "in_progress")
            print(f"  [serial] {task.id} {task.title}")
            ok, log = self._run_task_to_done(task, cwd=".")
            if not ok:
                set_task_status(task.id, "blocked")
                log_escalation(f"{task.id}: could not pass the quality gate within the limit; blocked.\n{log}")
                raise StopLoop(f"{task.id} is blocked. Human intervention needed.", code=1)
            if not self.dry_run:
                # Finalize the task diff only. The .agentloop/ orchestration state (tasks.yaml status, etc.)
                # is not included in the per-task commit (keeping one commit = one task).
                _run(["git", "add", "-A", "--", ".", ":(exclude).agentloop"], cwd=".")
                # If the implementer has not committed, finalize here (if there is a diff; no-op otherwise).
                _run(["git", "commit", "-m", f"{task.id}: {task.title}"], cwd=".")
            set_task_status(task.id, "done")

    def _consume_parallel(self, tasks: list[dag.Task]) -> None:
        """Implement independent leaves worktree-isolated up to max_parallel, then merge in ascending id order.

        Worktree creation is done serially on the main thread (avoiding .git index.lock contention);
        only the implementation is parallelized.
        """
        for task in tasks:
            set_task_status(task.id, "in_progress")
        # Worktree creation is serial (avoid git lock contention). The implementation is run in parallel after.
        branches = {task.id: self._add_worktree(task) for task in tasks}
        results: dict[str, tuple[bool, str]] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.max_parallel)) as pool:
            futures = {pool.submit(self._safe_run_task, t, self._worktree_path(t)): t for t in tasks}
            for future, task in futures.items():
                results[task.id] = future.result()

        blocked_any = False
        # Merge deterministically in ascending id order (sequential join).
        for task in sorted(tasks, key=lambda t: t.id):
            ok, log = results[task.id]
            if not ok:
                set_task_status(task.id, "blocked")
                log_escalation(f"{task.id}: could not pass the quality gate within the limit; blocked.\n{log}")
                blocked_any = True
                continue
            if self.merge_leaf(task, branches[task.id]):
                set_task_status(task.id, "done")
            else:
                set_task_status(task.id, "blocked")
                blocked_any = True
        if blocked_any:
            raise StopLoop("A blocked task occurred. Human intervention needed.", code=1)

    def _present_gate4(self, graph: dag.Graph) -> int:
        print("\n========== all tasks done (gate ④) ==========")
        print(dag.render(graph))
        print(
            "\nNext steps (human approval needed):\n"
            "  1. Run /security-review and resolve vulnerabilities in the work-branch diff.\n"
            "  2. Review the implementation summary and, if fine, approve at /build's gate ④.\n"
            "  * This script does not set gates.build to approved (only the human opens a gate)."
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the deterministic orchestrator for the implementation phase")
    parser.add_argument("--dry-run", action="store_true", help="run only the control flow without calling claude/git")
    parser.add_argument("--claude-bin", default="claude", help="the claude CLI for headless launch (default: claude)")
    args = parser.parse_args(argv)
    try:
        config = Config.load()
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"config load error: {exc}", file=sys.stderr)
        return 1
    return Orchestrator(config, dry_run=args.dry_run, claude_bin=args.claude_bin).run()


if __name__ == "__main__":
    raise SystemExit(main())
