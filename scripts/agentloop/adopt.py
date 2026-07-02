"""Install AgentLoop into an existing (brownfield) repository — run from the template checkout.

  make adopt TARGET=../myrepo NAME=myrepo [BRANCH=...] [TEST_CMD="npm test"] [CHECK_CMD="npm run lint"]

Unlike the greenfield path (copy the whole template, `make init`), an existing repo already has
its own CLAUDE.md / settings / makefile / docs — so adoption is additive and conflict-aware.
What it does (idempotent; --dry-run prints the plan only):

  copy   — .agentloop/ (state.md / tasks.yaml / config.yaml), scripts/agentloop/, agentloop.mk,
           .claude/commands + .claude/agents, and the docs scaffolds. **An existing file is never
           overwritten** (skipped and reported). The pristine docs scaffolds are snapshotted to
           .agentloop/scaffold/docs/ so `make cycle-close` can restore them later.
  merge  — CLAUDE.md: the template rules land in .agentloop/CLAUDE.agentloop.md and one @-import
           line is appended to the existing CLAUDE.md (marker-guarded, appended at most once).
           .claude/settings.json: missing permissions.allow entries and hook groups are appended.
  adapt  — the copied config.yaml gets brownfield defaults: template_mode off, guard_paths scoped
           to the **docs deliverables only** (existing code keeps flowing right after adoption;
           re-enable code paths like "src/": tasks when ready), and the quality-gate test/check
           commands from --test-cmd/--check-cmd.
  manual — what it deliberately does not touch, with instructions: your makefile (add one line,
           `include agentloop.mk`), .pre-commit-config.yaml (gitleaks recommended), creating the
           work branch.

Next step in the adopted repo: run /onboard to map the existing implementation into
docs/05-current-state.md, then start the first delta cycle with /req.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import cycle
import init

TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_IMPORT_MARKER = "<!-- agentloop-rules -->"
AGENTLOOP_RULES_PATH = ".agentloop/CLAUDE.agentloop.md"

# Trees copied file-by-file (never overwriting). The specially handled files below are excluded.
COPY_ROOTS = (".agentloop", "scripts/agentloop", ".claude/commands", ".claude/agents", "docs")
COPY_FILES = ("agentloop.mk",)
# Handled by dedicated logic, not the generic copy loop.
SPECIAL = {".agentloop/config.yaml", ".agentloop/state.md", "docs/00-product-brief.md"}
_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", "archive", "scaffold"}
_EXCLUDE_FILE_PREFIXES = ("build-loop.log",)

BRIEF_NOTE = (
    "\n> **Adopted into an existing codebase.** Write each cycle's brief as the *change* you want\n"
    "> (delta scope), not the whole product. Run /onboard first so docs/05-current-state.md maps\n"
    "> the existing implementation; /req and /design then start from that baseline and reuse\n"
    "> existing assets.\n"
)


@dataclass(frozen=True)
class Action:
    status: str  # copy | adapt | merge | skip | manual
    path: str
    note: str = ""


# --- pure logic (under test) --------------------------------------------------


def brownfield_config(text: str, test_cmd: str, check_cmd: str) -> str:
    """Adapt the template config.yaml for an adopted repo (pure text surgery, comments survive)."""
    text = init.disable_template_mode(text)
    # Scope the guard to the docs deliverables only: pending gates must not freeze normal
    # development on the existing code. The commented lines document how to re-enable them.
    for key in ("backend/", "frontend/", "scripts/"):
        text = re.sub(
            rf"^    ({re.escape(key)}: tasks.*)$",
            r"    # \1   # re-enable (or map your layout, e.g. src/) when ready",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if test_cmd:
        text = text.replace('run: "make test"', f'run: "{test_cmd}"', 1)
    if check_cmd:
        text = text.replace('run: "make check"', f'run: "{check_cmd}"', 1)
    return text


def claude_import_block() -> str:
    return (
        f"\n{CLAUDE_IMPORT_MARKER}\n"
        "## AgentLoop\n"
        "This repo uses AgentLoop (Human-on-the-Loop, gated delta cycles). The operating rules are\n"
        f"imported from:\n@{AGENTLOOP_RULES_PATH}\n"
    )


def merge_settings(existing: dict[str, Any], template: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Append the template's missing permissions.allow entries and hook groups. Returns (merged, notes).

    Additive only — nothing of the existing settings is removed or reordered. Hook groups are
    identified by their command strings, so a re-run appends nothing (idempotent).
    """
    notes: list[str] = []
    allow = existing.setdefault("permissions", {}).setdefault("allow", [])
    for entry in template.get("permissions", {}).get("allow") or []:
        if entry not in allow:
            allow.append(entry)
            notes.append(f"permissions.allow += {entry}")
    hooks = existing.setdefault("hooks", {})
    for event, groups in (template.get("hooks") or {}).items():
        have = {h.get("command") for g in hooks.get(event) or [] for h in g.get("hooks") or []}
        for group in groups:
            cmds = {h.get("command") for h in group.get("hooks") or []}
            if not cmds <= have:
                hooks.setdefault(event, []).append(group)
                notes.append(f"hooks.{event} += {len(cmds)} command(s)")
    return existing, notes


# --- installation steps ---------------------------------------------------------


def _iter_template_files() -> list[str]:
    """The template-relative paths the generic copy loop installs (deterministic order)."""
    rels: list[str] = [f for f in COPY_FILES if (TEMPLATE_ROOT / f).is_file()]
    for root in COPY_ROOTS:
        base = TEMPLATE_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIR_NAMES for part in path.relative_to(TEMPLATE_ROOT).parts[:-1]):
                continue
            if path.name.startswith(_EXCLUDE_FILE_PREFIXES):
                continue
            rel = path.relative_to(TEMPLATE_ROOT).as_posix()
            if rel not in SPECIAL:
                rels.append(rel)
    return sorted(rels)


class Installer:
    def __init__(self, target: Path, dry_run: bool) -> None:
        self.target = target
        self.dry_run = dry_run
        self.actions: list[Action] = []

    def _write(self, rel: str, text: str, status: str, note: str = "") -> None:
        self.actions.append(Action(status, rel, note))
        if self.dry_run:
            return
        dst = self.target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")

    def _install_fresh(self, rel: str, text: str, note: str = "") -> None:
        """Write one adapted file unless the target already has it (never overwrite)."""
        if (self.target / rel).exists():
            self.actions.append(Action("skip", rel, "already exists — left untouched"))
            return
        self._write(rel, text, "adapt", note)

    def copy_tree(self) -> None:
        for rel in _iter_template_files():
            if (self.target / rel).exists():
                self.actions.append(Action("skip", rel, "already exists — left untouched"))
                continue
            self.actions.append(Action("copy", rel))
            if not self.dry_run:
                dst = self.target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(TEMPLATE_ROOT / rel, dst)

    def install_special(self, name: str, branch: str, test_cmd: str, check_cmd: str) -> None:
        today = date.today().isoformat()
        state_text = init.fill_state(
            (TEMPLATE_ROOT / ".agentloop/state.md").read_text(encoding="utf-8"), name, branch, today
        )
        self._install_fresh(".agentloop/state.md", state_text, note=f"project={name}, branch={branch}")
        config_text = brownfield_config(
            (TEMPLATE_ROOT / ".agentloop/config.yaml").read_text(encoding="utf-8"), test_cmd, check_cmd
        )
        self._install_fresh(".agentloop/config.yaml", config_text, note="guard_paths=docs only, template_mode=false")
        brief = (TEMPLATE_ROOT / "docs/00-product-brief.md").read_text(encoding="utf-8") + BRIEF_NOTE
        self._install_fresh("docs/00-product-brief.md", brief, note="with brownfield note")

    def install_claude_md(self) -> None:
        rules_text = (TEMPLATE_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        dst = self.target / "CLAUDE.md"
        if not dst.exists():
            self._write("CLAUDE.md", rules_text, "copy")
            return
        if not (self.target / AGENTLOOP_RULES_PATH).exists():
            self._write(AGENTLOOP_RULES_PATH, rules_text, "copy", "the AgentLoop rules, imported by CLAUDE.md")
        existing = dst.read_text(encoding="utf-8")
        if CLAUDE_IMPORT_MARKER in existing:
            self.actions.append(Action("skip", "CLAUDE.md", "@import already present"))
            return
        self._write("CLAUDE.md", existing.rstrip("\n") + "\n" + claude_import_block(), "merge", "@import appended")

    def install_settings(self) -> None:
        rel = ".claude/settings.json"
        template = json.loads((TEMPLATE_ROOT / rel).read_text(encoding="utf-8"))
        dst = self.target / rel
        if not dst.exists():
            self._write(rel, json.dumps(template, ensure_ascii=False, indent=2) + "\n", "copy")
            return
        merged, notes = merge_settings(json.loads(dst.read_text(encoding="utf-8")), template)
        if not notes:
            self.actions.append(Action("skip", rel, "nothing missing"))
            return
        self._write(rel, json.dumps(merged, ensure_ascii=False, indent=2) + "\n", "merge", "; ".join(notes))

    def snapshot(self) -> None:
        if self.dry_run:
            return
        if cycle.snapshot_scaffold(str(self.target / "docs"), str(self.target / ".agentloop/scaffold/docs")):
            self.actions.append(Action("copy", ".agentloop/scaffold/docs/", "pristine scaffold snapshot"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="install AgentLoop into an existing repository")
    parser.add_argument("--target", default="", help="path to the existing repository")
    parser.add_argument("--name", default="", help="the product name (state.md project)")
    parser.add_argument("--branch", default="", help="the work branch to record (default: build/<name>)")
    parser.add_argument("--test-cmd", default="", help="this repo's test command for the quality gate")
    parser.add_argument("--check-cmd", default="", help="this repo's lint/type command for the quality gate")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)

    name = args.name.strip()
    target_arg = args.target.strip()
    if not target_arg or not name:
        print('usage: make adopt TARGET=../myrepo NAME=myproduct [TEST_CMD="..."] [CHECK_CMD="..."]', file=sys.stderr)
        return 2
    target = Path(target_arg).resolve()
    if not target.is_dir():
        print(f"target is not a directory: {target}", file=sys.stderr)
        return 1
    if target == TEMPLATE_ROOT:
        print("target is the template checkout itself — adopt installs into another repo.", file=sys.stderr)
        return 1
    branch = args.branch.strip() or f"build/{name}"

    inst = Installer(target, dry_run=args.dry_run)
    inst.copy_tree()
    inst.install_special(name, branch, args.test_cmd.strip(), args.check_cmd.strip())
    inst.install_claude_md()
    inst.install_settings()
    inst.snapshot()

    prefix = "[dry-run] " if args.dry_run else ""
    counts: dict[str, int] = {}
    for a in inst.actions:
        counts[a.status] = counts.get(a.status, 0) + 1
        if a.status == "copy" and not a.note:
            continue  # plain copies are summarized by count; only annotated rows are itemized
        print(f"{prefix}{a.status:<6} {a.path}" + (f"  ({a.note})" if a.note else ""))
    print(f"{prefix}summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    print(
        f"\n{prefix}Manual steps (adopt does not touch these):\n"
        "  1. Add one line to your makefile: `include agentloop.mk` "
        "(or run targets standalone: `make -f agentloop.mk build-loop`).\n"
        + (
            "  2. Set your test/check commands in .agentloop/config.yaml quality_gate.steps.\n"
            if not (args.test_cmd and args.check_cmd)
            else ""
        )
        + "  3. Recommended: add the gitleaks hook to your .pre-commit-config.yaml (secret scanning).\n"
        f"  4. Create the work branch when you start a cycle (state.md records: {branch}).\n"
        "\nNext, in the adopted repo: run /onboard (maps the existing implementation into\n"
        "docs/05-current-state.md), then start the first delta cycle with /req."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
