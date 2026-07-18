"""Read-only aggregation of what a human must read before opening a gate — the review pane's data.

status_api.py answers "where does the lifecycle stand"; this module answers the companion question
"what do I read to approve the gate in front of me". `collect_review(root, gate)` returns one JSON
object per gate: the phase deliverables rendered through mdlite (escape-first — see its threat
model), each deliverable's Self-assessment section split out so the pane can pin it, and for gate
④ the work-branch diff plus the security-review report's freshness.

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

from agentloop import common, mdlite
from agentloop import events as events_mod

_MAX_DELIVERABLE = 300_000  # bytes of one deliverable the pane will render
_MAX_PATCH = 200_000  # bytes of unified diff for gate ④
_GIT_TIMEOUT_SEC = 10
_GLOB_NAME_RE = re.compile(r"^(T|ADR)-[A-Za-z0-9_.-]+\.md$")
_TEMPLATE_NAMES = frozenset({"T-template.md", "ADR-template.md"})
_REVIEWED_HEAD_RE = re.compile(r"^Reviewed-HEAD:\s*([0-9a-fA-F]+)", re.MULTILINE)
_CONFIDENCE_LINE_RE = re.compile(r"^.*\bconfidence\b.*$", re.IGNORECASE | re.MULTILINE)
_LEVEL_RE = re.compile(r"\b(high|medium|low)\b", re.IGNORECASE)

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
    "tasks": {"main": [("glob", "docs/tasks", "T-*.md"), ("code", ".agentloop/tasks.yaml")], "context": []},
    "build": {"main": [".agentloop/security-review.md"], "context": []},
    "release": {"main": ["docs/test/test-plan.md", "docs/retrospective.md"], "context": []},
}


class ReviewError(Exception):
    """An unknown gate name — the only input error this module can be handed."""


def _confidence(section_md: str) -> str | None:
    """The single stated confidence level, or None when unset/ambiguous.

    The scaffold placeholder line reads `high / medium / low (…)` — several levels on one line
    means the agent never filled it in, which the pane must show as unset, not as "high". Lines
    mentioning confidence without a level (the section heading itself) are skipped.
    """
    for line in _CONFIDENCE_LINE_RE.finditer(section_md):
        levels = {m.group(1).lower() for m in _LEVEL_RE.finditer(line.group(0))}
        if len(levels) == 1:
            return next(iter(levels))
        if len(levels) > 1:
            return None
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


# -- gate ④: the work-branch diff and the security-review freshness --


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
    """Whether .agentloop/security-review.md speaks for the commit actually under review."""
    try:
        text = (root / ".agentloop" / "security-review.md").read_text(encoding="utf-8")
    except OSError:
        return {"reviewed_head": None, "head": head, "fresh": False}
    m = _REVIEWED_HEAD_RE.search(text)
    reviewed = m.group(1) if m else None
    return {"reviewed_head": reviewed, "head": head, "fresh": bool(reviewed and head and reviewed == head)}


def collect_review(root: str | Path, gate: str) -> dict[str, object]:
    """Everything the review pane shows for `gate`. Raises ReviewError only for an unknown gate."""
    if gate not in _GATE_SPEC:
        raise ReviewError(f"unknown gate '{gate}' (expected one of {', '.join(common.GATE_ORDER)})")
    root = Path(root)

    gates: dict[str, str] = {}
    try:
        gates = common.gates_of(common.read_frontmatter(str(root / ".agentloop" / "state.md"))) or {}
    except Exception:  # noqa: BLE001 - a broken SSOT must not take the review pane down
        pass
    awaiting = next((g for g in common.GATE_ORDER if gates.get(g) != "approved"), None)

    result: dict[str, object] = {
        "gate": gate,
        "index": common.GATE_ORDER.index(gate) + 1,
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
        opened = events_mod.open_escalations(events_mod.load_events(str(root / ".agentloop" / "events.ndjson")))
        result["open_escalations"] = len(opened)
    return result
