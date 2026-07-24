"""Oracle bundles: the acceptance-test boundary, frozen at gate ③ so nobody can move it later.

An acceptance oracle is a judgement boundary *separate from the implementer's own unit tests*
(plan §9.1). The implementer writes code and unit tests under the same mental model; if that
model is wrong, both are wrong together. The oracle is written before the implementation, runs
in a sealed sandbox against a harness the implementer never touches, and answers one question
the implementer cannot lean on: does the code do what the claim says, under a scenario chosen
in advance?

Two properties this module enforces, both about *freezing*:

**The bundle is closed and committed.** Every file the oracle needs — harness, fixtures,
expected results, negative-control subjects — lives under `.agentloop/oracles/<id>/` and is a
committed git blob. :func:`freeze` records each path's blob id and a digest over the whole
closure, so a later edit to a fixture (the classic "make the oracle pass by changing what it
checks") moves the bundle digest and is caught (E2E-12).

**A high/critical oracle must be able to fail.** It has to reject a known-violating subject
before it can be frozen (:func:`check_negative_controls`). An oracle that exits 0 on a
conforming subject *and* on a violating one has observed nothing; freezing it would bind a
green light to a check that is not looking (plan §9.4, E2E-25).

The freeze is a `revise --to tasks` boundary: once frozen, changing the bundle means rolling
back and re-approving, because the receipt the human signed covers the bundle digest.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from agentloop import digests, models
from agentloop import repo as repo_mod


class OracleBundleError(RuntimeError):
    """A bundle is incomplete, uncommitted, or an oracle cannot be frozen."""


@dataclass(frozen=True)
class BundleBlob:
    """One committed file in a bundle: its repo-relative path and git blob id."""

    path: str
    blob: str


@dataclass(frozen=True)
class FrozenBundle:
    """A bundle's frozen state: its root, the closure of blobs, and a digest over them all."""

    oracle_id: str
    root: str
    blobs: tuple[BundleBlob, ...]
    digest: str

    def to_plan_bundle(self) -> dict[str, object]:
        return {
            "root": self.root,
            "digest": self.digest,
            "git_blobs": [{"path": b.path, "blob": f"git-blob:{b.blob}"} for b in self.blobs],
        }


def _git(repo: repo_mod.Repo, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", str(repo.root), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


def bundle_closure(repo: repo_mod.Repo, root: str) -> list[BundleBlob]:
    """Every committed file under a bundle root, with its blob id, in POSIX path order.

    Reads the committed tree (`git ls-tree HEAD`), not the working directory: an oracle bundle
    that is not committed is not frozen, and freezing what is only on disk would let an
    uncommitted edit ride along invisibly.
    """
    rc, out = _git(repo, "ls-tree", "-r", "-z", "HEAD", "--", root)
    if rc != 0:
        raise OracleBundleError(f"cannot list the bundle at {root} (is it committed?): {out.strip()}")
    entries = digests.parse_ls_tree(out)
    if not entries:
        raise OracleBundleError(
            f"the bundle at {root} has no committed files — commit the oracle harness before freezing it"
        )
    return [BundleBlob(path=e.path, blob=e.blob) for e in sorted(entries)]


def freeze(repo: repo_mod.Repo, oracle: models.Oracle) -> FrozenBundle:
    """Freeze one oracle's bundle: the blob closure plus a digest over it (plan §16.4 step 4)."""
    root = oracle.bundle_root
    if not root:
        raise OracleBundleError(f"oracle {oracle.id} declares no bundle root")
    blobs = bundle_closure(repo, root)
    # The digest covers path + blob id of every file, in order, so a renamed, added, removed, or
    # edited file all move it. A bundle whose digest matches is byte-for-byte the reviewed one.
    digest = digests.of_texts(f"{b.path}\0{b.blob}" for b in blobs)
    return FrozenBundle(oracle_id=oracle.id, root=root, blobs=tuple(blobs), digest=digest)


def verify_frozen(repo: repo_mod.Repo, oracle: models.Oracle) -> tuple[bool, str]:
    """(ok, message): does the committed bundle still hash to the digest the plan pinned?

    The check that catches the "edit a fixture to make the oracle pass" move: the plan's frozen
    digest is compared against a fresh closure of the committed bundle (E2E-12).
    """
    pinned = oracle.bundle_digest
    if not pinned:
        return False, f"oracle {oracle.id} has no frozen bundle digest"
    try:
        current = freeze(repo, oracle).digest
    except OracleBundleError as exc:
        return False, str(exc)
    if not digests.matches(current, pinned):
        return False, (
            f"oracle {oracle.id}: the committed bundle no longer matches the digest frozen at gate 3 — "
            "a harness or fixture changed. Roll back with `agentloop revise --to tasks` to re-freeze it."
        )
    return True, f"oracle {oracle.id}: bundle intact ({pinned[:19]}…)"


# --- negative controls ---------------------------------------------------------


@dataclass(frozen=True)
class NegativeControlResult:
    """Whether an oracle correctly *rejected* a known-violating subject."""

    control_id: str
    subject_fixture: str
    expected_exit_code: int
    actual_exit_code: int

    @property
    def rejected(self) -> bool:
        # A negative control passes when the oracle exits with the expected non-zero code — the
        # oracle *rejected* the violation, which is the whole point.
        return self.actual_exit_code == self.expected_exit_code


def check_negative_controls(oracle: models.Oracle) -> list[str]:
    """Static readiness problems with an oracle's negative controls (plan §9.4).

    The *running* of a control is done by :mod:`agentloop.oracles` in a sandbox; this is the
    gate-3 readiness check that the controls are even declared. A high/critical oracle with no
    control is refused — an oracle that never demonstrably fails proves nothing.
    """
    problems: list[str] = []
    if oracle.requires_negative_control and not oracle.negative_controls:
        problems.append(
            f"oracle {oracle.id} is {oracle.risk} but declares no negative control — a high/critical "
            "oracle must demonstrably fail on a known-violating subject before it can be frozen"
        )
    for control in oracle.negative_controls:
        expected = control.get("expected_exit_code")
        if expected == 0:
            problems.append(
                f"oracle {oracle.id}: negative control {control.get('id')} expects exit 0 — a control "
                "that expects success cannot demonstrate the oracle rejecting anything"
            )
        if not control.get("subject_fixture"):
            problems.append(f"oracle {oracle.id}: negative control {control.get('id')} names no subject fixture")
    return problems


# --- the gate-3 freeze transaction ---------------------------------------------


@dataclass(frozen=True)
class FreezeReport:
    """The outcome of freezing every oracle at gate ③."""

    bundles: tuple[FrozenBundle, ...]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    def bundle_set_digest(self) -> str:
        """One digest over every oracle's frozen bundle — the `oracle_bundle_set_digest`."""
        return digests.of_texts(f"{b.oracle_id}\0{b.digest}" for b in sorted(self.bundles, key=lambda b: b.oracle_id))


def freeze_all(repo: repo_mod.Repo, plan: models.Plan) -> FreezeReport:
    """Freeze every oracle and collect the readiness problems that would block gate ③.

    Pure with respect to the store — it computes the frozen bundles and the problems; writing
    the digests into the plan and stamping the receipt is the gate transaction's job, so this
    can be run read-only by `doctor` and by the gate's readiness check alike.
    """
    bundles: list[FrozenBundle] = []
    problems: list[str] = []
    for oracle in plan.oracles:
        problems += check_negative_controls(oracle)
        try:
            bundles.append(freeze(repo, oracle))
        except OracleBundleError as exc:
            problems.append(str(exc))
    return FreezeReport(bundles=tuple(bundles), problems=tuple(problems))
