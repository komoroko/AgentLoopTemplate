"""Read-only aggregation of what a human must read before opening a gate — the review pane's data.

status_api.py answers "where does the lifecycle stand"; this module answers the companion question
"what do I read to approve the gate in front of me". `collect_review(root, gate)` returns one JSON
object per gate: the phase deliverables rendered through mdlite (escape-first — see its threat
model), each deliverable's Self-assessment section split out so the pane can pin it, and for gate
④ the work-branch diff plus the generated review's freshness.

Reach is fixed server-side, the same way ui.action_argv fixes command lines: the client sends only
a gate name; which files are read comes from the `_GATE_SPEC` constant plus a template-excluding
glob inside two fixed directories, every path is containment-checked after `resolve()` (a symlinked
deliverable pointing outside the repo is reported missing, never followed), and a single file is
capped at `_MAX_DELIVERABLE` bytes. Git use is read-only subprocesses with a timeout; a non-git or
detached repo degrades the diff block to an error/log field, never an exception.

Reads are tolerant like status_api: a missing deliverable renders as `exists: false` (the reviewer
should *see* that a gate's document is absent), and only an unknown gate raises (`ReviewError` →
the HTTP layer's 404).
"""

from __future__ import annotations

import html
import re
import subprocess
from datetime import datetime
from pathlib import Path

from agentloop import event_chain, human_review, mdlite, models, strict_yaml
from agentloop import events as events_mod

_MAX_DELIVERABLE = 300_000  # bytes of one deliverable the pane will render
_MAX_PATCH = 200_000  # bytes of unified diff for gate ④
_GIT_TIMEOUT_SEC = 10
_GLOB_NAME_RE = re.compile(r"^(T|ADR)-[A-Za-z0-9_.-]+\.md$")
_TEMPLATE_NAMES = frozenset({"T-template.md", "ADR-template.md"})
# The *labelled* confidence line ("- **Confidence**: …"), not any prose mentioning the word: the
# label must be what precedes the colon, so a sentence like "we have high confidence in X" is not
# mistaken for the assessment. The value is everything after that colon.
_CONFIDENCE_LINE_RE = re.compile(r"^[^:\n]*\bconfidence\b[^:\n]*:(?P<value>.*)$", re.IGNORECASE | re.MULTILINE)
_LEVEL_RE = re.compile(r"\b(high|medium|low)\b", re.IGNORECASE)
# The scaffold's unfilled placeholder is the three levels as a slash run ("high / medium / low",
# optionally "per area …"). A genuinely filled per-area line separates them differently
# ("architecture=high / choices=medium"), so this run is a precise "nobody answered" signal.
_PLACEHOLDER_RE = re.compile(r"\bhigh\s*/\s*medium\s*/\s*low\b", re.IGNORECASE)
_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}

# Gate -> what the human reads to open it. "main" is the deliverable under approval, "context" the
# upstream document it is judged against. ("glob", dir, pattern) expands inside that fixed
# directory only, excluding the scaffold templates; ("code", path) renders verbatim, not as
# markdown (tasks.yaml is machine truth — reviewers must see it exactly).
_SpecItem = str | tuple[str, str] | tuple[str, str, str]
_GATE_SPEC: dict[str, dict[str, list[_SpecItem]]] = {
    "requirements": {"main": ["docs/10-requirements.md"], "context": ["docs/00-product-brief.md"]},
    "design": {
        "main": ["docs/20-design.md", ("glob", "docs/decisions", "ADR-*.md")],
        "context": ["docs/10-requirements.md"],
    },
    "tasks": {"main": [("glob", "docs/tasks", "T-*.md"), ("code", ".agentloop/plan.yaml")], "context": []},
    # Gate 4 reviews the generated review, not a security-review markdown file: green tests
    # plus an AI's summary was never the evidence this gate is supposed to weigh.
    "build": {"main": [("code", ".agentloop/review.yaml")], "context": []},
    "release": {"main": ["docs/test/test-plan.md", "docs/retrospective.md"], "context": []},
}


class ReviewError(Exception):
    """An unknown gate name — the only input error this module can be handed."""


def _confidence(section_md: str) -> str | None:
    """The confidence level the pane badges, or None when the author never stated one.

    Two rules, both in service of "the badge must never look better than the document":

    - Read only the value of a **labelled** `Confidence:` line. The word also occurs in the section
      heading and in prose ("we have high confidence the runner exists"), and taking the first
      level found anywhere would let that prose badge a `low` self-assessment as `high`.
    - AGENTS.md asks for confidence *by area*, so one line legitimately carries several levels
      ("high (API surface), low (integration)"). Report the **weakest** — the low spot is the part
      the human must not miss. The unfilled scaffold placeholder is recognised separately and
      reads as unset rather than as a real `low`.
    """
    for line in _CONFIDENCE_LINE_RE.finditer(section_md):
        value = line.group("value")
        if _PLACEHOLDER_RE.search(value):
            return None
        levels = {m.group(1).lower() for m in _LEVEL_RE.finditer(value)}
        if levels:
            return min(levels, key=lambda lv: _LEVEL_RANK[lv])
    return None


def _within(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _deliverable(root: Path, rel: str | Path, *, kind: str = "markdown") -> dict[str, object]:
    """One deliverable entry: rendered body, split-out self-assessment, and honest absence."""
    rel = Path(rel)
    path = root / rel
    entry: dict[str, object] = {
        "id": rel.name,
        "label": str(rel),
        "kind": kind,
        "exists": False,
        "html": "",
        "self_assessment": None,
        "truncated": False,
        "mtime": None,
    }
    if not _within(root, path):
        return entry  # a symlink pointing out of the repo reads as absent, never followed
    try:
        raw = path.read_bytes()
        stat = path.stat()
    except OSError:
        return entry
    entry["exists"] = True
    entry["mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    if len(raw) > _MAX_DELIVERABLE:
        raw = raw[:_MAX_DELIVERABLE]
        entry["truncated"] = True
    text = raw.decode("utf-8", errors="replace")
    if kind == "code":
        entry["html"] = "<pre><code>" + html.escape(text, quote=True) + "</code></pre>"
        return entry
    section, rest = mdlite.extract_section(text, "Self-assessment")
    if section is not None:
        entry["self_assessment"] = {"html": mdlite.render(section), "confidence": _confidence(section)}
        text = rest
    entry["html"] = mdlite.render(text)
    return entry


def _expand(root: Path, spec: list[_SpecItem]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for item in spec:
        if isinstance(item, str):
            out.append(_deliverable(root, item))
        elif len(item) == 2:  # ("code", path)
            out.append(_deliverable(root, item[1], kind="code"))
        else:  # ("glob", dir, pattern) — fixed directory, template-free, name-validated, sorted
            _, rel_dir, pattern = item
            base = root / rel_dir
            names = sorted(
                p.name for p in base.glob(pattern) if p.name not in _TEMPLATE_NAMES and _GLOB_NAME_RE.match(p.name)
            )
            out.extend(_deliverable(root, Path(rel_dir) / n) for n in names)
    return out


# -- gate ④: the work-branch diff and the generated-review freshness --


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)


def _default_branch(root: Path) -> str | None:
    rc, out = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        return out.strip()
    for candidate in ("main", "master"):
        rc, _ = _git(root, "rev-parse", "--verify", "--quiet", candidate)
        if rc == 0:
            return candidate
    return None


def _diff_block(root: Path) -> dict[str, object]:
    """The gate-④ change set: merge-base(HEAD, default branch) diff, or an honest fallback.

    Same base definition as the build loop's security-review prompt. When no base exists (no
    default branch, HEAD *is* the base, single-branch repo) the block degrades to the last 20
    commits so the reviewer still sees what the branch contains.
    """
    rc, out = _git(root, "rev-parse", "HEAD")
    if rc != 0:
        return {"error": "not a git repository (or it has no commits)"}
    head = out.strip()
    base_ref = _default_branch(root)
    base = None
    if base_ref:
        rc, out = _git(root, "merge-base", "HEAD", base_ref)
        base = out.strip() if rc == 0 and out.strip() else None
    if base is None or base == head:
        rc, out = _git(root, "log", "--oneline", "-20")
        return {
            "head": head,
            "log": out.strip().splitlines() if rc == 0 else [],
            "note": "no merge-base diff (HEAD is at the base or no default branch); showing recent commits",
        }
    _, stat = _git(root, "diff", "--stat", f"{base}..HEAD")
    _, names = _git(root, "diff", "--name-status", f"{base}..HEAD")
    _, patch = _git(root, "diff", f"{base}..HEAD")
    truncated = len(patch.encode("utf-8", errors="replace")) > _MAX_PATCH
    if truncated:
        patch = patch.encode("utf-8", errors="replace")[:_MAX_PATCH].decode("utf-8", errors="replace")
    return {
        "head": head,
        "base": base,
        "base_ref": base_ref,
        "stat": stat.rstrip(),
        "name_status": [ln.split("\t", 1) for ln in names.strip().splitlines() if "\t" in ln],
        "patch": patch,  # raw text — the client renders it per line via textContent, never innerHTML
        "truncated": truncated,
    }


def _review_meta(root: Path, head: str | None) -> dict[str, object]:
    """Whether the generated machine review speaks for the commit actually under review.

    0.9.0 has no `security-review.md`: gate ④ approves the generated *review.yaml*, whose
    machine binding records the `subject_head_sha` it was produced against. Freshness is that
    sha against the current HEAD — a commit made after the review was generated leaves the
    review stale (plan §17.5, E2E-08), and the pane must show it rather than imply currency.
    """
    try:
        raw = strict_yaml.load_mapping((root / ".agentloop" / "review.yaml").read_text(encoding="utf-8"))
    except (OSError, strict_yaml.StrictParseError):
        return {"reviewed_head": None, "head": head, "fresh": False}
    machine = raw.get("machine")
    binding = machine.get("binding") if isinstance(machine, dict) else None
    reviewed = str(binding.get("subject_head_sha", "")) if isinstance(binding, dict) else ""
    reviewed_or_none = reviewed or None
    return {"reviewed_head": reviewed_or_none, "head": head, "fresh": bool(reviewed and head and reviewed == head)}


def _gate_statuses(root: Path) -> dict[str, str]:
    """Gate statuses from state.yaml; {} when it cannot be read.

    A broken SSOT must not take the review pane down — but an unreadable gate reads as
    `pending`, never as approved, so the pane can only ever understate what has been decided.
    """
    try:
        raw = strict_yaml.load_mapping((root / ".agentloop" / "state.yaml").read_text(encoding="utf-8"))
    except (OSError, strict_yaml.StrictParseError):
        return {}
    state = models.State(raw)
    return {gate: state.gate_status(gate) for gate in models.GATE_ORDER}


def collect_review(root: str | Path, gate: str) -> dict[str, object]:
    """Everything the review pane shows for `gate`. Raises ReviewError only for an unknown gate."""
    if gate not in _GATE_SPEC:
        raise ReviewError(f"unknown gate '{gate}' (expected one of {', '.join(models.GATE_ORDER)})")
    root = Path(root)

    gates = _gate_statuses(root)
    awaiting = next((g for g in models.GATE_ORDER if gates.get(g) != "approved"), None)

    result: dict[str, object] = {
        "gate": gate,
        "index": models.GATE_ORDER.index(gate) + 1,
        "status": gates.get(gate, "pending"),
        "awaiting": awaiting,
        "is_awaiting": gate == awaiting,
        "deliverables": _expand(root, _GATE_SPEC[gate]["main"]),
        "context": _expand(root, _GATE_SPEC[gate]["context"]),
        "diff": None,
        "review_meta": None,
        "open_escalations": None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if gate == "build":
        diff = _diff_block(root)
        result["diff"] = diff
        head_value = diff.get("head")
        result["review_meta"] = _review_meta(root, head_value if isinstance(head_value, str) else None)
    if gate == "release":
        events, _ = event_chain.scan(root / ".agentloop" / "events.ndjson")
        result["open_escalations"] = sum(1 for e in events if e.event in events_mod.ATTENTION_EVENTS)
    return result


# -- gate ④ Challenge-first session (plan §14.1, §21.1, §21.2) --

# The deliverable review above answers "what do I read"; the Challenge-first session answers the
# harder question gate ④ asks — "did *you* think about this before you saw the answer". The sequence
# is mechanical: `stage_data` refuses a priming stage (expected/actual, scenarios, the decision card)
# until the unprimed challenge is complete, so a reviewer using the UI cannot skip to the reveal. The
# rules live in human_review; this layer only shapes them into JSON. Every payload is machine-review
# content plus the reviewer's own progress — never raw agent HTML (the client renders via textContent).


def _load_review(root: Path) -> models.Review | None:
    """review.yaml as a Review, or None when absent/unreadable — tolerant like the rest of the pane."""
    try:
        raw = strict_yaml.load_mapping((root / ".agentloop" / "review.yaml").read_text(encoding="utf-8"))
    except (OSError, strict_yaml.StrictParseError):
        return None
    return models.Review(raw)


def _not_generated(stage: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"generated": False, "reason": "no machine review has been generated"}
    if stage is not None:
        payload["stage"] = stage
    return payload


def review_session(root: str | Path) -> dict[str, object]:
    """The whole state of the human review: stage progress, the challenge front, and every blocker.

    This is the one call the review pane polls: it carries the next challenge (reveal stripped),
    which stages are locked behind it, the expertise and budget verdicts, and the machine digest a
    subsequent write must echo back (a stale one is refused — plan §17.5).
    """
    root = Path(root)
    review = _load_review(root)
    if review is None or not review.is_generated:
        return _not_generated()
    human = dict(review.human)
    completed = set(_completed_stages(human))
    stages = [
        {"name": stage, "locked": human_review.stage_locked(review, human, stage), "complete": stage in completed}
        for stage in models.REVIEW_STAGE_ORDER
    ]
    return {
        "generated": True,
        "human_status": review.human_status,
        "machine_digest": review.machine_digest(),
        "next_challenge": human_review.next_challenge(review, human),
        "unanswered_challenges": human_review.unanswered_challenges(review, human),
        "open_counterfactuals": human_review.open_counterfactuals(review, human),
        "expertise_gaps": human_review.expertise_gaps(review, human),
        "budget": human_review.budget_report(review, human),
        "scope_split_required": human_review.scope_split_required(review, human),
        "completion_blockers": human_review.completion_blockers(review, human),
        "can_freeze": human_review.can_freeze(review, human),
        "stages": stages,
    }


def _completed_stages(human: dict[str, object]) -> list[str]:
    session = human.get("session")
    completed = session.get("completed_stages") if isinstance(session, dict) else None
    return [str(s) for s in completed] if isinstance(completed, list) else []


def stage_data(root: str | Path, stage: str) -> dict[str, object]:
    """The content of one review stage — or a `locked` refusal when a priming stage is reached early.

    Raises ReviewError for an unknown stage (the HTTP layer's 404); a locked stage is a normal 200
    with `locked: true`, because "you must answer the challenge first" is information the pane shows,
    not an error (plan §21.2).
    """
    if stage not in models.REVIEW_STAGE_VALUES:
        raise ReviewError(f"unknown review stage '{stage}' (one of {', '.join(models.REVIEW_STAGE_ORDER)})")
    root = Path(root)
    review = _load_review(root)
    if review is None or not review.is_generated:
        return _not_generated(stage)
    human = dict(review.human)
    if human_review.stage_locked(review, human, stage):
        return {"stage": stage, "locked": True, "reason": "complete the unprimed challenge stage first"}

    machine = review.machine
    payload: dict[str, object] = {"stage": stage, "locked": False, "generated": True}
    if stage == "challenge":
        payload["challenge"] = human_review.next_challenge(review, human)
        payload["remaining"] = human_review.unanswered_challenges(review, human)
    elif stage == "overview":
        payload["summary"] = machine.get("summary", {})
    elif stage == "risk_brief":
        payload["gaps"] = list(machine.get("gaps", []) or [])
        payload["extra_behaviors"] = list(review.extra_behaviors)
        payload["statements"] = list(machine.get("statements", []) or [])
    elif stage == "expected_actual":
        payload["claims"] = list(review.claim_results)
        payload["actual_extraction"] = list(review.actual_statements)
    elif stage == "scenarios":
        payload["scenarios"] = list(machine.get("scenarios", []) or [])
    elif stage == "evidence_matrix":
        payload["evidence_matrix"] = list(machine.get("evidence_matrix", []) or [])
    elif stage == "module_delta":
        payload["module_deltas"] = list(machine.get("module_deltas", []) or [])
    elif stage == "decision":
        payload["decision_cards"] = list(machine.get("decision_cards", []) or [])
    elif stage == "security":
        payload["findings"] = list(review.security_findings)
    elif stage == "raw_diff":
        payload["diff"] = _diff_block(root)
    elif stage == "attestation":
        payload["can_freeze"] = human_review.can_freeze(review, human)
        payload["completion_blockers"] = human_review.completion_blockers(review, human)
    return payload


def challenge_reveal(root: str | Path, challenge_id: str) -> dict[str, object]:
    """The reveal for an answered challenge — served only after the answer is recorded (plan §14.2)."""
    root = Path(root)
    review = _load_review(root)
    if review is None or not review.is_generated:
        return _not_generated()
    if challenge_id not in human_review.answered_challenge_ids(review.human):
        return {"challenge_id": challenge_id, "answered": False, "reveal": None}
    return {"challenge_id": challenge_id, "answered": True, "reveal": human_review.reveal_for(review, challenge_id)}
