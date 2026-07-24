"""The CI meta-policy: a base-side verifier that reads the head tree and refuses a weakening (plan §29).

A pull request cannot be trusted to check itself — the head could edit the very policy that judges it.
So CI runs *this* from the trusted base side, handing it the two commit SHAs from its own event context
(`github.event.pull_request.base.sha` / `head.sha`), never a branch name and never a base the head
declared. `policy-check` then reads the head tree read-only and fails closed on anything that would
re-open a boundary 0.9.0 closed (E2E-21):

- a non-exact ref (a branch, a short SHA, `HEAD`) for either side — a mutable base is no base at all;
- a legacy 0.8.x layout marker reappearing in the head tree (state.md / tasks.yaml / security-review.md);
- a banned config key that unbinds a gate (`gates.enforce_hook`, `build.headless.cmd`, `schema_version`,
  `post_build.security_review`, `skip_grounding`) — the Absolute-Block bypasses (plan §4.1, §15.4);
- a damaged audit chain (deletion, reorder, truncation, or a wholesale re-hash — E2E-22).

The one honesty this file owes the reader: introducing 0.9.0 is a bootstrap — there is no prior
0.9.0 base verifier to check the commit that adds this file, so that first run is *not* self-verified
and the workflow and README say so (plan §29.3).
"""

from __future__ import annotations

import argparse
import logging
import re

from agentloop import event_chain, strict_yaml
from agentloop import repo as repo_mod

logger = logging.getLogger(__name__)

_EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Config keys whose mere presence weakens a gate — the compatibility/bypass shims 0.9.0 deleted
#: (plan §4.1). `gates.enforce_hook` and friends are nested, so the scan is recursive by dotted path.
_BANNED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "gates.enforce_hook",
        "build.headless.cmd",
        "post_build.security_review",
        "skip_grounding",
    }
)

_LEGACY_TREE_PATHS: frozenset[str] = frozenset(
    {".agentloop/state.md", ".agentloop/tasks.yaml", ".agentloop/security-review.md"}
)


class PolicyCheckError(Exception):
    """policy-check could not run at all (a bad SHA, an unreadable tree) — distinct from a violation."""


def _show(repo: repo_mod.Repo, sha: str, path: str) -> str | None:
    """The content of `path` in the tree at `sha`, or None when it is not present there."""
    rc, out = repo._git_rc("show", f"{sha}:{path}")
    return out if rc == 0 else None


def _tree_paths(repo: repo_mod.Repo, sha: str) -> list[str]:
    rc, out = repo._git_rc("ls-tree", "-r", "--name-only", sha)
    if rc != 0:
        raise PolicyCheckError(f"cannot read the tree at {sha}: {out.strip()}")
    return [line for line in out.splitlines() if line]


def _banned_config_keys(text: str) -> list[str]:
    """Dotted paths present in a config document that are on the banned list (recursive)."""
    try:
        document = strict_yaml.load_mapping(text, what="config.yaml (head tree)")
    except strict_yaml.StrictParseError as exc:
        return [f"config.yaml does not parse under the strict loader: {exc}"]
    found: list[str] = []

    def walk(node: object, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if dotted in _BANNED_KEYS:
                found.append(dotted)
            walk(value, dotted)

    walk(document, "")
    return found


def check(repo: repo_mod.Repo, base_sha: str, head_sha: str) -> list[str]:
    """Every policy violation in the head tree; empty means CI may pass. Never short-circuits.

    `base_sha` is used only to prove the caller supplied an exact commit from a trusted context — it
    is never read *from* the head, which is the whole point of a base-side check (plan §29.3).
    """
    violations: list[str] = []
    for label, sha in (("--base-sha", base_sha), ("--head-sha", head_sha)):
        if not _EXACT_SHA.match(sha):
            violations.append(f"{label} {sha!r} is not an exact 40-hex commit SHA — a mutable ref is not a base")
    if violations:
        return violations  # nothing else can be trusted until the SHAs are exact

    for path in sorted(_LEGACY_TREE_PATHS & set(_tree_paths(repo, head_sha))):
        violations.append(f"the head tree reintroduces a 0.8.x layout marker: {path}")

    config_text = _show(repo, head_sha, ".agentloop/config.yaml")
    if config_text is not None:
        for key in _banned_config_keys(config_text):
            violations.append(f"the head config carries a banned key that would weaken a gate: {key}")

    _, defects = event_chain.scan(repo.events)
    if defects:
        violations.append(
            f"the audit chain has {len(defects)} defect(s) — deletion, reorder, truncation, or a "
            "re-hash breaks the chain a release attestation pins (E2E-22)"
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentloop policy-check",
        description="base-side CI meta-policy: read the head tree and refuse a weakening",
    )
    parser.add_argument("--base-sha", required=True, help="the PR base commit SHA (from CI, exact — never a branch)")
    parser.add_argument("--head-sha", required=True, help="the PR head commit SHA (from CI, exact)")
    parser.add_argument("--trust-manifest", default="", help="path to the external Trust Manifest (protected)")
    args = parser.parse_args(argv)

    from agentloop import common

    common.configure_logging()
    try:
        repo = repo_mod.get(None)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        violations = check(repo, args.base_sha, args.head_sha)
    except PolicyCheckError as exc:
        logger.error(str(exc))
        return 1
    if violations:
        logger.error("policy-check failed (%d violation(s)):", len(violations))
        for violation in violations:
            logger.error("  - %s", violation)
        return 1
    print(f"policy-check clear: head {args.head_sha[:12]} does not weaken the base policy")
    return 0
