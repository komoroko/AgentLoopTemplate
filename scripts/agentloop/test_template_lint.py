"""Verify template_lint.py's drift canaries — and run them against the live repo (the real gate)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import template_lint
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_STATE = """---
project: "demo"
gates:
  requirements: pending
  design: pending
  release: pending
---
"""

_CONFIG = """build:
  quality_gate:
    steps:
      - name: test
        kind: cmd
      - name: review
        kind: agent
"""

_CLAUDE = "kinds: foundation / parallel / integration. gates: requirements, design, release. steps: test, review.\n"
_TASKS_CMD = "kind: foundation | parallel | integration. status: todo in_progress blocked needs-revision done.\n"


def _files(**overrides: str) -> dict[str, str]:
    files = {
        template_lint.CLAUDE_MD: _CLAUDE,
        template_lint.TASKS_CMD: _TASKS_CMD,
        template_lint.STATE_PATH: _STATE,
        template_lint.CONFIG_PATH: _CONFIG,
    }
    files.update(overrides)
    return files


# --- vocabulary ------------------------------------------------------------------


def test_gate_names_reads_the_front_matter() -> None:
    assert template_lint.gate_names(_STATE) == ["design", "release", "requirements"]
    assert template_lint.gate_names("no front matter") == []


def test_quality_gate_steps_reads_the_dod_names() -> None:
    assert template_lint.quality_gate_steps(_CONFIG) == ["test", "review"]
    assert template_lint.quality_gate_steps("build: {}\n") == []


def test_check_vocabulary_is_green_when_everything_is_echoed() -> None:
    assert template_lint.check_vocabulary(_files()) == []


def test_check_vocabulary_trips_on_a_missing_kind() -> None:
    files = _files(**{template_lint.TASKS_CMD: _TASKS_CMD.replace("integration", "join")})
    failures = template_lint.check_vocabulary(files)
    assert any("tasks.md" in f and "`integration`" in f for f in failures)


def test_check_vocabulary_trips_on_a_missing_quality_gate_step() -> None:
    files = _files(**{template_lint.CLAUDE_MD: _CLAUDE.replace("review", "critique")})
    failures = template_lint.check_vocabulary(files)
    assert any("CLAUDE.md" in f and "`review`" in f for f in failures)


# --- README parity ---------------------------------------------------------------

_EN = "## A\n## B\nRun `make init` then `make -f agentloop.mk agentloop-upgrade`.\nSee scripts/agentloop/dag.py.\n"
_JA = "## あ\n## い\n`make init` の後 `make -f agentloop.mk agentloop-upgrade`。\nscripts/agentloop/dag.py を参照。\n"


def test_check_readme_parity_is_green_for_matching_structure() -> None:
    assert template_lint.check_readme_parity(_EN, _JA) == []


def test_check_readme_parity_trips_on_section_count() -> None:
    assert "sections" in template_lint.check_readme_parity(_EN, _JA + "## う\n")[0]


def test_check_readme_parity_trips_on_a_one_sided_make_target() -> None:
    failures = template_lint.check_readme_parity(_EN + "Also `make feedback`.\n", _JA)
    assert failures == ["README.ja.md: missing make-target mention `feedback` (present in README.md)"]


def test_check_readme_parity_trips_on_a_one_sided_script() -> None:
    failures = template_lint.check_readme_parity(_EN, _JA + "scripts/agentloop/adopt.py も。\n")
    assert failures == ["README.md: missing script mention `adopt.py` (present in README.ja.md)"]


def test_check_readme_parity_ignores_prose_make_mentions() -> None:
    # "make tasks visible" is prose, not a target — only backticked mentions count.
    assert template_lint.check_readme_parity(_EN + "We make tasks visible.\n", _JA) == []


# --- version ↔ changelog -----------------------------------------------------------


def test_check_version_changelog_green_and_drifts() -> None:
    log = "# Changelog\n\n## [0.2.0] - 2026-07-08\n\n## [0.1.0] - 2026-07-01\n"
    assert template_lint.check_version_changelog("0.2.0", log) == []
    assert "0.1.0" in template_lint.check_version_changelog("0.1.0", log)[0]
    assert "missing or empty" in template_lint.check_version_changelog("", log)[0]
    assert "no `## [x.y.z]`" in template_lint.check_version_changelog("0.2.0", "# Changelog\n")[0]


# --- against the live repo (the actual CI gate) ------------------------------------


def _live_template_mode() -> bool:
    config = yaml.safe_load((_REPO_ROOT / template_lint.CONFIG_PATH).read_text(encoding="utf-8")) or {}
    return bool((config.get("gates") or {}).get("template_mode") is True)


@pytest.mark.skipif(not _live_template_mode(), reason="not the template repo (gates.template_mode is false)")
def test_live_repo_has_no_drift() -> None:
    files = {
        path: (_REPO_ROOT / path).read_text(encoding="utf-8")
        for path in (
            template_lint.CLAUDE_MD,
            template_lint.TASKS_CMD,
            template_lint.STATE_PATH,
            template_lint.CONFIG_PATH,
            "README.md",
            "README.ja.md",
        )
    }
    failures = template_lint.check_vocabulary(files)
    failures += template_lint.check_readme_parity(files["README.md"], files["README.ja.md"])
    import adopt

    failures += template_lint.check_version_changelog(
        adopt.read_version(_REPO_ROOT), (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    )
    assert failures == []


def test_main_skips_in_a_product_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / ".agentloop").mkdir()
    (tmp_path / ".agentloop" / "config.yaml").write_text("gates:\n  template_mode: false\n", encoding="utf-8")
    prev = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert template_lint.main([]) == 0
    finally:
        os.chdir(prev)
    assert "skipped" in capsys.readouterr().out
