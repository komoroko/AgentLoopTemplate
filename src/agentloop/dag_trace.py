"""The consistency trace (requirements → design → tasks → test plan) over the task DAG.

Deterministically checks whether the requirement-ID thread (R-1, NFR-1, …) runs unbroken through
requirements → design → tasks (and, when a test plan is given, into it). Like fan-out, etc., it is
"a mechanical check, not left to LLM discretion". Run at the /tasks gate and in CI, it visualizes
"is every requirement linked to design and tasks" before the human's review; /verify re-runs it
with --test-plan for the coverage of §2/§1.

Split from dag.py (the model/validation half); consumers keep addressing everything through
`dag.trace` / `dag.parse_requirement_ids` etc. — dag.py re-exports them lazily (PEP 562).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from agentloop.dag import Graph, Task, _split_req, is_nfr

logger = logging.getLogger(__name__)

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
