"""Self-consistency canaries for the template repo (drift checks across hand-maintained files).

The always-loaded rules (CLAUDE.md), the per-phase commands, the bilingual READMEs, and the
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

CLAUDE_MD = "CLAUDE.md"
TASKS_CMD = ".claude/commands/tasks.md"
STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"

# Only backticked `make <target>` mentions count — prose like "make tasks visible" must not.
# The optional `-f \S+` arm covers the `make -f agentloop.mk agentloop-upgrade` form.
_MAKE_MENTION_RE = re.compile(r"`make (?:-f \S+ )?([a-z][a-z0-9_-]*)")
_SCRIPT_MENTION_RE = re.compile(r"scripts/agentloop/(\w+\.py)")


def _require(text: str, path: str, terms: list[str], what: str) -> list[str]:
    return [f"{path}: missing {what} `{t}`" for t in terms if t not in text]


def gate_names(state_text: str) -> list[str]:
    """The gate keys from state.md's front matter — the canonical gate list the docs must echo."""
    parts = state_text.split("---")
    front = yaml.safe_load(parts[1]) if len(parts) > 2 else None
    gates = (front or {}).get("gates") or {}
    return sorted(gates)


def quality_gate_steps(config_text: str) -> list[str]:
    """The DoD step names from config.yaml — defined once there, echoed by CLAUDE.md."""
    config = yaml.safe_load(config_text) or {}
    steps = ((config.get("build") or {}).get("quality_gate") or {}).get("steps") or []
    return [step["name"] for step in steps if isinstance(step, dict) and "name" in step]


def check_vocabulary(files: dict[str, str]) -> list[str]:
    """Assert the machine-read vocabulary appears verbatim in the prose that teaches it.

    dag.py's value sets are what `--validate` enforces on tasks.yaml; state.md's gate keys are
    what gate_guard.py and revise.py act on; config's step names are the single DoD definition.
    If any of these is renamed without updating CLAUDE.md / the /tasks procedure, the agent is
    taught vocabulary the code rejects — this is the drift these canaries trip on.
    """
    failures: list[str] = []
    kinds = sorted(dag.KIND_VALUES)
    failures += _require(files[CLAUDE_MD], CLAUDE_MD, kinds, "task kind (dag.KIND_VALUES)")
    failures += _require(files[TASKS_CMD], TASKS_CMD, kinds, "task kind (dag.KIND_VALUES)")
    failures += _require(files[TASKS_CMD], TASKS_CMD, sorted(dag.STATUS_VALUES), "task status (dag.STATUS_VALUES)")
    failures += _require(files[CLAUDE_MD], CLAUDE_MD, gate_names(files[STATE_PATH]), "gate (state.md front matter)")
    failures += _require(
        files[CLAUDE_MD], CLAUDE_MD, quality_gate_steps(files[CONFIG_PATH]), "quality-gate step (config.yaml)"
    )
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
            for path in (CLAUDE_MD, TASKS_CMD, STATE_PATH, CONFIG_PATH, "README.md", "README.ja.md")
        }
        failures = check_vocabulary(files)
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
