"""Close a delta cycle (`make cycle-close NAME=<slug>`): archive the filled deliverables and reset.

An adopted/ongoing repo runs AgentLoop as a series of **delta cycles** — each cycle's
requirements/design/tasks/test docs describe one change, not the whole product. When a cycle
reaches `done` (release approved at gate ⑤ and the retrospective is written), a human closes it:

  1. The filled deliverables (10-requirements.md, 20-design.md, decisions/, tasks/, test/,
     retrospective.md) move to `docs/archive/<YYYY-MM-DD>-<slug>/` (via `git mv`).
     `00-product-brief.md` and `05-current-state.md` (the persistent baseline) stay.
  2. Fresh scaffolds are restored from the snapshot in `.agentloop/scaffold/docs/`
     (taken by `make init` / `adopt.py` while the docs were still pristine).
  3. `tasks.yaml` resets to an empty task list; every gate resets to pending,
     `current_phase` returns to brief, and a row lands in the state.md roll-back log
     (reusing revise.py's surgical front-matter rewrites).

Closing a cycle is a human decision, like opening a gate — the agent never runs this on its own.
Idempotent: already-archived items are skipped; `--dry-run` prints the plan only.

Usage:
  make cycle-close NAME=payment-refactor
  uv run python scripts/agentloop/cycle.py --name payment-refactor [--dry-run]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import build_loop
import revise

DOCS_DIR = "docs"
SCAFFOLD_DIR = ".agentloop/scaffold/docs"
ARCHIVE_DIR = "docs/archive"
# The per-cycle deliverables (relative to docs/). Everything else in docs/ persists across
# cycles: 00-product-brief.md (the product vision) and 05-current-state.md (the baseline).
CYCLE_ITEMS: tuple[str, ...] = (
    "10-requirements.md",
    "20-design.md",
    "decisions",
    "tasks",
    "test",
    "retrospective.md",
)


def snapshot_scaffold(docs_dir: str = DOCS_DIR, scaffold_dir: str = SCAFFOLD_DIR) -> bool:
    """Copy the pristine docs scaffolds aside, once. Returns True if the snapshot was taken.

    Called by init.py / adopt.py while docs/ is still unfilled. A no-op when the snapshot
    already exists — re-running init after docs are filled must not overwrite the pristine copy.
    """
    dst = Path(scaffold_dir)
    if dst.exists():
        return False
    src = Path(docs_dir)
    if not src.is_dir():
        return False
    dst.mkdir(parents=True)
    for item in sorted(src.iterdir()):
        if item.name == Path(ARCHIVE_DIR).name:
            continue
        if item.is_dir():
            shutil.copytree(item, dst / item.name)
        else:
            shutil.copy2(item, dst / item.name)
    return True


def plan_close(slug: str, today: str, docs_dir: str = DOCS_DIR) -> list[tuple[str, str, str]]:
    """The deterministic archive plan: (action, source, destination) rows (pure w.r.t. the fs read).

    action is "archive" for an existing deliverable, "skip" for one already gone
    (already archived by a previous run — idempotence).
    """
    archive_base = f"{ARCHIVE_DIR}/{today}-{slug}"
    rows: list[tuple[str, str, str]] = []
    for name in CYCLE_ITEMS:
        src = f"{docs_dir}/{name}"
        action = "archive" if Path(src).exists() else "skip"
        rows.append((action, src, f"{archive_base}/{name}"))
    return rows


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _archive(rows: list[tuple[str, str, str]]) -> list[str]:
    """Execute the archive plan with `git mv` (falling back to a plain move for untracked files)."""
    moved: list[str] = []
    for action, src, dst in rows:
        if action != "archive":
            continue
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        rc, _ = _run(["git", "mv", src, dst])
        if rc != 0:  # untracked deliverable (never committed): move it all the same
            shutil.move(src, dst)
        moved.append(src)
    return moved


def _restore_scaffold(scaffold_dir: str = SCAFFOLD_DIR, docs_dir: str = DOCS_DIR) -> list[str]:
    """Recreate fresh per-cycle scaffolds from the snapshot (never overwriting existing files)."""
    restored: list[str] = []
    for name in CYCLE_ITEMS:
        src = Path(scaffold_dir) / name
        dst = Path(docs_dir) / name
        if not src.exists() or dst.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        restored.append(str(dst))
    return restored


def reset_state_text(text: str, slug: str, today: str, archive_base: str) -> str:
    """Reset every gate to pending, phase to brief, and log the close (pure; reuses revise.py)."""
    for gate in revise.GATE_ORDER:
        text = revise._set_gate_pending(text, gate)
    text = revise._set_current_phase(text, "brief")
    text = revise._set_updated_at(text, today)
    return revise._insert_log(
        text, f"cycle-close ({slug})", list(revise.GATE_ORDER), f"deliverables archived to {archive_base}", today
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="close the current delta cycle (archive docs, reset gates)")
    parser.add_argument("--name", default="", help="a short slug for the cycle (archive folder name)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)

    slug = args.name.strip()
    if not slug:
        print("usage: make cycle-close NAME=<slug>", file=sys.stderr)
        return 2
    today = date.today().isoformat()
    archive_base = f"{ARCHIVE_DIR}/{today}-{slug}"
    rows = plan_close(slug, today)

    if not Path(SCAFFOLD_DIR).is_dir():
        print(
            f"no scaffold snapshot at {SCAFFOLD_DIR} — run `make init` (or adopt.py) first so fresh"
            " scaffolds can be restored after archiving.",
            file=sys.stderr,
        )
        return 1

    for action, src, dst in rows:
        print(f"  {action:<7} {src} → {dst}" if action == "archive" else f"  {action:<7} {src} (already archived)")
    if args.dry_run:
        print("[dry-run] then: restore scaffolds, reset tasks.yaml, gates → pending, phase → brief")
        return 0

    _archive(rows)
    for path in _restore_scaffold():
        print(f"  restore {path} (fresh scaffold)")
    Path(build_loop.TASKS_PATH).write_text(build_loop.TASKS_HEADER + "tasks: []\n", encoding="utf-8")
    print(f"  reset   {build_loop.TASKS_PATH} (empty task list)")
    state = Path(revise.STATE_PATH)
    state.write_text(reset_state_text(state.read_text(encoding="utf-8"), slug, today, archive_base), encoding="utf-8")
    print(f"  reset   {revise.STATE_PATH} (all gates pending, current_phase: brief)")
    print(
        f'\nCycle "{slug}" closed (archive: {archive_base}).\n'
        "Next cycle: update docs/00-product-brief.md with the next change and start with /req."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
