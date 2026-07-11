"""PreToolUse hook: the mechanism layer that prevents editing a "next-phase deliverable"
while its prerequisite gate is unapproved.

It blocks in code, not relying on AGENTS.md's convention layer (each command checks its own gate).
Registered as a PreToolUse hook in `.claude/settings.json` (Claude Code, matched to Write/Edit)
and `.github/hooks/agentloop.json` (VS Code Copilot agent hooks, which ignore matchers and fire
on every tool), it cross-checks the edit's target path against the gates in `.agentloop/state.md`
and **denies** unless the prerequisite is approved.

Decision (the built-in default rules; override per repo with gates.guard_paths in config):
  docs/20-design.md, docs/decisions/**           → requirements must be approved
  docs/tasks/**                                  → design       must be approved
  backend/**, frontend/**, scripts/** (impl code) → tasks        must be approved
  docs/test/** (filling in test results)          → build        must be approved
However, scripts/agentloop/** (the template's foundational tools) is **always allowed** (so as not to block
the hook's own maintenance / speculative work). Any path not matched is also allowed unconditionally.
A brownfield repo (adopted into via adopt.py) typically scopes guard_paths to the docs deliverables
only — so existing code keeps flowing — and adds its own code paths (e.g. src/) when ready.

If gates.template_mode in `.agentloop/config.yaml` is true, always allow: the repo is the
template itself, whose scaffold originals share paths with product deliverables (`make init`
flips it to false when the template becomes a product).
If gates.enforce_hook is false, always allow
(if config cannot be read, it defaults to enabled = enforce-on, template_mode off).
If the state.md gates are unreadable (file missing / malformed front-matter), **fail closed (deny)**
for guarded paths: the guard is the only mechanism protecting the design/tasks phases
(build_loop.py double-checks only the build gate), so an unknown state must not silently
open every gate. The deny message points at the repair and the enforce_hook escape hatch.

I/O follows the hook convention shared by Claude Code and VS Code Copilot:
  stdin  : the hook event JSON (tool_name, tool_input.file_path, etc.). VS Code sends the
           target path camelCase (tool_input.filePath); both spellings are accepted.
  stdout : on deny, print JSON with hookSpecificOutput. On allow, print nothing.
  exit   : always 0 (the decision is conveyed via JSON).
A tool invocation that carries no file path always passes: under VS Code the hook fires for
every tool (reads, terminal, …), and the fail-closed rule above applies only to actual
guarded-path writes — never to path-less tools.

`--check-diff` is the **agent-agnostic commit-stage mode**: instead of one hook event, it takes
every path changed against HEAD (working tree, index, and untracked files via `git status
--porcelain`) and applies the same `evaluate()` rules; any denial lists the offending paths on
stderr and exits 1. Registered as a local pre-commit hook, it runs inside `make check` (= the
quality gate every agent's DoD executes) and on `git commit` once hooks are installed — so an
agent whose environment cannot intercept file edits (e.g. Codex), or an edit that bypasses the
tool hook (e.g. a shell redirect), is still gate-checked mechanically before the change lands.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import yaml

STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"

# Not guarded (always allowed regardless of gates). The template's foundational tools are where
# the hook itself runs, and the build gate must not block their maintenance.
_UNGUARDED_PREFIXES: tuple[str, ...] = ("scripts/agentloop/",)

# Built-in guard rules: path → prerequisite gate. A key ending in "/" guards the whole prefix;
# any other key guards that exact file. Overridable per repo via gates.guard_paths in config
# (a brownfield repo scopes this to the docs deliverables, or maps its own layout, e.g. src/).
# scripts/ requires tasks approval as product-script implementation code
# (scripts/agentloop/ is filtered out earlier by the exclusion above).
_DEFAULT_GUARD_PATHS: dict[str, str] = {
    "docs/20-design.md": "requirements",
    "docs/decisions/": "requirements",
    "docs/tasks/": "design",
    "docs/test/": "build",
    "backend/": "tasks",
    "frontend/": "tasks",
    "scripts/": "tasks",
}

_PHASE_LABEL = {
    "requirements": "/req (requirements)",
    "design": "/design (design)",
    "tasks": "/tasks (task plan)",
    "build": "/build (implementation)",
}


def _repo_relative(file_path: str) -> str | None:
    """Normalize the edit target to a repo-relative posix path. None if outside the repo."""
    try:
        rel = os.path.relpath(os.path.abspath(file_path), os.getcwd())
    except ValueError:
        return None
    if rel.startswith(".."):
        return None
    return PurePosixPath(rel).as_posix()


def required_gate(file_path: str, rules: dict[str, str] | None = None) -> str | None:
    """The gate name this edit requires. None if not guarded.

    An exact entry wins over prefix entries; among matching prefixes the longest wins
    (deterministic regardless of the config's key order).
    """
    rel = _repo_relative(file_path)
    if rel is None:
        return None
    if any(rel.startswith(p) for p in _UNGUARDED_PREFIXES):
        return None
    if rules is None:
        rules = _guard_paths()
    exact = rules.get(rel)
    if exact is not None:
        return exact
    best: tuple[int, str] | None = None
    for key, gate in rules.items():
        if key.endswith("/") and rel.startswith(key) and (best is None or len(key) > best[0]):
            best = (len(key), gate)
    return best[1] if best else None


def _gates_config() -> dict[str, object]:
    try:
        data = yaml.safe_load(Path(CONFIG_PATH).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}  # absent config = enabled defaults (fail-secure; fail-open only when state is absent)
    gates = data.get("gates")
    return gates if isinstance(gates, dict) else {}


def _enforce_enabled() -> bool:
    return bool(_gates_config().get("enforce_hook", True))


def _template_mode() -> bool:
    return bool(_gates_config().get("template_mode", False))


def _guard_paths() -> dict[str, str]:
    """The active guard rules: gates.guard_paths from config, or the built-in defaults."""
    raw = _gates_config().get("guard_paths")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    return _DEFAULT_GUARD_PATHS


def _read_gates() -> dict[str, str] | None:
    """Read the gates from state.md front-matter. None if unreadable (the caller fails closed)."""
    try:
        text = Path(STATE_PATH).read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return None
    gates = front.get("gates")
    if not isinstance(gates, dict):
        return None
    return {str(k): str(v) for k, v in gates.items()}


def evaluate(file_path: str) -> tuple[bool, str]:
    """Return (allowed, reason). When allowed=False, reason is the deny reason."""
    gate = required_gate(file_path)
    if gate is None:
        return True, ""
    if _template_mode() or not _enforce_enabled():
        return True, ""
    gates = _read_gates()
    if gates is None:  # unknown state fails closed: it must not silently open every gate
        return False, (
            "Blocked: cannot read the gates from .agentloop/state.md (file missing or malformed"
            " front-matter), so the gate guard fails closed. Repair state.md; in an emergency set"
            " gates.enforce_hook: false in .agentloop/config.yaml."
        )
    if gates.get(gate) == "approved":
        return True, ""
    phase = _PHASE_LABEL.get(gate, gate)
    return False, (
        f"Blocked: gate not approved. This edit requires approval of the prerequisite gate '{gate}'."
        f" Complete {phase} first and get human approval (check gates.{gate} in .agentloop/state.md)."
    )


def _changed_paths() -> list[str] | None:
    """Every path changed vs HEAD (worktree + index + untracked), repo-relative. None = git unusable.

    `git status --porcelain` covers all three in one call and, unlike `git diff HEAD`, also works
    in a repository that has no commit yet. `-uall` lists files inside untracked directories
    (the default collapses them to `dir/`, which would hide a brand-new `docs/tasks/T-001.md`).
    """
    try:
        proc = subprocess.run(["git", "status", "--porcelain", "-uall"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    paths = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename/copy: "R  old -> new" — the new path is what lands
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))  # git quotes paths with special characters
    return paths


def check_diff() -> int:
    """Commit-stage check: fail (1) when any changed path needs a gate that is not approved.

    The per-path decision is the same `evaluate()` the tool hooks use, so template_mode,
    enforce_hook, and the fail-closed rule for an unreadable state.md all carry over.
    """
    paths = _changed_paths()
    if paths is None:
        # Nothing to enforce against (not a git repo / git unavailable). The tool-hook layer,
        # where present, still guards individual edits.
        print("gate_guard --check-diff: git status unavailable; skipping.", file=sys.stderr)
        return 0
    denied = [(p, reason) for p in paths for ok, reason in [evaluate(p)] if not ok]
    if not denied:
        return 0
    print("gate_guard: changes to gate-guarded paths whose prerequisite gate is not approved:", file=sys.stderr)
    for path, reason in denied:
        print(f"  {path}: {reason}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "--check-diff" in argv:
        return check_diff()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # do not intervene if it cannot be parsed
    tool_input = payload.get("tool_input") or {}
    # Claude Code sends snake_case, VS Code Copilot camelCase — accept both.
    file_path = tool_input.get("file_path") or tool_input.get("filePath")
    if not isinstance(file_path, str) or not file_path:
        return 0
    allowed, reason = evaluate(file_path)
    if not allowed:
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(decision, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
