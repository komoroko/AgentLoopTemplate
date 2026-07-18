"""Importable test helpers: the `.agentloop/` repo skeleton, a state builder, and a git fake.

Every phase/command test needs the same handful of things — a tmp repo carrying the SSOT
trio and a stand-in for the real `git` calls. Before this module each test file re-implemented
them; here they live once. The *fixtures* that wrap these (``make_repo``, ``chdir_tmp``) are in
``conftest.py``; this module holds only the pure, import-anywhere pieces:

- `make_state(...)` / `DEMO_STATE` / `DEMO_CONFIG` / `DEMO_TASKS` — the canonical "demo" baseline
  a test overrides only where its scenario differs (a mid-build repo, gates approved through
  tasks). `make_state` generalizes the per-file `_STATE_TMPL.format(...)` pattern.
- `seed_repo(root, ...)` — write the skeleton; a `None` argument skips that file.
- `fake_git(...)` — a `build_loop._run` stand-in built from command-prefix → `(rc, output)`
  rules, defaulting to success; pass `record=[]` to capture the calls made.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

# The five lifecycle gates, in order — the state.md front-matter always lists them all.
GATE_ORDER = ("requirements", "design", "tasks", "build", "release")

DEMO_CONFIG = (
    "build:\n"
    "  max_parallel: 3\n"
    "  headless:\n"
    '    cmd: ["claude", "-p"]\n'
    "  worktree: {enabled: true, dir: .worktrees, branch_pattern: '{branch}-{task_id}'}\n"
    "  quality_gate:\n"
    "    steps:\n"
    "      - {name: test, kind: cmd, run: 'make test'}\n"
    "      - {name: check, kind: cmd, run: 'make check'}\n"
    "gates:\n  enforce_hook: true\n  template_mode: false\n"
)

DEMO_TASKS = "tasks:\n  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: todo, test: make test}\n"


def make_state(
    *,
    phase: str = "build",
    branch: str = "build/demo",
    gates: dict[str, str] | None = None,
    updated_at: str = "2026-07-03",
    project: str = "demo",
) -> str:
    """Build a state.md front-matter body; `gates` overrides the mid-build baseline per key."""
    resolved = {
        "requirements": "approved",
        "design": "approved",
        "tasks": "approved",
        "build": "pending",
        "release": "pending",
    }
    resolved.update(gates or {})
    gate_lines = "\n".join(f"  {name}: {resolved[name]}" for name in GATE_ORDER)
    return (
        "---\n"
        f'project: "{project}"\n'
        f'branch: "{branch}"\n'
        f"current_phase: {phase}\n"
        f"gates:\n{gate_lines}\n"
        f'updated_at: "{updated_at}"\n'
        "---\n"
        "# board\n"
    )


# The mid-lifecycle baseline: on the build phase, approved through tasks, build/release pending.
DEMO_STATE = make_state()

# The docs scaffold cycle.py archives/restores (name -> body); mirrors the real scaffold layout.
_DOCS_SCAFFOLD = {
    "00-product-brief.md": "scaffold: 00-product-brief.md\n",
    "10-requirements.md": "scaffold: 10-requirements.md\n",
    "20-design.md": "scaffold: 20-design.md\n",
    "retrospective.md": "scaffold: retrospective.md\n",
    "decisions/ADR-template.md": "scaffold: adr\n",
    "tasks/T-template.md": "scaffold: task\n",
    "test/test-plan.md": "scaffold: test-plan\n",
}


def seed_repo(
    root: Path,
    *,
    state: str | None = DEMO_STATE,
    config: str | None = None,
    tasks: str | None = None,
    settings: str | None = None,
    docs: bool = False,
    git: bool = False,
) -> Path:
    """Write an `.agentloop/` skeleton under `root`; a `None` arg skips that file. Returns `root`."""
    loop = root / ".agentloop"
    loop.mkdir(parents=True, exist_ok=True)
    if state is not None:
        (loop / "state.md").write_text(state, encoding="utf-8")
    if config is not None:
        (loop / "config.yaml").write_text(config, encoding="utf-8")
    if tasks is not None:
        (loop / "tasks.yaml").write_text(tasks, encoding="utf-8")
    if settings is not None:
        claude = root / ".claude"
        claude.mkdir(exist_ok=True)
        (claude / "settings.json").write_text(settings, encoding="utf-8")
    if docs:
        for rel, body in _DOCS_SCAFFOLD.items():
            dest = root / "docs" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


RunFake = Callable[..., tuple[int, str]]


def fake_git(
    responses: dict[tuple[str, ...], tuple[int, str]] | None = None,
    *,
    record: list[list[str]] | None = None,
) -> RunFake:
    """A `build_loop._run` stand-in: first command-prefix match wins, default `(0, "")`.

    `record`, if given, receives each `cmd` list as it is called (order preserved).
    """
    rules = responses or {}

    def _run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        if record is not None:
            record.append(cmd)
        for prefix, result in rules.items():
            if tuple(cmd[: len(prefix)]) == tuple(prefix):
                return result
        return 0, ""

    return _run
