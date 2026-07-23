"""Verify template_lint.py's drift canaries — and run them against the live repo (the real gate)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from agentloop import models, store, template_lint
from tests._support import make_config

_REPO_ROOT = Path(__file__).resolve().parents[1]


_CONFIG = store.dump_yaml(
    make_config(
        quality_gate=[
            {"name": "test", "kind": "command", "command": ["make", "test"], "executor_profile": "oracle"},
            {"name": "review", "kind": "agent", "agent_role": "code_reviewer"},
        ]
    )
).decode()

_AGENTS = (
    "kinds: foundation / parallel / integration. "
    "gates: requirements, design, tasks, build, release. steps: test, review.\n"
)
_TASKS_CMD = "kind: foundation | parallel | integration. status: todo in-progress blocked needs-revision done.\n"
_DOD_PROSE = "the pipeline runs test then review.\n"  # every prose copy of the DoD must echo the step names


def _files(**overrides: str) -> dict[str, str]:
    files = {
        template_lint.AGENTS_MD: _AGENTS,
        template_lint.TASKS_CMD: _TASKS_CMD,
        template_lint.BUILD_CMD: _DOD_PROSE,
        "README.md": _DOD_PROSE,
        "README.ja.md": _DOD_PROSE,
        template_lint.CONFIG_PATH: _CONFIG,
    }
    files.update(overrides)
    return files


# --- vocabulary ------------------------------------------------------------------


def test_gate_names_come_from_the_vocabulary_not_a_scraped_file() -> None:
    """0.8.x read them out of state.md's front matter. A constant cannot drift from the code
    that acts on it, which is the whole point of a canary."""
    assert template_lint.gate_names() == sorted(models.GATE_ORDER)


def test_quality_gate_steps_reads_the_dod_names() -> None:
    assert template_lint.quality_gate_steps(_CONFIG) == ["test", "review"]


def test_check_vocabulary_is_green_when_everything_is_echoed() -> None:
    assert template_lint.check_vocabulary(_files()) == []


def test_check_vocabulary_trips_on_a_missing_kind() -> None:
    files = _files(**{template_lint.TASKS_CMD: _TASKS_CMD.replace("integration", "join")})
    failures = template_lint.check_vocabulary(files)
    assert any("tasks.md" in f and "`integration`" in f for f in failures)


def test_check_vocabulary_trips_on_a_missing_quality_gate_step() -> None:
    files = _files(**{template_lint.AGENTS_MD: _AGENTS.replace("review", "critique")})
    failures = template_lint.check_vocabulary(files)
    assert any("AGENTS.md" in f and "`review`" in f for f in failures)


def test_check_vocabulary_trips_on_a_dod_copy_gone_stale() -> None:
    """The README/build.md prose copies of the DoD must echo the step names too."""
    files = _files(**{"README.ja.md": "the pipeline runs test then critique.\n"})
    failures = template_lint.check_vocabulary(files)
    assert any("README.ja.md" in f and "`review`" in f for f in failures)


# --- wrapper parity ----------------------------------------------------------------


def _wrapper_tree(root: Path) -> None:
    """A minimal healthy body+wrapper layout (one command, one agent role)."""
    (root / ".agentloop" / "prompts" / "commands").mkdir(parents=True)
    (root / ".agentloop" / "prompts" / "agents").mkdir(parents=True)
    (root / ".claude" / "commands").mkdir(parents=True)
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".github" / "prompts").mkdir(parents=True)
    (root / ".github" / "agents").mkdir(parents=True)
    (root / ".agentloop" / "prompts" / "commands" / "req.md").write_text("# /req\n", encoding="utf-8")
    (root / ".agentloop" / "prompts" / "agents" / "architect.md").write_text("# Role\n", encoding="utf-8")
    (root / ".claude" / "commands" / "req.md").write_text(
        "---\ndescription: Phase 1.\n---\n@.agentloop/prompts/commands/req.md\n", encoding="utf-8"
    )
    (root / ".github" / "prompts" / "req.prompt.md").write_text(
        "---\ndescription: Phase 1.\n---\nRead `.agentloop/prompts/commands/req.md`.\n", encoding="utf-8"
    )
    (root / ".claude" / "agents" / "architect.md").write_text(
        "---\nname: architect\ndescription: Designs.\n---\nRead `.agentloop/prompts/agents/architect.md`.\n",
        encoding="utf-8",
    )
    (root / ".github" / "agents" / "architect.agent.md").write_text(
        "---\ndescription: Designs.\n---\nRead `.agentloop/prompts/agents/architect.md`.\n", encoding="utf-8"
    )


def test_check_wrapper_parity_green(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    assert template_lint.check_wrapper_parity(tmp_path) == []


def test_check_wrapper_parity_trips_on_missing_wrapper(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    (tmp_path / ".github" / "prompts" / "req.prompt.md").unlink()
    failures = template_lint.check_wrapper_parity(tmp_path)
    assert any("missing wrapper req.prompt.md" in f for f in failures)


def test_check_wrapper_parity_trips_on_orphan_wrapper_and_stale_reference(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    (tmp_path / ".claude" / "commands" / "extra.md").write_text("---\ndescription: X.\n---\n", encoding="utf-8")
    (tmp_path / ".claude" / "agents" / "architect.md").write_text(
        "---\nname: architect\ndescription: Designs.\n---\nno reference here\n", encoding="utf-8"
    )
    failures = template_lint.check_wrapper_parity(tmp_path)
    assert any("extra.md: no shared body" in f for f in failures)
    assert any("does not reference .agentloop/prompts/agents/architect.md" in f for f in failures)


def test_check_wrapper_parity_trips_on_description_drift(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    (tmp_path / ".github" / "prompts" / "req.prompt.md").write_text(
        "---\ndescription: Phase one, reworded.\n---\nRead `.agentloop/prompts/commands/req.md`.\n", encoding="utf-8"
    )
    failures = template_lint.check_wrapper_parity(tmp_path)
    assert any("descriptions for `req` differ" in f for f in failures)


# --- capability mapping --------------------------------------------------------------

_CLAUDE_MAP = "| `structured-question` | AskUserQuestion |\n| `notify-and-wait` | PushNotification |\n"
_COPILOT_MAP = "| `structured-question` | numbered options in chat |\n| `notify-and-wait` | end the turn |\n"
_AGENTS_VOCAB = "vocabulary: `structured-question`, `notify-and-wait`.\n"


def test_check_capability_mapping_green() -> None:
    assert template_lint.check_capability_mapping(_CLAUDE_MAP, _COPILOT_MAP, _AGENTS_VOCAB) == []


def test_check_capability_mapping_trips_on_one_sided_token() -> None:
    failures = template_lint.check_capability_mapping(
        _CLAUDE_MAP + "| `session-compaction` | /compact |\n", _COPILOT_MAP, _AGENTS_VOCAB
    )
    assert any("missing capability `session-compaction`" in f and "instructions" in f for f in failures)


def test_check_capability_mapping_trips_on_undefined_token() -> None:
    failures = template_lint.check_capability_mapping(_CLAUDE_MAP, _COPILOT_MAP, "vocabulary: `notify-and-wait`.\n")
    assert any("`structured-question` is mapped but never defined" in f for f in failures)


def test_check_neutral_vocabulary_trips_on_dialect_leak() -> None:
    texts = {"AGENTS.md": "clean\n", ".agentloop/prompts/commands/req.md": "ask via AskUserQuestion\n"}
    failures = template_lint.check_neutral_vocabulary(texts)
    assert failures == [
        ".agentloop/prompts/commands/req.md: Claude-only mechanism `AskUserQuestion` leaked into a neutral file"
    ]


def test_neutral_texts_scans_docs_scaffolds_but_not_records(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (tmp_path / ".agentloop" / "prompts" / "commands").mkdir(parents=True)
    (tmp_path / ".agentloop" / "prompts" / "commands" / "req.md").write_text("body\n", encoding="utf-8")
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "docs" / "decisions" / "ADR-template.md").write_text("via AskUserQuestion\n", encoding="utf-8")
    (tmp_path / "docs" / "notes").mkdir()
    (tmp_path / "docs" / "notes" / "comparison.md").write_text("Claude Code's AskUserQuestion\n", encoding="utf-8")
    (tmp_path / "docs" / "archive").mkdir()
    (tmp_path / "docs" / "archive" / "old.md").write_text("AskUserQuestion transcript\n", encoding="utf-8")

    texts = template_lint.neutral_texts(tmp_path)
    assert set(texts) == {"AGENTS.md", ".agentloop/prompts/commands/req.md", "docs/decisions/ADR-template.md"}
    failures = template_lint.check_neutral_vocabulary(texts)
    assert failures == [
        "docs/decisions/ADR-template.md: Claude-only mechanism `AskUserQuestion` leaked into a neutral file"
    ]


# --- rules-module wiring -----------------------------------------------------------


def _rules_tree(root: Path) -> None:
    (root / ".agentloop" / "prompts" / "rules").mkdir(parents=True)
    (root / ".agentloop" / "prompts" / "rules" / "gate-workflow.md").write_text("# Rules\n", encoding="utf-8")


_WIRED = {".agentloop/prompts/commands/req.md": "read `.agentloop/prompts/rules/gate-workflow.md` first\n"}


def test_check_rules_wiring_green(tmp_path: Path) -> None:
    _rules_tree(tmp_path)
    assert template_lint.check_rules_wiring(tmp_path, dict(_WIRED)) == []


def test_check_rules_wiring_trips_on_an_orphan_module(tmp_path: Path) -> None:
    _rules_tree(tmp_path)
    # A reference from AGENTS.md alone doesn't count — only a command body loads a module.
    texts = {"AGENTS.md": "see `.agentloop/prompts/rules/gate-workflow.md`\n"}
    failures = template_lint.check_rules_wiring(tmp_path, texts)
    assert failures == [".agentloop/prompts/rules/gate-workflow.md: not read by any command body (orphan module)"]


def test_check_rules_wiring_trips_on_a_stale_reference(tmp_path: Path) -> None:
    _rules_tree(tmp_path)
    texts = dict(_WIRED)
    texts["docs/10-requirements.md"] = "spec: `.agentloop/prompts/rules/renamed.md`\n"
    failures = template_lint.check_rules_wiring(tmp_path, texts)
    assert failures == ["docs/10-requirements.md: references .agentloop/prompts/rules/renamed.md which does not exist"]


def test_check_rules_wiring_green_without_a_rules_dir(tmp_path: Path) -> None:
    """No rules/ directory and no references (a product repo that trimmed the modules) is healthy."""
    assert template_lint.check_rules_wiring(tmp_path, {"AGENTS.md": "rules\n"}) == []


# --- README parity ---------------------------------------------------------------

_EN = "## A\n## B\nRun `make init` then `make -f agentloop.mk agentloop-upgrade`.\nSee src/agentloop/dag.py.\n"
_JA = "## あ\n## い\n`make init` の後 `make -f agentloop.mk agentloop-upgrade`。\nsrc/agentloop/dag.py を参照。\n"


def test_check_readme_parity_is_green_for_matching_structure() -> None:
    assert template_lint.check_readme_parity(_EN, _JA) == []


def test_check_readme_parity_trips_on_section_count() -> None:
    assert "sections" in template_lint.check_readme_parity(_EN, _JA + "## う\n")[0]


def test_check_readme_parity_trips_on_a_one_sided_make_target() -> None:
    failures = template_lint.check_readme_parity(_EN + "Also `make feedback`.\n", _JA)
    assert failures == ["README.ja.md: missing make-target mention `feedback` (present in README.md)"]


def test_check_readme_parity_trips_on_a_one_sided_script() -> None:
    failures = template_lint.check_readme_parity(_EN, _JA + "src/agentloop/adopt.py も。\n")
    assert failures == ["README.md: missing script mention `adopt.py` (present in README.ja.md)"]


def test_check_readme_parity_ignores_prose_make_mentions() -> None:
    # "make tasks visible" is prose, not a target — only backticked mentions count.
    assert template_lint.check_readme_parity(_EN + "We make tasks visible.\n", _JA) == []


# --- version ↔ changelog -----------------------------------------------------------


def _config_with_guard(paths: dict[str, str]) -> str:
    entries = [{"path": path, "requires_gate": gate} for path, gate in paths.items()]
    return store.dump_yaml(make_config(guard_paths=entries)).decode()


def test_check_guard_defaults_green_and_drifts() -> None:
    """The block exists in two hand-maintained places on purpose — the code default applies
    when the key is omitted, the shipped config spells it out for the human editing it — so a
    rule added to only one of them is the drift this canary exists to catch."""
    from agentloop import gate_guard

    green = _config_with_guard(dict(gate_guard.DEFAULT_GUARD_PATHS))
    assert template_lint.check_guard_defaults(green) == []

    missing = template_lint.check_guard_defaults(_config_with_guard({"src/": "tasks"}))
    assert any("guard.paths is missing" in f for f in missing)

    extra = template_lint.check_guard_defaults(
        _config_with_guard({**gate_guard.DEFAULT_GUARD_PATHS, "extra/": "tasks"})
    )
    assert any("DEFAULT_GUARD_PATHS is missing `extra/`" in f for f in extra)

    mismatch = template_lint.check_guard_defaults(
        _config_with_guard({**gate_guard.DEFAULT_GUARD_PATHS, "src/": "design"})
    )
    assert any("`src/`" in f and "design" in f for f in mismatch)

    assert "guard.paths block is missing" in template_lint.check_guard_defaults(_config_with_guard({}))[0]


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
@pytest.mark.skip(reason="this repository is still on the 0.8.x layout; PR-G re-scaffolds it and re-enables the canary")
def test_live_repo_has_no_drift() -> None:
    files = {
        path: (_REPO_ROOT / path).read_text(encoding="utf-8")
        for path in (
            template_lint.AGENTS_MD,
            template_lint.TASKS_CMD,
            template_lint.BUILD_CMD,
            template_lint.CONFIG_PATH,
            "README.md",
            "README.ja.md",
        )
    }
    assert template_lint.check_vocabulary(files) == []


def test_main_skips_in_a_product_repo(make_repo: Callable[..., Path], capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(config=make_config(template_mode=False))
    assert template_lint.main([]) == 0
    assert "skipped" in capsys.readouterr().out


def test_main_reports_an_invalid_config_rather_than_skipping(
    make_repo: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A config it cannot parse is not "not the template repo" — treating it as one would turn
    every drift canary off silently."""
    root = make_repo()
    (root / ".agentloop" / "config.yaml").write_text("project: {}\n", encoding="utf-8")
    assert template_lint.main([]) == 1
    assert "is not valid" in capsys.readouterr().err
