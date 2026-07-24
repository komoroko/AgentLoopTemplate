"""`agentloop oracle validate|negative-control|freeze|run|inspect` — drive the oracle boundary.

`freeze` succeeds only inside the gate-③ transaction (plan §20.3): freezing an oracle stamps a
digest the human's approval will bind, so doing it any other time would bind a digest to
nothing. The other verbs are diagnostic — `validate` and `negative-control` check readiness
before the freeze, `run` executes a frozen oracle in its sandbox, `inspect` shows what is
bound.
"""

from __future__ import annotations

import argparse
import logging

from agentloop import common, event_chain, models, oracle_bundle, oracles
from agentloop import repo as repo_mod
from agentloop import store as store_mod

logger = logging.getLogger(__name__)


def _load(repo: repo_mod.Repo, oracle_id: str) -> tuple[models.Plan, models.Oracle]:
    plan = store_mod.Store(repo).read_plan()
    if plan is None:
        raise OracleCliError("no plan yet — /design and /tasks define the oracles")
    oracle = plan.oracle(oracle_id)
    if oracle is None:
        raise OracleCliError(f"no oracle {oracle_id!r} in the plan (have: {', '.join(o.id for o in plan.oracles)})")
    return plan, oracle


class OracleCliError(RuntimeError):
    """A read-only oracle command could not run."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentloop oracle", description="the acceptance-oracle boundary")
    sub = parser.add_subparsers(dest="command", required=True)
    for verb, help_text in (
        ("validate", "check an oracle's bundle and negative controls are declared"),
        ("negative-control", "static check that the negative controls can demonstrate a rejection"),
        ("run", "run a frozen oracle in its sandbox"),
        ("inspect", "show what an oracle binds"),
        ("freeze", "freeze the bundle (only valid inside the gate-3 transaction)"),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument("oracle", help="the oracle id (e.g. O-002)")
        p.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")

    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
        repo.require_supported_layout()
    except (repo_mod.RepoNotFoundError, repo_mod.UnsupportedLayoutError) as exc:
        logger.error(str(exc))
        return 1

    try:
        plan, oracle = _load(repo, args.oracle)
    except (OracleCliError, models.DocumentError) as exc:
        logger.error(str(exc))
        return 1

    if args.command in {"validate", "negative-control"}:
        problems = oracle_bundle.check_negative_controls(oracle)
        if args.command == "validate":
            try:
                oracle_bundle.bundle_closure(repo, oracle.bundle_root)
            except oracle_bundle.OracleBundleError as exc:
                problems.append(str(exc))
        if problems:
            for problem in problems:
                logger.error(problem)
            return 1
        print(f"oracle {oracle.id}: ready ({len(oracle.negative_controls)} negative control(s))")
        return 0

    if args.command == "inspect":
        ok, message = oracle_bundle.verify_frozen(repo, oracle)
        print(f"oracle {oracle.id}\n  risk: {oracle.risk}\n  claims: {', '.join(oracle.claim_ids)}")
        print(f"  bundle: {message}")
        print(f"  negative controls: {len(oracle.negative_controls)}")
        return 0 if ok else 1

    if args.command == "freeze":
        # Freezing binds a digest the gate-3 approval covers; doing it outside that transaction
        # would stamp a digest onto nothing. `agentloop approve tasks` runs the freeze itself.
        logger.error(
            "`oracle freeze` runs inside the gate-3 transaction, not on its own — approve gate 3 "
            "(`agentloop approve tasks`) freezes every oracle bundle as part of the freeze."
        )
        return 2

    # run
    events, _ = event_chain.scan(repo.events)
    change_digest = event_chain.chain_root(events)  # a placeholder subject until the review pipeline binds one
    config = store_mod.Store(repo).read_config()
    try:
        result = oracles.run_oracle(repo, oracle, change_digest=change_digest, config=config)
    except (oracles.OracleError, oracle_bundle.OracleBundleError) as exc:
        logger.error(str(exc))
        return 1
    verdict = "PASS" if result.conclusive else "FAIL"
    print(f"oracle {oracle.id}: {verdict} (exit {result.exit_code}, image {result.image_digest[:19]}…)")
    for control in result.negative_controls:
        state = "rejected" if control.rejected else "DID NOT REJECT"
        print(f"  negative control {control.control_id}: {state} (exit {control.actual_exit_code})")
    if not result.conclusive:
        print(result.output[-2000:])
    return 0 if result.conclusive else 1


if __name__ == "__main__":
    raise SystemExit(main())
