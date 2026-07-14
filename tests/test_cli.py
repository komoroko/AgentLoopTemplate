"""Verify cli.py: the `agentloop` dispatcher stays a thin, predictable verb surface."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentloop import cli, init_cmd, ui

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: build
gates:
  requirements: approved      # 2026-07-01
  design: approved            # 2026-07-02
  tasks: approved             # 2026-07-03
  build: pending
  release: pending
updated_at: "2026-07-03"
---
# board
"""

_CONFIG = """build:
  headless:
    cmd: ["claude", "-p"]
gates:
  template_mode: false
"""


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    loop = tmp_path / ".agentloop"
    loop.mkdir()
    (loop / "state.md").write_text(_STATE, encoding="utf-8")
    (loop / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_help_lists_the_verbs_and_the_operations(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for verb in ("start", "next", "ui", "agent", "init", "install", "sync", "upgrade", "approve", "guard"):
        assert verb in out
    assert "NEVER pre-authorize" in out  # gate rule 2's single guarded spelling stays discoverable
    assert cli.main(["--help"]) == 0


def test_unknown_verb_points_at_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["frobnicate"]) == 2
    assert "--help" in capsys.readouterr().err


def test_next_passes_through_to_status_api(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["next"]) == 0
    assert capsys.readouterr().out.startswith("next: /build")
    assert cli.main(["next", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["command"] == "/build" and parsed["kind"] == "run_phase"


def test_agent_passes_through_to_agent_cli(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["agent", "codex"]) == 0
    assert 'cmd: ["codex", "exec"]' in (repo / ".agentloop" / "config.yaml").read_text(encoding="utf-8")


def test_ui_passes_its_args_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_ui_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(ui, "main", fake_ui_main)
    assert cli.main(["ui", "--read-only", "--port", "0"]) == 0
    assert seen == [["--read-only", "--port", "0"]]


# --- start: wizard on a fresh copy, orientation afterwards -------------------------


def test_start_initialized_prints_where_you_are_and_next(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert "project: demo" in out and "gates: 3/5 approved" in out
    assert "next: /build" in out


def test_start_uninitialized_non_tty_refuses_with_the_make_alternative(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".agentloop" / "config.yaml").write_text("gates:\n  template_mode: true\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["start"]) == 2
    assert "agentloop init --name" in capsys.readouterr().err


def test_start_uninitialized_tty_runs_the_wizard(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / ".agentloop" / "config.yaml").write_text("gates:\n  template_mode: true\n", encoding="utf-8")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    called: list[bool] = []

    def fake_wizard(root: Path | None = None) -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(init_cmd, "wizard", fake_wizard)
    assert cli.main(["start"]) == 0
    assert called == [True]


def test_start_rejects_extra_arguments(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start", "--force"]) == 2
    assert "no arguments" in capsys.readouterr().err
