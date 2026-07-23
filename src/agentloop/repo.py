"""Repository-root discovery, the absolute-path bundle, and the refusal to read a 0.8.x layout.

Installed as an external CLI the tool can be launched from anywhere, so the root is
*discovered* once per invocation and carried as resolved absolute paths. Resolution
precedence (the first hit wins; an explicit choice that does not hold an ``.agentloop/``
directory is an error, never silently walked past):

  1. ``--repo PATH``   (the CLI's global flag → the ``override`` argument)
  2. ``$AGENTLOOP_ROOT``
  3. walking up from ``start`` (default: cwd) to the first directory containing ``.agentloop/``

Two things are new in 0.9.0.

**Repository identity.** Runtime state (locks, the control socket) and the evidence cache no
longer live inside the working tree — they live under ``$XDG_RUNTIME_DIR`` and
``$XDG_CACHE_HOME`` keyed by :attr:`Repo.repo_id`. That id is derived from the realpath of the
*git common dir*, which is shared by a repository and all of its worktrees. A leaf worktree
and the canonical checkout therefore resolve to the same id — which is the whole point: in
0.8.x each worktree got its own ``.agentloop/`` lock inode, so two leaves could hold "the"
lock simultaneously and a decision recorded in a leaf vanished when the worktree was removed
(plan §11.1).

**Fail-closed on the old layout.** 0.9.0 does not read, migrate, or repair a 0.8.x repository.
:func:`unsupported_layout` detects one and every verb except ``doctor --unsupported-layout``
stops with an explicit message. There is deliberately no migration path: rebuilding a plan
from old artifacts would mean *manufacturing* evidence and authority for decisions that were
never grounded, which is exactly the failure 0.9.0 exists to prevent.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Files whose presence proves a repository is still on the 0.8.x layout.
LEGACY_MARKERS: tuple[str, ...] = (
    ".agentloop/state.md",
    ".agentloop/tasks.yaml",
    ".agentloop/security-review.md",
)

UNSUPPORTED_LAYOUT_MESSAGE = (
    "Unsupported AgentLoop layout detected.\n"
    "AgentLoop 0.9.0 does not read or migrate this repository.\n"
    "Archive the current cycle, remove .agentloop, and run `agentloop init` again."
)

_GIT_TIMEOUT_SEC = 10


class RepoNotFoundError(RuntimeError):
    """No .agentloop/ directory was found — the command has no repository to operate on."""


class UnsupportedLayoutError(RuntimeError):
    """The repository is on the 0.8.x layout. Carries the standard operator message."""

    def __init__(self, found: tuple[str, ...]) -> None:
        self.found = found
        super().__init__(f"{UNSUPPORTED_LAYOUT_MESSAGE}\n\nfound: {', '.join(found)}")


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
    """The discovered repository: one absolute root plus every derived path.

    Constructed once per invocation (see :func:`get`); everything downstream reads paths from
    here, so no tool depends on the process cwd.
    """

    root: Path
    _cache: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def path(self, rel: str) -> Path:
        """The absolute path of repo-relative posix path `rel`."""
        return self.root / rel

    def rel(self, p: str | Path) -> str | None:
        """`p` as a repo-relative posix path, or None when `p` lies outside the root.

        A hook fired from a subdirectory or from a leaf worktree still resolves against the
        discovered root.
        """
        resolved = Path(p) if Path(p).is_absolute() else self.root / p
        try:
            return resolved.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None

    # --- the four SSOT artifacts (plan §5.1) ---------------------------------

    @property
    def agentloop_dir(self) -> Path:
        return self.root / ".agentloop"

    @property
    def plan(self) -> Path:
        """The Expected Model — frozen at gate ③."""
        return self.root / ".agentloop/plan.yaml"

    @property
    def state(self) -> Path:
        """Mutable state only; written exclusively by the Central Store's transaction."""
        return self.root / ".agentloop/state.yaml"

    @property
    def review(self) -> Path:
        return self.root / ".agentloop/review.yaml"

    @property
    def events(self) -> Path:
        return self.root / ".agentloop/events.ndjson"

    # --- satellites ----------------------------------------------------------

    @property
    def config(self) -> Path:
        return self.root / ".agentloop/config.yaml"

    @property
    def lock(self) -> Path:
        return self.root / ".agentloop/agentloop.lock"

    @property
    def attestations(self) -> Path:
        """Signed envelopes. Git-managed: a gate receipt is worthless if the signature it
        names is not in the tree a reviewer can fetch."""
        return self.root / ".agentloop/attestations"

    @property
    def oracles(self) -> Path:
        return self.root / ".agentloop/oracles"

    @property
    def prompts(self) -> Path:
        return self.root / ".agentloop/prompts"

    @property
    def schema_dir(self) -> Path:
        return self.root / ".agentloop/schema"

    @property
    def scaffold(self) -> Path:
        return self.root / ".agentloop/scaffold"

    @property
    def rules(self) -> Path:
        return self.root / ".agentloop/AGENTS.agentloop.md"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    # --- identity and checkout kind ------------------------------------------

    def _git(self, *args: str) -> str:
        """One read-only git query against this root; "" on any failure (git absent, not a repo)."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    @property
    def git_common_dir(self) -> Path | None:
        """The realpath of the git *common* dir — shared by the repository and its worktrees.

        None when this is not a git checkout. Used for identity rather than the root path so
        that a leaf worktree and the canonical checkout agree on one lock and one store.
        """
        cached = self._cache.get("git_common_dir")
        if cached is None:
            raw = self._git("rev-parse", "--path-format=absolute", "--git-common-dir")
            resolved = Path(raw).resolve() if raw else None
            self._cache["git_common_dir"] = resolved or False
            return resolved
        return cached if isinstance(cached, Path) else None

    @property
    def repo_id(self) -> str:
        """A stable, filesystem-safe identity for this repository (16 hex chars).

        Derived from the git common dir when there is one, else from the resolved root — so a
        non-git directory still gets a private runtime/cache namespace instead of colliding
        with every other one.
        """
        cached = self._cache.get("repo_id")
        if isinstance(cached, str):
            return cached
        anchor = self.git_common_dir or self.root
        value = hashlib.sha256(str(anchor).encode("utf-8")).hexdigest()[:16]
        self._cache["repo_id"] = value
        return value

    @property
    def is_canonical_checkout(self) -> bool:
        """True when this is the main checkout rather than a linked worktree.

        Only the canonical checkout may mutate the store directly; a leaf worktree has to go
        through the control plane, or its decisions die with the worktree (plan §11.4).
        """
        common = self.git_common_dir
        if common is None:
            return True  # not a git checkout: there are no worktrees to be a leaf of
        git_dir = self._git("rev-parse", "--path-format=absolute", "--absolute-git-dir")
        return bool(git_dir) and Path(git_dir).resolve() == common

    # --- layout ---------------------------------------------------------------

    def legacy_markers(self) -> tuple[str, ...]:
        """Every 0.8.x artifact present in this repository, in declaration order."""
        return tuple(marker for marker in LEGACY_MARKERS if (self.root / marker).exists())

    def require_supported_layout(self) -> None:
        """Raise :class:`UnsupportedLayoutError` when this is still a 0.8.x repository."""
        found = self.legacy_markers()
        if found:
            raise UnsupportedLayoutError(found)


def get(override: str | None = None, start: Path | None = None) -> Repo:
    """find_root + Repo — the one constructor every entry point calls.

    Deliberately does *not* check the layout: `doctor --unsupported-layout` needs a Repo for a
    repository this version refuses to operate on. The layout check is a separate, explicit
    call the CLI makes for every other verb.
    """
    return Repo(find_root(start=start, override=override))
