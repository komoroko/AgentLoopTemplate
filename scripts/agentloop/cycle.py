"""Close a delta cycle (`make cycle-close NAME=<slug>`): archive the filled deliverables and reset.

An adopted/ongoing repo runs AgentLoop as a series of **delta cycles** — each cycle's
requirements/design/tasks/test docs describe one change, not the whole product. When a cycle
reaches `done` (release approved at gate ⑤ and the retrospective is written), a human closes it:

  1. The filled deliverables (10-requirements.md, 20-design.md, decisions/, tasks/, test/,
     retrospective.md) move to `docs/archive/<YYYY-MM-DD>-<slug>/` (via `git mv`), and the
     orchestration event log (`.agentloop/events.ndjson` + its `.1` rotation) moves with them.
     `00-product-brief.md` and `05-current-state.md` (the persistent baseline) stay.
  2. Fresh scaffolds are restored from the snapshot in `.agentloop/scaffold/docs/`
     (taken by `make init` / `adopt.py` while the docs were still pristine).
  3. `tasks.yaml` resets to an empty task list; every gate resets to pending and
     `current_phase` returns to brief. state.md's human-facing body (phase progress,
     task table, execution plan, escalation/speculative logs, and the stale gate
     comments) is refreshed from the pristine state.md snapshot taken at init — the
     accumulating roll-back log is carried forward and a cycle-close row appended.
     (When no state snapshot exists — a repo initialised before this feature — the
     reset falls back to front-matter only, leaving the body as-is.)

Closing a cycle is a human decision, like opening a gate — the agent never runs this on its own.
Idempotent: already-archived items are skipped; `--dry-run` prints the plan only.

Usage:
  make cycle-close NAME=payment-refactor
  uv run python scripts/agentloop/cycle.py --name payment-refactor [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import build_loop
import events
import revise

DOCS_DIR = "docs"
SCAFFOLD_DIR = ".agentloop/scaffold/docs"
SCAFFOLD_STATE = ".agentloop/scaffold/state.md"
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
    """Copy the pristine docs scaffolds *and* state.md aside, once. Returns True if anything was taken.

    Called by init.py / adopt.py while docs/ and state.md are still pristine. A no-op per target
    when its snapshot already exists — re-running init after they are filled must not overwrite the
    pristine copy. The state.md snapshot is what `reset_state_text` restores the human-facing body
    from at cycle-close (each is guarded independently, so an older repo can gain the state snapshot
    on its own).
    """
    took = False
    dst = Path(scaffold_dir)
    src = Path(docs_dir)
    if not dst.exists() and src.is_dir():
        dst.mkdir(parents=True)
        for item in sorted(src.iterdir()):
            if item.name == Path(ARCHIVE_DIR).name:
                continue
            if item.is_dir():
                shutil.copytree(item, dst / item.name)
            else:
                shutil.copy2(item, dst / item.name)
        took = True
    # state.md lives beside docs/ (repo root = docs_dir's parent), so derive both from docs_dir —
    # this keeps the snapshot target-relative for adopt.py, which passes a foreign repo's paths.
    root = Path(docs_dir).parent
    state_dst = root / SCAFFOLD_STATE
    state_src = root / revise.STATE_PATH
    if not state_dst.exists() and state_src.is_file():
        state_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(state_src, state_dst)
        took = True
    return took


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


def _get_field(text: str, field: str) -> str:
    """Read a `field: "value"` front-matter value (empty string if absent)."""
    m = re.search(rf'^{re.escape(field)}: "([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def _set_field(text: str, field: str, value: str) -> str:
    """Set a `field: "value"` front-matter value (first occurrence)."""
    return re.sub(rf'^({re.escape(field)}: ")[^"]*(")', rf"\g<1>{value}\g<2>", text, count=1, flags=re.MULTILINE)


def _is_separator_row(row: str) -> bool:
    """True for a markdown table separator like `|------|-----|` (only pipes/dashes/colons/space)."""
    s = row.strip()
    return "-" in s and set(s) <= set("|-: ")


def _rollback_rows(text: str) -> list[str]:
    """The roll-back log's data rows (the `| … |` lines after its header separator, before the marker)."""
    before = text.split(revise.REVISE_MARKER, 1)[0]
    block: list[str] = []
    for line in reversed(before.splitlines()):
        if not line.strip() or not line.lstrip().startswith("|"):
            break
        block.append(line)
    block.reverse()  # now [header, separator, data...]
    sep = next((i for i, r in enumerate(block) if _is_separator_row(r)), None)
    return block[sep + 1 :] if sep is not None else []


def _restore_from_pristine(pristine: str, live: str) -> str:
    """Pristine body with the live repo's identity (`project`/`branch`) and roll-back log carried over."""
    base = _set_field(pristine, "project", _get_field(live, "project"))
    base = _set_field(base, "branch", _get_field(live, "branch"))
    carried = _rollback_rows(live)
    if carried and revise.REVISE_MARKER in base:
        base = base.replace(revise.REVISE_MARKER, "\n".join(carried) + "\n" + revise.REVISE_MARKER, 1)
    return base


def reset_state_text(text: str, slug: str, today: str, archive_base: str, pristine: str | None = None) -> str:
    """Reset gates/phase/updated_at and log the close; when `pristine` is given, also refresh the body.

    Without `pristine` (no state snapshot — a repo initialised before this feature) the reset is
    front-matter only, leaving the human-facing body untouched (the historical behaviour). With the
    pristine snapshot, the body is restored from it — carrying the live `project`/`branch` and the
    accumulating roll-back log across — so cycle-close leaves a clean next-cycle board, not a stale one.
    """
    base = text if pristine is None else _restore_from_pristine(pristine, text)
    for gate in revise.GATE_ORDER:
        base = revise._set_gate_pending(base, gate)
    base = revise._set_current_phase(base, "brief")
    base = revise._set_updated_at(base, today)
    return revise._insert_log(
        base, f"cycle-close ({slug})", list(revise.GATE_ORDER), f"deliverables archived to {archive_base}", today
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
    # The orchestration event log (and its rotated generation) and the post-build security-review
    # report belong to the closing cycle: archive them alongside the deliverables so the next cycle
    # starts clean (CLAUDE.md "Context budget" — logs are rotated/archived, never left to grow
    # without bound). The legacy pre-events build-loop.log is swept the same way.
    runtime_artifacts = [
        f"{base}{suffix}" for base in (events.EVENTS_PATH, ".agentloop/build-loop.log") for suffix in ("", ".1")
    ]
    runtime_artifacts.append(build_loop.SECURITY_REVIEW_PATH)
    for name in runtime_artifacts:
        log_src = Path(name)
        if log_src.is_file():
            log_dst = Path(archive_base) / log_src.name
            log_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(log_src), log_dst)
            print(f"  archive {log_src} → {log_dst}")
    for path in _restore_scaffold():
        print(f"  restore {path} (fresh scaffold)")
    Path(build_loop.TASKS_PATH).write_text(build_loop.TASKS_HEADER + "tasks: []\n", encoding="utf-8")
    print(f"  reset   {build_loop.TASKS_PATH} (empty task list)")
    state = Path(revise.STATE_PATH)
    snapshot = Path(SCAFFOLD_STATE)
    pristine = snapshot.read_text(encoding="utf-8") if snapshot.is_file() else None
    state.write_text(
        reset_state_text(state.read_text(encoding="utf-8"), slug, today, archive_base, pristine), encoding="utf-8"
    )
    body = "body refreshed" if pristine is not None else "body kept — no state snapshot"
    print(f"  reset   {revise.STATE_PATH} (gates pending, phase brief; {body})")
    print(
        f'\nCycle "{slug}" closed (archive: {archive_base}).\n'
        "Next cycle: update docs/00-product-brief.md with the next change and start with /req."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
