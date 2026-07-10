"""agentloop doctor — one-shot diagnosis of the environment and the SSOT's consistency (read-only).

The orchestration depends on several pieces staying mutually consistent: the binaries on PATH,
`.agentloop/config.yaml` (knobs), `.agentloop/state.md` (gates/phase), `.agentloop/tasks.yaml`
(the DAG), the gate-guard hook registration, and git's actual state (branch, worktrees, lock).
Each failure mode surfaces late and cryptically — build-loop refusing to start, a hook silently
absent, a downstream gate approved while its upstream is pending. This command checks them all
up front and prints one PASS/INFO/WARN/FAIL line per aspect.

Levels: FAIL = broken invariant, fix before running the loop (exit code 1).
        WARN = suspicious but not fatal (leftovers of an interruption, drift to tidy up).
        INFO = context worth knowing. PASS = checked and healthy.
Read-only: doctor never repairs anything — every fix stays a deliberate human/agent action.

Usage:
  make doctor
  uv run --no-project --with pyyaml python scripts/agentloop/doctor.py
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import build_loop
import dag
import events
import revise
import yaml

SETTINGS_PATH = ".claude/settings.json"
MANIFEST_PATH = ".agentloop/adopt-manifest.yaml"
PHASE_VALUES = ("brief", "requirements", "design", "tasks", "build", "verify", "done")
GATE_VALUES = ("pending", "approved")


@dataclass(frozen=True)
class Finding:
    level: str  # FAIL | WARN | INFO | PASS
    area: str
    message: str


def _read_yaml(path: str) -> dict[str, object] | None:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def check_binaries() -> list[Finding]:
    """The binaries the loop shells out to. uv/git are load-bearing; claude/gh degrade features."""
    out: list[Finding] = []
    for name, level, why in (
        ("uv", "FAIL", "every agentloop.mk target launches through it"),
        ("git", "FAIL", "worktrees/merges/commits are git operations"),
        ("claude", "WARN", "build-loop's headless implementer/reviewer launches need it"),
        ("gh", "INFO", "only needed when github.enabled/feedback is turned on"),
    ):
        if shutil.which(name):
            out.append(Finding("PASS", "env", f"{name} found on PATH"))
        else:
            out.append(Finding(level, "env", f"{name} not found on PATH — {why}"))
    return out


def check_config() -> tuple[list[Finding], dict[str, object]]:
    """config.yaml must parse and its quality_gate.steps must be a loadable DoD."""
    raw = _read_yaml(build_loop.CONFIG_PATH)
    if raw is None:
        return [Finding("FAIL", "config", f"{build_loop.CONFIG_PATH} is missing or not valid YAML")], {}
    try:
        config = build_loop.Config.load()
    except (OSError, yaml.YAMLError, ValueError) as exc:
        return [Finding("FAIL", "config", f"config does not load: {exc}")], raw
    findings = [Finding("PASS", "config", f"loads; DoD steps: {', '.join(s.name for s in config.steps)}")]
    runnable = [s for s in config.steps if s.kind == "cmd" and s.run]
    if not runnable:
        findings.append(Finding("WARN", "config", "no cmd step has a command — the quality gate would check nothing"))
    return findings, raw


def check_state() -> tuple[list[Finding], dict[str, object]]:
    """state.md's front matter: gate vocabulary, the gate-chain invariant, and the phase value."""
    try:
        front = build_loop.read_frontmatter()
    except OSError:
        return [Finding("FAIL", "state", f"{build_loop.STATE_PATH} is missing")], {}
    if not front:
        return [Finding("FAIL", "state", f"{build_loop.STATE_PATH} has no parseable front matter")], {}
    findings: list[Finding] = []
    gates = front.get("gates")
    if not isinstance(gates, dict):
        return [Finding("FAIL", "state", "front matter has no gates: mapping")], front
    for gate in revise.GATE_ORDER:
        value = gates.get(gate)
        if value is None:
            findings.append(Finding("FAIL", "state", f"gate '{gate}' is missing"))
        elif value not in GATE_VALUES:
            findings.append(Finding("FAIL", "state", f"gate '{gate}' has invalid value {value!r} (pending|approved)"))
    # Chain invariant (CLAUDE.md "Roll back"): if an upstream gate is pending, no downstream
    # gate may stay approved — a violated chain means an approval survived a roll back.
    pending_seen: str | None = None
    for gate in revise.GATE_ORDER:
        if gates.get(gate) != "approved":
            pending_seen = pending_seen or str(gate)
        elif pending_seen is not None:
            findings.append(
                Finding("FAIL", "state", f"gate '{gate}' is approved while upstream '{pending_seen}' is pending")
            )
    phase = front.get("current_phase")
    if phase not in PHASE_VALUES:
        findings.append(Finding("FAIL", "state", f"current_phase {phase!r} is not one of {'|'.join(PHASE_VALUES)}"))
    if not findings:
        findings.append(Finding("PASS", "state", f"gates consistent; current_phase: {phase}"))
    return findings, front


def check_placeholders(front: dict[str, object], config: dict[str, object]) -> list[Finding]:
    """Unfilled `<...>` placeholders are expected in the pristine template, fatal in a product."""
    gates_cfg = config.get("gates") if isinstance(config.get("gates"), dict) else {}
    template_mode = bool(gates_cfg.get("template_mode")) if isinstance(gates_cfg, dict) else False
    unfilled = [k for k in ("project", "branch") if str(front.get(k, "")).startswith("<")]
    if not unfilled:
        return [Finding("PASS", "init", "project/branch are filled in")]
    if template_mode:
        return [Finding("INFO", "init", f"{'/'.join(unfilled)} still placeholder (fine: template_mode is on)")]
    return [Finding("FAIL", "init", f"{'/'.join(unfilled)} still placeholder — run `make init NAME=<product>`")]


def check_tasks() -> list[Finding]:
    """tasks.yaml must be a valid DAG; leftovers that stall or need a human are surfaced."""
    if not Path(build_loop.TASKS_PATH).is_file():
        return [Finding("INFO", "tasks", f"{build_loop.TASKS_PATH} not present yet (before /tasks)")]
    try:
        graph = dag.load(build_loop.TASKS_PATH)
    except (OSError, dag.DagError, yaml.YAMLError) as exc:
        return [Finding("FAIL", "tasks", f"tasks.yaml does not load: {exc}")]
    findings: list[Finding] = []
    counts = graph.counts()
    stuck = [t.id for t in graph.tasks if t.status == "in_progress"]
    if stuck:
        findings.append(
            Finding("WARN", "tasks", f"in_progress leftovers: {', '.join(stuck)} (build-loop resets them on start)")
        )
    needy = [t.id for t in graph.tasks if t.status in ("blocked", "needs-revision")]
    if needy:
        findings.append(Finding("WARN", "tasks", f"awaiting human intervention: {', '.join(needy)}"))
    if not findings:
        summary = " / ".join(f"{s}={counts[s]}" for s in dag.STATUS_ORDER if counts[s])
        findings.append(Finding("PASS", "tasks", f"valid DAG ({len(graph.tasks)} tasks: {summary or 'empty'})"))
    return findings


def check_git(front: dict[str, object], config: dict[str, object]) -> list[Finding]:
    """git reality vs the SSOT: current branch, leftover worktrees, and the single-run lock."""
    rc, out = build_loop._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=".")
    if rc != 0:
        return [Finding("FAIL", "git", "not a git repository (or git is broken here)")]
    findings: list[Finding] = []
    current = out.strip()
    declared = str(front.get("branch", ""))
    if declared and not declared.startswith("<") and current != declared:
        findings.append(Finding("WARN", "git", f"checked-out branch '{current}' ≠ state.md branch '{declared}'"))
    build_cfg = config.get("build")
    wt_cfg = build_cfg.get("worktree") if isinstance(build_cfg, dict) else None
    wt_dir = Path(str(wt_cfg.get("dir", ".worktrees"))) if isinstance(wt_cfg, dict) else Path(".worktrees")
    leftovers = sorted(p.name for p in wt_dir.iterdir() if p.is_dir()) if wt_dir.is_dir() else []
    if leftovers:
        findings.append(
            Finding("WARN", "git", f"leftover worktrees under {wt_dir}: {', '.join(leftovers)} (interrupted run?)")
        )
    lock = Path(build_loop.LOCK_PATH)
    if lock.is_file():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = 0
        if pid > 0 and build_loop._pid_alive(pid):
            findings.append(Finding("INFO", "git", f"a build-loop run appears active (lock PID {pid})"))
        else:
            findings.append(Finding("WARN", "git", "stale build-loop.lock (dead PID; auto-reclaimed on next run)"))
    if not findings:
        findings.append(Finding("PASS", "git", f"on branch '{current}'; no leftover worktrees or lock"))
    return findings


def check_hook(config: dict[str, object]) -> list[Finding]:
    """The gate guard is only real if the PreToolUse hook is actually registered in settings.json."""
    gates_cfg = config.get("gates") if isinstance(config.get("gates"), dict) else {}
    enforce = bool(gates_cfg.get("enforce_hook", True)) if isinstance(gates_cfg, dict) else True
    if not enforce:
        return [Finding("INFO", "hook", "gates.enforce_hook is false — convention layer only")]
    try:
        settings = Path(SETTINGS_PATH).read_text(encoding="utf-8")
    except OSError:
        return [Finding("FAIL", "hook", f"{SETTINGS_PATH} missing while gates.enforce_hook is true")]
    if "gate_guard.py" not in settings:
        return [Finding("FAIL", "hook", f"gate_guard.py is not registered in {SETTINGS_PATH} (mechanism layer absent)")]
    return [Finding("PASS", "hook", "gate_guard hook registered")]


def check_events() -> list[Finding]:
    """Open escalations are pending human decisions — they must not sit forgotten."""
    opened = events.open_escalations(events.load_events())
    if opened:
        ids = ", ".join(f"#{e.id} {e.event}({e.task or '-'})" for e in opened)
        return [Finding("WARN", "events", f"{len(opened)} open escalation(s): {ids} — resolve via `make events`")]
    return [Finding("PASS", "events", "no open escalations")]


def check_version() -> list[Finding]:
    """Which template version this repo runs (identity only; upgrades are a human action)."""
    manifest = _read_yaml(MANIFEST_PATH)
    if manifest is not None:
        template = manifest.get("template") if isinstance(manifest.get("template"), dict) else {}
        version = template.get("version") if isinstance(template, dict) else None
        return [Finding("INFO", "version", f"adopt-manifest: template version {version or '(pre-0.1.0, unrecorded)'}")]
    try:
        version_text = Path("VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return [Finding("INFO", "version", "no adopt-manifest and no VERSION file (pre-manifest setup)")]
    return [Finding("INFO", "version", f"template repo, VERSION {version_text}")]


def run_checks() -> list[Finding]:
    findings = check_binaries()
    config_findings, config = check_config()
    findings += config_findings
    state_findings, front = check_state()
    findings += state_findings
    findings += check_placeholders(front, config)
    findings += check_tasks()
    findings += check_git(front, config)
    findings += check_hook(config)
    findings += check_events()
    findings += check_version()
    return findings


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="diagnose the AgentLoop environment and SSOT (read-only)").parse_args(argv)
    findings = run_checks()
    for f in findings:
        print(f"  [{f.level:<4}] {f.area}: {f.message}")
    fails = sum(1 for f in findings if f.level == "FAIL")
    warns = sum(1 for f in findings if f.level == "WARN")
    print(f"\ndoctor: {fails} FAIL / {warns} WARN / {len(findings)} checks")
    if fails:
        print("fix the FAIL items before running the loop.", file=sys.stderr)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
