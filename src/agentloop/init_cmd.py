"""`agentloop init` — seed a repository with AgentLoop state (greenfield and brownfield alike).

The copy-the-template model is gone: this command writes everything a repo needs *from the
package payload*, so the working tree gains only state — `.agentloop/` (SSOT + materialized
prompts/schema/rules + scaffold snapshot + lock) and `docs/` (deliverable scaffolds) — plus a
marker-guarded pointer block in AGENTS.md. Nothing else is touched: no pyproject rewrite, no
makefile, no agent surfaces (those are opt-in: `agentloop install claude|copilot`).

Brownfield is auto-detected (any existing code layout / build manifest at the root): the
seeded config scopes `gates.guard_paths` to the docs deliverables only — pending gates must
not freeze development on existing code — fills the quality-gate test/check commands from the
repo's own tooling when recognizable (overridable with --test-cmd/--check-cmd), and the brief
carries the adopted-note pointing at /onboard. --greenfield/--brownfield override the
detection. Existing files are never overwritten (idempotent re-runs).

Usage:
  uvx --from git+<agentloop repo> agentloop init --name myproduct   # first contact
  agentloop init --name myproduct [--branch build/myproduct] [--source <url>]
  agentloop init                  # interactive wizard on a TTY
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

import agentloop
from agentloop import agent_cli, common, cycle
from agentloop import data as data_mod
from agentloop import install as install_mod
from agentloop import lock as lock_mod
from agentloop import repo as repo_mod

BRIEF_PATH = "docs/00-product-brief.md"

BRIEF_NOTE = (
    "\n> **Adopted into an existing codebase.** Write each cycle's brief as the *change* you want\n"
    "> (delta scope), not the whole product. Run /onboard first so docs/05-current-state.md maps\n"
    "> the existing implementation; /req and /design then start from that baseline and reuse\n"
    "> existing assets.\n"
)

# Root entries whose presence marks an existing codebase (brownfield). Directories the tool
# itself writes (.agentloop, docs, .claude, .github) deliberately don't count.
_CODE_MARKERS = (
    "src",
    "lib",
    "app",
    "backend",
    "frontend",
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
)


# --- pure text surgery (under test; ported from the retired init.py/adopt.py) ----


def fill_state(text: str, project: str, branch: str, today: str) -> str:
    """Fill the state.md front-matter placeholders, keeping trailing comments intact."""
    text = re.sub(r'^(project: ")[^"]*(")', rf"\g<1>{project}\g<2>", text, count=1, flags=re.MULTILINE)
    text = re.sub(r'^(branch: ")[^"]*(")', rf"\g<1>{branch}\g<2>", text, count=1, flags=re.MULTILINE)
    return re.sub(r'^(updated_at: ")[^"]*(")', rf"\g<1>{today}\g<2>", text, count=1, flags=re.MULTILINE)


def disable_template_mode(text: str) -> str:
    return re.sub(r"^(\s*template_mode:\s*)true\b", r"\g<1>false", text, count=1, flags=re.MULTILINE)


def brownfield_config(text: str, test_cmd: str, check_cmd: str) -> str:
    """Adapt the scaffold config.yaml for an existing repo (pure text surgery, comments survive)."""
    text = disable_template_mode(text)
    # Scope the guard to the docs deliverables only: pending gates must not freeze normal
    # development on the existing code. The commented lines document how to re-enable them.
    for key in ("src/", "lib/", "app/", "backend/", "frontend/", "scripts/"):
        text = re.sub(
            rf"^    ({re.escape(key)}: tasks.*)$",
            r"    # \1   # re-enable (or map your layout) when ready",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if test_cmd:
        text = text.replace('run: "make test"', f'run: "{test_cmd}"', 1)
    if check_cmd:
        text = text.replace('run: "make check"', f'run: "{check_cmd}"', 1)
    return text


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


def detect_commands(files: dict[str, str]) -> dict[str, list[str]]:
    """Best-effort test/check command detection from a repo's root manifests (pure).

    `files` maps a root file name to its content ("" for presence-only markers). Returns
    candidate commands most-specific first; the caller takes the first of each.
    """
    import json as json_mod

    test: list[str] = []
    check: list[str] = []
    pkg = files.get("package.json")
    if pkg:
        try:
            scripts = json_mod.loads(pkg).get("scripts") or {}
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


def is_brownfield(root: Path) -> bool:
    """True when the root already carries a codebase (see _CODE_MARKERS)."""
    return any((root / marker).exists() for marker in _CODE_MARKERS)


# --- application ------------------------------------------------------------------


def _root_files(root: Path) -> dict[str, str]:
    """Root manifests for detect_commands: name -> content (best-effort reads)."""
    out: dict[str, str] = {}
    for name in ("package.json", "pyproject.toml", "makefile", "Makefile"):
        try:
            out[name] = (root / name).read_text(encoding="utf-8")
        except OSError:
            continue
    for name in ("pnpm-lock.yaml", "yarn.lock", "uv.lock", "Cargo.toml", "go.mod"):
        if (root / name).exists():
            out[name] = ""
    return out


def _seed(root: Path, rel: str, content: bytes) -> bool:
    """Write a seed file unless it already exists. Returns True when written."""
    dest = root / rel
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return True


def _switch_branch(root: Path, branch: str) -> str:
    """Create/switch to the work branch (best-effort). Returns a status line for the summary."""
    rc, out = common.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root))
    if rc != 0:
        return f"git: not a repository — run `git init && git switch -c {branch}` yourself"
    if out.strip() == branch:
        return f"git: already on {branch}"
    rc, _ = common.run(["git", "switch", "-c", branch], cwd=str(root))
    if rc == 0:
        return f"git: created and switched to {branch}"
    rc, out = common.run(["git", "switch", branch], cwd=str(root))
    if rc == 0:
        return f"git: switched to existing {branch}"
    return f"git: could not switch to {branch} — {out.strip().splitlines()[-1] if out.strip() else 'unknown error'}"


def run_init(
    root: Path,
    name: str,
    branch: str,
    source: str,
    *,
    test_cmd: str = "",
    check_cmd: str = "",
    mode: str = "auto",
) -> int:
    """Seed the repo (SSOT + docs scaffolds + materialized artifacts + pointer block + lock)."""
    today = datetime.date.today().isoformat()
    root = root.resolve()
    brownfield = is_brownfield(root) if mode == "auto" else (mode == "brownfield")
    flavor = "brownfield (existing codebase detected)" if brownfield else "greenfield"
    print(f"init: {flavor}")

    if brownfield and not (test_cmd and check_cmd):
        detected = detect_commands(_root_files(root))
        test_cmd = test_cmd or (detected["test"][0] if detected["test"] else "")
        check_cmd = check_cmd or (detected["check"][0] if detected["check"] else "")
        for kind, cmd in (("test", test_cmd), ("check", check_cmd)):
            if cmd:
                print(f'  detected      quality-gate {kind} command: "{cmd}"')

    # 1) the SSOT trio, placeholder-filled (never overwriting an existing file).
    state_text = fill_state(data_mod.read_text("scaffold/agentloop/state.md"), name, branch, today)
    config_text = data_mod.read_text("scaffold/agentloop/config.yaml")
    config_text = brownfield_config(config_text, test_cmd, check_cmd) if brownfield else disable_template_mode(config_text)
    seeds: list[tuple[str, bytes]] = [
        (".agentloop/state.md", state_text.encode()),
        (".agentloop/config.yaml", config_text.encode()),
        (".agentloop/tasks.yaml", data_mod.read_bytes("scaffold/agentloop/tasks.yaml")),
    ]
    # 2) the docs scaffolds (with the brownfield note on the brief).
    for rel, blob in data_mod.iter_files("scaffold/docs"):
        dest_rel = "docs/" + rel[len("scaffold/docs/") :]
        if brownfield and dest_rel == BRIEF_PATH:
            blob = blob + BRIEF_NOTE.encode()
        seeds.append((dest_rel, blob))
    seeded: dict[str, str] = {}
    for rel, blob in seeds:
        wrote = _seed(root, rel, blob)
        print(f"  {'seed' if wrote else 'skip':<13} {rel}{'' if wrote else '  (already exists — left untouched)'}")
        if wrote:
            seeded[rel] = lock_mod.norm_hash(blob)
    (root / "docs" / "notes").mkdir(parents=True, exist_ok=True)

    repo = repo_mod.Repo(root)
    # 3) the materialized artifacts (prompts/schema/rules) + the lock skeleton they update.
    lock_data = lock_mod.read(repo.lock) or lock_mod.new(agentloop.__version__, source)
    if source:
        lock_data.setdefault("agentloop", {})["source"] = source
    existing_seeded = lock_data.get("seeded") if isinstance(lock_data.get("seeded"), dict) else {}
    lock_data["seeded"] = {**(existing_seeded or {}), **seeded}
    lock_mod.write(repo.lock, lock_data)
    rc = install_mod.sync(repo)
    if rc != 0:
        return rc
    # 4) the pristine scaffold snapshot cycle-close restores from.
    if cycle.snapshot_scaffold(docs_dir=str(repo.docs), scaffold_dir=str(repo.path(cycle.SCAFFOLD_DIR))):
        print(f"  snapshot      docs scaffolds → {cycle.SCAFFOLD_DIR}")
    # 5) the agent-neutral rules pointer (AGENTS.md), appended at most once.
    agents_md = root / "AGENTS.md"
    text = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else "# Repository rules\n"
    if install_mod.CLAUDE_IMPORT_MARKER not in text:
        agents_md.write_text(text + install_mod.agents_pointer_block(), encoding="utf-8")
        print("  merge         AGENTS.md (AgentLoop pointer block appended)")
    print(f"  {_switch_branch(root, branch)}")

    next_step = (
        "Next: run /onboard to map the existing code into docs/05-current-state.md, then start with /req."
        if brownfield
        else "Next: write a few lines into docs/00-product-brief.md and start with /req."
    )
    print(
        f'\nInitialized "{name}" (work branch: {branch}; the gate guard is live).\n'
        "Add an agent surface when you want one: `agentloop install claude` / `agentloop install copilot`.\n"
        + next_step
    )
    return 0


# --- interactive wizard (`agentloop init` / `agentloop start` on a TTY) ------------


def _ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input(shown).strip() or default


def _ask_agent_cli() -> str:
    """The headless-CLI question: a preset number, or a custom command string."""
    presets = list(agent_cli.PRESETS)
    print("4/5 headless agent CLI for mode A (`agentloop build`):")
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


def wizard(root: Path | None = None) -> int:
    """Interactive first-run setup: ask everything first, then write (Ctrl+C mid-question loses nothing)."""
    root = (root or Path.cwd()).resolve()
    print("AgentLoop setup — Enter accepts the [default]; Ctrl+C aborts without writing.")
    try:
        name = ""
        while not name:
            name = input("1/5 product name: ").strip()
        branch = _ask("2/5 work branch", f"build/{name}")
        source = _ask("3/5 agentloop source URL (recorded for `agentloop upgrade`; Enter to skip)")
        cli = _ask_agent_cli()
        summary = _ask_brief()
    except (KeyboardInterrupt, EOFError):
        print("\naborted — nothing was written.", file=sys.stderr)
        return 130
    rc = run_init(root, name, branch, source)
    if rc != 0:
        return rc
    if agent_cli.main([cli, "--repo", str(root)]) != 0:
        return 1
    if summary:
        brief = root / BRIEF_PATH
        try:
            brief.write_text(fill_brief(brief.read_text(encoding="utf-8"), summary), encoding="utf-8")
            print(f"  updated: {BRIEF_PATH} (your summary — flesh it out anytime)")
        except OSError as exc:
            print(f"could not write {BRIEF_PATH}: {exc} — add your summary there by hand.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentloop init", description="seed this repository with AgentLoop state")
    parser.add_argument("--name", default="", help="the product name (state.md project)")
    parser.add_argument("--branch", default="", help="the work branch (default: build/<name>)")
    parser.add_argument("--source", default="", help="the agentloop source URL (recorded in the lock for upgrade)")
    parser.add_argument("--test-cmd", default="", help="quality-gate test command (brownfield; else auto-detected)")
    parser.add_argument("--check-cmd", default="", help="quality-gate check command (brownfield; else auto-detected)")
    flavor = parser.add_mutually_exclusive_group()
    flavor.add_argument("--greenfield", action="store_true", help="skip the brownfield auto-detection")
    flavor.add_argument("--brownfield", action="store_true", help="force the brownfield adaptations")
    parser.add_argument("--repo", default=None, help="directory to initialize (default: cwd)")
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve() if args.repo else Path.cwd()
    name = args.name.strip()
    if not name:
        if sys.stdin.isatty():
            return wizard(root)
        print("usage: agentloop init --name <product> [--branch build/<product>] (or run on a TTY for the wizard)",
              file=sys.stderr)
        return 2
    branch = args.branch.strip() or f"build/{name}"
    mode = "greenfield" if args.greenfield else "brownfield" if args.brownfield else "auto"
    return run_init(root, name, branch, args.source.strip(), test_cmd=args.test_cmd.strip(),
                    check_cmd=args.check_cmd.strip(), mode=mode)
