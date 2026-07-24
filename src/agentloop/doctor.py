"""`agentloop doctor` — one read-only diagnosis of everything the guarantees rest on.

Every failure mode 0.9.0 defends against surfaces late and cryptically if nobody looks for it:
a Trust Manifest that is not there, a runtime directory another user owns, a sandbox profile
that is quietly running repository code on the host, an audit chain with a hole in it. This
command asks all of those questions at once and prints one line each, in the five categories
of plan §26: **format · trust · runtime/sandbox · plan/evidence · review**.

Levels: FAIL = a broken invariant, fix before continuing (exit 1). WARN = suspicious or
weaker-than-intended. INFO = context worth knowing. PASS = checked and healthy.

Two rules make the output trustworthy:

**It never repairs anything.** A doctor that fixes what it finds is a doctor whose findings
nobody reads, and several of the things it checks (an approval, an audit record) must only
ever change by a deliberate human action.

**It never reports "not measured" as "fine".** A missing Trust Manifest is not a pass, and an
unreadable chain is not an empty one. Where the honest answer is "this could not be checked",
that is what the line says.

`--unsupported-layout` is the one mode that runs against a 0.8.x repository — the single
command this release will execute there, so that a human who upgrades gets a diagnosis instead
of a bare refusal.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import agentloop
from agentloop import common, dag, dag_trace, event_chain, install, models, strict_yaml
from agentloop import lock as lock_mod
from agentloop import repo as repo_mod
from agentloop import store as store_mod

logger = logging.getLogger(__name__)

SETTINGS_PATH = ".claude/settings.json"
COPILOT_HOOKS_DIR = ".github/hooks"
TRUST_MANIFEST_ENV = "AGENTLOOP_TRUST_MANIFEST"


@dataclass(frozen=True)
class Finding:
    level: str  # FAIL | WARN | INFO | PASS
    area: str
    message: str


# --- format --------------------------------------------------------------------


def check_layout(repo: repo_mod.Repo) -> list[Finding]:
    """Is this repository on the 0.9.0 layout at all?"""
    legacy = repo.legacy_markers()
    if legacy:
        return [
            Finding(
                "FAIL", "format", f"0.8.x layout detected ({', '.join(legacy)}). {repo_mod.UNSUPPORTED_LAYOUT_MESSAGE}"
            )
        ]
    missing = [
        name
        for name, path in (
            ("config.yaml", repo.config),
            ("state.yaml", repo.state),
            ("plan.yaml", repo.plan),
            ("review.yaml", repo.review),
        )
        if not path.exists()
    ]
    if missing:
        return [Finding("FAIL", "format", f"missing SSOT document(s): {', '.join(missing)} — run `agentloop init`")]
    return [Finding("PASS", "format", "the four SSOT documents are present")]


def check_lock(repo: repo_mod.Repo) -> list[Finding]:
    try:
        data = lock_mod.read(repo.lock)
    except lock_mod.LockError as exc:
        return [Finding("FAIL", "format", str(exc))]
    if data is None:
        return [Finding("INFO", "format", f"no {lock_mod.LOCK_NAME} yet — `agentloop init`/`sync` writes it")]
    findings = [Finding("PASS", "format", f"{lock_mod.LOCK_NAME} readable (format {data.get('format')})")]
    warning = lock_mod.startup_warning(repo, agentloop.__version__)
    if warning:
        findings.append(Finding("WARN", "format", warning))
    return findings


def check_documents(repo: repo_mod.Repo) -> tuple[list[Finding], dict[str, object]]:
    """Every SSOT document against its strict schema and cross-references."""
    store = store_mod.Store(repo)
    findings: list[Finding] = []
    loaded: dict[str, object] = {}
    for name, reader in (
        ("config", store.read_config),
        ("state", store.read_state),
        ("plan", store.read_plan),
        ("review", store.read_review),
    ):
        try:
            value = reader()
        except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
            findings.append(Finding("FAIL", "format", f"{name}.yaml: {exc}"))
            continue
        if value is None:
            findings.append(Finding("INFO", "format", f"{name}.yaml is absent"))
            continue
        loaded[name] = value
        findings.append(Finding("PASS", "format", f"{name}.yaml valid (schema + cross-references)"))
    return findings, loaded


def check_materialized(repo: repo_mod.Repo) -> list[Finding]:
    """The materialized prompts/schema/oci/rules must equal the packaged payload.

    Reuses install's own destination map rather than re-deriving it, so "what sync writes" and
    "what doctor checks" cannot answer differently — a drift canary that drifts is worse than
    none at all.
    """
    desired = install._dest_map(install.MATERIALIZED)
    drifted: list[str] = []
    for rel, blob in sorted(desired.items()):
        try:
            if lock_mod.norm_hash(repo.path(rel).read_bytes()) != lock_mod.norm_hash(blob):
                drifted.append(rel)
        except OSError:
            drifted.append(f"{rel} (absent)")
    if drifted:
        return [
            Finding(
                "WARN",
                "format",
                f"{len(drifted)} materialized file(s) differ from the packaged payload "
                f"(e.g. {drifted[0]}) — `agentloop sync --check` lists them all",
            )
        ]
    return [Finding("PASS", "format", f"{len(desired)} materialized file(s) match the packaged payload")]


# --- trust ----------------------------------------------------------------------


def trust_manifest_path() -> Path:
    """Where the external Trust Manifest is expected. Deliberately outside the repository."""
    from agentloop import trust

    return trust.manifest_path()


def check_trust() -> list[Finding]:
    """The Trust Manifest, its allowed-signers file, and the signing toolchain.

    Its absence is a FAIL, not a warning: without it there is no authorized principal, so no
    gate can open. Saying "PASS: no manifest configured" would describe a repository in which
    nothing can ever be approved as healthy.
    """
    from agentloop import trust

    findings: list[Finding] = []
    path = trust_manifest_path()
    try:
        manifest = trust.load(path)
    except trust.TrustError as exc:
        findings.append(Finding("FAIL", "trust", str(exc)))
    else:
        findings.append(
            Finding(
                "PASS",
                "trust",
                f"Trust Manifest readable at {path} ({len(manifest.identities)} identity/identities)",
            )
        )
        if hasattr(os, "getuid") and (path.stat().st_mode & 0o077):
            findings.append(
                Finding("WARN", "trust", f"{path} is group/world accessible (mode {oct(path.stat().st_mode & 0o777)})")
            )
        # A manifest that names an allowed-signers file that is not there is worse than a
        # missing manifest: it looks configured, and every signature verification fails.
        if not manifest.allowed_signers_file:
            findings.append(
                Finding("FAIL", "trust", "the manifest names no allowed_signers_file — signatures cannot be verified")
            )
        elif not Path(manifest.allowed_signers_file).exists():
            findings.append(
                Finding("FAIL", "trust", f"allowed_signers_file {manifest.allowed_signers_file} does not exist")
            )
        else:
            findings.append(Finding("PASS", "trust", f"allowed-signers file present ({manifest.allowed_signers_file})"))

    if shutil.which("ssh-keygen"):
        findings.append(Finding("PASS", "trust", "ssh-keygen found (attestation signing/verification)"))
    else:
        findings.append(Finding("FAIL", "trust", "ssh-keygen not found on PATH — attestations cannot be verified"))
    return findings


def check_attestations(repo: repo_mod.Repo, state: models.State | None) -> list[Finding]:
    """Every approved gate must cite an attestation that is actually in the tree."""
    if state is None:
        return []
    findings: list[Finding] = []
    for gate in models.GATE_ORDER:
        if state.gate_status(gate) != "approved":
            continue
        receipt = state.gate_receipt(gate) or {}
        attestation_id = receipt.get("attestation_id")
        if not isinstance(attestation_id, str):
            findings.append(Finding("FAIL", "trust", f"gate '{gate}' is approved with no attestation id"))
            continue
        if not (repo.attestations / f"{attestation_id}.json").exists():
            findings.append(
                Finding(
                    "FAIL", "trust", f"gate '{gate}' cites {attestation_id}, which is not in .agentloop/attestations/"
                )
            )
        else:
            findings.append(Finding("PASS", "trust", f"gate '{gate}' receipt cites {attestation_id}"))
    if not findings:
        findings.append(Finding("INFO", "trust", "no gate is approved yet"))
    return findings


# --- runtime and sandbox ---------------------------------------------------------


def check_runtime(repo: repo_mod.Repo) -> list[Finding]:
    """The runtime directory, its privacy, and any leftovers from an interrupted run."""
    findings: list[Finding] = []
    base, private = store_mod.runtime_home()
    runtime = store_mod.runtime_dir(repo)
    if not private:
        findings.append(
            Finding(
                "WARN",
                "runtime",
                f"XDG_RUNTIME_DIR is unset; falling back to {base}. A temp directory is not guaranteed to be "
                "cleared at logout or unreachable by other users — the isolation is weaker, not equivalent.",
            )
        )
    if runtime.exists():
        try:
            store_mod.ensure_private_dir(runtime)
            findings.append(Finding("PASS", "runtime", f"runtime directory {runtime} is private (0700)"))
        except store_mod.StoreError as exc:
            findings.append(Finding("FAIL", "runtime", str(exc)))
        store = store_mod.Store(repo)
        if store.journal.exists():
            findings.append(
                Finding(
                    "WARN",
                    "runtime",
                    "a store journal is present — a transaction was interrupted. The next command recovers it "
                    "automatically (forward past the point events were appended, back before it).",
                )
            )
    else:
        findings.append(Finding("INFO", "runtime", f"no runtime directory yet ({runtime})"))

    if repo.git_common_dir is None:
        findings.append(
            Finding("WARN", "runtime", "not a git checkout — change digests and blob anchors are unavailable")
        )
    elif not repo.is_canonical_checkout:
        findings.append(
            Finding(
                "INFO",
                "runtime",
                "this is a linked worktree; mutations must go through the control plane, not the store directly",
            )
        )
    return findings


def check_sandbox(config: models.Config | None) -> list[Finding]:
    """Executor profiles: anything running repository code must be an OCI profile (plan §10.1)."""
    if config is None:
        return []
    findings: list[Finding] = []
    offenders = config.unsandboxed_code_profiles()
    if offenders:
        findings.append(
            Finding(
                "FAIL",
                "sandbox",
                f"profile(s) {', '.join(offenders)} run repository-derived code on the host. A test file is "
                "code an agent wrote, and it would run with your credentials. Build the packaged image "
                "(`agentloop oci build --profile <name>`) and pin its digest.",
            )
        )
    for name, profile in sorted(config.profiles.items()):
        if profile.is_sandboxed and not profile.image_digest:
            findings.append(Finding("FAIL", "sandbox", f"profile '{name}' has no digest-pinned image"))
        elif profile.is_sandboxed:
            findings.append(Finding("PASS", "sandbox", f"profile '{name}' pinned to {profile.image_digest[:19]}…"))

    if any(p.is_sandboxed for p in config.profiles.values()):
        runtime = shutil.which("docker") or shutil.which("podman")
        level, message = (
            ("PASS", f"container runtime found ({Path(runtime).name})")
            if runtime
            else ("FAIL", "no docker/podman on PATH, but OCI profiles are configured")
        )
        findings.append(Finding(level, "sandbox", message))
    return findings


def check_independence(config: models.Config | None) -> list[Finding]:
    """The actual-extractor / comparator pair (plan §12.4)."""
    if config is None:
        return []
    from agentloop import agent_cli

    warnings = agent_cli.independence_report(config)
    if not warnings:
        left = config.independence_group("actual_extractor")
        right = config.independence_group("comparator")
        return [Finding("PASS", "review", f"independent groups: {left} vs {right}")]
    # A shared group blocks critical work; distinct models of one provider merely weaken it.
    level = (
        "FAIL" if any("share the independence group" in w or "no independence_group" in w for w in warnings) else "WARN"
    )
    return [Finding(level, "review", w) for w in warnings]


def check_binaries() -> list[Finding]:
    findings: list[Finding] = []
    for name, level, why in (
        ("git", "FAIL", "change digests, blob anchors and worktrees are git operations"),
        ("uv", "WARN", "the documented way to run this tool and its quality gate"),
        ("gh", "INFO", "only needed when github.enabled is turned on"),
    ):
        if shutil.which(name):
            findings.append(Finding("PASS", "env", f"{name} found on PATH"))
        else:
            findings.append(Finding(level, "env", f"{name} not found on PATH — {why}"))
    return findings


def _mentions_guard(text: str) -> bool:
    return "agentloop guard" in text or "gate_guard" in text


def check_hook(repo: repo_mod.Repo) -> list[Finding]:
    """The gate guard is only real if a PreToolUse hook actually invokes it.

    There is no `enforce_hook` knob to check any more: a guard with an off switch an agent can
    reach is a convention, so the only question left is whether a host carries it.
    """
    surfaces: list[str] = []
    try:
        if _mentions_guard(repo.path(SETTINGS_PATH).read_text(encoding="utf-8")):
            surfaces.append("claude")
    except OSError:
        pass
    for hook_file in sorted(repo.path(COPILOT_HOOKS_DIR).glob("*.json")):
        try:
            if _mentions_guard(hook_file.read_text(encoding="utf-8")):
                surfaces.append("copilot")
                break
        except OSError:
            continue
    if not surfaces:
        return [
            Finding(
                "WARN",
                "hook",
                f"the gate guard is registered in neither {SETTINGS_PATH} nor {COPILOT_HOOKS_DIR}/*.json — "
                "edit-time enforcement is absent. The commit-stage check (`agentloop guard --check-diff`) "
                "still applies if the pre-commit hook is installed.",
            )
        ]
    findings = [Finding("PASS", "hook", f"gate guard registered ({', '.join(surfaces)})")]
    if len(surfaces) == 1:
        other = "VS Code Copilot" if surfaces == ["claude"] else "Claude Code"
        findings.append(
            Finding("INFO", "hook", f"only the {surfaces[0]} hook host is registered — {other} sessions run without it")
        )
    return findings


# --- plan and evidence ------------------------------------------------------------


def check_plan(plan: models.Plan | None, state: models.State | None) -> list[Finding]:
    if plan is None:
        return [Finding("INFO", "plan", "no plan yet — /req and /design fill it")]
    findings: list[Finding] = []
    try:
        graph = dag.join(plan, state)
        findings.append(Finding("PASS", "plan", f"the task DAG is acyclic ({len(graph.tasks)} task(s))"))
        orphan_claims = graph.claims_without_a_task(plan)
        if orphan_claims:
            findings.append(Finding("WARN", "plan", f"claims with no answerable task: {', '.join(orphan_claims)}"))
    except dag.DagError as exc:
        findings.append(Finding("FAIL", "plan", str(exc)))

    report = dag_trace.trace(plan)
    findings += [Finding("FAIL", "evidence", e) for e in report.errors]
    findings += [Finding("WARN", "evidence", w) for w in report.warnings]
    if not report.errors and not report.warnings:
        findings.append(Finding("PASS", "evidence", "the requirement → claim → task/oracle thread is whole"))

    unavailable = sorted({p for s in plan.searches for p in s.unavailable_providers})
    if unavailable:
        # Surfaced even when an alternate evidence path succeeded: a hidden provider failure is
        # how "no documentation exists" gets invented (plan §15.3).
        findings.append(Finding("INFO", "evidence", f"provider(s) unavailable during search: {', '.join(unavailable)}"))
    for source in plan.sources:
        if source.verification_status == "failed":
            findings.append(Finding("FAIL", "evidence", f"{source.id}: source verification failed"))
    return findings


def check_gate_chain(state: models.State | None) -> list[Finding]:
    if state is None:
        return []
    violations = state.gate_chain_violations()
    if violations:
        return [
            Finding(
                "FAIL",
                "gates",
                f"gate '{approved}' is approved while '{pending}' upstream is pending — an approval survived "
                "a roll back, so downstream work stands on a withdrawn decision",
            )
            for approved, pending in violations
        ]
    return [Finding("PASS", "gates", "the gate chain invariant holds")]


# --- review and audit chain --------------------------------------------------------


def check_chain(repo: repo_mod.Repo) -> list[Finding]:
    events, defects = event_chain.scan(repo.events)
    if defects:
        shown = "; ".join(str(d) for d in defects[:3])
        return [
            Finding(
                "FAIL",
                "event-chain",
                f"{len(defects)} defect(s): {shown}{' …' if len(defects) > 3 else ''}. "
                "Restore events.ndjson from git — never rewrite it to agree with the current state.",
            )
        ]
    return [
        Finding("PASS", "event-chain", f"{len(events)} event(s), chain intact, root {event_chain.chain_root(events)}")
    ]


def check_review(review: models.Review | None) -> list[Finding]:
    if review is None or not review.is_generated:
        return [Finding("INFO", "review", "no machine review generated yet")]
    findings: list[Finding] = []
    if review.coverage_sufficient:
        findings.append(
            Finding("PASS", "review", f"coverage sufficient; {len(review.extra_behaviors)} extra behaviour(s)")
        )
    else:
        findings.append(
            Finding(
                "FAIL",
                "review",
                "the coverage manifest is insufficient — extra-behaviour counts are undeterminable, not zero",
            )
        )
    blocking = review.blocking_security_findings
    findings.append(Finding("FAIL" if blocking else "PASS", "review", f"{len(blocking)} blocking security finding(s)"))
    findings.append(Finding("INFO", "review", f"human review: {review.human_status}"))
    return findings


# --- driver -----------------------------------------------------------------------


def run_checks(repo: repo_mod.Repo | None = None) -> list[Finding]:
    if repo is None:
        try:
            repo = repo_mod.get()
        except repo_mod.RepoNotFoundError:
            repo = repo_mod.Repo(Path.cwd().resolve())

    findings = check_layout(repo)
    if any(f.level == "FAIL" and "0.8.x layout" in f.message for f in findings):
        return findings  # nothing below can be read against this layout

    findings += check_binaries()
    findings += check_lock(repo)
    document_findings, loaded = check_documents(repo)
    findings += document_findings
    findings += check_materialized(repo)

    config = loaded.get("config")
    state = loaded.get("state")
    plan = loaded.get("plan")
    review = loaded.get("review")
    assert config is None or isinstance(config, models.Config)
    assert state is None or isinstance(state, models.State)
    assert plan is None or isinstance(plan, models.Plan)
    assert review is None or isinstance(review, models.Review)

    findings += check_trust()
    findings += check_attestations(repo, state)
    findings += check_runtime(repo)
    findings += check_sandbox(config)
    findings += check_independence(config)
    findings += check_hook(repo)
    findings += check_gate_chain(state)
    findings += check_plan(plan, state)
    findings += check_chain(repo)
    findings += check_review(review)
    return findings


def unsupported_layout_report(repo: repo_mod.Repo) -> list[Finding]:
    """The only diagnosis this release runs against a 0.8.x repository."""
    legacy = repo.legacy_markers()
    if not legacy:
        return [Finding("PASS", "format", "no 0.8.x artifacts — this repository is on the 0.9.0 layout")]
    return [Finding("FAIL", "format", f"0.8.x artifact present: {marker}") for marker in legacy] + [
        Finding(
            "INFO",
            "format",
            "There is deliberately no migration. Rebuilding a plan from these artifacts would mean "
            "manufacturing evidence and authority for decisions that were never grounded — which is the "
            "failure 0.9.0 exists to prevent. Archive the cycle, remove .agentloop, and run `agentloop init`.",
        )
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="diagnose the AgentLoop environment and SSOT (read-only)")
    parser.add_argument(
        "--unsupported-layout",
        action="store_true",
        help="diagnose a 0.8.x repository (the only command that runs against one)",
    )
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo) if args.repo else repo_mod.get()
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    findings = unsupported_layout_report(repo) if args.unsupported_layout else run_checks(repo)
    for f in findings:
        print(f"  [{f.level:<4}] {f.area}: {f.message}")
    fails = sum(1 for f in findings if f.level == "FAIL")
    warns = sum(1 for f in findings if f.level == "WARN")
    print(f"\ndoctor: {fails} FAIL / {warns} WARN / {len(findings)} checks")
    if fails:
        logger.error("fix the FAIL items before continuing.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
