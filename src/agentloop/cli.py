"""The `agentloop` console entry point — every verb of the installed harness.

One dispatcher, one implementation per operation: each verb hands its remaining arguments to
the owning module's entry function, so nothing is implemented twice. The daily verbs stay the
memorable four (start / next / ui / agent); the rest are the setup and operational commands
that used to hide behind make targets in the copy-distribution era.

`approve` is deliberately here (products have no makefile anymore) but must NEVER be
pre-authorized in an agent's permission settings: its permission prompt is the human's
approval confirmation (AGENTS.md gate rule 2 — the single spelling to guard is
`agentloop approve`).

Every invocation runs the cheap lock check (lock.startup_warning): a lock written by a newer
tool warns toward `uv tool upgrade agentloop`; a newer lock *format* is a hard error.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import agentloop
from agentloop import common
from agentloop import lock as lock_mod
from agentloop import repo as repo_mod

logger = logging.getLogger(__name__)

# verb → "module" or "module:function" (function defaults to `main`). Resolution is lazy and
# happens per call, so a verb's module is only imported when invoked (and monkeypatching a
# module's entry function in tests keeps working). `install` owns four verbs, hence its
# per-verb `cmd_*` names; every single-verb module exposes `main(argv)`.
VERBS: dict[str, str] = {
    "status": "status_api",
    "ui": "ui",
    "agent": "agent_cli",
    "project": "registry",
    "init": "init_cmd",
    "install": "install:cmd_install",
    "uninstall": "install:cmd_uninstall",
    "sync": "install:cmd_sync",
    "upgrade": "install:cmd_upgrade",
    "approve": "approve",
    "revise": "revise",
    "build": "build_loop",
    "doctor": "doctor",
    "events": "events",
    "cycle-close": "cycle",
    "issue-sync": "issue_sync",
    "pr-draft": "pr_draft",
    "guard": "gate_guard",
    "dag": "dag",
    "template-lint": "template_lint",
}


def _resolve(spec: str) -> Callable[[list[str] | None], int]:
    mod_name, _, func = spec.partition(":")
    module = importlib.import_module(f"agentloop.{mod_name}")
    entry: Callable[[list[str] | None], int] = getattr(module, func or "main")
    return entry


HELP = """usage: agentloop [--repo PATH] <verb> [args]

setup:
  init [--name N ...]     seed this repo with AgentLoop state (wizard on a TTY; brownfield auto-detected)
  install claude|copilot  add an agent's surfaces (wrappers, settings/hooks) — opt-in per environment
  uninstall <name>|--all  retract integration surfaces (pristine files only)
  sync [--check|--force]  rematerialize .agentloop/prompts|schema|rules from the installed package
  upgrade [--dry-run]     changelog transition + sync + refresh installed integrations

daily verbs:
  start                   first run: interactive setup wizard; afterwards: where you are + what's next
  next [--json]           only the next recommended command (deterministic; --json for integrations)
  ui [args]               local dashboard — approve gates, run doctor/revise/cycle-close from the page
  agent <cli>             switch the headless agent CLI (claude | codex | gemini | a custom command)
  project [add|use|...]   the named repos the ui switches between (add/list/remove/use)
  status [--json]         the full status object (/status reads this)

operations:
  approve <gate> [--by NAME]   record a human gate approval (NEVER pre-authorize this verb)
  revise --to <phase> ...      roll back upstream (gates reset in a chain)
  build [--dry-run]            the deterministic /build orchestrator (mode A)
  dag [--render|--trace|...]   derive/inspect the task DAG (read-only; /tasks & /status use it)
  doctor                       read-only environment + SSOT diagnosis
  events [args]                view/record/resolve orchestration events
  cycle-close --name <slug>    archive the finished delta cycle and reset
  issue-sync [--dry-run]       one-way mirror tasks.yaml -> GitHub Issues (opt-in)
  pr-draft [args]              assemble a PR body from the SSOT (read-only)
  guard [--check-diff]         the gate-guard hook / commit-stage check
  template-lint                drift canaries (template repo only; products exit 0)
  version                      print the tool version
"""


def _start(rest: list[str]) -> int:
    """First run → the init wizard; an initialized repo → a one-line where-you-are + what's next."""
    from agentloop import init_cmd, status_api

    if rest:
        logger.error(f"agentloop start takes no arguments (got: {' '.join(rest)})")
        return 2
    try:
        root = repo_mod.get().root
    except repo_mod.RepoNotFoundError:
        if not sys.stdin.isatty():
            logger.error(
                "this directory is not initialized and stdin is not a TTY — run the"
                " non-interactive `agentloop init --name <product>` instead."
            )
            return 2
        return init_cmd.wizard()
    status = status_api.collect_status(root)
    rec = status["next"]
    assert isinstance(rec, dict)  # asdict(Recommendation)
    if rec.get("kind") == "setup":
        if not sys.stdin.isatty():
            logger.error(
                "this repo is not initialized and stdin is not a TTY — run the"
                " non-interactive `agentloop init --name <product>` instead."
            )
            return 2
        return init_cmd.wizard(Path(root))
    gates = status.get("gates")
    gate_rows = gates if isinstance(gates, list) else []
    approved = sum(1 for g in gate_rows if g.get("status") == "approved")
    print(
        f"project: {status.get('project')}   phase: {status.get('current_phase')}"
        f"   gates: {approved}/{len(gate_rows)} approved"
    )
    print(status_api.render_next(rec))
    return 0


def _lock_check(repo_flag: str | None) -> int:
    """The cheap per-invocation lock check. 0 = go on; 1 = hard stop (newer lock format)."""
    try:
        repo = repo_mod.get(repo_flag)
    except repo_mod.RepoNotFoundError:
        return 0  # verbs that need a repo will say so themselves, with their own message
    try:
        warning = lock_mod.startup_warning(repo, agentloop.__version__)
    except lock_mod.LockError as exc:
        logger.error(f"agentloop: {exc}")
        return 1
    if warning:
        logger.warning(f"agentloop: {warning}")
    return 0


def main(argv: list[str] | None = None) -> int:
    common.configure_logging()
    args = sys.argv[1:] if argv is None else list(argv)
    # The global --repo (also accepted by every verb) may precede the verb.
    repo_flag: str | None = None
    if args[:1] == ["--repo"] and len(args) >= 2:
        repo_flag = args[1]
        args = args[2:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    verb, rest = args[0], args[1:]
    if repo_flag is not None:
        rest = [*rest, "--repo", repo_flag]

    if verb == "version":
        print(agentloop.__version__)
        return 0

    rc = _lock_check(repo_flag)
    if rc != 0:
        return rc

    if verb == "start":
        return _start(args[1:])
    if verb == "next":
        return _resolve("status_api")(["--next", *rest])
    spec = VERBS.get(verb)
    if spec is None:
        logger.error(f"agentloop: unknown verb '{verb}' — run `agentloop --help` for the verb list")
        return 2
    return _resolve(spec)(rest)


if __name__ == "__main__":
    raise SystemExit(main())
