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

import re
from pathlib import Path

# --- the SSOT trio (see AGENTS.md "Single Source of Truth") -----------------

STATE_PATH = ".agentloop/state.md"
CONFIG_PATH = ".agentloop/config.yaml"
TASKS_PATH = ".agentloop/tasks.yaml"

# --- lifecycle vocabulary (AGENTS.md "Development lifecycle") ----------------

# The forward gate order (state.md front-matter keys). Roll-back resets a chain of these.
GATE_ORDER = ("requirements", "design", "tasks", "build", "release")
# current_phase values, in lifecycle order (brief precedes gate ①; done follows gate ⑤).
PHASE_ORDER = ("brief", "requirements", "design", "tasks", "build", "verify", "done")


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


# --- the gate-chain invariant (AGENTS.md "Roll back") -------------------------
#
# If an upstream gate is pending, no downstream gate may stay approved — a violated chain
# means an approval survived a roll back. doctor reports every violation, status_api treats
# any violation as "repair before continuing", and ui refuses to create one.


def gate_chain_violations(gates: dict[str, str]) -> list[tuple[str, str]]:
    """Every (approved_gate, first_pending_upstream) pair violating the chain invariant."""
    violations: list[tuple[str, str]] = []
    first_pending: str | None = None
    for gate in GATE_ORDER:
        if gates.get(gate) != "approved":
            first_pending = first_pending or gate
        elif first_pending is not None:
            violations.append((gate, first_pending))
    return violations


def pending_upstream(gates: dict[str, str], gate: str) -> str | None:
    """The first not-approved gate upstream of `gate` — approving `gate` now would break the chain."""
    for upstream in GATE_ORDER[: GATE_ORDER.index(gate)]:
        if gates.get(upstream) != "approved":
            return upstream
    return None


def rewrite_gate_line(text: str, gate: str, old: str, new: str, *, keep_trailer: bool) -> tuple[str, int]:
    """Surgically rewrite the front-matter line `<gate>: <old> …` to `<gate>: <new>`.

    The rest of the document survives byte-for-byte (regex line surgery, never a YAML
    round-trip — state.md's comments and layout must be preserved), and only the front-matter
    fence is touched, so a matching line in the body cannot be rewritten by accident.
    keep_trailer keeps the trailing text (roll-back preserves the human's approval note);
    otherwise the trailer is replaced wholesale (approval stamps its own date comment).
    Returns (new_text, substitutions); 0 substitutions = no matching line.
    """
    if not text.startswith("---"):
        return text, 0
    parts = text.split("---", 2)
    if len(parts) < 3:
        return text, 0
    pattern = re.compile(rf"^(\s*{re.escape(gate)}:\s*){re.escape(old)}\b(.*)$", re.MULTILINE)

    def _sub(m: re.Match[str]) -> str:
        return m.group(1) + new + (m.group(2) if keep_trailer else "")

    new_front, n = pattern.subn(_sub, parts[1], count=1)
    return f"---{new_front}---{parts[2]}", n


def read_yaml(path: str) -> dict[str, object] | None:
    """Load a YAML mapping from `path`; None for any unreadable/non-mapping case (tolerant reads)."""
    import yaml  # lazy: keep `import common` stdlib-only (module docstring)

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None
