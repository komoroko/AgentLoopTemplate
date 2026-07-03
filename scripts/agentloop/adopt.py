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
           line is appended to the existing CLAUDE.md (marker-guarded, appended at most once;
           a repo without CLAUDE.md gets a minimal one holding just the import).
           .claude/settings.json: missing permissions.allow entries and hook groups are appended.
  adapt  — the copied config.yaml gets brownfield defaults: template_mode off, guard_paths scoped
           to the **docs deliverables only** (existing code keeps flowing right after adoption;
           re-enable code paths like "src/": tasks when ready), and the quality-gate test/check
           commands from --test-cmd/--check-cmd.
  record — .agentloop/adopt-manifest.yaml: provenance (template source/commit) plus a hash of
           every installed file, split into `template`-owned tooling and `seeded` repo state.
  manual — what it deliberately does not touch, with instructions: your makefile (add one line,
           `include agentloop.mk`), .pre-commit-config.yaml (gitleaks recommended), creating the
           work branch.

Two more manifest-driven modes (adopt-only — a greenfield `make init` records no manifest):

  --upgrade  — refresh the template-owned tooling from a newer template checkout. A file is
               overwritten only while **pristine** (its hash still matches the manifest); local
               modifications are skipped and reported (--force overrides). Repo-owned state
               (config.yaml, state.md, tasks.yaml, filled docs, your CLAUDE.md) is never touched.
  --uninstall — remove everything adopt installed, restoring the pre-adopt state. Pristine files
               only: anything the human edited (filled docs, tuned config, updated state) stays
               and is listed for manual review. The CLAUDE.md @import block and the merged
               settings.json entries are retracted too. Needs no template checkout.
  --from-git URL [--ref branch-or-tag] — shallow-clone the template into a temp dir instead of
               running from a checkout. Inside an adopted repo, `--upgrade` without --from-git
               falls back to the source recorded in the manifest (self-upgrade):
                 make -f agentloop.mk agentloop-upgrade [FROM=<url-or-path>]

Next step in the adopted repo: run /onboard to map the existing implementation into
docs/05-current-state.md, then start the first delta cycle with /req.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

# NOTE: no lazy imports of sibling modules anywhere in this file — a self-upgrade (running
# `adopt.py --upgrade` from inside the adopted repo) overwrites these very files mid-run, so the
# whole import graph must be resolved before the first write.
import cycle
import init
import yaml

TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_IMPORT_MARKER = "<!-- agentloop-rules -->"
AGENTLOOP_RULES_PATH = ".agentloop/CLAUDE.agentloop.md"
MANIFEST_PATH = ".agentloop/adopt-manifest.yaml"
SETTINGS_PATH = ".claude/settings.json"
SCAFFOLD_PREFIX = ".agentloop/scaffold/docs/"

# Trees copied file-by-file (never overwriting). The specially handled files below are excluded.
COPY_ROOTS = (".agentloop", "scripts/agentloop", ".claude/commands", ".claude/agents", "docs")
COPY_FILES = ("agentloop.mk",)
# Handled by dedicated logic, not the generic copy loop.
SPECIAL = {".agentloop/config.yaml", ".agentloop/state.md", "docs/00-product-brief.md"}
_EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "archive", "scaffold"}
_EXCLUDE_FILE_PREFIXES = ("build-loop.log", "adopt-manifest.yaml")

# Ownership of installed files, recorded per file in the manifest. `template` = the mechanism
# itself, safe to refresh on --upgrade while pristine; `seeded` = adopt wrote it once but the
# repo owns it from then on (upgrade never touches it).
_TEMPLATE_OWNED_PREFIXES = ("scripts/agentloop/", ".claude/commands/", ".claude/agents/")
_TEMPLATE_OWNED_FILES = ("agentloop.mk", AGENTLOOP_RULES_PATH)

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


@dataclass(frozen=True)
class PlanItem:
    op: str  # update | new | restore | remove | unchanged | skip-modified | leave-modified
    rel: str
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


def norm_hash(data: bytes) -> str:
    """sha256 of the CRLF-normalized bytes — a checkout's line-ending conversion is not an edit."""
    return "sha256:" + hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def default_owner(rel: str) -> str:
    """Ownership by path: the mechanism's own files are `template`, everything else is `seeded`."""
    if rel in _TEMPLATE_OWNED_FILES or rel.startswith(_TEMPLATE_OWNED_PREFIXES):
        return "template"
    return "seeded"


def _canon(obj: Any) -> str:
    """Canonical JSON for byte-stable equality of settings entries (key order must not matter)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def build_manifest(
    files: dict[str, dict[str, str]],
    settings_record: dict[str, Any],
    claude_record: dict[str, Any],
    source: str,
    ref: str,
    commit: str,
    adopted_at: str,
    upgraded_at: str | None,
) -> dict[str, Any]:
    """The adopt-manifest structure (see the module docstring's `record` step)."""
    template: dict[str, str] = {"source": source, "commit": commit}
    if ref:
        template["ref"] = ref
    return {
        "version": 1,
        "template": template,
        "adopted_at": adopted_at,
        "upgraded_at": upgraded_at,
        "files": {rel: files[rel] for rel in sorted(files)},
        "settings": settings_record,
        "claude_md": claude_record,
    }


def parse_manifest(text: str) -> dict[str, Any]:
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError(f"unsupported {MANIFEST_PATH} (expected `version: 1`) — it is machine-written; do not edit")
    return data


def plan_upgrade(
    manifest_files: dict[str, dict[str, str]],
    template_hashes: dict[str, str],
    target_hashes: dict[str, str | None],
    force: bool,
) -> list[PlanItem]:
    """The deterministic upgrade decision per template-owned file (pure; the full case table).

    A file is only ever overwritten/removed while **pristine** — its current hash equals the
    manifest's record — so a user's local modification always survives (unless force).
    """
    items: list[PlanItem] = []
    for rel in sorted(set(manifest_files) | set(template_hashes)):
        recorded = manifest_files.get(rel, {}).get("hash")
        wanted = template_hashes.get(rel)
        current = target_hashes.get(rel)
        if recorded and wanted:
            if current is None:
                items.append(PlanItem("restore", rel, "was deleted — reinstalled"))
            elif current == wanted:
                items.append(PlanItem("unchanged", rel))
            elif current == recorded:
                items.append(PlanItem("update", rel))
            elif force:
                items.append(PlanItem("update", rel, "forced — local modification overwritten"))
            else:
                items.append(PlanItem("skip-modified", rel, "locally modified — --force to overwrite"))
        elif wanted:  # new in the template
            if current is None:
                items.append(PlanItem("new", rel))
            elif current == wanted:
                items.append(PlanItem("unchanged", rel, "already matches — recorded in the manifest"))
            elif force:
                items.append(PlanItem("update", rel, "forced — existing file overwritten"))
            else:
                items.append(PlanItem("skip-modified", rel, "exists but not installed by adopt — --force overwrites"))
        else:  # removed upstream
            if current is None:
                items.append(PlanItem("unchanged", rel, "already gone — dropped from the manifest"))
            elif current == recorded:
                items.append(PlanItem("remove", rel, "removed from the template"))
            elif force:
                items.append(PlanItem("remove", rel, "forced — locally modified file removed"))
            else:
                items.append(PlanItem("leave-modified", rel, "removed upstream but locally modified — left in place"))
    return items


def detect_commands(files: dict[str, str]) -> dict[str, list[str]]:
    """Best-effort test/check command suggestions from a repo's build files (pure; propose-only).

    `files` maps a known filename (package.json, lockfiles, pyproject.toml, Cargo.toml, go.mod,
    makefile) to its content. Returns {"test": [...], "check": [...]} — printed as suggestions
    in the adopt report, never written into config.yaml.
    """
    test: list[str] = []
    check: list[str] = []
    pkg = files.get("package.json")
    if pkg:
        try:
            scripts = json.loads(pkg).get("scripts") or {}
        except ValueError:
            scripts = {}
        runner = "pnpm" if "pnpm-lock.yaml" in files else "yarn" if "yarn.lock" in files else "npm"
        if "test" in scripts:
            test.append(f"{runner} test" if runner != "npm" else "npm test")
        for name in ("lint", "check"):
            if name in scripts:
                check.append(f"{runner} run {name}")
                break
    pyproject = files.get("pyproject.toml")
    if pyproject:
        if "pytest" in pyproject:
            test.append("uv run pytest" if "uv.lock" in files else "pytest")
        if "ruff" in pyproject:
            check.append("ruff check .")
    if "Cargo.toml" in files:
        test.append("cargo test")
        check.append("cargo clippy -- -D warnings")
    if "go.mod" in files:
        test.append("go test ./...")
        check.append("go vet ./...")
    makefile = files.get("makefile") or files.get("Makefile")
    if makefile:
        targets = set(re.findall(r"^([A-Za-z][\w-]*):", makefile, flags=re.MULTILINE))
        if "test" in targets:
            test.append("make test")
        for name in ("check", "lint"):
            if name in targets:
                check.append(f"make {name}")
                break
    return {"test": test, "check": check}


def merge_settings(
    existing: dict[str, Any], template: dict[str, Any]
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Append the template's missing permissions.allow entries and hook groups.

    Additive only — nothing of the existing settings is removed or reordered. Hook groups are
    identified by their command strings, so a re-run appends nothing (idempotent).
    Returns (merged, notes, added) where `added` records exactly what this call appended
    (the manifest keeps it so upgrade/uninstall can find *our* entries again later).
    """
    notes: list[str] = []
    added: dict[str, Any] = {"permissions_allow": [], "hooks": {}}
    allow = existing.setdefault("permissions", {}).setdefault("allow", [])
    for entry in template.get("permissions", {}).get("allow") or []:
        if entry not in allow:
            allow.append(entry)
            added["permissions_allow"].append(entry)
            notes.append(f"permissions.allow += {entry}")
    hooks = existing.setdefault("hooks", {})
    for event, groups in (template.get("hooks") or {}).items():
        have = {h.get("command") for g in hooks.get(event) or [] for h in g.get("hooks") or []}
        for group in groups:
            cmds = {h.get("command") for h in group.get("hooks") or []}
            if not cmds <= have:
                hooks.setdefault(event, []).append(group)
                added["hooks"].setdefault(event, []).append(group)
                notes.append(f"hooks.{event} += {len(cmds)} command(s)")
    return existing, notes, added


def upgrade_settings(
    existing: dict[str, Any], installed: dict[str, Any], template: dict[str, Any]
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Replace the previously installed settings entries with the new template's versions.

    Only **pristine** installed hook groups (canonically equal to the manifest record) are
    retracted; a locally modified group is left alone and noted. The additive merge then re-adds
    whatever the new template wants, so a pristine group whose command changed upstream is
    replaced without duplication. Returns (merged, notes, added) like merge_settings — `added`
    is the fresh record of what is ours now.
    """
    notes: list[str] = []
    template_allow = set(template.get("permissions", {}).get("allow") or [])
    allow = existing.get("permissions", {}).get("allow")
    if isinstance(allow, list):
        for entry in installed.get("permissions_allow") or []:
            if entry in allow:
                allow.remove(entry)  # the merge below re-adds it if the template still wants it
                if entry not in template_allow:
                    notes.append(f"permissions.allow -= {entry} (dropped by the template)")
    hooks = existing.get("hooks") or {}
    for event, groups in (installed.get("hooks") or {}).items():
        lst = hooks.get(event)
        if not isinstance(lst, list):
            continue
        for old_group in groups:
            idx = next((i for i, g in enumerate(lst) if _canon(g) == _canon(old_group)), None)
            if idx is None:
                notes.append(f"hooks.{event}: an installed group was locally modified or removed — left as-is")
            else:
                del lst[idx]
    retracted_allow = set(installed.get("permissions_allow") or [])
    retracted_groups = {_canon(g) for gs in (installed.get("hooks") or {}).values() for g in gs}
    merged, _merge_notes, added = merge_settings(existing, template)
    # Note only the net changes — re-adding what step 1 just retracted is not news.
    for entry in added["permissions_allow"]:
        if entry not in retracted_allow:
            notes.append(f"permissions.allow += {entry}")
    for event, groups in added["hooks"].items():
        for group in groups:
            if _canon(group) not in retracted_groups:
                notes.append(f"hooks.{event} += 1 group")
    for event in [e for e, v in (merged.get("hooks") or {}).items() if v == []]:
        del merged["hooks"][event]
    return merged, notes, added


def plan_uninstall(
    manifest_files: dict[str, dict[str, str]],
    target_hashes: dict[str, str | None],
    force: bool,
) -> list[PlanItem]:
    """The deterministic uninstall decision per installed file (template **and** seeded).

    Only pristine files (hash still matches the record) are removed — anything the human or the
    cycles have edited (filled docs, tuned config.yaml, updated state.md) is left in place and
    reported for manual review (--force removes those too).
    """
    items: list[PlanItem] = []
    for rel in sorted(manifest_files):
        recorded = manifest_files[rel].get("hash")
        current = target_hashes.get(rel)
        if current is None:
            items.append(PlanItem("unchanged", rel, "already gone"))
        elif current == recorded:
            items.append(PlanItem("remove", rel))
        elif force:
            items.append(PlanItem("remove", rel, "forced — locally modified file removed"))
        else:
            items.append(PlanItem("leave-modified", rel, "locally modified — left for manual removal"))
    return items


def unmerge_settings(existing: dict[str, Any], installed: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Retract exactly the settings entries the manifest says adopt appended (pristine groups only).

    A hook group the user has since modified no longer canonically matches the record — it is
    theirs now and stays, with a note. Event lists that end up empty are pruned.
    """
    notes: list[str] = []
    allow = existing.get("permissions", {}).get("allow")
    if isinstance(allow, list):
        for entry in installed.get("permissions_allow") or []:
            if entry in allow:
                allow.remove(entry)
                notes.append(f"permissions.allow -= {entry}")
    hooks = existing.get("hooks") or {}
    for event, groups in (installed.get("hooks") or {}).items():
        lst = hooks.get(event)
        if not isinstance(lst, list):
            continue
        for old_group in groups:
            idx = next((i for i, g in enumerate(lst) if _canon(g) == _canon(old_group)), None)
            if idx is None:
                notes.append(f"hooks.{event}: an installed group was locally modified — left as-is")
            else:
                del lst[idx]
                notes.append(f"hooks.{event} -= 1 group")
    for event in [e for e, v in (existing.get("hooks") or {}).items() if v == []]:
        del existing["hooks"][event]
    # Drop the containers the merge's setdefault may have introduced, once they empty out.
    perms = existing.get("permissions")
    if isinstance(perms, dict) and perms.get("allow") == []:
        del perms["allow"]
    for key in ("permissions", "hooks"):
        if existing.get(key) == {}:
            del existing[key]
    return existing, notes


def remove_claude_import(text: str) -> str:
    """Strip the marker..@import block adopt appended to CLAUDE.md (idempotent; pure).

    Works line-wise from the marker through the @import line, so it also removes blocks written
    by other template versions whose wording differs.
    """
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == CLAUDE_IMPORT_MARKER), None)
    if start is None:
        return text
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("@")), start)
    del lines[start : end + 1]
    if 0 < start <= len(lines) and lines[start - 1].strip() == "":
        del lines[start - 1]  # the one blank line the appended block contributed
    return "\n".join(lines)


# --- installation steps ---------------------------------------------------------


def _iter_template_files(template_root: Path) -> list[str]:
    """The template-relative paths the generic copy loop installs (deterministic order)."""
    rels: list[str] = [f for f in COPY_FILES if (template_root / f).is_file()]
    for root in COPY_ROOTS:
        base = template_root / root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIR_NAMES for part in path.relative_to(template_root).parts[:-1]):
                continue
            if path.name.startswith(_EXCLUDE_FILE_PREFIXES):
                continue
            rel = path.relative_to(template_root).as_posix()
            if rel not in SPECIAL:
                rels.append(rel)
    return sorted(rels)


def template_items(template_root: Path) -> dict[str, tuple[str, str, Path | str]]:
    """Everything a fresh adopt would install verbatim: rel -> (hash, owner, content source).

    Includes the scaffold-snapshot copies of the template docs (template-owned: cycle-close
    restores from them) and the CLAUDE rules body. The SPECIAL files are absent — their
    installed content is target-adapted (seeded), so no template hash compares against them.
    """
    items: dict[str, tuple[str, str, Path | str]] = {}
    for rel in _iter_template_files(template_root):
        src = template_root / rel
        digest = norm_hash(src.read_bytes())
        items[rel] = (digest, default_owner(rel), src)
        if rel.startswith("docs/"):
            items[SCAFFOLD_PREFIX + rel[len("docs/") :]] = (digest, "template", src)
    rules = (template_root / "CLAUDE.md").read_text(encoding="utf-8")
    items[AGENTLOOP_RULES_PATH] = (norm_hash(rules.encode("utf-8")), "template", rules)
    return items


def _remove_and_prune(target: Path, rel: str) -> None:
    """Delete one installed file and any directories that emptied out around it."""
    dst = target / rel
    dst.unlink(missing_ok=True)
    parent = dst.parent
    while parent != target and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def git_head(root: Path) -> str:
    proc = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def resolve_template_root(from_git: str, ref: str) -> tuple[Path, Callable[[], None]]:
    """Shallow-clone the template from a URL/path into a temp dir; returns (root, cleanup).

    `ref` must be a branch or tag (a bare commit SHA cannot be shallow-cloned).
    """
    tmp = Path(tempfile.mkdtemp(prefix="agentloop-template-"))
    cmd = ["git", "clone", "--depth", "1", *(["--branch", ref] if ref else []), from_git, str(tmp / "template")]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else from_git
        raise RuntimeError(f"git clone failed: {detail}")
    return tmp / "template", lambda: shutil.rmtree(tmp, ignore_errors=True)


def _manifest_source(target: Path) -> tuple[str, str]:
    """The template source/ref an adopted repo recorded at adopt time ("" when unavailable)."""
    path = target / MANIFEST_PATH
    if not path.is_file():
        return "", ""
    try:
        data = parse_manifest(path.read_text(encoding="utf-8"))
    except ValueError:
        return "", ""
    template = data.get("template") or {}
    return str(template.get("source") or ""), str(template.get("ref") or "")


def _detect_target_commands(target: Path) -> dict[str, list[str]]:
    names = ("package.json", "pnpm-lock.yaml", "yarn.lock", "pyproject.toml", "uv.lock", "Cargo.toml", "go.mod")
    files: dict[str, str] = {}
    for name in (*names, "makefile", "Makefile"):
        path = target / name
        if path.is_file():
            files[name] = path.read_text(encoding="utf-8", errors="replace")
    return detect_commands(files)


def tracked_dirty_paths(target: Path, rels: list[str]) -> list[str]:
    """Tracked files with uncommitted changes among rels (untracked are fine — hashes protect them)."""
    if not rels:
        return []
    proc = subprocess.run(
        ["git", "-C", str(target), "status", "--porcelain", "--", *rels], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return []  # not a git repo — the per-file hash checks still protect local edits
    return [line[3:] for line in proc.stdout.splitlines() if line and not line.startswith("??")]


class Installer:
    def __init__(self, target: Path, template_root: Path, dry_run: bool) -> None:
        self.target = target
        self.template_root = template_root
        self.dry_run = dry_run
        self.actions: list[Action] = []
        # Manifest bookkeeping: rel -> {hash, owner} of what this run wrote, plus which live
        # docs came verbatim from the template (their scaffold snapshots are template-owned too).
        self.files: dict[str, dict[str, str]] = {}
        self.copied_docs: set[str] = set()
        self.settings_record: dict[str, Any] = {"created": False, "permissions_allow": [], "hooks": {}}
        self.claude_record: dict[str, Any] = {"mode": "merged"}

    def _record(self, rel: str, text: str, owner: str) -> None:
        self.files[rel] = {"hash": norm_hash(text.encode("utf-8")), "owner": owner}

    def _write(self, rel: str, text: str, status: str, note: str = "") -> None:
        self.actions.append(Action(status, rel, note))
        if self.dry_run:
            return
        dst = self.target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")

    def _install_fresh(self, rel: str, text: str, note: str = "") -> bool:
        """Write one adapted file unless the target already has it (never overwrite)."""
        if (self.target / rel).exists():
            self.actions.append(Action("skip", rel, "already exists — left untouched"))
            return False
        self._write(rel, text, "adapt", note)
        self._record(rel, text, "seeded")
        return True

    def copy_tree(self) -> None:
        for rel in _iter_template_files(self.template_root):
            if (self.target / rel).exists():
                self.actions.append(Action("skip", rel, "already exists — left untouched"))
                continue
            self.actions.append(Action("copy", rel))
            data = (self.template_root / rel).read_bytes()
            self.files[rel] = {"hash": norm_hash(data), "owner": default_owner(rel)}
            if rel.startswith("docs/"):
                self.copied_docs.add(rel)
            if not self.dry_run:
                dst = self.target / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.template_root / rel, dst)

    def install_special(self, name: str, branch: str, test_cmd: str, check_cmd: str) -> None:
        today = date.today().isoformat()
        state_text = init.fill_state(
            (self.template_root / ".agentloop/state.md").read_text(encoding="utf-8"), name, branch, today
        )
        self._install_fresh(".agentloop/state.md", state_text, note=f"project={name}, branch={branch}")
        config_text = brownfield_config(
            (self.template_root / ".agentloop/config.yaml").read_text(encoding="utf-8"), test_cmd, check_cmd
        )
        self._install_fresh(".agentloop/config.yaml", config_text, note="guard_paths=docs only, template_mode=false")
        brief = (self.template_root / "docs/00-product-brief.md").read_text(encoding="utf-8") + BRIEF_NOTE
        self._install_fresh("docs/00-product-brief.md", brief, note="with brownfield note")

    def install_claude_md(self) -> None:
        rules_text = (self.template_root / "CLAUDE.md").read_text(encoding="utf-8")
        if not (self.target / AGENTLOOP_RULES_PATH).exists():
            self._write(AGENTLOOP_RULES_PATH, rules_text, "copy", "the AgentLoop rules, imported by CLAUDE.md")
            self._record(AGENTLOOP_RULES_PATH, rules_text, "template")
        dst = self.target / "CLAUDE.md"
        if not dst.exists():
            text = claude_import_block().lstrip("\n")
            self._write("CLAUDE.md", text, "copy", "created with the @import only — put your own rules here")
            self.claude_record = {"mode": "created", "hash": norm_hash(text.encode("utf-8"))}
            return
        existing = dst.read_text(encoding="utf-8")
        if CLAUDE_IMPORT_MARKER in existing:
            self.actions.append(Action("skip", "CLAUDE.md", "@import already present"))
            return
        self._write("CLAUDE.md", existing.rstrip("\n") + "\n" + claude_import_block(), "merge", "@import appended")

    def install_settings(self) -> None:
        template_path = self.template_root / SETTINGS_PATH
        template = json.loads(template_path.read_text(encoding="utf-8"))
        dst = self.target / SETTINGS_PATH
        if not dst.exists():
            text = template_path.read_text(encoding="utf-8")
            self._write(SETTINGS_PATH, text, "copy")
            _merged, _notes, added = merge_settings({}, template)
            self.settings_record = {"created": True, "hash": norm_hash(text.encode("utf-8")), **added}
            return
        merged, notes, added = merge_settings(json.loads(dst.read_text(encoding="utf-8")), template)
        self.settings_record = {"created": False, **added}
        if not notes:
            self.actions.append(Action("skip", SETTINGS_PATH, "nothing missing"))
            return
        self._write(SETTINGS_PATH, json.dumps(merged, ensure_ascii=False, indent=2) + "\n", "merge", "; ".join(notes))

    def snapshot(self) -> None:
        if self.dry_run:
            return
        snap_root = self.target / ".agentloop/scaffold/docs"
        if cycle.snapshot_scaffold(str(self.target / "docs"), str(snap_root)):
            self.actions.append(Action("copy", ".agentloop/scaffold/docs/", "pristine scaffold snapshot"))
            for path in sorted(snap_root.rglob("*")):
                if not path.is_file():
                    continue
                inner = path.relative_to(snap_root).as_posix()
                owner = "template" if f"docs/{inner}" in self.copied_docs else "seeded"
                self.files[SCAFFOLD_PREFIX + inner] = {"hash": norm_hash(path.read_bytes()), "owner": owner}

    def write_manifest(self, source: str, ref: str, commit: str) -> None:
        """Record provenance + per-file hashes, written last (see the module docstring)."""
        note = "provenance + installed-file hashes (drives --upgrade / --uninstall)"
        path = self.target / MANIFEST_PATH
        if self.dry_run:
            self.actions.append(Action("adapt", MANIFEST_PATH, note))
            return
        if path.exists():
            # A re-run over an adopted repo: keep the original record, absorb newly installed files.
            data = parse_manifest(path.read_text(encoding="utf-8"))
            files: dict[str, dict[str, str]] = data.get("files") or {}
            files.update(self.files)
            data["files"] = {rel: files[rel] for rel in sorted(files)}
            record = data.get("settings") or {"created": False, "permissions_allow": [], "hooks": {}}
            for entry in self.settings_record.get("permissions_allow") or []:
                if entry not in (record.get("permissions_allow") or []):
                    record.setdefault("permissions_allow", []).append(entry)
            for event, groups in (self.settings_record.get("hooks") or {}).items():
                have = {_canon(g) for g in (record.get("hooks") or {}).get(event) or []}
                for group in groups:
                    if _canon(group) not in have:
                        record.setdefault("hooks", {}).setdefault(event, []).append(group)
            data["settings"] = record
            self.actions.append(Action("merge", MANIFEST_PATH, "newly installed files recorded"))
            path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
            return
        manifest = build_manifest(
            self.files, self.settings_record, self.claude_record, source, ref, commit, date.today().isoformat(), None
        )
        self._write(MANIFEST_PATH, yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), "adapt", note)


class Upgrader:
    """Refresh an adopted repo's template-owned tooling from a newer template (manifest-driven)."""

    def __init__(self, target: Path, template_root: Path, dry_run: bool, force: bool) -> None:
        self.target = target
        self.template_root = template_root
        self.dry_run = dry_run
        self.force = force

    def _apply(self, item: PlanItem, items: dict[str, tuple[str, str, Path | str]]) -> None:
        if self.dry_run or item.op not in ("update", "new", "restore", "remove"):
            return
        dst = self.target / item.rel
        if item.op == "remove":
            _remove_and_prune(self.target, item.rel)
            return
        src = items[item.rel][2]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(src, Path):
            shutil.copy2(src, dst)
        else:
            dst.write_text(src, encoding="utf-8")

    def run(self, source_label: str, ref: str) -> int:
        manifest_path = self.target / MANIFEST_PATH
        if not manifest_path.is_file():
            print(
                f"no {MANIFEST_PATH} — this repo was set up greenfield (`make init`) or never adopted;"
                " --upgrade/--uninstall are adopt-only.",
                file=sys.stderr,
            )
            return 1
        manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
        mf_files: dict[str, dict[str, str]] = manifest.get("files") or {}
        mf_template = {rel: e for rel, e in mf_files.items() if e.get("owner") == "template"}
        items = template_items(self.template_root)
        template_hashes = {rel: h for rel, (h, owner, _src) in items.items() if owner == "template"}
        target_hashes: dict[str, str | None] = {
            rel: (norm_hash((self.target / rel).read_bytes()) if (self.target / rel).is_file() else None)
            for rel in set(mf_template) | set(template_hashes)
        }
        plan = plan_upgrade(mf_template, template_hashes, target_hashes, self.force)

        # New seeded scaffolds the template gained (installed only where absent; never re-seeded).
        seeded_new = [
            rel
            for rel, (_h, owner, _src) in sorted(items.items())
            if owner == "seeded" and rel not in mf_files and not (self.target / rel).exists()
        ]

        # Settings: swap our installed entries for the new template's (pristine groups only).
        old_settings_record: dict[str, Any] = manifest.get("settings") or {}
        new_settings_record = dict(old_settings_record)
        settings_notes: list[str] = []
        new_settings_text = ""
        template_settings_path = self.template_root / SETTINGS_PATH
        dst_settings = self.target / SETTINGS_PATH
        if template_settings_path.is_file() and dst_settings.is_file():
            existing = json.loads(dst_settings.read_text(encoding="utf-8"))
            before = _canon(existing)
            merged, settings_notes, added = upgrade_settings(
                existing, old_settings_record, json.loads(template_settings_path.read_text(encoding="utf-8"))
            )
            new_settings_record = {"created": bool(old_settings_record.get("created")), **added}
            if _canon(merged) != before:
                new_settings_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
                if new_settings_record["created"]:
                    new_settings_record["hash"] = norm_hash(new_settings_text.encode("utf-8"))
            elif old_settings_record.get("hash"):
                new_settings_record["hash"] = old_settings_record["hash"]

        to_touch = [i.rel for i in plan if i.op in ("update", "remove", "restore")]
        if new_settings_text:
            to_touch.append(SETTINGS_PATH)
        if not self.force and not self.dry_run:
            dirty = tracked_dirty_paths(self.target, to_touch)
            if dirty:
                print(
                    "refusing to upgrade over uncommitted changes — commit first so `git diff` shows"
                    " exactly what the upgrade did (--force overrides):",
                    file=sys.stderr,
                )
                for p in dirty:
                    print(f"  {p}", file=sys.stderr)
                return 1

        prefix = "[dry-run] " if self.dry_run else ""
        counts: dict[str, int] = {}
        for item in plan:
            counts[item.op] = counts.get(item.op, 0) + 1
            if item.op != "unchanged":
                print(f"{prefix}{item.op:<14} {item.rel}" + (f"  ({item.note})" if item.note else ""))
            self._apply(item, items)
        for rel in seeded_new:
            counts["new"] = counts.get("new", 0) + 1
            print(f"{prefix}{'new':<14} {rel}  (new scaffold — seeded)")
            self._apply(PlanItem("new", rel), items)
        for note in settings_notes:
            print(f"{prefix}{'settings':<14} {SETTINGS_PATH}  ({note})")
        if new_settings_text and not self.dry_run:
            dst_settings.write_text(new_settings_text, encoding="utf-8")

        if not self.dry_run:
            # Rebuild the manifest last — a crash before this line re-converges on the next run.
            new_files = {rel: e for rel, e in mf_files.items() if e.get("owner") == "seeded"}
            for item in plan:
                if item.rel in template_hashes and item.op in ("update", "new", "restore", "unchanged"):
                    new_files[item.rel] = {"hash": template_hashes[item.rel], "owner": "template"}
                elif item.op in ("skip-modified", "leave-modified") and item.rel in mf_template:
                    new_files[item.rel] = mf_template[item.rel]
            for rel in seeded_new:
                new_files[rel] = {"hash": items[rel][0], "owner": "seeded"}
            today = date.today().isoformat()
            data = build_manifest(
                new_files,
                new_settings_record,
                manifest.get("claude_md") or {"mode": "merged"},
                source_label,
                ref,
                git_head(self.template_root),
                manifest.get("adopted_at") or today,
                today,
            )
            manifest_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")

        print(f"{prefix}summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        if not self.dry_run:
            print("\nReview the upgrade with `git diff`, then commit it.")
        return 0


class Uninstaller:
    """Remove everything adopt installed, restoring the pre-adopt state (manifest-driven).

    Pristine files only — anything the human edited (filled docs, tuned config, updated state)
    stays and is listed for manual review. Needs no template checkout.
    """

    def __init__(self, target: Path, dry_run: bool, force: bool) -> None:
        self.target = target
        self.dry_run = dry_run
        self.force = force

    def run(self) -> int:
        manifest_path = self.target / MANIFEST_PATH
        if not manifest_path.is_file():
            print(
                f"no {MANIFEST_PATH} — this repo was set up greenfield (`make init`) or never adopted;"
                " --upgrade/--uninstall are adopt-only.",
                file=sys.stderr,
            )
            return 1
        manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
        mf_files: dict[str, dict[str, str]] = manifest.get("files") or {}
        target_hashes: dict[str, str | None] = {
            rel: (norm_hash((self.target / rel).read_bytes()) if (self.target / rel).is_file() else None)
            for rel in mf_files
        }
        plan = plan_uninstall(mf_files, target_hashes, self.force)

        # CLAUDE.md: delete outright if we created it and it is untouched; otherwise strip the block.
        claude_record: dict[str, Any] = manifest.get("claude_md") or {}
        claude_path = self.target / "CLAUDE.md"
        claude_delete = False
        claude_new_text: str | None = None
        if claude_path.is_file():
            text = claude_path.read_text(encoding="utf-8")
            if claude_record.get("mode") == "created" and claude_record.get("hash") == norm_hash(text.encode("utf-8")):
                claude_delete = True
            else:
                stripped = remove_claude_import(text)
                if stripped != text:
                    claude_new_text = stripped

        # settings.json: same rule — wholesale delete only while pristine-created, else retract entries.
        settings_record: dict[str, Any] = manifest.get("settings") or {}
        settings_path = self.target / SETTINGS_PATH
        settings_delete = False
        settings_new_text: str | None = None
        settings_notes: list[str] = []
        if settings_path.is_file():
            text = settings_path.read_text(encoding="utf-8")
            if settings_record.get("created") and settings_record.get("hash") == norm_hash(text.encode("utf-8")):
                settings_delete = True
            else:
                merged, settings_notes = unmerge_settings(json.loads(text), settings_record)
                if settings_notes:
                    settings_new_text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"

        to_touch = [i.rel for i in plan if i.op == "remove"] + [MANIFEST_PATH]
        if claude_delete or claude_new_text is not None:
            to_touch.append("CLAUDE.md")
        if settings_delete or settings_new_text is not None:
            to_touch.append(SETTINGS_PATH)
        if not self.force and not self.dry_run:
            dirty = tracked_dirty_paths(self.target, to_touch)
            if dirty:
                print(
                    "refusing to uninstall over uncommitted changes — commit first so `git diff` shows"
                    " exactly what was removed (--force overrides):",
                    file=sys.stderr,
                )
                for p in dirty:
                    print(f"  {p}", file=sys.stderr)
                return 1

        prefix = "[dry-run] " if self.dry_run else ""
        counts: dict[str, int] = {}
        for item in plan:
            counts[item.op] = counts.get(item.op, 0) + 1
            if item.op != "unchanged":
                print(f"{prefix}{item.op:<14} {item.rel}" + (f"  ({item.note})" if item.note else ""))
            if item.op == "remove" and not self.dry_run:
                _remove_and_prune(self.target, item.rel)
        if claude_delete:
            print(f"{prefix}{'remove':<14} CLAUDE.md  (created by adopt, still pristine)")
        elif claude_new_text is not None:
            print(f"{prefix}{'unmerge':<14} CLAUDE.md  (@import block removed)")
        if settings_delete:
            print(f"{prefix}{'remove':<14} {SETTINGS_PATH}  (created by adopt, still pristine)")
        for note in settings_notes:
            print(f"{prefix}{'unmerge':<14} {SETTINGS_PATH}  ({note})")
        if not self.dry_run:
            if claude_delete:
                _remove_and_prune(self.target, "CLAUDE.md")
            elif claude_new_text is not None:
                claude_path.write_text(claude_new_text, encoding="utf-8")
            if settings_delete:
                _remove_and_prune(self.target, SETTINGS_PATH)
            elif settings_new_text is not None:
                settings_path.write_text(settings_new_text, encoding="utf-8")
            _remove_and_prune(self.target, MANIFEST_PATH)
        print(f"{prefix}{'remove':<14} {MANIFEST_PATH}")
        counts["remove"] = counts.get("remove", 0) + 1
        print(f"{prefix}summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        left = [i.rel for i in plan if i.op == "leave-modified"]
        if left:
            print(f"\n{prefix}Left for manual removal (you edited these since adopt):")
            for rel in left:
                print(f"  {rel}")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="install AgentLoop into an existing repository")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--upgrade", action="store_true", help="refresh the template-owned tooling of an adopted repo")
    mode.add_argument("--uninstall", action="store_true", help="remove what adopt installed (pristine files only)")
    parser.add_argument("--target", default="", help="path to the existing repository")
    parser.add_argument("--name", default="", help="the product name (state.md project)")
    parser.add_argument("--branch", default="", help="the work branch to record (default: build/<name>)")
    parser.add_argument("--test-cmd", default="", help="this repo's test command for the quality gate")
    parser.add_argument("--check-cmd", default="", help="this repo's lint/type command for the quality gate")
    parser.add_argument("--from-git", default="", help="clone the template from this URL/path instead of a checkout")
    parser.add_argument("--ref", default="", help="branch or tag for --from-git (default: the default branch)")
    parser.add_argument("--force", action="store_true", help="upgrade: also overwrite/remove locally modified files")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)

    name = args.name.strip()
    target_arg = args.target.strip()
    from_git = args.from_git.strip()
    ref = args.ref.strip()
    if not target_arg or (not name and not (args.upgrade or args.uninstall)):
        print('usage: make adopt TARGET=../myrepo NAME=myproduct [TEST_CMD="..."] [CHECK_CMD="..."]', file=sys.stderr)
        return 2
    if ref and not from_git:
        print("--ref requires --from-git", file=sys.stderr)
        return 2
    if args.uninstall and from_git:
        print("--uninstall is manifest-driven and needs no template — drop --from-git", file=sys.stderr)
        return 2
    target = Path(target_arg).resolve()
    if not target.is_dir():
        print(f"target is not a directory: {target}", file=sys.stderr)
        return 1

    if args.uninstall:
        return Uninstaller(target, args.dry_run, args.force).run()

    template_root = TEMPLATE_ROOT
    source_label = str(TEMPLATE_ROOT)
    cleanup: Callable[[], None] | None = None
    if not from_git and args.upgrade and target == TEMPLATE_ROOT:
        # Self-upgrade from inside the adopted repo: this adopt.py *is* the installed copy, so
        # fall back to the template source recorded in the manifest at adopt time.
        from_git, ref = _manifest_source(target)
        if from_git and "://" not in from_git and not from_git.startswith("git@"):
            local = Path(from_git)
            if local.is_dir() and local.resolve() != target:
                template_root, source_label, from_git = local.resolve(), from_git, ""
            else:
                print("the recorded template source is unavailable — pass --from-git <url-or-path>", file=sys.stderr)
                return 1
        elif not from_git:
            print("cannot locate a template: run from a checkout or pass --from-git <url-or-path>", file=sys.stderr)
            return 1
    if from_git:
        source_label = from_git
        try:
            template_root, cleanup = resolve_template_root(from_git, ref)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    try:
        if target == template_root:
            print("target is the template checkout itself — adopt installs into another repo.", file=sys.stderr)
            return 1

        if args.upgrade:
            return Upgrader(target, template_root, args.dry_run, args.force).run(source_label, ref)

        branch = args.branch.strip() or f"build/{name}"
        inst = Installer(target, template_root, dry_run=args.dry_run)
        inst.copy_tree()
        inst.install_special(name, branch, args.test_cmd.strip(), args.check_cmd.strip())
        inst.install_claude_md()
        inst.install_settings()
        inst.snapshot()
        inst.write_manifest(source_label, ref, git_head(template_root))

        prefix = "[dry-run] " if args.dry_run else ""
        counts: dict[str, int] = {}
        for a in inst.actions:
            counts[a.status] = counts.get(a.status, 0) + 1
            if a.status == "copy" and not a.note:
                continue  # plain copies are summarized by count; only annotated rows are itemized
            print(f"{prefix}{a.status:<6} {a.path}" + (f"  ({a.note})" if a.note else ""))
        print(f"{prefix}summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

        hints: list[str] = []
        if not (args.test_cmd and args.check_cmd):
            detected = _detect_target_commands(target)
            if not args.test_cmd and detected["test"]:
                hints.append("test → " + "  or  ".join(f'"{c}"' for c in detected["test"]))
            if not args.check_cmd and detected["check"]:
                hints.append("check → " + "  or  ".join(f'"{c}"' for c in detected["check"]))
        print(
            f"\n{prefix}Manual steps (adopt does not touch these):\n"
            "  - Add one line to your makefile: `include agentloop.mk` "
            "(or run targets standalone: `make -f agentloop.mk build-loop`).\n"
            + (
                "  - Set your test/check commands in .agentloop/config.yaml quality_gate.steps.\n"
                if not (args.test_cmd and args.check_cmd)
                else ""
            )
            + (f"    Detected here (suggestions only): {'; '.join(hints)}\n" if hints else "")
            + "  - Recommended: add the gitleaks hook to your .pre-commit-config.yaml (secret scanning).\n"
            f"  - Create the work branch when you start a cycle (state.md records: {branch}).\n"
            "\nNext, in the adopted repo: run /onboard (maps the existing implementation into\n"
            "docs/05-current-state.md), then start the first delta cycle with /req."
        )
        return 0
    finally:
        if cleanup:
            cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
