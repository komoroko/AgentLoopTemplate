"""Verify feedback.py's upstream resolution, draft validation, dedup, and offline dry-run."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import feedback
import pytest

# --- pure logic ----------------------------------------------------------------

_DRAFTS = """issues:
  - title: "adopt: clearer upgrade summary"
    body: |
      The upgrade summary counts ops but not which gate they affect.
  - title: "docs: link the coverage lens"
    body: |
      README could link the requirements coverage lens.
"""


def test_parse_source_repo_accepts_github_forms() -> None:
    assert feedback.parse_source_repo("https://github.com/you/tpl") == "you/tpl"
    assert feedback.parse_source_repo("https://github.com/you/tpl.git") == "you/tpl"
    assert feedback.parse_source_repo("https://github.com/you/tpl/") == "you/tpl"
    assert feedback.parse_source_repo("git@github.com:you/tpl.git") == "you/tpl"
    assert feedback.parse_source_repo("/home/dev/AgentLoopTemplate") == ""  # local path: no repo
    assert feedback.parse_source_repo("https://gitlab.com/you/tpl") == ""
    assert feedback.parse_source_repo("") == ""


def test_resolve_upstream_prefers_config_over_manifest() -> None:
    manifest = "version: 1\ntemplate:\n  source: https://github.com/you/tpl.git\n"
    assert feedback.resolve_upstream("other/repo", manifest) == "other/repo"
    assert feedback.resolve_upstream("", manifest) == "you/tpl"
    assert feedback.resolve_upstream("", None) == ""
    assert feedback.resolve_upstream("", "template:\n  source: /local/path\n") == ""


def test_load_drafts_parses_titles_and_bodies() -> None:
    drafts = feedback.load_drafts(_DRAFTS)
    assert [d.title for d in drafts] == ["adopt: clearer upgrade summary", "docs: link the coverage lens"]
    assert "counts ops" in drafts[0].body


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty file
        "issues: []\n",  # empty list
        "notes: hi\n",  # missing key
        'issues:\n  - title: "x"\n',  # body missing
        'issues:\n  - body: "x"\n',  # title missing
        "issues: [nonsense]\n",  # not a mapping
    ],
)
def test_load_drafts_rejects_invalid_input(text: str) -> None:
    with pytest.raises(feedback.FeedbackError):
        feedback.load_drafts(text)


def test_draft_hash_is_deterministic_and_content_bound() -> None:
    a = feedback.Draft(title="t", body="b")
    assert feedback.draft_hash(a) == feedback.draft_hash(feedback.Draft(title="t", body="b"))
    assert feedback.draft_hash(a) != feedback.draft_hash(feedback.Draft(title="t", body="b2"))


def test_render_body_embeds_footer_and_marker() -> None:
    draft = feedback.Draft(title="t", body="the proposal")
    body = feedback.render_body(draft)
    assert body.startswith("the proposal\n")
    assert "make feedback" in body
    assert f"<!-- agentloop-feedback:{feedback.draft_hash(draft)} -->" in body
    # The marker round-trips through the search regex used for dedup.
    assert feedback._MARKER_RE.findall(body) == [feedback.draft_hash(draft)]


def test_plan_filings_skips_already_filed() -> None:
    drafts = feedback.load_drafts(_DRAFTS)
    filed = {feedback.draft_hash(drafts[0])}
    assert feedback.plan_filings(drafts, filed) == [drafts[1]]
    assert feedback.plan_filings(drafts, set()) == drafts


def test_preflight_skips_when_disabled() -> None:
    cfg = feedback.FeedbackConfig(enabled=False, repo="", label="agentloop-feedback")
    ready, reason = feedback.preflight(cfg, "you/tpl")
    assert not ready and "enabled=false" in reason


def test_preflight_skips_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = feedback.FeedbackConfig(enabled=True, repo="", label="agentloop-feedback")
    monkeypatch.setattr("shutil.which", lambda name: None)
    ready, reason = feedback.preflight(cfg, "you/tpl")
    assert not ready and "gh CLI" in reason


def test_preflight_skips_without_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = feedback.FeedbackConfig(enabled=True, repo="", label="agentloop-feedback")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    ready, reason = feedback.preflight(cfg, "")
    assert not ready and "github.feedback.repo" in reason


# --- main / gh execution ---------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".agentloop" / "config.yaml").write_text(
        'github:\n  feedback:\n    enabled: true\n    repo: ""\n    label: agentloop-feedback\n',
        encoding="utf-8",
    )
    (tmp_path / ".agentloop" / "adopt-manifest.yaml").write_text(
        "version: 1\ntemplate:\n  source: https://github.com/you/tpl.git\n  commit: abc\n",
        encoding="utf-8",
    )
    (tmp_path / ".agentloop" / "feedback.yaml").write_text(_DRAFTS, encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(prev)


def test_dry_run_is_offline_and_lists_the_plan(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(feedback, "_run", lambda cmd: pytest.fail("dry-run must not call gh"))
    assert feedback.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "upstream repo: you/tpl" in out  # resolved from the manifest's template source
    assert "adopt: clearer upgrade summary" in out and "docs: link the coverage lens" in out


def test_main_requires_a_drafts_file(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / ".agentloop" / "feedback.yaml").unlink()
    assert feedback.main(["--dry-run"]) == 1
    assert "/verify drafts it" in capsys.readouterr().err


def test_main_files_new_drafts_and_skips_filed_ones(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drafts = feedback.load_drafts(_DRAFTS)
    already = feedback.render_body(drafts[0])  # first draft already filed upstream
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> tuple[int, str]:
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return 0, json.dumps([{"body": already}])
        if cmd[:3] == ["gh", "label", "create"]:
            return 0, ""
        if cmd[:3] == ["gh", "issue", "create"]:
            return 0, "https://github.com/you/tpl/issues/42\n"
        pytest.fail(f"unexpected gh call: {cmd}")

    monkeypatch.setattr(feedback, "_run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    assert feedback.main([]) == 0
    out = capsys.readouterr().out
    assert "issues/42" in out
    assert "1 filed, 1 already filed" in out
    creates = [c for c in calls if c[:3] == ["gh", "issue", "create"]]
    assert len(creates) == 1
    assert ["--repo", "you/tpl"] == creates[0][3:5]
    assert "--label" in creates[0] and "agentloop-feedback" in creates[0]
    assert drafts[1].title in creates[0]


def test_main_retries_without_label_when_attaching_is_denied(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creates: list[list[str]] = []

    def fake_run(cmd: list[str]) -> tuple[int, str]:
        if cmd[:3] == ["gh", "issue", "list"]:
            return 0, "[]"
        if cmd[:3] == ["gh", "label", "create"]:
            return 1, "HTTP 403"  # no push access upstream: label cannot be provisioned
        if cmd[:3] == ["gh", "issue", "create"]:
            creates.append(cmd)
            return 0, "https://github.com/you/tpl/issues/7\n"
        pytest.fail(f"unexpected gh call: {cmd}")

    monkeypatch.setattr(feedback, "_run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    assert feedback.main([]) == 0
    assert creates and all("--label" not in c for c in creates)


def test_main_stops_when_dedup_snapshot_may_be_truncated(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    page = json.dumps([{"body": ""}] * feedback.FETCH_LIMIT)

    def fake_run(cmd: list[str]) -> tuple[int, str]:
        assert cmd[:3] == ["gh", "issue", "list"]
        return 0, page

    monkeypatch.setattr(feedback, "_run", fake_run)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    assert feedback.main([]) == 1
    assert "truncated" in capsys.readouterr().err
