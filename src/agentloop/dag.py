"""Deterministic derivation utilities for the task graph (DAG).

Reads `.agentloop/tasks.yaml` and provides pure functions that **deterministically derive from blockedBy**
the executable frontier, execution layers, critical path, and fan-out.
Shared by src/agentloop/build_loop.py (deciding the consumption order) and /status (`--render`).

Derived values (fan-out, etc.) are not saved to the file. They are always computed from the graph, so they never drift.
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

logger = logging.getLogger(__name__)

# The possible values of status. Only done is considered dependency-satisfied.
STATUS_VALUES = frozenset({"todo", "in_progress", "blocked", "needs-revision", "done"})
# The display order of STATUS_VALUES (shared by count display and Mermaid color-coding).
STATUS_ORDER = ("todo", "in_progress", "blocked", "needs-revision", "done")
KIND_VALUES = frozenset({"foundation", "parallel", "integration"})
# The lifecycle phase a task originates from (see .agentloop/prompts/commands/tasks.md). Validated because a
# typo (e.g. "biuld") would otherwise silently drop the task from --trace's build-coverage check.
# (Distinct from common.PHASE_ORDER, the current_phase lifecycle: a task never originates in brief/tasks/done.)
PHASE_ORDER = ("requirements", "design", "build", "verify")
PHASE_VALUES = frozenset(PHASE_ORDER)

# Requirement IDs are `R-<number>` for functional and `NFR-<number>` for non-functional requirements
# (R-1, NFR-2, …). Shared vocabulary across requirements/design documents and task `req`. A task's req is
# validated at load time to ensure the whole token has this form (rejecting typos like R1 / Req-1 / nfr-1).
_REQ_ID_EXACT_RE = re.compile(r"^(?:R|NFR)-\d+$")


def is_nfr(req_id: str) -> bool:
    """True for a non-functional requirement ID (NFR-N). NFRs trace with softer rules than R-N:
    a missing design section or covering task is a WARN (many NFRs are cross-cutting and are
    verified at /verify), while a dangling reference stays an ERROR like any other."""
    return req_id.startswith("NFR-")


def _split_req(req: str) -> list[str]:
    """Split the req field ("R-1" / "R-1,R-3" / "R-1 R-3") into a token list (comma or whitespace)."""
    return [tok for tok in re.split(r"[,\s]+", req.strip()) if tok]


class DagError(ValueError):
    """An inconsistency in tasks.yaml (cycle, unknown dependency, duplicate ID, invalid value)."""


@dataclass(frozen=True)
class Task:
    """One task from tasks.yaml. Holds no derived values (fan-out, etc.)."""

    id: str
    title: str
    kind: str
    blocked_by: tuple[str, ...] = ()
    status: str = "todo"
    test: str = ""
    # Display/label-only metadata (not used for DAG derivation). req=covered requirement
    # (e.g. "R-1" / "R-1,R-3" / "NFR-2"), phase=lifecycle phase (requirements|design|build|verify; default build).
    req: str = ""
    phase: str = "build"

    @property
    def is_done(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True)
class Graph:
    """A validated task DAG. Created only via `load`/`from_tasks`."""

    tasks: tuple[Task, ...]
    _by_id: dict[str, Task] = field(default_factory=dict)

    @classmethod
    def from_tasks(cls, tasks: list[Task]) -> Graph:
        by_id: dict[str, Task] = {}
        for t in tasks:
            if t.id in by_id:
                raise DagError(f"duplicate task ID: {t.id}")
            if t.kind not in KIND_VALUES:
                raise DagError(f"{t.id}: invalid kind '{t.kind}' (one of {sorted(KIND_VALUES)})")
            if t.status not in STATUS_VALUES:
                raise DagError(f"{t.id}: invalid status '{t.status}' (one of {sorted(STATUS_VALUES)})")
            if t.phase not in PHASE_VALUES:
                raise DagError(f"{t.id}: invalid phase '{t.phase}' (one of {sorted(PHASE_VALUES)})")
            for tok in _split_req(t.req):
                if not _REQ_ID_EXACT_RE.match(tok):
                    raise DagError(f"{t.id}: invalid req token '{tok}' (must be in R-<number> form)")
            by_id[t.id] = t
        for t in tasks:
            for dep in t.blocked_by:
                if dep not in by_id:
                    raise DagError(f"{t.id}: references unknown dependency '{dep}'")
                if dep == t.id:
                    raise DagError(f"{t.id}: depends on itself")
        graph = cls(tasks=tuple(tasks), _by_id=by_id)
        graph._ensure_acyclic()
        return graph

    def get(self, task_id: str) -> Task:
        return self._by_id[task_id]

    def _ensure_acyclic(self) -> None:
        # If Kahn's algorithm cannot extract every node, there is a cycle.
        if len(self._topo_order()) != len(self.tasks):
            raise DagError("the dependency graph has a cycle (it is not a DAG)")

    def _topo_order(self) -> list[str]:
        """A deterministic topological order (ties by ascending id). On a cycle, returns only what was extracted."""
        indegree = {t.id: len(t.blocked_by) for t in self.tasks}
        dependents = self._dependents_map()
        ready = sorted(tid for tid, d in indegree.items() if d == 0)
        order: list[str] = []
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            newly: list[str] = []
            for child in dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    newly.append(child)
            # Re-sort each time for determinism.
            ready = sorted(ready + newly)
        return order

    def _dependents_map(self) -> dict[str, list[str]]:
        """Each task -> the list of task IDs that directly depend on it (its dependents)."""
        dependents: dict[str, list[str]] = {t.id: [] for t in self.tasks}
        for t in self.tasks:
            for dep in t.blocked_by:
                dependents[dep].append(t.id)
        return dependents

    # ---- derivation -------------------------------------------------------

    def fan_out(self) -> dict[str, int]:
        """The dependent count of each task (how many tasks are directly waiting on it)."""
        return {tid: len(children) for tid, children in self._dependents_map().items()}

    def dependents_closure(self, seed_ids: list[str]) -> set[str]:
        """Return the **transitive dependents** of the seed tasks (those depending on them, directly or indirectly).

        Used for task impact analysis in a roll back (/revise). Give the directly-affected tasks of an upstream
        change as seeds, and the downstream tasks chained to them are surfaced for re-review exhaustively.
        **The seeds themselves are excluded from the result** (a seed is excluded even if it ends up downstream
        of another seed via mutual dependency; seed=direct impact, return value=ripple beyond, a disjoint set).
        Unknown seed IDs are ignored (assume the caller has validated).
        """
        dependents = self._dependents_map()
        result: set[str] = set()
        stack = [tid for tid in seed_ids if tid in self._by_id]
        while stack:
            current = stack.pop()
            for child in dependents.get(current, []):
                if child not in result:
                    result.add(child)
                    stack.append(child)
        return result - set(seed_ids)

    def frontier(self) -> list[Task]:
        """todo startable right now (status==todo and all blockedBy are done). Ascending id."""
        result = [t for t in self.tasks if t.status == "todo" and all(self.get(dep).is_done for dep in t.blocked_by)]
        return sorted(result, key=lambda t: t.id)

    def layers(self) -> list[list[str]]:
        """Structural execution layers. Layer depth = longest dependency chain length. Within a layer, ascending id."""
        depth: dict[str, int] = {}
        for tid in self._topo_order():
            deps = self.get(tid).blocked_by
            depth[tid] = 1 + max((depth[d] for d in deps), default=-1)
        max_depth = max(depth.values(), default=-1)
        return [sorted(tid for tid, d in depth.items() if d == level) for level in range(max_depth + 1)]

    def critical_path(self) -> list[str]:
        """The longest chain (dependency path with the most nodes). Ties pick one deterministically by ascending id."""
        length: dict[str, int] = {}
        pred: dict[str, str | None] = {}
        for tid in self._topo_order():
            best_len = 0
            best_pred: str | None = None
            for dep in sorted(self.get(tid).blocked_by):
                if length[dep] > best_len:
                    best_len, best_pred = length[dep], dep
            length[tid] = best_len + 1
            pred[tid] = best_pred
        if not length:
            return []
        end = min(length, key=lambda t: (-length[t], t))
        path: list[str] = []
        node: str | None = end
        while node is not None:
            path.append(node)
            node = pred[node]
        return list(reversed(path))

    def order_frontier(self) -> list[Task]:
        """The frontier ordered by optimal consumption.

        Priority: ① foundation / high fan-out → ② on the critical path → ③ the rest. Ties deterministic by ascending id.
        """
        fan = self.fan_out()
        on_critical = set(self.critical_path())
        return sorted(
            self.frontier(),
            key=lambda t: (
                0 if t.kind == "foundation" else 1,
                -fan[t.id],
                0 if t.id in on_critical else 1,
                t.id,
            ),
        )

    def counts(self) -> dict[str, int]:
        """Counts by status."""
        result = {s: 0 for s in STATUS_VALUES}
        for t in self.tasks:
            result[t.status] += 1
        return result


def _task_from_raw(raw: dict[str, object]) -> Task:
    if not isinstance(raw, dict):
        raise DagError(f"a task must be a mapping (an element with id/title/...): {raw!r}")
    if "id" not in raw:
        raise DagError(f"there is a task with no id: {raw!r}")
    blocked = raw.get("blockedBy", []) or []
    if not isinstance(blocked, list):
        raise DagError(f"{raw['id']}: blockedBy must be a list")
    return Task(
        id=str(raw["id"]),
        title=str(raw.get("title", "")),
        kind=str(raw.get("kind", "parallel")),
        blocked_by=tuple(str(d) for d in blocked),
        status=str(raw.get("status", "todo")),
        test=str(raw.get("test", "")),
        req=str(raw.get("req") or ""),
        phase=str(raw.get("phase") or "build"),
    )


# The tasks.yaml schema version this parser understands (see data/schema/tasks.schema.json).
SCHEMA_VERSION = 1


def load(path: str | Path = ".agentloop/tasks.yaml") -> Graph:
    """Load tasks.yaml and return a validated Graph.

    A file declaring a `schema_version` newer than this parser knows is refused — guessing at
    unknown semantics is how a newer repo gets silently mis-parsed by an older tool. A missing
    schema_version stays accepted (pre-versioning repos).
    """
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    declared = data.get("schema_version")
    if isinstance(declared, int) and declared > SCHEMA_VERSION:
        raise DagError(
            f"tasks.yaml declares schema_version {declared} but this agentloop understands {SCHEMA_VERSION} — "
            "upgrade the tool (`uv tool upgrade agentloop`)"
        )
    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raise DagError("'tasks' in tasks.yaml must be a list")
    return Graph.from_tasks([_task_from_raw(r) for r in raw_tasks])


# The render (dag_render.py) and trace (dag_trace.py) halves split out; consumers keep
# addressing everything through `dag.` — re-exported lazily below (PEP 562 __getattr__),
# because a module-level import back from the split halves would be circular.
if TYPE_CHECKING:
    # The `x as x` form marks these as re-exports (so the linter keeps them and mypy types
    # `dag.render` etc.); at runtime the same names resolve through __getattr__ below.
    from agentloop.dag_render import mermaid as mermaid
    from agentloop.dag_render import render as render
    from agentloop.dag_trace import _CODE_FENCE_RE as _CODE_FENCE_RE
    from agentloop.dag_trace import _HEADING_RE as _HEADING_RE
    from agentloop.dag_trace import _REQ_ID_RE as _REQ_ID_RE
    from agentloop.dag_trace import TraceReport as TraceReport
    from agentloop.dag_trace import parse_requirement_ids as parse_requirement_ids
    from agentloop.dag_trace import render_trace as render_trace
    from agentloop.dag_trace import task_req_ids as task_req_ids
    from agentloop.dag_trace import trace as trace

_SPLIT_HOMES = {
    "render": "dag_render",
    "mermaid": "dag_render",
    "parse_requirement_ids": "dag_trace",
    "task_req_ids": "dag_trace",
    "TraceReport": "dag_trace",
    "trace": "dag_trace",
    "render_trace": "dag_trace",
    "_run_trace": "dag_trace",
    "_read_optional": "dag_trace",
    "_HEADING_RE": "dag_trace",
    "_REQ_ID_RE": "dag_trace",
    "_CODE_FENCE_RE": "dag_trace",
}


def __getattr__(name: str) -> object:
    """Lazy re-exports of the split render/trace halves (PEP 562)."""
    if home := _SPLIT_HOMES.get(name):
        import importlib

        return getattr(importlib.import_module(f"agentloop.{home}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deterministically derive the DAG from tasks.yaml")
    parser.add_argument("path", nargs="?", default="", help="path to tasks.yaml (default: the discovered repo's)")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render", action="store_true", help="output the summary for /status")
    group.add_argument("--mermaid", action="store_true", help="output the dependency graph as Mermaid (graph TD)")
    group.add_argument("--frontier", action="store_true", help="output optimal-order frontier IDs, newline-separated")
    group.add_argument("--validate", action="store_true", help="validate DAG consistency only (non-zero on error)")
    group.add_argument(
        "--impacted",
        metavar="IDS",
        help="roll-back impact: transitive dependents of the given tasks (comma-separated), newline-separated",
    )
    group.add_argument(
        "--trace",
        action="store_true",
        help="check requirements → design → tasks consistency (exit code 0=OK / 1=missing / 2=cannot check)",
    )
    parser.add_argument(
        "--requirements",
        default=None,
        help="path to the requirements document (for --trace; default docs/10-requirements.md)",
    )
    parser.add_argument(
        "--design",
        default=None,
        help="design document path (for --trace; default docs/20-design.md; skipped if absent)",
    )
    parser.add_argument(
        "--require-design",
        action="store_true",
        help="with --trace, do not allow a missing design document and exit 2 (for the design-approved phase gate)",
    )
    parser.add_argument(
        "--test-plan",
        default=None,
        metavar="PATH",
        help="with --trace, also require every R/NFR to appear in this test plan (for /verify;"
        " typically docs/test/test-plan.md)",
    )
    args = parser.parse_args(argv)
    # Lazy: keep the pure-graph module importable alone (and the split halves import this module).
    from agentloop import common, dag_render, dag_trace

    common.configure_logging()

    if not args.trace and (
        args.requirements is not None or args.design is not None or args.require_design or args.test_plan is not None
    ):
        logger.warning(
            "warning: --requirements/--design/--require-design/--test-plan are valid only with --trace (ignoring)"
        )

    if not args.path or args.trace:
        from agentloop import repo as repo_mod  # lazy: keep the pure-graph module importable alone

        try:
            repo = repo_mod.get(args.repo)
        except repo_mod.RepoNotFoundError as exc:
            logger.error(str(exc))
            return 2 if args.trace else 1
        args.path = args.path or str(repo.tasks)
        args.requirements = args.requirements or str(repo.path(dag_trace._DEFAULT_REQUIREMENTS))
        args.design = args.design or str(repo.path(dag_trace._DEFAULT_DESIGN))

    try:
        graph = load(args.path)
    except (OSError, DagError, yaml.YAMLError) as exc:
        logger.error(f"error: cannot load {args.path}: {exc} — fix it (or run `agentloop doctor` to diagnose)")
        # With --trace, represent "cannot check" with 2 (tasks.yaml unreadable = trace not established).
        return 2 if args.trace else 1

    if args.frontier:
        print("\n".join(t.id for t in graph.order_frontier()))
    elif args.validate:
        pass  # load success = validation OK
    elif args.mermaid:
        print(dag_render.mermaid(graph))
    elif args.impacted is not None:
        seeds = [s.strip() for s in args.impacted.split(",") if s.strip()]
        print("\n".join(sorted(graph.dependents_closure(seeds))))
    elif args.trace:
        return dag_trace._run_trace(
            graph,
            requirements_path=args.requirements or dag_trace._DEFAULT_REQUIREMENTS,
            design_path=args.design or dag_trace._DEFAULT_DESIGN,
            require_design=args.require_design,
            test_plan_path=args.test_plan,
        )
    else:  # --render (default)
        print(dag_render.render(graph))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
