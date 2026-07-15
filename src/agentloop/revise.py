"""Deterministic helper for rolling back (returning upstream).

The **first-class operation** for a human rewinding approval. Resets every gate from the target phase's gate onward
to pending **in a chain**, updates `current_phase` and `updated_at`, and appends one line to the roll-back log. This
mechanically prevents the stale-approval inconsistency of "upstream pending while downstream still approved"
(the editing order from then on is enforced by gate_guard).

To preserve state.md's comments and formatting, it **surgically rewrites only the target lines with regex**
(it does not rewrite the whole YAML).

`--impacted T-00x,T-00y` is the deterministic half of the task impact analysis: the directly-affected
seeds **plus their transitive dependents** (dag.dependents_closure) are all marked `needs-revision` in
tasks.yaml in code. Missing an impacted task is the dangerous direction, so the whole closure is marked
mechanically; "keep" is a deliberate, human-presented reclassification during the `/tasks` reconcile
(gate ③), never a silent default. Former statuses are printed so the reconcile knows what `done` work
was invalidated (those revert to `todo` there).

Usage:
  agentloop revise --to design --reason "rethink the auth method"
  agentloop revise --to design --impacted T-003,T-007
  agentloop revise --to requirements --dry-run
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from agentloop import common
from agentloop import repo as repo_mod

STATE_PATH = common.STATE_PATH
# The forward gate order (defined once in common.py). Reset the target onward to pending in a chain.
GATE_ORDER = common.GATE_ORDER
# Roll-back target phase -> the gate the chain starts at (verify precedes the release gate, so it is not a target).
_PHASE_GATE = {"requirements": "requirements", "design": "design", "tasks": "tasks", "build": "build"}
REVISE_MARKER = "<!-- REVISE-LOG -->"


class ReviseError(ValueError):
    """A roll-back operation failure, such as an invalid target."""


def cascade_gates(target_phase: str) -> list[str]:
    """Return the gates from the target phase's gate onward (the ones to reset to pending)."""
    if target_phase not in _PHASE_GATE:
        raise ReviseError(f"invalid target '{target_phase}' (one of {sorted(_PHASE_GATE)})")
    start = GATE_ORDER.index(_PHASE_GATE[target_phase])
    return list(GATE_ORDER[start:])


def _set_gate_pending(text: str, gate: str) -> str:
    """Set just the value of the front-matter "  <gate>: approved   # comment" to pending (preserving the comment)."""
    new, _ = common.rewrite_gate_line(text, gate, "approved", "pending", keep_trailer=True)
    return new


def _insert_log(text: str, target: str, gates: list[str], reason: str, today: str) -> str:
    """Append one row to the roll-back log table (right before the marker). Do nothing if the marker is absent."""
    if REVISE_MARKER not in text:
        return text
    row = f"| {today} | {target} | {', '.join(gates)} | {reason or '-'} |"
    return text.replace(REVISE_MARKER, f"{row}\n{REVISE_MARKER}", 1)


def apply_revision(text: str, target: str, reason: str, today: str) -> str:
    """Apply the roll-back to the state.md text and return the new text (pure function)."""
    gates = cascade_gates(target)
    new = text
    for gate in gates:
        new = _set_gate_pending(new, gate)
    new = common.set_current_phase(new, target)
    new = common.set_updated_at(new, today)
    new = _insert_log(new, target, gates, reason, today)
    return new


def mark_impacted(seeds: list[str], dry_run: bool, repo: repo_mod.Repo | None = None) -> int:
    """Mark the seeds and their transitive dependents `needs-revision` in tasks.yaml (in code).

    The frontier never picks a needs-revision task, so everything in the closure stays parked
    until the /tasks reconcile reclassifies it under human review at gate ③.
    """
    import yaml

    from agentloop import (
        build_loop,  # lazy: pyyaml is needed only for this mode; the gate rollback stays stdlib-only
        dag,
    )

    repo = repo or repo_mod.get()
    try:
        graph = dag.load(repo.tasks)
    except (OSError, dag.DagError, yaml.YAMLError) as exc:
        print(
            f"cannot load .agentloop/tasks.yaml: {exc} — fix it (or run `agentloop doctor` to diagnose the SSOT)",
            file=sys.stderr,
        )
        return 1
    known = {t.id for t in graph.tasks}
    unknown = [s for s in seeds if s not in known]
    if unknown:
        print(f"unknown task id(s): {', '.join(unknown)}", file=sys.stderr)
        return 1
    impacted = sorted(set(seeds) | graph.dependents_closure(seeds))
    prefix = "[dry-run] would mark" if dry_run else "marked"
    for tid in impacted:
        former = graph.get(tid).status
        if not dry_run and former != "needs-revision":
            build_loop.set_task_status(tid, "needs-revision", tasks_path=str(repo.tasks))
        invalidated = " — done work invalidated (reverts to todo at the /tasks reconcile)" if former == "done" else ""
        print(f"{prefix} {tid} → needs-revision (was {former}){invalidated}")
    print(
        f"impact set: {len(seeds)} seed(s) + {len(impacted) - len(seeds)} transitive dependent(s). "
        "Reclassify keep/modify/obsolete/new in the /tasks reconcile and present it at gate ③."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Roll back (reset upstream gates to pending in a chain)")
    parser.add_argument("--to", choices=sorted(_PHASE_GATE), help="the target phase to roll back to")
    parser.add_argument("--reason", default="", help="the roll-back reason (recorded in the roll-back log)")
    parser.add_argument(
        "--impacted",
        default="",
        help="comma-separated ids of the directly-affected tasks; marks them AND their transitive "
        "dependents needs-revision in tasks.yaml (deterministic impact marking)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan only without writing anything")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    seeds = [s.strip() for s in args.impacted.split(",") if s.strip()]
    if not args.to and not seeds:
        parser.error("nothing to do: pass --to <phase> and/or --impacted <ids>")
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    state_path = repo.state

    if args.to:
        gates = cascade_gates(args.to)
        today = date.today().isoformat()
        if args.dry_run:
            print(f"[dry-run] target phase: {args.to}")
            print(f"[dry-run] gates reset to pending: {', '.join(gates)}")
            print(f"[dry-run] current_phase -> {args.to} / updated_at -> {today}")
        else:
            try:
                text = state_path.read_text(encoding="utf-8")
            except OSError as exc:
                print(f"cannot read state.md: {exc}", file=sys.stderr)
                return 1
            state_path.write_text(apply_revision(text, args.to, args.reason, today), encoding="utf-8")
            print(f"roll-back complete: phase={args.to}. gates reset to pending: {', '.join(gates)}")

    if seeds:
        rc = mark_impacted(seeds, args.dry_run, repo)
        if rc != 0:
            return rc
    elif args.to and not args.dry_run:
        print(
            "Next: rebuild with the command for that phase → re-approve. Do not discard existing tasks; "
            "mark the ripple with --impacted (or enumerate it with dag.py --impacted) and reconcile."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
