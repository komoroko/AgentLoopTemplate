"""Turn a freshly copied template into a product (`make init NAME=<product>`).

One idempotent command replaces the manual, easy-to-forget setup edits:

  1. pyproject.toml       — set the project `name`.
  2. .agentloop/state.md  — fill the `project` / `branch` / `updated_at` placeholders.
  3. .agentloop/config.yaml — flip `gates.template_mode` to false (the gate guard goes live;
     the template repo ships with true so scaffold maintenance is not self-blocked).
  4. git                  — create/switch to the work branch (best-effort: a repo without
     `git init` gets a hint instead of a hard failure).

The text replacements are surgical regexes (comments and layout survive), pure and unit-tested.
Re-running with the same arguments is a no-op. build_loop.py refuses to start while the
state.md placeholders are still present, pointing here.

Usage:
  make init NAME=myproduct [BRANCH=build/myproduct]
  uv run python scripts/agentloop/init.py --name myproduct [--branch build/myproduct]
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

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
    print(f"  {_switch_branch(branch)}")
    print(
        f'\nInitialized "{name}" (work branch: {branch}; the gate guard is now live).\n'
        "Next: write a few lines into docs/00-product-brief.md and start with /req."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
