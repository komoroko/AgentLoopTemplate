"""pr-draft — assemble a PR body from the cycle's SSOT and deliverables (read-only).

Push / PR creation are outward-facing and stay human-run (AGENTS.md "Branch / commit
conventions") — but the *body* of a good PR is exactly what the loop already recorded:
gate approvals in state.md, the task table in tasks.yaml, requirement coverage, the
security-review binding, and the commit list. This tool aggregates those into
`.agentloop/pr-draft.md` so the human's step is reviewing and running the printed
`gh pr create` line, not re-transcribing the SSOT by hand. It never invokes gh itself.

Usage:
  make pr-draft                       # writes .agentloop/pr-draft.md against base 'main'
  make pr-draft ARGS='--base develop'
  make pr-draft ARGS='--stdout'       # print instead of writing
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from agentloop import build_loop, common, dag, events
from agentloop import repo as repo_mod

OUT_PATH = ".agentloop/pr-draft.md"
REQUIREMENTS_PATH = "docs/10-requirements.md"
TEST_PLAN_PATH = "docs/test/test-plan.md"

# A gate line in state.md's front matter, with the approval note the YAML parser drops
# (AGENTS.md: approvals are recorded as a trailing comment, e.g. `tasks: approved  # 2026-07-07 alice`).
_GATE_LINE_RE = re.compile(
    r"^\s*(requirements|design|tasks|build|release):\s*(\w[\w-]*)\s*(?:#\s*(.*?))?\s*$", re.MULTILINE
)


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _gate_rows(state_text: str) -> list[tuple[str, str, str]]:
    """(gate, value, approval note) in file order — the note is the human/date comment."""
    return [(m.group(1), m.group(2), m.group(3) or "") for m in _GATE_LINE_RE.finditer(state_text)]


def _requirement_headings(req_text: str) -> list[str]:
    """Heading lines that name an R-N / NFR-N, with their titles (the covered scope)."""
    out = []
    for heading in dag._HEADING_RE.findall(req_text):
        if dag._REQ_ID_RE.search(heading):
            out.append(heading.strip())
    return out


def _reviewed_head(repo: repo_mod.Repo) -> str:
    review_path = str(repo.path(build_loop.SECURITY_REVIEW_PATH))
    m = re.search(r"^Reviewed-HEAD:\s*([0-9a-fA-F]+)", _read(review_path), re.MULTILINE)
    return m.group(1) if m else ""


def _git(args: list[str], root: str) -> str:
    rc, out = build_loop._run(["git", *args], cwd=root)
    return out.strip() if rc == 0 else ""


def _task_section(graph: dag.Graph) -> list[str]:
    counts = graph.counts()
    done = counts.get("done", 0)
    lines = [
        f"{done}/{len(graph.tasks)} tasks done.",
        "",
        "| task | kind | status | req | title |",
        "|---|---|---|---|---|",
    ]
    for t in graph.tasks:
        lines.append(f"| {t.id} | {t.kind} | {t.status} | {t.req or '-'} | {t.title} |")
    return lines


def build_draft(base: str, repo: repo_mod.Repo | None = None) -> str:
    repo = repo or repo_mod.get()
    root = str(repo.root)
    state_text = _read(str(repo.state))
    front = common.parse_frontmatter(state_text) or {}
    project = str(front.get("project", ""))
    branch = str(front.get("branch", ""))
    lines: list[str] = [f"# {project}: {branch}", ""]

    lines += ["## Gates", ""]
    for gate, value, note in _gate_rows(state_text):
        mark = "x" if value == "approved" else " "
        # The trailing comment is the approval record (date/approver) only on an approved gate;
        # on a pending one it is scaffold instruction text — don't quote that into the PR.
        lines.append(f"- [{mark}] {gate}: {value}" + (f" ({note})" if note and value == "approved" else ""))

    headings = _requirement_headings(_read(str(repo.path(REQUIREMENTS_PATH))))
    if headings:
        lines += ["", "## Requirements in this cycle", ""]
        lines += [f"- {h}" for h in headings]

    lines += ["", "## Tasks", ""]
    try:
        graph = dag.load(repo.tasks)
        lines += _task_section(graph)
    except (OSError, dag.DagError, yaml.YAMLError):
        lines.append("_(no readable tasks.yaml)_")

    lines += ["", "## Quality evidence", ""]
    lines.append(
        f"- Test plan: `{TEST_PLAN_PATH}` " + ("present" if repo.path(TEST_PLAN_PATH).exists() else "**absent**")
    )
    reviewed, head = _reviewed_head(repo), _git(["rev-parse", "HEAD"], root)
    if reviewed:
        # Either hash may be abbreviated — prefix-match in both directions.
        fresh = "current" if head and (head.startswith(reviewed) or reviewed.startswith(head)) else "**STALE**"
        lines.append(f"- Security review: `{build_loop.SECURITY_REVIEW_PATH}` Reviewed-HEAD {reviewed[:12]} ({fresh})")
    else:
        lines.append(f"- Security review: no `{build_loop.SECURITY_REVIEW_PATH}` report")
    open_esc = events.open_escalations(events.load_events(str(repo.events)))
    lines.append(f"- Open escalations: {len(open_esc)}" + (" — resolve before merging" if open_esc else ""))

    log = _git(["log", "--oneline", "--no-decorate", f"{base}..HEAD"], root)
    lines += ["", f"## Commits ({base}..HEAD)", ""]
    lines += ["```", log or "(none — is the base branch right?)", "```", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="assemble a PR body from the SSOT (read-only; never calls gh)")
    parser.add_argument("--base", default="main", help="base branch for the commit list (default: main)")
    parser.add_argument("--out", default="", help=f"output path (default: {OUT_PATH} under the discovered root)")
    parser.add_argument("--stdout", action="store_true", help="print the draft instead of writing a file")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    args.out = args.out or str(repo.path(OUT_PATH))
    draft = build_draft(args.base, repo)
    if args.stdout:
        print(draft)
        return 0
    Path(args.out).write_text(draft, encoding="utf-8")
    shown = repo.rel(args.out) or args.out  # repo-relative in messages; the write used the absolute path
    print(f"wrote {shown}")
    print("review it, then create the PR yourself (outward-facing = human-run):")
    print(f"  gh pr create --draft --base {args.base} --body-file {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
