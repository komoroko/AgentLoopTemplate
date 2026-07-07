"""File cycle feedback as issues on the UPSTREAM template repository (opt-in, human-run).

At /verify's retrospective step, rows of docs/retrospective.md marked `Promote? = upstream`
are improvement proposals for the template itself, not for this product. The agent drafts one
self-contained issue per row into `.agentloop/feedback.yaml`; the human reviews the drafts and
runs `make feedback` (preview first with ARGS=--dry-run). This script is only the mechanism —
it never composes content:

  - Off by default. Enable with `github.feedback.enabled: true` in `.agentloop/config.yaml`.
  - The upstream repo comes from `github.feedback.repo` (owner/repo), falling back to the
    template source recorded in `.agentloop/adopt-manifest.yaml` (when it is a github.com URL).
  - If disabled / `gh` absent / no upstream resolvable, **print an explicit message and exit 0**
    (auto-skip, same doctrine as issue_sync.py).
  - Idempotent via the hidden body marker `<!-- agentloop-feedback:<content-hash> -->`:
    already-filed drafts are skipped. Existing markers are found with
    `gh issue list --search "agentloop-feedback in:body"` — label-independent, so dedup works
    even where the label could not be attached. GitHub's search index lags a little, so
    re-running within seconds of a filing could double-post; human-run with a dry-run preview,
    that stays theoretical.
  - The label is best-effort: creating/attaching it needs triage access to the upstream repo,
    which a non-collaborator does not have — filing proceeds without it.
  - One-way and additive only: never reads issues back, never edits or closes anything upstream.

`--dry-run` calls gh not at all and lists the resolved upstream repo and planned filings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = ".agentloop/config.yaml"
MANIFEST_PATH = ".agentloop/adopt-manifest.yaml"
FEEDBACK_PATH = ".agentloop/feedback.yaml"
FETCH_LIMIT = 1000  # gh issue list page cap; a truncated view could double-post, so stop instead


class FeedbackError(RuntimeError):
    """A drafts-file or gh integration failure."""


@dataclass(frozen=True)
class FeedbackConfig:
    enabled: bool
    repo: str
    label: str

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> FeedbackConfig:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        gh = (data.get("github") if isinstance(data, dict) else None) or {}
        fb = (gh.get("feedback") if isinstance(gh, dict) else None) or {}
        return cls(
            enabled=bool(fb.get("enabled", False)),
            repo=str(fb.get("repo", "") or ""),
            label=str(fb.get("label", "agentloop-feedback")),
        )


@dataclass(frozen=True)
class Draft:
    title: str
    body: str


# --- pure logic (under test) -----------------------------------------------

_GITHUB_SOURCE_RES = (
    re.compile(r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"),
    re.compile(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?$"),
)


def parse_source_repo(source: str) -> str:
    """`owner/repo` from a github.com URL (https or ssh); "" for anything else (local paths etc.)."""
    for pattern in _GITHUB_SOURCE_RES:
        match = pattern.match(source.strip())
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return ""


def resolve_upstream(cfg_repo: str, manifest_text: str | None) -> str:
    """The upstream repo to file against: explicit config wins, else the manifest's template source."""
    if cfg_repo:
        return cfg_repo
    if not manifest_text:
        return ""
    try:
        data = yaml.safe_load(manifest_text) or {}
    except yaml.YAMLError:
        return ""
    template = (data.get("template") if isinstance(data, dict) else None) or {}
    return parse_source_repo(str(template.get("source") or ""))


def load_drafts(text: str) -> list[Draft]:
    """Parse and validate the drafts file (`issues:` list of {title, body}, both non-empty)."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FeedbackError(f"cannot parse the drafts file: {exc}") from exc
    issues = data.get("issues") if isinstance(data, dict) else None
    if not isinstance(issues, list) or not issues:
        raise FeedbackError("the drafts file must contain a non-empty `issues:` list of {title, body} entries")
    drafts: list[Draft] = []
    for index, item in enumerate(issues, 1):
        title = str((item or {}).get("title") or "").strip() if isinstance(item, dict) else ""
        body = str((item or {}).get("body") or "").rstrip() if isinstance(item, dict) else ""
        if not title or not body:
            raise FeedbackError(f"issues[{index}]: needs a non-empty `title` and `body`")
        drafts.append(Draft(title=title, body=body))
    return drafts


def draft_hash(draft: Draft) -> str:
    """Deterministic content hash — the identity that makes re-filing the same draft a no-op."""
    return hashlib.sha256(f"{draft.title}\n{draft.body}".encode()).hexdigest()[:12]


def render_body(draft: Draft) -> str:
    """The issue body as filed: the draft plus a provenance footer and the hidden dedup marker."""
    footer = "_Filed by AgentLoop `make feedback` from a cycle retrospective._"
    return f"{draft.body}\n\n---\n{footer}\n<!-- agentloop-feedback:{draft_hash(draft)} -->\n"


def plan_filings(drafts: list[Draft], existing: set[str]) -> list[Draft]:
    """The drafts not yet filed upstream (their content hash is absent from the existing markers)."""
    return [d for d in drafts if draft_hash(d) not in existing]


def preflight(cfg: FeedbackConfig, repo: str) -> tuple[bool, str]:
    """Decide whether filing is possible. If not, return (False, reason); the caller exits 0 and skips."""
    if not cfg.enabled:
        return False, "Skipped upstream feedback because github.feedback.enabled=false."
    if shutil.which("gh") is None:
        return False, "Skipped upstream feedback because the gh CLI was not found."
    if not repo:
        return False, (
            "Skipped upstream feedback: no upstream repo — set github.feedback.repo (owner/repo)"
            f" or record a github.com template source in {MANIFEST_PATH}."
        )
    return True, ""


# --- gh execution ----------------------------------------------------------

_MARKER_RE = re.compile(r"<!--\s*agentloop-feedback:([0-9a-f]+)\s*-->")


def _run(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def existing_markers(repo: str) -> set[str]:
    """The dedup markers already present upstream (all states, label-independent body search)."""
    rc, out = _run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--search",
            "agentloop-feedback in:body",
            "--json",
            "body",
            "--limit",
            str(FETCH_LIMIT),
        ]
    )
    if rc != 0:
        raise FeedbackError(f"gh issue list failed:\n{out[-500:]}")
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise FeedbackError(f"cannot parse gh issue list output: {exc}") from exc
    if len(data) >= FETCH_LIMIT:
        raise FeedbackError(
            f"gh issue list returned {FETCH_LIMIT} issues (the fetch limit) — the dedup snapshot may be"
            " truncated and filing could create duplicates."
        )
    markers: set[str] = set()
    for item in data:
        markers.update(_MARKER_RE.findall(str(item.get("body", ""))))
    return markers


def ensure_label(repo: str, label: str) -> bool:
    """Best-effort label provisioning; False (file without the label) when we lack access upstream."""
    rc, _ = _run(["gh", "label", "create", label, "--repo", repo, "--color", "BFD4F2", "--force"])
    return rc == 0


def file_issue(repo: str, draft: Draft, label: str) -> str:
    """Create one upstream issue; returns its URL. Retries without the label if attaching it is denied."""
    base = ["gh", "issue", "create", "--repo", repo, "--title", draft.title, "--body", render_body(draft)]
    rc, out = _run(base + (["--label", label] if label else []))
    if rc != 0 and label:
        rc, out = _run(base)  # a non-collaborator may create issues but not attach labels
    if rc != 0:
        raise FeedbackError(f"issue creation failed for {draft.title!r}:\n{out[-500:]}")
    match = re.search(r"https://\S+", out)
    return match.group(0) if match else "(created; URL not found in gh output)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="file retrospective feedback as issues on the upstream template repo")
    parser.add_argument("--file", default=FEEDBACK_PATH, help=f"the drafts file (default: {FEEDBACK_PATH})")
    parser.add_argument("--dry-run", action="store_true", help="list the plan without calling gh (offline)")
    args = parser.parse_args(argv)

    drafts_path = Path(args.file)
    if not drafts_path.is_file():
        print(
            f"no drafts at {args.file} — /verify drafts it from the retrospective rows marked"
            " `Promote? = upstream` (see .claude/commands/verify.md).",
            file=sys.stderr,
        )
        return 1

    cfg = FeedbackConfig.load()
    manifest_path = Path(MANIFEST_PATH)
    manifest_text = manifest_path.read_text(encoding="utf-8") if manifest_path.is_file() else None
    repo = resolve_upstream(cfg.repo, manifest_text)

    try:
        drafts = load_drafts(drafts_path.read_text(encoding="utf-8"))

        if args.dry_run:
            print(f"[dry-run] upstream repo: {repo or 'UNRESOLVED (set github.feedback.repo)'}")
            for draft in drafts:
                print(f"[dry-run] create  {draft.title}  (marker {draft_hash(draft)})")
            return 0

        ready, reason = preflight(cfg, repo)
        if not ready:
            print(reason)
            return 0

        plan = plan_filings(drafts, existing_markers(repo))
        label = cfg.label if cfg.label and ensure_label(repo, cfg.label) else ""
        for draft in plan:
            print(f"filed: {draft.title} → {file_issue(repo, draft, label)}")
    except (OSError, FeedbackError) as exc:
        print(f"upstream feedback failed: {exc}", file=sys.stderr)
        return 1

    skipped = len(drafts) - len(plan)
    print(f"upstream feedback complete: {len(plan)} filed, {skipped} already filed (marker match).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
