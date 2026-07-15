"""Repository-root discovery and the absolute-path bundle every tool works from.

Before this module the whole toolset assumed ``cwd == repo root``: the SSOT constants in
common.py were cwd-relative strings, gate_guard relativized hook paths against
``os.getcwd()``, and the ``./agentloop`` wrapper manufactured the invariant with
``cd $(dirname $0)``. Installed as an external CLI the tool can be launched from anywhere,
so the root is now *discovered* once per invocation and carried as resolved absolute paths.

Resolution precedence (the first hit wins; an explicit choice that does not hold an
``.agentloop/`` directory is an error, never silently walked past):

  1. ``--repo PATH``   (the CLI's global flag → the ``override`` argument)
  2. ``$AGENTLOOP_ROOT``
  3. walking up from ``start`` (default: cwd) to the first directory containing ``.agentloop/``

Threading rule: ``main()`` entry points resolve a :class:`Repo` once and pass it down;
library functions take explicit paths. Module-level constants survive only as repo-relative
*names* (common.STATE_PATH etc.), joined via :meth:`Repo.path`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class RepoNotFoundError(RuntimeError):
    """No .agentloop/ directory was found — the command has no repository to operate on."""


def _has_marker(candidate: Path) -> bool:
    return (candidate / ".agentloop").is_dir()


def find_root(start: Path | None = None, override: str | None = None) -> Path:
    """The repository root, per the module-docstring precedence. Always absolute, resolved."""
    if override:
        root = Path(override).resolve()
        if not _has_marker(root):
            raise RepoNotFoundError(f"--repo {override}: no .agentloop/ directory there — not an AgentLoop repository")
        return root
    env = os.environ.get("AGENTLOOP_ROOT", "")
    if env:
        root = Path(env).resolve()
        if not _has_marker(root):
            raise RepoNotFoundError(f"AGENTLOOP_ROOT={env}: no .agentloop/ directory there — unset it or fix the path")
        return root
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if _has_marker(candidate):
            return candidate
    raise RepoNotFoundError(
        f"no .agentloop/ found walking up from {current} — run `agentloop init` there, "
        "pass --repo PATH, or set AGENTLOOP_ROOT"
    )


@dataclass(frozen=True)
class Repo:
    """The discovered repository: one absolute root plus every derived SSOT path.

    Constructed once per invocation (see :func:`get`); everything downstream reads paths
    from here so no tool ever depends on the process cwd again.
    """

    root: Path

    def path(self, rel: str) -> Path:
        """The absolute path of repo-relative posix path `rel`."""
        return self.root / rel

    def rel(self, p: str | Path) -> str | None:
        """`p` as a repo-relative posix path, or None when `p` lies outside the root.

        Replaces the old ``os.path.relpath(..., os.getcwd())`` in gate_guard: a hook fired
        from a subdirectory or a worktree still resolves against the discovered root.
        """
        resolved = Path(p) if Path(p).is_absolute() else self.root / p
        try:
            return resolved.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None

    # --- the SSOT trio and its satellites (absolute) --------------------------------

    @property
    def agentloop_dir(self) -> Path:
        return self.root / ".agentloop"

    @property
    def state(self) -> Path:
        return self.root / ".agentloop/state.md"

    @property
    def config(self) -> Path:
        return self.root / ".agentloop/config.yaml"

    @property
    def tasks(self) -> Path:
        return self.root / ".agentloop/tasks.yaml"

    @property
    def events(self) -> Path:
        return self.root / ".agentloop/events.ndjson"

    @property
    def lock(self) -> Path:
        return self.root / ".agentloop/agentloop.lock"

    @property
    def prompts(self) -> Path:
        return self.root / ".agentloop/prompts"

    @property
    def schema_dir(self) -> Path:
        return self.root / ".agentloop/schema"

    @property
    def scaffold(self) -> Path:
        return self.root / ".agentloop/scaffold/docs"

    @property
    def rules(self) -> Path:
        return self.root / ".agentloop/AGENTS.agentloop.md"

    @property
    def docs(self) -> Path:
        return self.root / "docs"


def get(override: str | None = None, start: Path | None = None) -> Repo:
    """find_root + Repo — the one constructor every entry point calls."""
    return Repo(find_root(start=start, override=override))
