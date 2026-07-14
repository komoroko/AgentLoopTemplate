"""The one human entry point for the daily AgentLoop verbs — `./agentloop <verb>`.

The template's human-facing surface is deliberately compressed to four memorable verbs
(everything else lives behind the UI's buttons or the operational make targets — see HELP):

  start   first run: interactive setup wizard; afterwards: where you are + what is next
  next    only the next recommended command (--json for integrations)
  ui      the local dashboard "driver's seat" (gate approval, doctor, revise, … as buttons)
  agent   switch the headless agent CLI mode A launches (claude | codex | gemini | custom)

This module is a thin dispatcher: each verb passes its remaining arguments straight to the
owning module's main() (status_api / ui / agent_cli / init), so there is exactly one
implementation per operation. `approve` is deliberately NOT a verb here: recording a gate
approval stays `make approve` only, so the "never pre-authorize it — the permission prompt is
the human's confirmation" rule guards a single spelling (AGENTS.md gate rule 2).

Usage:
  ./agentloop start
  ./agentloop next --json
  ./agentloop ui --read-only
  ./agentloop agent codex
"""

from __future__ import annotations

import sys

import agent_cli
import init
import status_api
import ui

HELP = """usage: agentloop <verb> [args]

daily verbs:
  start          first run: interactive setup wizard; afterwards: where you are + what's next
  next [--json]  only the next recommended command (deterministic; --json for integrations)
  ui [args]      local dashboard — approve gates, run doctor/revise/cycle-close from the page
  agent <cli>    switch the headless agent CLI (claude | codex | gemini | a custom command)

operational make targets (unchanged; see agentloop.mk):
  make approve GATE=<gate> [BY=<name>]   record a human gate approval (never pre-authorize)
  make revise ARGS='--to <phase> ...'    roll back upstream (gates reset in a chain)
  make doctor                            read-only environment + SSOT diagnosis
  make events ARGS=...                   view/record/resolve orchestration events
  make cycle-close NAME=<slug>           archive the finished delta cycle and reset
  make build-loop                        the deterministic /build orchestrator (mode A)
  make -f agentloop.mk agentloop-upgrade refresh template-owned tooling from a newer template
"""


def _start(rest: list[str]) -> int:
    """First run → the init wizard; an initialized repo → a one-line where-you-are + what's next."""
    if rest:
        print(f"agentloop start takes no arguments (got: {' '.join(rest)})", file=sys.stderr)
        return 2
    status = status_api.collect_status(".")
    rec = status["next"]
    assert isinstance(rec, dict)  # asdict(Recommendation)
    if rec.get("kind") == "setup":
        if not sys.stdin.isatty():
            print(
                "this checkout is not initialized and stdin is not a TTY — run the"
                " non-interactive `make init NAME=<product>` instead.",
                file=sys.stderr,
            )
            return 2
        return init.wizard()
    gates = status.get("gates")
    gate_rows = gates if isinstance(gates, list) else []
    approved = sum(1 for g in gate_rows if g.get("status") == "approved")
    print(
        f"project: {status.get('project')}   phase: {status.get('current_phase')}"
        f"   gates: {approved}/{len(gate_rows)} approved"
    )
    print(status_api.render_next(rec))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    verb, rest = args[0], args[1:]
    if verb == "start":
        return _start(rest)
    if verb == "next":
        return status_api.main(["--next", *rest])
    if verb == "ui":
        return ui.main(rest)
    if verb == "agent":
        return agent_cli.main(rest)
    print(f"agentloop: unknown verb '{verb}' — run `./agentloop --help` for the verb list", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
