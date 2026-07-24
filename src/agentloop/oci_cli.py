"""`agentloop oci build|verify|list` — build the sandbox images locally and pin their digests.

The sandbox a review runs in has to be reproducible from the repository, not fetched from a
registry that could serve different bytes tomorrow (plan §10.2). So the Containerfiles ship in
the package, `build` builds one locally and prints the `sha256:` digest to pin into a config
profile, and `verify` checks that a profile's pinned digest matches a local image — turning
"the oracle failed to start" into "build the image first".
"""

from __future__ import annotations

import argparse
import logging

from agentloop import common, executors, models
from agentloop import repo as repo_mod

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentloop oci", description="build and verify the sandbox images")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a packaged Containerfile locally and print its digest")
    build.add_argument("--profile", required=True, help=f"one of: {', '.join(executors.containerfile_names())}")

    sub.add_parser("verify", help="check that every OCI profile's pinned image is present locally")
    sub.add_parser("list", help="list the packaged Containerfiles")

    for name in ("build", "verify", "list"):
        sub.choices[name].add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")

    args = parser.parse_args(argv)
    common.configure_logging()

    if args.command == "list":
        print("\n".join(executors.containerfile_names()) or "(none)")
        return 0

    if args.command == "build":
        try:
            digest = executors.build_image(args.profile)
        except executors.ExecutorError as exc:
            logger.error(str(exc))
            return 1
        print(
            f"built agentloop-{args.profile}\n"
            f"digest: {digest}\n\n"
            f"Pin it in .agentloop/config.yaml under executor_profiles.{args.profile}:\n"
            f"  image: localhost/agentloop-{args.profile}@{digest}"
        )
        return 0

    # verify
    try:
        repo = repo_mod.get(args.repo)
        repo.require_supported_layout()
    except (repo_mod.RepoNotFoundError, repo_mod.UnsupportedLayoutError) as exc:
        logger.error(str(exc))
        return 1
    from agentloop import store as store_mod

    try:
        config = store_mod.Store(repo).read_config()
    except (models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    if config is None:
        logger.error("no .agentloop/config.yaml")
        return 1

    failures = 0
    for _name, profile in sorted(config.profiles.items()):
        ok, message = executors.verify_pinned(profile)
        print(f"  [{'PASS' if ok else 'FAIL'}] {message}")
        failures += 0 if ok else 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
