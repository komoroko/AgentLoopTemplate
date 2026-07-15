"""PreToolUse hook: the mechanism layer that prevents editing a "next-phase deliverable"
while its prerequisite gate is unapproved.

It blocks in code, not relying on AGENTS.md's convention layer (each command checks its own gate).
Registered as a PreToolUse hook by `agentloop install claude|copilot` (into
.claude/settings.json for Claude Code, .github/hooks/agentloop.json for VS Code Copilot), it
cross-checks the edit's target path against the gates in `.agentloop/state.md` and **denies**
unless the prerequisite is approved.

Decision (the built-in default rules; override per repo with gates.guard_paths in config):
  docs/20-design.md, docs/decisions/**           → requirements must be approved
  docs/tasks/**                                  → design       must be approved
  src/**, lib/**, app/**, backend/**, frontend/**, scripts/** (impl code) → tasks must be approved
  docs/test/** (filling in test results)          → build        must be approved
Any path not matched is allowed unconditionally — tests/** is deliberately unguarded so
approval-wait speculative work (fixtures, harness prep) keeps flowing. A brownfield repo
(`agentloop init` auto-detects one) typically scopes guard_paths to the docs deliverables only —
so existing code keeps flowing — and adds its own code paths (e.g. src/) when ready. (The harness
is an installed package, not repo source, so there is no self-protection path carve-out.)

If gates.template_mode in `.agentloop/config.yaml` is true, always allow: the repo is the
template itself, whose scaffold originals share paths with product deliverables (`agentloop
init` flips it to false in a product).
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

Besides the deliverable-path rules, both checkpoints guard **gate approval itself** (AGENTS.md
gate rule 2: only humans open a gate). The sanctioned pending→approved write path is
approve.py (`agentloop approve`), which also records a `gate_approved` event. Edit-time, a
Write/Edit whose result flips any state.md gate to `approved` is denied outright; commit-stage,
a flip against HEAD without a matching `gate_approved` event in `.agentloop/events.ndjson`
fails (that is how a shell-redirect flip that never passed the tool hook is still caught).
Unlike the path rules this check is **not** relaxed by `gates.template_mode`: the template
repo's scaffold state.md has no legitimate approved-flip either — only `gates.enforce_hook:
false` disables it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from agentloop import common
from agentloop import repo as repo_mod

STATE_PATH = common.STATE_PATH
CONFIG_PATH = common.CONFIG_PATH


def _repo_or_cwd(start: Path | None = None) -> repo_mod.Repo:
    """The discovered repo, or a cwd-anchored one when no .agentloop/ exists anywhere above.

    The fallback preserves the pre-discovery posture outside an AgentLoop repository: config
    and state reads fail there, which makes guarded-path writes fail closed (deny) exactly as
    before — a guard that cannot know its gates must not silently open them.
    """
    try:
        return repo_mod.get(start=start)
    except repo_mod.RepoNotFoundError:
        return repo_mod.Repo((start or Path.cwd()).resolve())


# Built-in guard rules: path → prerequisite gate. A key ending in "/" guards the whole prefix;
# any other key guards that exact file. Overridable per repo via gates.guard_paths in config
# (a brownfield repo scopes this to the docs deliverables, or maps its own layout).
# The code prefixes cover the common layouts (src/lib/app/backend/frontend/scripts); tests/ is
# deliberately NOT guarded — preparing fixtures/harness while a gate is pending is sanctioned
# speculative work (AGENTS.md "Minimizing the approval-wait bottleneck").
# scripts/ requires tasks approval as product-script implementation code. (The harness itself is
# an installed package now, not repo source, so no self-protection carve-out is needed.)
_DEFAULT_GUARD_PATHS: dict[str, str] = {
    "docs/20-design.md": "requirements",
    "docs/decisions/": "requirements",
    "docs/tasks/": "design",
    "docs/test/": "build",
    "src/": "tasks",
    "lib/": "tasks",
    "app/": "tasks",
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


def required_gate(file_path: str, rules: dict[str, str] | None = None, repo: repo_mod.Repo | None = None) -> str | None:
    """The gate name this edit requires. None if not guarded.

    An exact entry wins over prefix entries; among matching prefixes the longest wins
    (deterministic regardless of the config's key order).
    """
    repo = repo or _repo_or_cwd()
    rel = repo.rel(file_path)
    if rel is None:
        return None
    if rules is None:
        rules = _guard_paths(repo)
    exact = rules.get(rel)
    if exact is not None:
        return exact
    best: tuple[int, str] | None = None
    for key, gate in rules.items():
        if key.endswith("/") and rel.startswith(key) and (best is None or len(key) > best[0]):
            best = (len(key), gate)
    return best[1] if best else None


def _gates_config(repo: repo_mod.Repo) -> dict[str, object]:
    # absent config = enabled defaults (fail-secure; fail-open only when state is absent)
    data = common.read_yaml(str(repo.config)) or {}
    gates = data.get("gates")
    return gates if isinstance(gates, dict) else {}


def _enforce_enabled(repo: repo_mod.Repo) -> bool:
    return bool(_gates_config(repo).get("enforce_hook", True))


def _template_mode(repo: repo_mod.Repo) -> bool:
    return bool(_gates_config(repo).get("template_mode", False))


def _guard_paths(repo: repo_mod.Repo) -> dict[str, str]:
    """The active guard rules: gates.guard_paths from config, or the built-in defaults."""
    raw = _gates_config(repo).get("guard_paths")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    return _DEFAULT_GUARD_PATHS


def _read_gates(repo: repo_mod.Repo) -> dict[str, str] | None:
    """Read the gates from state.md front-matter. None if unreadable (the caller fails closed)."""
    try:
        front = common.parse_frontmatter(repo.state.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return common.gates_of(front)


def evaluate(file_path: str, repo: repo_mod.Repo | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). When allowed=False, reason is the deny reason."""
    repo = repo or _repo_or_cwd()
    gate = required_gate(file_path, repo=repo)
    if gate is None:
        return True, ""
    if _template_mode(repo) or not _enforce_enabled(repo):
        return True, ""
    gates = _read_gates(repo)
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


# --- gate-approval write protection (AGENTS.md gate rule 2, mechanism layer) --


def _gates_or_empty(text: str) -> dict[str, str]:
    """The gates mapping of a state.md text; {} for any unreadable/absent case.

    {} is the right posture for both callers: an unreadable *current* state makes every
    proposed `approved` count as a flip (fail closed for flips), and an unreadable *proposed*
    state has no approved gates to open (corrupting state.md is caught by the ordinary
    fail-closed rule the next time a guarded path is edited, not here).
    """
    try:
        gates = common.gates_of(common.parse_frontmatter(text))
    except yaml.YAMLError:
        return {}
    return gates or {}


def _proposed_state_text(current_text: str, tool_input: dict[str, Any]) -> str | None:
    """state.md's content as it would be after this Write/Edit/MultiEdit. None = unknown shape.

    Write carries the whole new content; Edit carries one old/new pair (both host spellings
    accepted); MultiEdit carries an `edits` list applied in order. The simulation mirrors the
    tools' own semantics closely enough for a gate-value comparison — an old_string that does
    not occur simply leaves the text unchanged (the real tool would error out anyway).
    """
    content = tool_input.get("content")
    if isinstance(content, str):
        return content
    edits = tool_input.get("edits")
    if not isinstance(edits, list):
        edits = [tool_input]
    text = current_text
    saw_edit = False
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        old = edit.get("old_string") or edit.get("oldString")
        new = edit.get("new_string") if "new_string" in edit else edit.get("newString")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            continue
        saw_edit = True
        if edit.get("replace_all") or edit.get("replaceAll"):
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
    return text if saw_edit else None


def gate_flip_denial(tool_input: dict[str, Any], repo: repo_mod.Repo | None = None) -> str:
    """Deny reason when this edit would flip a state.md gate to approved; "" to allow.

    Approval is a human operation: the only sanctioned write path is approve.py
    (`agentloop approve`), so any tool edit whose *result* turns a not-approved gate `approved`
    is denied — regardless of template_mode (see the module docstring). An edit payload
    whose shape cannot be simulated is allowed with a stderr trace; the commit-stage flip
    check still covers whatever it wrote.
    """
    repo = repo or _repo_or_cwd()
    try:
        current_text = repo.state.read_text(encoding="utf-8")
    except OSError:
        current_text = ""
    proposed_text = _proposed_state_text(current_text, tool_input)
    if proposed_text is None:
        print(
            "gate_guard: state.md write with an unrecognized payload shape — allowing"
            " (the commit-stage --check-diff flip check still applies)",
            file=sys.stderr,
        )
        return ""
    current = _gates_or_empty(current_text)
    flips = [g for g, v in _gates_or_empty(proposed_text).items() if v == "approved" and current.get(g) != "approved"]
    if not flips:
        return ""
    return (
        f"Blocked: this edit would set gates.{', gates.'.join(flips)} to approved. Opening a gate is a"
        " human operation — ask the human to run `agentloop approve <gate>`,"
        " which stamps the approval and records the gate_approved event. Never edit a gate line directly."
    )


def _head_state_gates(repo: repo_mod.Repo) -> dict[str, str] | None:
    """The gates in HEAD's state.md; None when HEAD has no copy (fresh repo / untracked state.md)."""
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{STATE_PATH}"], capture_output=True, text=True, timeout=30, cwd=repo.root
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _gates_or_empty(proc.stdout) if proc.returncode == 0 else None


def _flip_failures(repo: repo_mod.Repo) -> list[str]:
    """Commit-stage twin of gate_flip_denial: flips vs HEAD lacking a gate_approved event.

    approve.py writes the state.md flip and the event in one operation, so a legitimate
    approval always passes; a flip smuggled past the tool hook (shell redirect, sed) has no
    event and fails here before it can land. With no committed baseline (HEAD has no
    state.md) there is no diff to judge a flip against, so the check does not apply — the
    edit-time hook already denies *creating* a state.md with approved gates.
    """
    from agentloop import events  # lazy: keep the edit-time hook path free of the extra import

    try:
        worktree = _gates_or_empty(repo.state.read_text(encoding="utf-8"))
    except OSError:
        return []
    head = _head_state_gates(repo)
    if head is None:
        return []
    flips = [g for g, v in worktree.items() if v == "approved" and head.get(g) != "approved"]
    if not flips:
        return []
    recorded = {e.gate for e in events.load_events(str(repo.events)) if e.event == "gate_approved"}
    return [
        f"gates.{g}: flipped to approved with no gate_approved event — approvals are recorded with"
        f" `agentloop approve {g}`, never by editing state.md"
        for g in flips
        if g not in recorded
    ]


def _changed_paths(repo: repo_mod.Repo) -> list[str] | None:
    """Every path changed vs HEAD (worktree + index + untracked), repo-relative. None = git unusable.

    `git status --porcelain` covers all three in one call and, unlike `git diff HEAD`, also works
    in a repository that has no commit yet. `-uall` lists files inside untracked directories
    (the default collapses them to `dir/`, which would hide a brand-new `docs/tasks/T-001.md`).
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-uall"], capture_output=True, text=True, timeout=30, cwd=repo.root
        )
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


def check_diff(repo: repo_mod.Repo | None = None) -> int:
    """Commit-stage check: fail (1) when any changed path needs a gate that is not approved.

    The per-path decision is the same `evaluate()` the tool hooks use, so template_mode,
    enforce_hook, and the fail-closed rule for an unreadable state.md all carry over.
    """
    repo = repo or _repo_or_cwd()
    paths = _changed_paths(repo)
    if paths is None:
        # Nothing to enforce against (not a git repo / git unavailable). The tool-hook layer,
        # where present, still guards individual edits.
        print("gate_guard --check-diff: git status unavailable; skipping.", file=sys.stderr)
        return 0
    # git status paths are repo-relative; anchor them so evaluate() judges them against the
    # discovered root no matter where the process was launched.
    denied = [(p, reason) for p in paths for ok, reason in [evaluate(str(repo.path(p)), repo)] if not ok]
    # A changed state.md is additionally checked for approved-flips (module docstring: not
    # relaxed by template_mode — only the enforce_hook escape hatch disables it).
    flips = _flip_failures(repo) if STATE_PATH in paths and _enforce_enabled(repo) else []
    if not denied and not flips:
        return 0
    if denied:
        print("gate_guard: changes to gate-guarded paths whose prerequisite gate is not approved:", file=sys.stderr)
        for path, reason in denied:
            print(f"  {path}: {reason}", file=sys.stderr)
    for failure in flips:
        print(f"  {failure}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if "--check-diff" in argv:
        return check_diff()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Fail-open by design: some hosts fire hooks for every tool and a malformed payload must
        # not block path-less tools — but leave a trace, so a guard that stopped guarding is
        # visible in the hook log instead of silently absent.
        print("gate_guard: unparseable hook payload on stdin — allowing without a gate check", file=sys.stderr)
        return 0
    tool_input = payload.get("tool_input") or {}
    # Claude Code sends snake_case, VS Code Copilot camelCase — accept both.
    file_path = tool_input.get("file_path") or tool_input.get("filePath")
    if not isinstance(file_path, str) or not file_path:
        return 0
    # The hook payload carries the session's cwd — resolve the repo from there (a hook fired
    # from a subdirectory or a worktree still finds the right root), falling back to our own cwd.
    payload_cwd = payload.get("cwd")
    start = Path(payload_cwd) if isinstance(payload_cwd, str) and payload_cwd else None
    repo = _repo_or_cwd(start)
    allowed, reason = evaluate(file_path, repo)
    # state.md is not a guarded deliverable, but its gate lines are write-protected: an edit
    # that flips a gate to approved is denied even in template_mode (only enforce_hook: false
    # bypasses — module docstring).
    if allowed and repo.rel(file_path) == STATE_PATH and _enforce_enabled(repo):
        denial = gate_flip_denial(tool_input, repo)
        if denial:
            allowed, reason = False, denial
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
