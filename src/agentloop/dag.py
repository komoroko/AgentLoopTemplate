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


def render(graph: Graph) -> str:
    """Deterministic rendering of the human-facing DAG view (task table, layers, critical path, frontier).

    /status prints it as-is; state.md embeds it between the DAG-VIEW markers (pasted by hand at
    /tasks, refreshed automatically by build_loop.py in deterministic mode A).
    """
    lines: list[str] = []
    counts = graph.counts()
    lines.append("Counts: " + " / ".join(f"{s}={counts[s]}" for s in STATUS_ORDER))
    lines.append("")
    lines.append("### Task table")
    if graph.tasks:
        fan = graph.fan_out()
        lines.append("| ID | Title | Kind | blockedBy | req | fan-out | status | Test |")
        lines.append("|----|-------|------|-----------|-----|---------|--------|------|")
        for t in graph.tasks:
            blocked = ", ".join(t.blocked_by) if t.blocked_by else "-"
            lines.append(
                f"| {t.id} | {t.title} | {t.kind} | {blocked} | {t.req or '-'} | {fan[t.id]} "
                f"| {t.status} | {t.test or '-'} |"
            )
    else:
        lines.append("- (no tasks)")
    lines.append("")
    lines.append("### Execution layers (within a layer, parallel is possible)")
    layers = graph.layers()
    if layers:
        for i, layer in enumerate(layers):
            lines.append(f"- L{i}: {', '.join(layer)}")
    else:
        lines.append("- (no tasks)")
    lines.append("")
    critical = graph.critical_path()
    lines.append("### Critical path (longest chain)")
    lines.append("- " + (" → ".join(critical) if critical else "(no tasks)"))
    lines.append("")
    lines.append("### Current executable frontier (optimal consumption order)")
    ordered = graph.order_frontier()
    if ordered:
        fan = graph.fan_out()
        for t in ordered:
            lines.append(f"- {t.id} [{t.kind}, fan-out={fan[t.id]}] {t.title}")
    else:
        lines.append("- (no startable todo)")
    return "\n".join(lines)


# status -> Mermaid classDef (fill=status color, critical=bold border). The class name replaces `-` in status with `_`.
_STATUS_CLASSDEFS = (
    "classDef todo fill:#eeeeee,stroke:#999999,color:#333333;",
    "classDef in_progress fill:#cfe8ff,stroke:#3b82f6,color:#06325e;",
    "classDef blocked fill:#ffd6d6,stroke:#ee2233,color:#7a0010;",
    "classDef needs_revision fill:#ffe9c7,stroke:#f59e0b,color:#7a4a00;",
    "classDef done fill:#d7f5dd,stroke:#22a04b,color:#0b3d1d;",
    "classDef critical stroke-width:3px;",
)


def _node_key(task_id: str) -> str:
    """Sanitize for a Mermaid node ID (`-` cannot be used in an identifier, so → `_`)."""
    return task_id.replace("-", "_")


def mermaid(graph: Graph) -> str:
    """Deterministically output the dependency graph as Mermaid (graph TD). Color-coded by status, critical path bold.

    Returns Mermaid text (wrapped in a ```mermaid fence) that renders directly in GitHub / VS Code / Markdown
    (rasterizing would break offline-ness, so leave rendering to the client).
    """
    tasks = sorted(graph.tasks, key=lambda t: t.id)
    lines: list[str] = ["```mermaid", "graph TD"]
    if not tasks:
        lines.append('  empty["(no tasks)"]')
        lines.append("```")
        return "\n".join(lines)
    for t in tasks:
        label = f"{t.id}: {t.title}".replace('"', "'")
        lines.append(f'  {_node_key(t.id)}["{label}"]')
    for t in tasks:
        for dep in t.blocked_by:
            lines.append(f"  {_node_key(dep)} --> {_node_key(t.id)}")
    lines.extend(f"  {cd}" for cd in _STATUS_CLASSDEFS)
    for status in STATUS_ORDER:
        ids = [_node_key(t.id) for t in tasks if t.status == status]
        if ids:
            lines.append(f"  class {','.join(ids)} {status.replace('-', '_')};")
    critical = graph.critical_path()
    if critical:
        lines.append(f"  class {','.join(_node_key(i) for i in critical)} critical;")
    lines.append("```")
    return "\n".join(lines)


# ---- consistency trace (requirements → design → tasks → test plan) -----------
# Deterministically checks whether the requirement-ID thread (R-1, NFR-1, …) runs unbroken through
# requirements → design → tasks (and, when a test plan is given, into it). Like fan-out, etc., it is "a mechanical
# check, not left to LLM discretion". Run at the /tasks gate and in CI, it visualizes "is every requirement linked
# to design and tasks" before the human's review; /verify re-runs it with --test-plan for the coverage of §2/§1.

# Pick requirement IDs from the **heading lines** of the requirements/design documents. Limited to heading lines, so
# R-x mentions in body text or comments are not picked up (avoiding false positives). Not tied to heading depth
# (number of #) (picked from any of H1–H6). When multiple IDs are written in one heading (e.g. `### R-1, R-2 → ...`),
# all IDs are picked up. The requirements document and task req share the same extraction rule.
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}\s+(.*)$", re.MULTILINE)
# Requirement IDs within heading text. The lookbehind avoids mid-word matches (rejects FOOR-1, and keeps NFR-1
# from also matching as R-1 mid-word), and the lookahead avoids cutting on a trailing digit (does not mistake
# R-12 for R-1). No trailing \b so it matches before CJK, etc.
_REQ_ID_RE = re.compile(r"(?<![0-9A-Za-z_])(?:R|NFR)-\d+(?!\d)")
# A code fence delimited by ``` or ~~~ (removed before extraction so example headings are not mistaken for real IDs).
_CODE_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)[^\n]*$.*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)


def parse_requirement_ids(text: str) -> list[str]:
    """Extract requirement IDs from a requirements/design document's headings **in order of appearance, deduplicated**.

    Example headings inside code fences are removed before scanning so they are not mistaken for real IDs.
    """
    body = _CODE_FENCE_RE.sub("", text)
    ids = (rid for heading in _HEADING_RE.findall(body) for rid in _REQ_ID_RE.findall(heading))
    return list(dict.fromkeys(ids))  # dedupe while preserving order of appearance


def task_req_ids(task: Task) -> list[str]:
    """Split a task's req field into a list of requirement IDs (order of appearance, deduplicated).

    Accepts either comma or whitespace separators. Each token's form (R-<number>) is already validated at load time by
    `Graph.from_tasks`, so by the time we get here there are no invalid tokens.
    """
    return list(dict.fromkeys(_split_req(task.req)))


@dataclass(frozen=True)
class TraceReport:
    """The result of the requirements → design → tasks (→ test plan) consistency (traceability) check.

    The set of requirement IDs, coverage, and uncovered are all derived from `req_to_tasks`/`nfr_to_tasks`
    alone (no duplicated state). The order is preserved by insertion order (= requirements-document order).
    Functional (R-N) and non-functional (NFR-N) requirements trace with different strictness: an R with no
    design section or covering build task is an ERROR, while for an NFR both are WARNs (many NFRs are
    cross-cutting and are verified at /verify via the test plan rather than implemented by one task).
    A dangling reference (design/task naming an unknown ID) is an ERROR for both. When a test plan is
    checked, every R **and** NFR must appear in it (ERROR otherwise) — that is /verify's coverage check.
    """

    req_to_tasks: dict[str, list[str]]  # functional R -> build task IDs for it (req order, values asc id)
    nfr_to_tasks: dict[str, list[str]]  # non-functional NFR -> build task IDs (coverage gaps are WARN)
    design_checked: bool  # whether the design dimension was checked (False=design document not checked)
    requirements_missing_design: tuple[str, ...]  # ERROR: R with no design section (when design checked)
    nfrs_missing_design: tuple[str, ...]  # WARN: NFR with no design section (when design checked)
    unknown_in_design: tuple[str, ...]  # ERROR: design references an ID not in the requirements
    unknown_in_tasks: tuple[tuple[str, str], ...]  # ERROR: a task references an ID not in the requirements
    tasks_without_req: tuple[str, ...]  # WARN: a build task with no req set
    test_plan_checked: bool = False  # whether the test-plan dimension was checked (--test-plan)
    missing_in_test_plan: tuple[str, ...] = ()  # ERROR: R/NFR that never appears in the test plan

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        """Functional requirement IDs from the requirements document (order of appearance)."""
        return tuple(self.req_to_tasks)

    @property
    def nfr_ids(self) -> tuple[str, ...]:
        """Non-functional requirement IDs from the requirements document (order of appearance)."""
        return tuple(self.nfr_to_tasks)

    @property
    def uncovered_requirements(self) -> tuple[str, ...]:
        """ERROR: functional requirements with no build task covering them."""
        return tuple(r for r, tasks in self.req_to_tasks.items() if not tasks)

    @property
    def uncovered_nfrs(self) -> tuple[str, ...]:
        """WARN: NFRs with no build task (legitimate when the test plan verifies them instead)."""
        return tuple(r for r, tasks in self.nfr_to_tasks.items() if not tasks)

    @property
    def ok(self) -> bool:
        """True if there is not a single ERROR (a WARN does not break ok)."""
        return not (
            self.uncovered_requirements
            or self.requirements_missing_design
            or self.unknown_in_design
            or self.unknown_in_tasks
            or self.missing_in_test_plan
        )


def trace(
    graph: Graph,
    requirement_ids: list[str],
    design_ids: list[str] | None,
    test_plan_text: str | None = None,
) -> TraceReport:
    """Cross-check requirement IDs, design IDs, task req (and optionally the test plan), detecting thread breaks.

    `requirement_ids` is the mixed R/NFR list extracted from the requirements document; the R and NFR
    dimensions are split here (see TraceReport for the asymmetric strictness). Coverage is judged by
    **build-phase tasks** only (a bug fix originating from verify, etc., is not an implementation plan, so it
    does not count toward coverage). A reference to an ID not in the requirements is an ERROR (any phase).
    If design_ids=None, treat the design document as absent and skip the design dimension (so it does not
    fail in an early phase or right after a design roll back). If test_plan_text is given (/verify), every
    R and NFR must appear somewhere in it — the mechanical "is each requirement in the test plan" check.
    """
    req_set = set(requirement_ids)
    req_to_tasks: dict[str, list[str]] = {r: [] for r in requirement_ids if not is_nfr(r)}
    nfr_to_tasks: dict[str, list[str]] = {r: [] for r in requirement_ids if is_nfr(r)}
    unknown_in_tasks: list[tuple[str, str]] = []
    tasks_without_req: list[str] = []
    for t in sorted(graph.tasks, key=lambda t: t.id):
        ids = task_req_ids(t)
        if not ids:
            # A build-phase task should have a covered requirement (a verify-originated bug fix, etc., is excluded).
            if t.phase == "build":
                tasks_without_req.append(t.id)
            continue
        for r in ids:
            if r not in req_set:
                unknown_in_tasks.append((t.id, r))  # dangling reference (ERROR regardless of phase)
            elif t.phase == "build":
                (nfr_to_tasks if is_nfr(r) else req_to_tasks)[r].append(t.id)  # coverage is build tasks only

    requirements_missing_design: tuple[str, ...] = ()
    nfrs_missing_design: tuple[str, ...] = ()
    unknown_in_design: tuple[str, ...] = ()
    if design_ids is not None:
        design_set = set(design_ids)
        missing = [r for r in requirement_ids if r not in design_set]
        requirements_missing_design = tuple(r for r in missing if not is_nfr(r))
        nfrs_missing_design = tuple(r for r in missing if is_nfr(r))
        unknown_in_design = tuple(d for d in design_ids if d not in req_set)

    missing_in_test_plan: tuple[str, ...] = ()
    if test_plan_text is not None:
        plan_ids = set(_REQ_ID_RE.findall(_CODE_FENCE_RE.sub("", test_plan_text)))
        missing_in_test_plan = tuple(r for r in requirement_ids if r not in plan_ids)

    return TraceReport(
        req_to_tasks=req_to_tasks,
        nfr_to_tasks=nfr_to_tasks,
        design_checked=design_ids is not None,
        requirements_missing_design=requirements_missing_design,
        nfrs_missing_design=nfrs_missing_design,
        unknown_in_design=unknown_in_design,
        unknown_in_tasks=tuple(unknown_in_tasks),
        tasks_without_req=tuple(tasks_without_req),
        test_plan_checked=test_plan_text is not None,
        missing_in_test_plan=missing_in_test_plan,
    )


def _coverage_lines(report: TraceReport, to_tasks: dict[str, list[str]], missing_design: set[str]) -> list[str]:
    """One `- ID: design✓/✗ tasks…` bullet per requirement (shared by the R and NFR sections)."""
    lines: list[str] = []
    for r, tasks in to_tasks.items():
        design_mark = ""
        if report.design_checked:
            design_mark = "design✗ " if r in missing_design else "design✓ "
        plan_mark = ""
        if report.test_plan_checked:
            plan_mark = "test-plan✗ " if r in report.missing_in_test_plan else "test-plan✓ "
        task_mark = ", ".join(tasks) if tasks else "(no task)"
        lines.append(f"- {r}: {design_mark}{plan_mark}{task_mark}")
    return lines


def render_trace(report: TraceReport) -> str:
    """Deterministically output a human-facing report of the consistency trace (coverage table + findings list)."""
    lines: list[str] = ["## Consistency trace (requirements → design → tasks)", ""]
    lines.append("### Requirement coverage")
    if report.req_to_tasks:
        lines.extend(_coverage_lines(report, report.req_to_tasks, set(report.requirements_missing_design)))
    else:
        lines.append("- (no requirement IDs found)")
    lines.append("")
    if report.nfr_to_tasks:
        lines.append("### Non-functional requirement coverage")
        lines.extend(_coverage_lines(report, report.nfr_to_tasks, set(report.nfrs_missing_design)))
        lines.append("")

    problems: list[str] = []
    for r in report.uncovered_requirements:
        problems.append(f"ERROR requirement {r}: no task covering it (not in the implementation plan)")
    for r in report.requirements_missing_design:
        problems.append(f"ERROR requirement {r}: no corresponding design section (requirements → design is broken)")
    for d in report.unknown_in_design:
        problems.append(f"ERROR design references unknown requirement {d} (not in the requirements)")
    for tid, r in report.unknown_in_tasks:
        problems.append(f"ERROR task {tid}: references unknown requirement {r} (not in the requirements)")
    for r in report.missing_in_test_plan:
        problems.append(f"ERROR requirement {r}: not covered in the test plan (add a check for it)")
    for r in report.nfrs_missing_design:
        problems.append(f"WARN  {r}: no design section (fine for a cross-cutting NFR; confirm it is deliberate)")
    for r in report.uncovered_nfrs:
        problems.append(f"WARN  {r}: no build task (fine when the test plan verifies it at /verify)")
    for tid in report.tasks_without_req:
        problems.append(f"WARN  task {tid}: no req set (a build task should have a covered requirement)")

    lines.append("### Findings")
    if problems:
        lines.extend(f"- {p}" for p in problems)
    else:
        lines.append("- No problems (every requirement is linked to design and tasks)")
    return "\n".join(lines)


def _read_optional(path: str | Path) -> str | None:
    """Return the contents if it exists, else None (for skipping a trace dimension).

    Any unreadable case (absent, a directory, insufficient permissions, etc.) collapses to None. The --trace branch's
    call is outside the try/except wrapping load(), so without catching OSError here, a directory path or permission
    error on the requirements/design path would escape main uncaught.
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


# Default document paths for --trace (used when not explicitly specified).
_DEFAULT_REQUIREMENTS = "docs/10-requirements.md"
_DEFAULT_DESIGN = "docs/20-design.md"


def _run_trace(
    graph: Graph, *, requirements_path: str, design_path: str, require_design: bool, test_plan_path: str | None
) -> int:
    """Run --trace and decide the exit code.

    The exit code distinguishes the cause (so CI/gates do not mistake "what is wrong"):
      0 = consistent
      1 = trace missing (uncovered requirement, dangling reference, etc.; **needs attention**)
      2 = cannot run the check (requirements document absent / 0 requirement IDs, design required but
          absent, or --test-plan given but unreadable)
    """
    req_text = _read_optional(requirements_path)
    if req_text is None:
        logger.error(f"error: cannot read the requirements document: {requirements_path}")
        return 2
    requirement_ids = parse_requirement_ids(req_text)
    if not requirement_ids:
        logger.error(
            f"error: cannot extract requirement IDs (R-N / NFR-N) from the requirements document:"
            f" {requirements_path} (write them in heading lines like `### R-1: ...`)"
        )
        return 2
    design_text = _read_optional(design_path)
    if design_text is None and require_design:
        logger.error(f"error: cannot read the design document: {design_path} (required when --require-design is given)")
        return 2
    test_plan_text: str | None = None
    if test_plan_path is not None:
        test_plan_text = _read_optional(test_plan_path)
        if test_plan_text is None:
            logger.error(f"error: cannot read the test plan: {test_plan_path} (given via --test-plan)")
            return 2
    design_ids = parse_requirement_ids(design_text) if design_text is not None else None
    report = trace(graph, requirement_ids, design_ids, test_plan_text)
    print(render_trace(report))
    if design_ids is None:
        logger.info(f"note: design {design_path} is absent, so design coverage was not checked")
    return 0 if report.ok else 1


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
        help=f"path to the requirements document (for --trace; default {_DEFAULT_REQUIREMENTS})",
    )
    parser.add_argument(
        "--design",
        default=None,
        help=f"design document path (for --trace; default {_DEFAULT_DESIGN}; skipped if absent)",
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
    from agentloop import common  # lazy: keep the pure-graph module importable alone

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
        args.requirements = args.requirements or str(repo.path(_DEFAULT_REQUIREMENTS))
        args.design = args.design or str(repo.path(_DEFAULT_DESIGN))

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
        print(mermaid(graph))
    elif args.impacted is not None:
        seeds = [s.strip() for s in args.impacted.split(",") if s.strip()]
        print("\n".join(sorted(graph.dependents_closure(seeds))))
    elif args.trace:
        return _run_trace(
            graph,
            requirements_path=args.requirements or _DEFAULT_REQUIREMENTS,
            design_path=args.design or _DEFAULT_DESIGN,
            require_design=args.require_design,
            test_plan_path=args.test_plan,
        )
    else:  # --render (default)
        print(render(graph))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
