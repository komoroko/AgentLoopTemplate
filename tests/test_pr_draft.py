"""Verify pr_draft.py — the read-only PR-body assembler (never calls gh)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import build_loop, pr_draft
from tests._support import seed_repo

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: verify
gates:
  requirements: approved   # 2026-07-01 alice
  design: approved   # 2026-07-02
  tasks: approved
  build: approved   # 2026-07-08
  release: pending       # decide after /verify
updated_at: "2026-07-09"
---
# board
"""

_TASKS = (
    "tasks:\n"
    "  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: done, test: make test, req: R-1}\n"
    "  - {id: T-002, title: leaf, kind: parallel, blockedBy: [T-001], status: done, test: make test, req: NFR-1}\n"
)

_REQUIREMENTS = "# req\n\n### R-1: parse input\n\n### NFR-1: performance\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    seed_repo(tmp_path, state=_STATE, tasks=_TASKS)
    (tmp_path / "docs" / "test").mkdir(parents=True)
    (tmp_path / "docs" / "10-requirements.md").write_text(_REQUIREMENTS, encoding="utf-8")
    (tmp_path / "docs" / "test" / "test-plan.md").write_text("# plan\n", encoding="utf-8")

    # A bespoke fake: pr_draft must shell out to git only, so the assertion guards that contract.
    def fake_run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int, str]:
        assert cmd[0] == "git", f"pr_draft must only shell out to git, got: {cmd}"
        if cmd[1] == "rev-parse":
            return 0, "abc1234def5678\n"
        if cmd[1] == "log":
            return 0, "abc1234 T-002: leaf\n1111111 T-001: base\n"
        return 1, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_draft_carries_gates_tasks_requirements_and_commits(project: Path) -> None:
    draft = pr_draft.build_draft("main")
    assert draft.startswith("# demo: build/demo")
    assert "- [x] requirements: approved (2026-07-01 alice)" in draft  # the approval record survives
    assert "- [ ] release: pending\n" in draft  # instruction comments on pending gates do NOT
    assert "### R-1" not in draft and "- R-1: parse input" in draft and "- NFR-1: performance" in draft
    assert "| T-001 | foundation | done | R-1 | base |" in draft
    assert "2/2 tasks done." in draft
    assert "abc1234 T-002: leaf" in draft


def test_draft_reports_security_review_freshness(project: Path) -> None:
    (project / ".agentloop" / "security-review.md").write_text("Reviewed-HEAD: abc1234def5678\n", encoding="utf-8")
    assert "(current)" in pr_draft.build_draft("main")
    (project / ".agentloop" / "security-review.md").write_text("Reviewed-HEAD: 9999999\n", encoding="utf-8")
    assert "(**STALE**)" in pr_draft.build_draft("main")


def test_draft_degrades_on_missing_files(project: Path) -> None:
    (project / ".agentloop" / "tasks.yaml").unlink()
    (project / "docs" / "10-requirements.md").unlink()
    (project / "docs" / "test" / "test-plan.md").unlink()
    draft = pr_draft.build_draft("main")
    assert "_(no readable tasks.yaml)_" in draft
    assert "Requirements in this cycle" not in draft
    assert "**absent**" in draft
    assert "no `.agentloop/security-review.md` report" in draft


def test_main_writes_file_and_prints_gh_hint_without_calling_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pr_draft.main([]) == 0
    out = capsys.readouterr().out
    assert Path(".agentloop/pr-draft.md").exists()
    assert "gh pr create --draft --base main --body-file .agentloop/pr-draft.md" in out  # a hint, never executed


def test_main_stdout_mode_writes_nothing(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert pr_draft.main(["--stdout", "--base", "develop"]) == 0
    assert not Path(".agentloop/pr-draft.md").exists()
    assert "## Commits (develop..HEAD)" in capsys.readouterr().out
