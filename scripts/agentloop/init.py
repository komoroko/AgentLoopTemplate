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
  ./agentloop start      # the same sequence as an interactive wizard (plus the headless-CLI
                         # and product-brief questions) on a fresh copy
  make init NAME=myproduct [BRANCH=build/myproduct] [FROM=<template-url-or-path>]
  uv run python scripts/agentloop/init.py --name myproduct [--branch ...] [--source ...]
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from collections.abc import Callable
from pathlib import Path

# Circular with adopt.py (which imports init for shared text surgery) — safe: neither module
# touches the other's attributes at import time, so the partially initialized module binds fine.
import adopt
import agent_cli
import common
import cycle
import yaml

PYPROJECT_PATH = "pyproject.toml"
STATE_PATH = common.STATE_PATH
CONFIG_PATH = common.CONFIG_PATH
BRIEF_PATH = "docs/00-product-brief.md"


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


def fill_brief(text: str, summary: str) -> str:
    """Insert the wizard's 1–3 lines under the brief's first section (pure).

    A no-op when the section already holds non-comment content (never overwrite the human's
    words) or when the heading is absent (a customized scaffold). The scaffold's example
    comment is kept — the summary lands right after it.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("## What do you want to build"))
    except StopIteration:
        return text
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    if any(ln.strip() and not ln.lstrip().startswith("<!--") for ln in lines[start + 1 : end]):
        return text
    insert_at = start + 1
    if insert_at < end and lines[insert_at].lstrip().startswith("<!--"):
        insert_at += 1
    new_lines = lines[:insert_at] + [summary.strip()] + lines[insert_at:]
    return "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")


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
            inner = rel[len(adopt.SCAFFOLD_PREFIX) :] if rel.startswith(adopt.SCAFFOLD_PREFIX) else ""
            # Snapshots of target-adapted files (state.md, the SPECIAL docs) are seeded, as in
            # adopt — they have no pristine template counterpart for upgrade to compare against.
            owner = "seeded" if rel == cycle.SCAFFOLD_STATE or f"docs/{inner}" in adopt.SPECIAL else "template"
            files[rel] = {"hash": adopt.norm_hash(path.read_bytes()), "owner": owner}
    for rel in sorted(adopt.SPECIAL):
        path = root / rel
        if path.is_file():
            files[rel] = {"hash": adopt.norm_hash(path.read_bytes()), "owner": "seeded"}

    # settings.json / the root AGENTS.md and CLAUDE.md are the product's own from day one in a
    # greenfield copy ({"mode": "owned"}): upgrade and uninstall leave them alone. commit is
    # "unknown" — after `rm -rf .git && git init`, HEAD is the product's history, not the template's.
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
        agents_record={"mode": "owned"},
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
    rc, out = common.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        return f"git: not a repository — run `git init && git switch -c {branch}` yourself"
    if out.strip() == branch:
        return f"git: already on {branch}"
    rc, _ = common.run(["git", "switch", "-c", branch])
    if rc == 0:
        return f"git: created and switched to {branch}"
    rc, out = common.run(["git", "switch", branch])
    if rc == 0:
        return f"git: switched to existing {branch}"
    return f"git: could not switch to {branch} — {out.strip().splitlines()[-1] if out.strip() else 'unknown error'}"


def run_init(name: str, branch: str, source: str) -> int:
    """Apply the whole init sequence (fills, snapshot, manifest, branch). Shared by CLI and wizard."""
    today = datetime.date.today().isoformat()
    try:
        results = [
            (PYPROJECT_PATH, _apply(PYPROJECT_PATH, lambda t: replace_pyproject_name(t, name))),
            (STATE_PATH, _apply(STATE_PATH, lambda t: fill_state(t, name, branch, today))),
            (CONFIG_PATH, _apply(CONFIG_PATH, disable_template_mode)),
        ]
    except OSError as exc:
        print(
            f"init failed while filling the placeholders: {exc} — run from the repository root"
            " and check the named file is writable",
            file=sys.stderr,
        )
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
        print(f"  {record_manifest(Path(), source, today)}")
    except (OSError, ValueError) as exc:
        print(f"init failed while recording the adopt-manifest: {exc} — check .agentloop/ is writable", file=sys.stderr)
        return 1
    print(f"  {_switch_branch(branch)}")
    print(
        f'\nInitialized "{name}" (work branch: {branch}; the gate guard is now live).\n'
        "Next: write a few lines into docs/00-product-brief.md and start with /req."
    )
    return 0


# --- interactive wizard (`./agentloop start` on a fresh copy) ------------------


def _ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input(shown).strip() or default


def _ask_agent_cli() -> str:
    """The headless-CLI question: a preset number, or a custom command string."""
    presets = list(agent_cli.PRESETS)
    print("4/5 headless agent CLI for mode A (`make build-loop`):")
    for i, preset in enumerate(presets, 1):
        suffix = " (default)" if i == 1 else ""
        print(f"  {i}) {preset}{suffix}")
    print(f"  {len(presets) + 1}) custom command")
    choice = _ask(f"choose 1-{len(presets) + 1}", "1")
    if choice.isdigit() and 1 <= int(choice) <= len(presets):
        return presets[int(choice) - 1]
    answer = ""
    while not answer:
        answer = input('custom command (e.g. "mytool run"): ').strip()
    return answer


def _ask_brief() -> str:
    print("5/5 What do you want to build? (1-3 lines for docs/00-product-brief.md;")
    lines: list[str] = []
    while len(lines) < 3:
        line = input("  empty line to finish, Enter now to skip: " if not lines else "  ").strip()
        if not line:
            break
        lines.append(line)
    return "\n".join(lines)


def wizard() -> int:
    """Interactive first-run setup: ask everything first, then write (Ctrl+C mid-question loses nothing).

    Runs the exact same sequence as `make init NAME=...` (run_init), then applies the two
    extra answers: the headless agent CLI (agent_cli.set_headless_cmd) and the product-brief
    one-liner (fill_brief — written only after run_init's pristine scaffold snapshot).
    """
    print("AgentLoop setup — Enter accepts the [default]; Ctrl+C aborts without writing.")
    try:
        name = ""
        while not name:
            name = input("1/5 product name: ").strip()
        branch = _ask("2/5 work branch", f"build/{name}")
        source = _ask("3/5 template git URL/path (reused by agentloop-upgrade; Enter to skip)")
        cli = _ask_agent_cli()
        summary = _ask_brief()
    except (KeyboardInterrupt, EOFError):
        print("\naborted — nothing was written.", file=sys.stderr)
        return 130
    rc = run_init(name, branch, source)
    if rc != 0:
        return rc
    if agent_cli.main([cli]) != 0:
        return 1
    if summary:
        brief = Path(BRIEF_PATH)
        try:
            brief.write_text(fill_brief(brief.read_text(encoding="utf-8"), summary), encoding="utf-8")
            print(f"  updated: {BRIEF_PATH} (your summary — flesh it out anytime)")
        except OSError as exc:
            print(f"could not write {BRIEF_PATH}: {exc} — add your summary there by hand.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="initialize the copied template into a product")
    parser.add_argument("--name", default="", help="the product name (pyproject name / state.md project)")
    parser.add_argument("--branch", default="", help="the work branch (default: build/<name>)")
    parser.add_argument("--source", default="", help="the template's git URL/path (recorded for agentloop-upgrade)")
    args = parser.parse_args(argv)

    name = args.name.strip()
    if not name:
        print(
            "usage: make init NAME=<product> [BRANCH=build/<product>] — or run the interactive"
            " wizard with `./agentloop start`",
            file=sys.stderr,
        )
        return 2
    branch = args.branch.strip() or f"build/{name}"
    return run_init(name, branch, args.source.strip())


if __name__ == "__main__":
    raise SystemExit(main())
