"""The git/worktree mechanics build_loop drives — isolation, preservation, salvage, merge.

Kept apart from the Orchestrator so the data-loss-avoidance rules (nothing unmerged may be
the only copy) can be read and tested without the scheduling machinery around them; the
Orchestrator's git methods are thin delegates into one `GitWorkspace`. The subprocess runner
is injected and late-bound through `build_loop._run` — the single patch point the tests and
doctor/pr_draft already rely on (see build_loop's `_late_run`).
"""

from __future__ import annotations

from typing import Protocol

from agentloop import common, events
from agentloop import repo as repo_mod
from agentloop.common import StopLoop


class Runner(Protocol):
    """common.run's shape: (returncode, stdout+stderr merged)."""

    def __call__(self, cmd: list[str], cwd: str | None = None, timeout: float | None = None) -> tuple[int, str]: ...


class GitWorkspace:
    """Every git call of one build run, anchored to the repo root and the work branch."""

    def __init__(
        self,
        repo: repo_mod.Repo,
        branch: str,
        *,
        dry_run: bool,
        worktree_dir: str,
        branch_pattern: str,
        run: Runner,
    ) -> None:
        self.repo = repo
        self.root = str(repo.root)
        self.branch = branch
        self.dry_run = dry_run
        self.worktree_dir = worktree_dir
        self.branch_pattern = branch_pattern
        self._run = run

    def git(self, args: list[str], cwd: str | None = None) -> None:
        """Run one git command; StopLoop on failure; prints and no-ops under dry-run."""
        cwd = cwd or self.root
        if self.dry_run:
            print(f"    [dry-run] git {' '.join(args)} (cwd={cwd})")
            return
        rc, out = self._run(["git", *args], cwd=cwd)
        if rc != 0:
            raise StopLoop(f"git {' '.join(args)} failed (rc={rc})\n{out[-1000:]}")

    def branch_for(self, task_id: str) -> str:
        """The leaf branch name per branch_pattern."""
        return self.branch_pattern.format(branch=self.branch, task_id=task_id)

    def worktree_path(self, task_id: str) -> str:
        """The leaf worktree path under worktree_dir."""
        return str(self.repo.path(self.worktree_dir) / task_id)

    def tree_state(self, cwd: str) -> tuple[str, str]:
        """(HEAD hash, porcelain status) — the change-detection fingerprint."""
        _, head = self._run(["git", "rev-parse", "HEAD"], cwd=cwd)
        _, dirty = self._run(["git", "status", "--porcelain"], cwd=cwd)
        return head.strip(), dirty.strip()

    def head(self, cwd: str | None = None) -> str:
        """The current HEAD hash ("" when unavailable)."""
        _, out = self._run(["git", "rev-parse", "HEAD"], cwd=cwd or self.root)
        return out.strip()

    def finalize_commit(self, cwd: str, message: str) -> bool:
        """Commit any outstanding diff in `cwd` (excluding .agentloop/) — a no-op on a clean tree.

        The implementer is instructed to commit, but an uncommitted tree must never be the only
        copy: a leaf's worktree is removed with --force after the merge (or when blocked), and only
        what is on the branch survives. Finalizing here makes the branch the complete record.

        Returns False (after escalating) when a dirty tree could not be committed — a real failure
        (unset git identity, index lock, disk full) is the precursor of data loss, so the caller
        must keep the tree/worktree intact instead of removing it. The clean-tree no-op is decided
        by `git status --porcelain` up front, which is what makes a non-zero commit rc a genuine
        failure rather than "nothing to commit". The commit runs --no-verify: this is a
        preservation commit, not a quality decision (the quality gate already ran), and a hook
        rejection that silently drops the WIP would defeat its purpose. That also bypasses the
        commit-stage gate guard — covered instead by the loop's own merge/finalize-stage check
        (_gate_violations): everything a task changed is re-evaluated against the gate rules
        before it merges into the work branch (leaf) or is marked done (serial), so a stray
        out-of-scope edit escalates instead of landing silently in HEAD.
        """
        if self.dry_run:
            return True
        pathspec = [".", ":(exclude).agentloop"]
        rc, out = self._run(["git", "status", "--porcelain", "--", *pathspec], cwd=cwd)
        if rc == 0 and not out.strip():
            return True  # clean tree — nothing to preserve
        if rc == 0:
            rc, out = self._run(["git", "add", "-A", "--", *pathspec], cwd=cwd)
        if rc == 0:
            rc, out = self._run(["git", "commit", "--no-verify", "-m", message], cwd=cwd)
        if rc != 0:
            task_id = message.split(":", 1)[0]
            events.log_escalation(
                "blocked",
                f"{task_id}: finalize commit failed in {cwd} (rc={rc}) — the uncommitted diff is "
                f"preserved only in that tree, which is kept for manual recovery.\n"
                f"{common.summarize_failure('git finalize commit', rc, out)}",
                task=task_id,
            )
            return False
        return True
