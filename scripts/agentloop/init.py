"""Turn a freshly copied template into a product (`make init NAME=<product>`).

One idempotent command replaces the manual, easy-to-forget setup edits:

  1. pyproject.toml       — set the project `name`.
  2. .agentloop/state.md  — fill the `project` / `branch` / `updated_at` placeholders.
  3. .agentloop/config.yaml — flip `gates.template_mode` to false (the gate guard goes live;
     the template repo ships with true so scaffold maintenance is not self-blocked).
  4. scaffold snapshot    — copy the pristine docs scaffolds to .agentloop/scaffold/docs/
     (cycle.py restores fresh ones from here when `make cycle-close` archives a cycle).
  5. adopt-manifest       — record provenance (`mode: init`, the template source passed via
     FROM=, per-file hashes), so `agentloop-upgrade` / `agentloop-uninstall` work for
     greenfield copies too. An existing manifest is never overwritten; a re-run with FROM=
     backfills only a missing template source.
  6. git                  — create/switch to the work branch (best-effort: a repo without
     `git init` gets a hint instead of a hard failure).

The text replacements are surgical regexes (comments and layout survive), pure and unit-tested.
Re-running with the same arguments is a no-op. build_loop.py refuses to start while the
state.md placeholders are still present, pointing here.

Usage:
  make init NAME=myproduct [BRANCH=build/myproduct] [FROM=<template-url-or-path>]
  uv run python scripts/agentloop/init.py --name myproduct [--branch ...] [--source ...]
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# Circular with adopt.py (which imports init for shared text surgery) — safe: neither module
# touches the other's attributes at import time, so the partially initialized module binds fine.
import adopt
import cycle
import yaml

PYPROJECT_PATH = "pyproject.toml"
STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"


# --- pure text surgery (under test) ------------------------------------------


def replace_pyproject_name(text: str, name: str) -> str:
    return re.sub(r'^(name = ")[^"]*(")', rf"\g<1>{name}\g<2>", text, count=1, flags=re.MULTILINE)


def fill_state(text: str, project: str, branch: str, today: str) -> str:
    """Fill the state.md front-matter placeholders, keeping trailing comments intact."""
    text = re.sub(r'^(project: ")[^"]*(")', rf"\g<1>{project}\g<2>", text, count=1, flags=re.MULTILINE)
    text = re.sub(r'^(branch: ")[^"]*(")', rf"\g<1>{branch}\g<2>", text, count=1, flags=re.MULTILINE)
    return re.sub(r'^(updated_at: ")[^"]*(")', rf"\g<1>{today}\g<2>", text, count=1, flags=re.MULTILINE)


def disable_template_mode(text: str) -> str:
    return re.sub(r"^(\s*template_mode:\s*)true\b", r"\g<1>false", text, count=1, flags=re.MULTILINE)


# --- application --------------------------------------------------------------


def record_manifest(root: Path, source: str, today: str) -> str:
    """Write the greenfield adopt-manifest (mode: init) once; returns a summary line.

    An existing manifest is never rebuilt (idempotence; also protects an adopted repo from a
    stray `make init`) — except that `--source` may backfill a still-empty template source,
    the one field upgrade cannot work without. Hashes are taken from the files as they are
    right now, which for a fresh copy is the pristine template state.
    """
    manifest_path = root / adopt.MANIFEST_PATH
    if manifest_path.is_file():
        data = adopt.parse_manifest(manifest_path.read_text(encoding="utf-8"))
        template = data.get("template") or {}
        if source and not template.get("source"):
            template["source"] = source
            data["template"] = template
            manifest_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
            return f"updated (template source recorded): {adopt.MANIFEST_PATH}"
        return f"ok (already set): {adopt.MANIFEST_PATH}"

    files: dict[str, dict[str, str]] = {}
    for rel, (digest, owner, _src) in adopt.template_items(root, "init").items():
        if rel.startswith(adopt.SCAFFOLD_PREFIX):
            continue  # recorded from the real snapshot below, not the docs/ mirror
        files[rel] = {"hash": digest, "owner": owner}
    scaffold_root = root / ".agentloop" / "scaffold"
    if scaffold_root.is_dir():
        for path in sorted(scaffold_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            # The state.md snapshot is target-adapted like state.md itself -> seeded (as in adopt).
            owner = "seeded" if rel == cycle.SCAFFOLD_STATE else "template"
            files[rel] = {"hash": adopt.norm_hash(path.read_bytes()), "owner": owner}
    for rel in sorted(adopt.SPECIAL):
        path = root / rel
        if path.is_file():
            files[rel] = {"hash": adopt.norm_hash(path.read_bytes()), "owner": "seeded"}

    # settings.json / the root CLAUDE.md are the product's own from day one in a greenfield
    # copy ({"mode": "owned"}): upgrade and uninstall leave both alone. commit is "unknown" —
    # after `rm -rf .git && git init`, HEAD is the product's history, not the template's.
    data = adopt.build_manifest(
        files,
        {"mode": "owned"},
        {"mode": "owned"},
        source,
        "",
        "unknown",
        today,
        None,
        version=adopt.read_version(root),
        mode="init",
    )
    manifest_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return f"created: {adopt.MANIFEST_PATH} (drives agentloop-upgrade / agentloop-uninstall)"


def _apply(path: str, transform: Callable[[str], str]) -> bool:
    """Transform the file's text and write it back if it changed. Returns True when updated."""
    p = Path(path)
    old = p.read_text(encoding="utf-8")
    new = transform(old)
    if new == old:
        return False
    p.write_text(new, encoding="utf-8")
    return True


def _switch_branch(branch: str) -> str:
    """Create/switch to the work branch (best-effort). Returns a status line for the summary."""
    rc, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        return f"git: not a repository — run `git init && git switch -c {branch}` yourself"
    if out.strip() == branch:
        return f"git: already on {branch}"
    rc, _ = _run(["git", "switch", "-c", branch])
    if rc == 0:
        return f"git: created and switched to {branch}"
    rc, out = _run(["git", "switch", branch])
    if rc == 0:
        return f"git: switched to existing {branch}"
    return f"git: could not switch to {branch} — {out.strip().splitlines()[-1] if out.strip() else 'unknown error'}"


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="initialize the copied template into a product")
    parser.add_argument("--name", default="", help="the product name (pyproject name / state.md project)")
    parser.add_argument("--branch", default="", help="the work branch (default: build/<name>)")
    parser.add_argument("--source", default="", help="the template's git URL/path (recorded for agentloop-upgrade)")
    args = parser.parse_args(argv)

    name = args.name.strip()
    if not name:
        print("usage: make init NAME=<product> [BRANCH=build/<product>]", file=sys.stderr)
        return 2
    branch = args.branch.strip() or f"build/{name}"
    today = datetime.date.today().isoformat()

    try:
        results = [
            (PYPROJECT_PATH, _apply(PYPROJECT_PATH, lambda t: replace_pyproject_name(t, name))),
            (STATE_PATH, _apply(STATE_PATH, lambda t: fill_state(t, name, branch, today))),
            (CONFIG_PATH, _apply(CONFIG_PATH, disable_template_mode)),
        ]
    except OSError as exc:
        print(f"init failed: {exc}", file=sys.stderr)
        return 1

    for path, updated in results:
        print(f"  {'updated' if updated else 'ok (already set)'}: {path}")
    # Snapshot the pristine docs scaffolds while they are still unfilled, so `make cycle-close`
    # can restore fresh ones after archiving a finished cycle. No-op if already snapshotted.
    if cycle.snapshot_scaffold():
        print(f"  snapshot: docs scaffolds → {cycle.SCAFFOLD_DIR}")
    else:
        print(f"  ok (already set): {cycle.SCAFFOLD_DIR}")
    # After the snapshot (its files are part of the record) and after the fills above (the
    # seeded hashes must match what is on disk).
    try:
        print(f"  {record_manifest(Path(), args.source.strip(), today)}")
    except (OSError, ValueError) as exc:
        print(f"init failed: {exc}", file=sys.stderr)
        return 1
    print(f"  {_switch_branch(branch)}")
    print(
        f'\nInitialized "{name}" (work branch: {branch}; the gate guard is now live).\n'
        "Next: write a few lines into docs/00-product-brief.md and start with /req."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
