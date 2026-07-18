"""review_api: gate → fixed deliverable set, split-out self-assessment, and the gate-④ diff.

The reach-safety class matters most: the module decides *server-side* which files the review pane
may read, so these tests pin the template exclusion, the containment check on symlinks, the size
caps, and that agent-written markup never reaches the payload as live HTML.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentloop import review_api
from tests._support import make_state

MakeRepo = Callable[..., Path]


def _review(root: Path, gate: str) -> dict[str, Any]:
    """collect_review with assertion-friendly typing (the payload is JSON-shaped by contract)."""
    return review_api.collect_review(root, gate)


REQ_DOC = (
    "# Requirements\n\n## Summary\nvalue\n\n### R-1: thing\n- [ ] criterion\n\n"
    "## Self-assessment (assumptions, confidence)\n- **Confidence**: low (unverified integration)\n"
    "- **Assumptions made**: none\n"
)


def _write(root: Path, rel: str, text: str) -> Path:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


class TestGateMapping:
    def test_unknown_gate_raises(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        with pytest.raises(review_api.ReviewError):
            _review(root, "nope")

    def test_requirements_gate_deliverable_and_context(self, make_repo: MakeRepo) -> None:
        root = make_repo(
            state=make_state(phase="requirements", gates={g: "pending" for g in ("requirements", "design", "tasks")})
        )
        _write(root, "docs/10-requirements.md", REQ_DOC)
        _write(root, "docs/00-product-brief.md", "# Brief\ngoal")
        out = _review(root, "requirements")
        assert out["is_awaiting"] is True and out["awaiting"] == "requirements"
        (main,) = out["deliverables"]
        assert main["exists"] is True
        assert "<h2>Summary</h2>" in main["html"]
        assert main["self_assessment"]["confidence"] == "low"
        assert "Confidence" not in main["html"]  # the section is split out, not duplicated
        (ctx,) = out["context"]
        assert ctx["label"] == "docs/00-product-brief.md" and "<h1>Brief</h1>" in ctx["html"]

    def test_unfilled_confidence_placeholder_reads_unset(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, "docs/10-requirements.md", "## Self-assessment\n- **Confidence**: high / medium / low\n")
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["self_assessment"]["confidence"] is None

    def test_prose_mentioning_a_level_does_not_become_the_confidence(self, make_repo: MakeRepo) -> None:
        # The badge must never look better than the document: only the *labelled* Confidence line
        # counts, or an assumption written as "we have high confidence …" would badge a low
        # self-assessment as high.
        root = make_repo()
        _write(
            root,
            "docs/10-requirements.md",
            "## Self-assessment\n"
            "- **Assumptions made**: we have high confidence the runner exists\n"
            "- **Confidence**: low (integration unverified)\n",
        )
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["self_assessment"]["confidence"] == "low"

    def test_per_area_confidence_reports_the_weakest_area(self, make_repo: MakeRepo) -> None:
        # AGENTS.md asks for confidence by area, so several levels on one line is the filled-in
        # form, not the placeholder — and the low spot is the part the human must not miss.
        root = make_repo()
        _write(
            root,
            "docs/10-requirements.md",
            "## Self-assessment\n- **Confidence**: high (API surface), low (integration with CI)\n",
        )
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["self_assessment"]["confidence"] == "low"

    def test_per_area_placeholder_prose_still_reads_unset(self, make_repo: MakeRepo) -> None:
        # docs/20-design.md's scaffold line — "per area high / medium / low (e.g. …)".
        root = make_repo()
        _write(
            root,
            "docs/20-design.md",
            "## Self-assessment\n- **Confidence**: per area high / medium / low (e.g. architecture=high)\n",
        )
        main = _review(root, "design")["deliverables"][0]
        assert main["self_assessment"]["confidence"] is None

    def test_design_glob_excludes_template_and_sorts(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        for name in ("ADR-002.md", "ADR-001.md", "ADR-template.md"):
            _write(root, f"docs/decisions/{name}", f"# {name}")
        labels = [d["label"] for d in _review(root, "design")["deliverables"]]
        assert labels == ["docs/20-design.md", "docs/decisions/ADR-001.md", "docs/decisions/ADR-002.md"]

    def test_tasks_gate_renders_tickets_and_verbatim_yaml(self, make_repo: MakeRepo) -> None:
        root = make_repo(tasks="tasks: []  # <script>alert(1)</script>\n")
        _write(root, "docs/tasks/T-001.md", "# T-001\n\n## Self-assessment\n- **Confidence**: medium\n")
        _write(root, "docs/tasks/T-template.md", "# template")
        out = _review(root, "tasks")
        labels = [d["label"] for d in out["deliverables"]]
        assert labels == ["docs/tasks/T-001.md", ".agentloop/tasks.yaml"]
        yaml_entry = out["deliverables"][1]
        assert yaml_entry["kind"] == "code"
        assert yaml_entry["html"].startswith("<pre><code>")
        assert "<script>" not in yaml_entry["html"]

    def test_missing_deliverable_is_reported_absent(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["exists"] is False and main["html"] == ""

    def test_release_gate_counts_open_escalations(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        line = {"id": 1, "ts": "2026-07-19T00:00:00", "event": "blocked", "task": "T-001", "detail": "x"}
        _write(root, ".agentloop/events.ndjson", json.dumps(line) + "\n")
        assert _review(root, "release")["open_escalations"] == 1


class TestReachSafety:
    def test_symlink_escaping_the_repo_reads_absent(
        self, make_repo: MakeRepo, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        root = make_repo()
        outside = tmp_path_factory.mktemp("outside") / "secret.md"
        outside.write_text("# secret", encoding="utf-8")
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "10-requirements.md").symlink_to(outside)
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["exists"] is False and "secret" not in main["html"]

    def test_oversize_deliverable_is_truncated(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, "docs/10-requirements.md", "x" * (review_api._MAX_DELIVERABLE + 100))
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["truncated"] is True

    def test_agent_markup_never_survives_rendering(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, "docs/10-requirements.md", "# R\n<script>fetch('/api/gate/approve')</script>")
        (main,) = _review(root, "requirements")["deliverables"]
        assert "<script" not in main["html"]


class TestBuildGateDiff:
    def test_non_git_repo_degrades_to_error(self, make_repo: MakeRepo) -> None:
        out = _review(make_repo(), "build")
        assert "error" in out["diff"]
        assert out["review_meta"]["fresh"] is False

    def test_branch_diff_against_merge_base(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "a.txt", "base\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "base")
        _git(root, "checkout", "-qb", "build/demo")
        _write(root, "a.txt", "base\nnew line\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "T-001: change")
        out = _review(root, "build")
        diff = out["diff"]
        assert diff["base_ref"] == "main" and diff["base"] != diff["head"]
        assert "+new line" in diff["patch"]
        assert ["M", "a.txt"] in diff["name_status"]
        assert diff["truncated"] is False

    def test_head_at_base_falls_back_to_log(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "a.txt", "base\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "only commit")
        diff = _review(root, "build")["diff"]
        assert "patch" not in diff
        assert diff["log"] and "only commit" in diff["log"][0]

    def test_security_review_freshness(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "a.txt", "x\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "c")
        head = _git(root, "rev-parse", "HEAD")
        _write(root, ".agentloop/security-review.md", f"Reviewed-HEAD: {head}\nverdict: fine\n")
        assert _review(root, "build")["review_meta"]["fresh"] is True
        _write(root, ".agentloop/security-review.md", "Reviewed-HEAD: 0000000\n")
        meta = _review(root, "build")["review_meta"]
        assert meta["fresh"] is False and meta["reviewed_head"] == "0000000"
