"""Switch the headless agent CLI (`agentloop agent <cli>`) without hand-editing config.yaml.

`build.headless.cmd` in .agentloop/config.yaml is the command mode A launches for every
headless run (implementer / review step / integration fixer / post-build security review).
Editing one YAML line is easy to get wrong (quoting, list syntax) and hard to discover, so
this small operation rewrites exactly that line: pass a known preset name (the same table the
config comment shows) or any custom command string, which is shlex-split into the argv list.

The rewrite is surgical line surgery (never a YAML round-trip), so every comment and the file
layout survive. The prompt is appended by build_loop.py as the last
argument — a CLI that cannot take it that way is the documented (and deliberately unbuilt)
extension point in the config comment, not this tool's concern.

Usage:
  agentloop agent codex
  agentloop agent "mytool run --flag"
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys

from agentloop import common
from agentloop import repo as repo_mod

CONFIG_PATH = common.CONFIG_PATH
# The known headless CLIs — kept in lockstep with the comment table in .agentloop/config.yaml.
PRESETS: dict[str, list[str]] = {
    "claude": ["claude", "-p"],
    "codex": ["codex", "exec"],
    "gemini": ["gemini", "-p"],
}


class AgentCliError(Exception):
    """A refused switch, with a message that already names the next step."""


def resolve_argv(cli: str) -> list[str]:
    """A preset name becomes its argv; anything else is shlex-split as a custom command."""
    if cli in PRESETS:
        return list(PRESETS[cli])
    argv = shlex.split(cli)
    if not argv:
        raise AgentCliError(
            f"empty command — pass a preset ({', '.join(PRESETS)}) or a custom command string,"
            ' e.g. agentloop agent "mytool run"'
        )
    return argv


def current_cmd(text: str) -> list[str] | None:
    """The argv currently on the `cmd:` line under `headless:` (None when the line is absent)."""
    m = _cmd_line(text)
    if m is None:
        return None
    try:
        parsed = json.loads(m.group("value"))
    except json.JSONDecodeError:
        return None
    return [str(item) for item in parsed] if isinstance(parsed, list) else None


def set_headless_cmd(text: str, argv: list[str]) -> str:
    """Rewrite only the `cmd: [...]` line under `headless:` (pure; comments/layout survive)."""
    m = _cmd_line(text)
    if m is None:
        raise AgentCliError(
            f"no `cmd:` line under `headless:` found in {CONFIG_PATH} — restore the"
            " `build.headless.cmd` key (see the template's config.yaml), then retry"
        )
    return text[: m.start("value")] + json.dumps(argv) + text[m.end("value") :]


def _cmd_line(text: str) -> re.Match[str] | None:
    """Match the first `cmd: [...]` line inside the `headless:` block (comments in between are fine)."""
    return re.search(
        r"^\s*headless:\s*(?:#.*)?\n(?:\s*(?:#.*)?\n)*?^\s*cmd:\s*(?P<value>\[[^\]\n]*\])",
        text,
        re.MULTILINE,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="switch the headless agent CLI mode A launches (build.headless.cmd)",
        epilog=f"presets: {', '.join(f'{k} = {json.dumps(v)}' for k, v in PRESETS.items())};"
        " anything else is used as a custom command string",
    )
    parser.add_argument("cli", help="a preset name (claude | codex | gemini) or a custom command string")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)

    try:
        config_path = repo_mod.get(args.repo).config
    except repo_mod.RepoNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        wanted = resolve_argv(args.cli)
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentCliError(f"cannot read {config_path}: {exc}") from None
        before = current_cmd(text)
        if before == wanted:
            print(f"headless.cmd is already {json.dumps(wanted)} (nothing to do)")
            return 0
        config_path.write_text(set_headless_cmd(text, wanted), encoding="utf-8")
    except AgentCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    shown_before = json.dumps(before) if before is not None else "(unreadable)"
    print(f"headless.cmd: {shown_before} → {json.dumps(wanted)}")
    print("  used by mode A (`agentloop build`) for implementer/review/security-review launches;")
    print("  the prompt is appended as the last argument.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
