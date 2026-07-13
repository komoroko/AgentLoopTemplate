"""Shared primitives for the scripts/agentloop toolset — the single home for what every tool reads.

Before this module the cross-cutting pieces lived wherever they were first written: the SSOT
path constants were re-declared per module, four divergent front-matter parsers grew up in
build_loop/gate_guard/ui/template_lint (with different split rules and failure postures), and
build_loop.py had become the de-facto utils host everyone imported from. One definition here,
re-exported where older names must survive, is what keeps those from drifting apart again.

Failure-posture rule for the front-matter parser: structural absence (no fence, unterminated
fence, non-mapping YAML) is a *state*, returned as None; malformed YAML is an *error*, raised
as yaml.YAMLError. Each caller then makes its posture explicit at the call site —
build_loop fails open to {}, gate_guard fails closed to deny, ui answers HTTP 500.

Module-level imports stay stdlib-only (yaml is imported lazily inside the functions that
need it) so the pyyaml-free paths — revise.py's gate rollback — keep working from a bare
`python` without the makefile's `--with pyyaml` injection.
"""

from __future__ import annotations

from pathlib import Path

# --- the SSOT trio (see AGENTS.md "Single Source of Truth") -----------------

STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"
TASKS_PATH = ".agentloop/tasks.yaml"


# --- subprocess ---------------------------------------------------------------


def run(cmd: list[str], cwd: str | None = None, timeout: float | None = None) -> tuple[int, str]:
    """Run a command; (returncode, stdout+stderr). A hang past `timeout` kills it, rc 124.

    124 is the coreutils convention; without the kill, a stuck `claude -p` or test run would
    stall the autonomous loop forever with no escalation. The expiry flows through the normal
    failure paths (retry budget / StopLoop).
    """
    import subprocess  # lazy: keep `import common` light for the hook path (gate_guard on every edit)

    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = "".join(
            part if isinstance(part, str) else part.decode(errors="replace")
            for part in (exc.stdout, exc.stderr)
            if part
        )
        return 124, f"{partial}\ntimed out after {int(exc.timeout)}s (process killed)"
    return proc.returncode, proc.stdout + proc.stderr


# --- front-matter / YAML reading ---------------------------------------------


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """The YAML front-matter mapping of `text`, or None when none is structurally present.

    None covers: no leading `---`, an unterminated fence, or a front matter that is not a
    mapping. Malformed YAML raises yaml.YAMLError instead — see the module docstring for
    why the two failure modes are kept distinct.
    """
    import yaml  # lazy: keep `import common` stdlib-only (module docstring)

    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    loaded = yaml.safe_load(parts[1])
    return loaded if isinstance(loaded, dict) else None


def read_frontmatter(path: str = STATE_PATH) -> dict[str, object]:
    """state.md front-matter, failing open to {} when structurally absent.

    A missing file raises OSError and malformed YAML raises yaml.YAMLError — callers that
    must not crash catch those and choose their posture (status_api warns, doctor FAILs).
    """
    front = parse_frontmatter(Path(path).read_text(encoding="utf-8"))
    return front if front is not None else {}


def gates_of(front: dict[str, object] | None) -> dict[str, str] | None:
    """The `gates:` mapping of a parsed front matter, coerced to str→str.

    None when the front matter (or its gates key) is absent or not a mapping — the same
    posture split as parse_frontmatter: gate_guard fails closed on None, readers that only
    display fall back to {}.
    """
    raw = (front or {}).get("gates")
    if not isinstance(raw, dict):
        return None
    return {str(k): str(v) for k, v in raw.items()}


def read_yaml(path: str) -> dict[str, object] | None:
    """Load a YAML mapping from `path`; None for any unreadable/non-mapping case (tolerant reads)."""
    import yaml  # lazy: keep `import common` stdlib-only (module docstring)

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None
