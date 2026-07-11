"""Self-consistency canaries for the template repo (drift checks across hand-maintained files).

The always-loaded rules (AGENTS.md), the per-phase procedures, the bilingual READMEs, and the
code that parses the machine-read vocabulary are maintained by hand in parallel. The classic
failure is a rename or addition that lands in one file and silently drifts the rest — e.g. a
new make target documented only in README.md, or a task-status value renamed in dag.py but
not in the /tasks procedure. Exact byte comparison is impossible across a translation or
between prose and code, so these checks are *canaries*: they assert the load-bearing
vocabulary and structure survive verbatim in every file that reads them. A tripped canary
usually means "propagate the change everywhere", not "revert the change".

Template-repo only: after `make init` flips `gates.template_mode` to false, a product owns
its READMEs (and may replace them wholesale), so `main()` skips unless this repo IS the
template. test_template_lint.py runs the same checks against the live repo under
`make test-tools`, which is how CI catches a drifting commit.

Usage:
  uv run --no-project --with pyyaml python scripts/agentloop/template_lint.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import adopt
import dag
import yaml

AGENTS_MD = "AGENTS.md"
TASKS_CMD = ".agentloop/prompts/commands/tasks.md"
STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"
CLAUDE_MAPPING = "CLAUDE.md"
COPILOT_MAPPING = ".github/instructions/agentloop.instructions.md"

# The shared procedure/role bodies and their per-agent thin wrappers. Each body must have a
# wrapper in every dialect, and each wrapper must reference its body — check_wrapper_parity.
_WRAPPER_SETS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (".agentloop/prompts/commands", ((".claude/commands", "{stem}.md"), (".github/prompts", "{stem}.prompt.md"))),
    (".agentloop/prompts/agents", ((".claude/agents", "{stem}.md"), (".github/agents", "{stem}.agent.md"))),
)

# Only backticked `make <target>` mentions count — prose like "make tasks visible" must not.
# The optional `-f \S+` arm covers the `make -f agentloop.mk agentloop-upgrade` form.
_MAKE_MENTION_RE = re.compile(r"`make (?:-f \S+ )?([a-z][a-z0-9_-]*)")
_SCRIPT_MENTION_RE = re.compile(r"scripts/agentloop/(\w+\.py)")
# A capability token is the backticked kebab word opening a mapping-table row.
_CAPABILITY_ROW_RE = re.compile(r"^\|\s*`([a-z][a-z-]+)`\s*\|", re.MULTILINE)
_DESCRIPTION_RE = re.compile(r"^description:\s*(.+?)\s*$", re.MULTILINE)
# Claude-only mechanism names must never leak into the agent-neutral files — they belong in
# the capability mappings alone. A leak means a neutral body regressed to one agent's dialect.
_CLAUDE_ONLY_TERMS = ("AskUserQuestion", "PushNotification", "ExitPlanMode")


def _require(text: str, path: str, terms: list[str], what: str) -> list[str]:
    return [f"{path}: missing {what} `{t}`" for t in terms if t not in text]


def gate_names(state_text: str) -> list[str]:
    """The gate keys from state.md's front matter — the canonical gate list the docs must echo."""
    parts = state_text.split("---")
    front = yaml.safe_load(parts[1]) if len(parts) > 2 else None
    gates = (front or {}).get("gates") or {}
    return sorted(gates)


def quality_gate_steps(config_text: str) -> list[str]:
    """The DoD step names from config.yaml — defined once there, echoed by AGENTS.md."""
    config = yaml.safe_load(config_text) or {}
    steps = ((config.get("build") or {}).get("quality_gate") or {}).get("steps") or []
    return [step["name"] for step in steps if isinstance(step, dict) and "name" in step]


def check_vocabulary(files: dict[str, str]) -> list[str]:
    """Assert the machine-read vocabulary appears verbatim in the prose that teaches it.

    dag.py's value sets are what `--validate` enforces on tasks.yaml; state.md's gate keys are
    what gate_guard.py and revise.py act on; config's step names are the single DoD definition.
    If any of these is renamed without updating AGENTS.md / the /tasks procedure, the agent is
    taught vocabulary the code rejects — this is the drift these canaries trip on.
    """
    failures: list[str] = []
    kinds = sorted(dag.KIND_VALUES)
    failures += _require(files[AGENTS_MD], AGENTS_MD, kinds, "task kind (dag.KIND_VALUES)")
    failures += _require(files[TASKS_CMD], TASKS_CMD, kinds, "task kind (dag.KIND_VALUES)")
    failures += _require(files[TASKS_CMD], TASKS_CMD, sorted(dag.STATUS_VALUES), "task status (dag.STATUS_VALUES)")
    failures += _require(files[AGENTS_MD], AGENTS_MD, gate_names(files[STATE_PATH]), "gate (state.md front matter)")
    failures += _require(
        files[AGENTS_MD], AGENTS_MD, quality_gate_steps(files[CONFIG_PATH]), "quality-gate step (config.yaml)"
    )
    return failures


def _description(text: str) -> str:
    m = _DESCRIPTION_RE.search(text)
    return m.group(1) if m else ""


def check_wrapper_parity(root: Path) -> list[str]:
    """Every shared body has a wrapper in both dialects; every wrapper points at a real body.

    The bodies in .agentloop/prompts/ are the single procedure source; .claude/* and .github/*
    are thin wrappers. The drift this trips on: a new phase/role added in one place only, a
    wrapper whose body reference went stale after a rename, or the two dialects' `description:`
    frontmatter diverging (they must stay byte-identical — same command, same one-liner).
    """
    failures: list[str] = []
    for body_dir, wrappers in _WRAPPER_SETS:
        stems = sorted(p.stem for p in (root / body_dir).glob("*.md"))
        if not stems:
            failures.append(f"{body_dir}: no shared bodies found")
            continue
        descriptions: dict[str, dict[str, str]] = {}
        for wrapper_dir, pattern in wrappers:
            suffix = pattern.replace("{stem}", "")
            found = {p.name[: -len(suffix)] for p in (root / wrapper_dir).glob(f"*{suffix}")}
            for stem in sorted(set(stems) - found):
                failures.append(f"{wrapper_dir}: missing wrapper {pattern.format(stem=stem)} for {body_dir}/{stem}.md")
            for stem in sorted(found - set(stems)):
                failures.append(f"{wrapper_dir}/{pattern.format(stem=stem)}: no shared body {body_dir}/{stem}.md")
            for stem in sorted(set(stems) & found):
                text = (root / wrapper_dir / pattern.format(stem=stem)).read_text(encoding="utf-8")
                body_ref = f"{body_dir}/{stem}.md"
                if body_ref not in text:
                    failures.append(f"{wrapper_dir}/{pattern.format(stem=stem)}: does not reference {body_ref}")
                descriptions.setdefault(stem, {})[wrapper_dir] = _description(text)
        for stem, per_dir in sorted(descriptions.items()):
            if len(set(per_dir.values())) > 1:
                failures.append(f"wrapper descriptions for `{stem}` differ across {', '.join(sorted(per_dir))}")
    return failures


def check_capability_mapping(claude_text: str, copilot_text: str, agents_text: str) -> list[str]:
    """The two capability mappings cover the same token set, and AGENTS.md defines every token.

    The mapping tables (CLAUDE.md, the Copilot instructions file) are hand-maintained mirrors;
    the vocabulary itself lives in AGENTS.md. A capability added to one mapping only — or one
    that AGENTS.md never defines — is the drift.
    """
    failures: list[str] = []
    claude_tokens = set(_CAPABILITY_ROW_RE.findall(claude_text))
    copilot_tokens = set(_CAPABILITY_ROW_RE.findall(copilot_text))
    for token in sorted(claude_tokens - copilot_tokens):
        failures.append(f"{COPILOT_MAPPING}: missing capability `{token}` (mapped in {CLAUDE_MAPPING})")
    for token in sorted(copilot_tokens - claude_tokens):
        failures.append(f"{CLAUDE_MAPPING}: missing capability `{token}` (mapped in {COPILOT_MAPPING})")
    for token in sorted(claude_tokens | copilot_tokens):
        if f"`{token}`" not in agents_text:
            failures.append(f"{AGENTS_MD}: capability `{token}` is mapped but never defined here")
    return failures


def neutral_texts(root: Path) -> dict[str, str]:
    """The agent-neutral files the dialect canary scans: AGENTS.md, the shared bodies, and the
    docs scaffolds (docs/notes/ and docs/archive/ are records, not scaffolds — Claude mentions
    there are legitimate)."""
    texts = {AGENTS_MD: (root / AGENTS_MD).read_text(encoding="utf-8")}
    scans: tuple[tuple[Path, tuple[str, ...]], ...] = (
        (root / ".agentloop" / "prompts", ()),
        (root / "docs", ("notes", "archive")),
    )
    for base, excluded in scans:
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(root)
            if len(rel.parts) > 1 and rel.parts[1] in excluded:
                continue
            texts[rel.as_posix()] = path.read_text(encoding="utf-8")
    return texts


def check_neutral_vocabulary(texts: dict[str, str]) -> list[str]:
    """No Claude-only mechanism name may appear in the agent-neutral files (AGENTS.md, bodies, scaffolds)."""
    failures: list[str] = []
    for path, text in sorted(texts.items()):
        for term in _CLAUDE_ONLY_TERMS:
            if term in text:
                failures.append(f"{path}: Claude-only mechanism `{term}` leaked into a neutral file")
    return failures


def check_readme_parity(en: str, ja: str) -> list[str]:
    """Structural canaries between the bilingual READMEs (byte-compare is impossible across a translation).

    A `##` section, a make target, or a script added to one language and not the other is the
    drift; the sets/counts below are language-independent, so they must match exactly.
    """
    failures: list[str] = []
    n_en = len(re.findall(r"^## ", en, re.MULTILINE))
    n_ja = len(re.findall(r"^## ", ja, re.MULTILINE))
    if n_en != n_ja:
        failures.append(f"README.md has {n_en} `##` sections but README.ja.md has {n_ja}")
    for what, pattern in (("make-target", _MAKE_MENTION_RE), ("script", _SCRIPT_MENTION_RE)):
        only_en = set(pattern.findall(en)) - set(pattern.findall(ja))
        only_ja = set(pattern.findall(ja)) - set(pattern.findall(en))
        for name in sorted(only_en):
            failures.append(f"README.ja.md: missing {what} mention `{name}` (present in README.md)")
        for name in sorted(only_ja):
            failures.append(f"README.md: missing {what} mention `{name}` (present in README.ja.md)")
    return failures


def check_version_changelog(version: str, changelog: str) -> list[str]:
    """VERSION and CHANGELOG.md's newest `## [x.y.z]` heading must agree.

    Guards the release failure where the identity files go stale *together* (bump one, forget
    the other) — upgrade's `template version: A → B` display then lies to every downstream repo.
    """
    if not version:
        return ["VERSION is missing or empty"]
    m = adopt._CHANGELOG_HEADING_RE.search(changelog)
    if not m:
        return ["CHANGELOG.md has no `## [x.y.z]` version heading"]
    if m.group(1) != version:
        return [f"VERSION says {version} but CHANGELOG.md's newest heading says {m.group(1)}"]
    return []


def main(argv: list[str] | None = None) -> int:
    config_text = Path(CONFIG_PATH).read_text(encoding="utf-8")
    config = yaml.safe_load(config_text) or {}
    if (config.get("gates") or {}).get("template_mode") is not True:
        print("skipped (gates.template_mode is false: not the template repo)")
        return 0

    try:
        files = {
            path: Path(path).read_text(encoding="utf-8")
            for path in (AGENTS_MD, TASKS_CMD, STATE_PATH, CONFIG_PATH, "README.md", "README.ja.md")
        }
        failures = check_vocabulary(files)
        failures += check_wrapper_parity(Path())
        failures += check_capability_mapping(
            Path(CLAUDE_MAPPING).read_text(encoding="utf-8"),
            Path(COPILOT_MAPPING).read_text(encoding="utf-8"),
            files[AGENTS_MD],
        )
        failures += check_neutral_vocabulary(neutral_texts(Path()))
        failures += check_readme_parity(files["README.md"], files["README.ja.md"])
        failures += check_version_changelog(
            adopt.read_version(Path()), Path("CHANGELOG.md").read_text(encoding="utf-8")
        )
    except OSError as exc:
        print(f"template-lint failed: {exc}", file=sys.stderr)
        return 1

    for failure in failures:
        print(f"  drift: {failure}")
    if failures:
        print(f"{len(failures)} drift(s) — propagate the change to every listed file (or revert it).")
        return 1
    print("template-lint: no drift.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
