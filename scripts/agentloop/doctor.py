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
import json
import re
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
        ("gh", "INFO", "only needed when github.enabled is turned on"),
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
    findings.extend(_check_step_commands(config, raw))
    findings.extend(_check_legacy_keys(raw))
    return findings, raw


def _check_legacy_keys(raw: dict[str, object]) -> list[Finding]:
    """Keys from the pre-0.3.0 config form are dead weight: with `steps` present they are
    silently ignored, which can fool a reader into thinking they still steer the gate."""
    build = raw.get("build") if isinstance(raw.get("build"), dict) else {}
    qg = build.get("quality_gate") if isinstance(build, dict) else {}
    stale = [f"quality_gate.{k}" for k in ("test_cmd", "check_cmd") if isinstance(qg, dict) and k in qg]
    if isinstance(build, dict) and isinstance(build.get("retries"), dict):
        stale.append("build.retries")
    if stale:
        return [
            Finding(
                "WARN",
                "config",
                f"legacy pre-0.3.0 keys are ignored: {', '.join(stale)} — the DoD lives in "
                "quality_gate.steps (per-step retries); delete them",
            )
        ]
    return []


def _check_step_commands(config: build_loop.Config, raw: dict[str, object]) -> list[Finding]:
    """A `required` step with no command is a contradiction build_loop refuses to run (FAIL here
    too, so it surfaces before the loop is even launched). An empty smoke without a *deliberate*
    `required: false` gets a WARN: a runnable deliverable whose DoD never launches it is the
    exact miss the smoke step exists to catch."""
    build = raw.get("build") if isinstance(raw.get("build"), dict) else {}
    qg = build.get("quality_gate") if isinstance(build, dict) else {}
    raw_steps = qg.get("steps") if isinstance(qg, dict) else None
    explicit: dict[str, bool] = {}  # step name → `required` key present in the YAML
    if isinstance(raw_steps, list):
        for entry in raw_steps:
            if isinstance(entry, dict):
                explicit[str(entry.get("name", ""))] = "required" in entry
    out: list[Finding] = []
    for step in config.steps:
        if step.kind != "cmd" or step.run.strip():
            continue
        if step.required:
            out.append(
                Finding(
                    "FAIL",
                    "config",
                    f"step '{step.name}' is `required: true` but has no command — "
                    "build_loop refuses to start; fill `run` or drop `required`",
                )
            )
        elif step.name == "smoke" and not explicit.get("smoke", False):
            out.append(
                Finding(
                    "WARN",
                    "config",
                    "smoke has no command — fill `run` (+ `required: true`) for a runnable "
                    "deliverable, or set `required: false` explicitly to record the decision",
                )
            )
    return out


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
    findings.extend(_check_ticket_parity(graph))
    if not findings:
        summary = " / ".join(f"{s}={counts[s]}" for s in dag.STATUS_ORDER if counts[s])
        findings.append(Finding("PASS", "tasks", f"valid DAG ({len(graph.tasks)} tasks: {summary or 'empty'})"))
    return findings


def _check_ticket_parity(graph: dag.Graph) -> list[Finding]:
    """Every task id should have its docs/tasks/T-NNN.md ticket (the implementer reads it first).

    A task without a ticket runs the implementer on title-only context; an orphan ticket usually
    means a task was dropped from tasks.yaml without cleaning up (or a scaffold example remains).
    """
    tickets_dir = Path("docs/tasks")
    if not graph.tasks or not tickets_dir.is_dir():
        return []
    tickets = {p.stem for p in tickets_dir.glob("T-*.md")}
    ids = {t.id for t in graph.tasks}
    out: list[Finding] = []
    if missing := sorted(ids - tickets):
        out.append(Finding("WARN", "tasks", f"no ticket file under docs/tasks/ for: {', '.join(missing)}"))
    if orphans := sorted(tickets - ids):
        out.append(Finding("INFO", "tasks", f"ticket files with no tasks.yaml entry: {', '.join(orphans)}"))
    return out


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
    findings.extend(_check_leaf_branches(declared, wt_cfg if isinstance(wt_cfg, dict) else {}))
    if not findings:
        findings.append(Finding("PASS", "git", f"on branch '{current}'; no leftover worktrees or lock"))
    return findings


def _check_leaf_branches(declared: str, wt_cfg: dict[str, object]) -> list[Finding]:
    """Leaf branches (`<branch>-T-NNN`) left behind by parallel batches.

    A *merged* leaf branch is normal residue (merge_leaf removes only the worktree) — INFO with a
    cleanup hint. An *unmerged* one is interrupted or blocked work whose diff lives only there —
    WARN, because deleting it by reflex would lose that diff.
    """
    if not declared or declared.startswith("<"):
        return []
    pattern = str(wt_cfg.get("branch_pattern", "{branch}-{task_id}"))
    glob = pattern.format(branch=declared, task_id="T-*")
    rc, all_out = build_loop._run(["git", "branch", "--list", glob], cwd=".")
    if rc != 0:
        return []
    leaves = {ln.strip().lstrip("* ") for ln in all_out.splitlines() if ln.strip()}
    if not leaves:
        return []
    rc, unmerged_out = build_loop._run(["git", "branch", "--no-merged", "HEAD", "--list", glob], cwd=".")
    unmerged = {ln.strip().lstrip("* ") for ln in unmerged_out.splitlines() if ln.strip()} if rc == 0 else set()
    out: list[Finding] = []
    if unmerged:
        out.append(
            Finding(
                "WARN",
                "git",
                f"UNMERGED leaf branch(es): {', '.join(sorted(unmerged))} — interrupted/blocked work; "
                "inspect before deleting (the diff may live only there)",
            )
        )
    if merged := sorted(leaves - unmerged):
        out.append(Finding("INFO", "git", f"merged leaf branch(es) left behind: {', '.join(merged)} — safe to delete"))
    return out


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
    findings: list[Finding] = []
    opened = events.open_escalations(events.load_events())
    if opened:
        ids = ", ".join(f"#{e.id} {e.event}({e.task or '-'})" for e in opened)
        findings.append(
            Finding("WARN", "events", f"{len(opened)} open escalation(s): {ids} — resolve via `make events`")
        )
    log = Path(events.EVENTS_PATH)
    if log.is_file() and log.stat().st_size > events.EVENTS_MAX_BYTES * 0.8:
        findings.append(
            Finding(
                "INFO",
                "events",
                f"{events.EVENTS_PATH} is {log.stat().st_size // 1024} KiB (>80% of the rotation "
                "threshold) — the next build-loop run rotates it, carrying open escalations forward",
            )
        )
    return findings or [Finding("PASS", "events", "no open escalations")]


def check_security_review() -> list[Finding]:
    """Once every task is done, the security-review report must exist and be bound to HEAD.

    Before that point the report legitimately doesn't exist yet, so the check stays silent.
    A stale Reviewed-HEAD means commits landed after the review — gate ④ must not treat the
    old report as covering them.
    """
    try:
        graph = dag.load(build_loop.TASKS_PATH)
    except (OSError, dag.DagError, yaml.YAMLError):
        return []
    if not graph.tasks or any(t.status != "done" for t in graph.tasks):
        return []
    try:
        text = Path(build_loop.SECURITY_REVIEW_PATH).read_text(encoding="utf-8")
    except OSError:
        return [
            Finding(
                "INFO",
                "security",
                f"all tasks done but no {build_loop.SECURITY_REVIEW_PATH} (mode B, or the knob is off) — "
                "run /security-review before gate ④",
            )
        ]
    m = re.search(r"^Reviewed-HEAD:\s*([0-9a-fA-F]+)", text, re.MULTILINE)
    reviewed = m.group(1) if m else ""
    rc, out = build_loop._run(["git", "rev-parse", "HEAD"], cwd=".")
    head = out.strip() if rc == 0 else ""
    if reviewed and head and (head.startswith(reviewed) or reviewed.startswith(head)):
        return [Finding("PASS", "security", f"security review is bound to HEAD ({reviewed[:12]})")]
    return [
        Finding(
            "WARN",
            "security",
            f"security review is STALE (Reviewed-HEAD {reviewed[:12] or '(missing)'} ≠ HEAD {head[:12]}) — "
            "commits landed after the review; re-run it before gate ④",
        )
    ]


def check_guard_paths(config: dict[str, object]) -> list[Finding]:
    """guard_paths values must be real gate names — a typo silently disables that path's guard."""
    gates_cfg = config.get("gates") if isinstance(config.get("gates"), dict) else {}
    guard = gates_cfg.get("guard_paths") if isinstance(gates_cfg, dict) else None
    if not isinstance(guard, dict):
        return []
    bad = {str(path): str(gate) for path, gate in guard.items() if gate not in revise.GATE_ORDER}
    if bad:
        detail = ", ".join(f"{p!r}: {g!r}" for p, g in bad.items())
        return [
            Finding(
                "FAIL",
                "config",
                f"guard_paths values must be one of {'|'.join(revise.GATE_ORDER)} — invalid: {detail}",
            )
        ]
    return [Finding("PASS", "config", f"guard_paths valid ({len(guard)} entries)")]


SCHEMA_DIR = ".agentloop/schema"


def check_schema() -> list[Finding]:
    """Validate config.yaml / tasks.yaml against the bundled JSON Schemas when possible.

    The schemas (.agentloop/schema/*.schema.json) are primarily editor tooling (the
    yaml-language-server modeline); here they double as a lint. jsonschema is an optional
    extra — `make doctor` provides it via `--with`, a bare python run degrades to INFO —
    so the ordinary agentloop runtime stays pyyaml-only.
    """
    try:
        import jsonschema
    except ImportError:
        return [Finding("INFO", "schema", "jsonschema not installed — schema validation skipped (make doctor has it)")]
    out: list[Finding] = []
    for data_path, schema_name in ((build_loop.CONFIG_PATH, "config"), (build_loop.TASKS_PATH, "tasks")):
        schema_path = Path(SCHEMA_DIR) / f"{schema_name}.schema.json"
        if not schema_path.exists():
            out.append(Finding("INFO", "schema", f"{schema_path} absent (older template) — skipped"))
            continue
        data = _read_yaml(data_path)
        if data is None:
            continue  # unreadable/missing data files are already FAILed by their own checks
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as exc:
            where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
            out.append(Finding("FAIL", "schema", f"{data_path} violates its schema at {where}: {exc.message}"))
        except (OSError, ValueError, jsonschema.SchemaError) as exc:
            out.append(Finding("WARN", "schema", f"cannot validate {data_path}: {exc}"))
        else:
            out.append(Finding("PASS", "schema", f"{data_path} matches {schema_path.name}"))
    return out


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
    findings += check_guard_paths(config)
    findings += check_tasks()
    findings += check_git(front, config)
    findings += check_hook(config)
    findings += check_events()
    findings += check_security_review()
    findings += check_schema()
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
