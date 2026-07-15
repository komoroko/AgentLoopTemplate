"""Verify agent_cli.py: the headless-CLI switch (`./agentloop agent <cli>`) rewrites one line only."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentloop import agent_cli

_CONFIG = """# top comment stays
build:
  max_parallel: 3

  # The headless agent CLI mode A launches. Comment lines survive the switch.
  headless:
    cmd: ["claude", "-p"]

  timeouts:
    cmd_sec: 1800            # must never be touched by the switch
    agent_sec: 3600
"""


# --- resolve_argv / set_headless_cmd: the pure pieces ------------------------------


def test_presets_resolve_to_their_argv() -> None:
    assert agent_cli.resolve_argv("claude") == ["claude", "-p"]
    assert agent_cli.resolve_argv("codex") == ["codex", "exec"]
    assert agent_cli.resolve_argv("gemini") == ["gemini", "-p"]


def test_custom_command_is_shlex_split() -> None:
    assert agent_cli.resolve_argv('mytool run --model "gpt x"') == ["mytool", "run", "--model", "gpt x"]


def test_empty_command_is_refused() -> None:
    with pytest.raises(agent_cli.AgentCliError, match="preset"):
        agent_cli.resolve_argv("   ")


def test_set_headless_cmd_rewrites_only_that_line() -> None:
    out = agent_cli.set_headless_cmd(_CONFIG, ["codex", "exec"])
    assert '    cmd: ["codex", "exec"]\n' in out
    # everything else — comments, the timeouts block's cmd_sec — is byte-identical
    assert out.replace('cmd: ["codex", "exec"]', 'cmd: ["claude", "-p"]') == _CONFIG
    assert "cmd_sec: 1800" in out


def test_set_headless_cmd_is_idempotent() -> None:
    once = agent_cli.set_headless_cmd(_CONFIG, ["gemini", "-p"])
    assert agent_cli.set_headless_cmd(once, ["gemini", "-p"]) == once


def test_missing_headless_block_is_refused_with_next_step() -> None:
    with pytest.raises(agent_cli.AgentCliError, match="build.headless.cmd"):
        agent_cli.set_headless_cmd("build:\n  max_parallel: 3\n", ["codex", "exec"])


def test_current_cmd_reads_the_line() -> None:
    assert agent_cli.current_cmd(_CONFIG) == ["claude", "-p"]
    assert agent_cli.current_cmd("build: {}\n") is None


# --- main: the CLI over a real config file -----------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".agentloop" / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_main_switches_and_reports_before_after(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_cli.main(["codex"]) == 0
    out = capsys.readouterr().out
    assert '["claude", "-p"] → ["codex", "exec"]' in out
    text = (repo / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert 'cmd: ["codex", "exec"]' in text
    assert "# must never be touched by the switch" in text


def test_main_noop_when_already_set(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = (repo / ".agentloop" / "config.yaml").read_text(encoding="utf-8")
    assert agent_cli.main(["claude"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert (repo / ".agentloop" / "config.yaml").read_text(encoding="utf-8") == before


def test_main_missing_config_names_next_step(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".agentloop" / "config.yaml").unlink()
    assert agent_cli.main(["codex"]) == 1
    assert "config.yaml" in capsys.readouterr().err
