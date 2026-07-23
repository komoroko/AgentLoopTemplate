"""`agentloop approve <gate>` — check readiness and produce an attestation request.

**This command does not open a gate.** That is the most important thing about it, and the
reason it was rewritten. In 0.8.x `agentloop approve build` stamped the gate line: the
permission prompt in front of the command *was* the human's approval, and `--force` skipped
even the evidence check. Anything that could run the command could open the gate.

In 0.9.0 a gate opens only when :mod:`agentloop.attestations` imports an envelope signed by a
key the **external** Trust Manifest authorizes for that role, bound to the exact digests being
approved. So this command does three things and stops:

  1. **Readiness** — every mechanical precondition for the gate (:func:`readiness`).
  2. **Request** — an unsigned envelope naming what would be approved, and its digests.
  3. **Instructions** — the two commands the human runs to sign and import it.

`--force` does not exist and is not coming back, and neither does `--by`: an identity you can
type is not an identity, so the principal is resolved from the signing key. Nothing here can
be pre-authorized into opening a gate, because nothing here opens one.

:func:`record_approval` is the other half — the Central Store transaction that writes the
receipt once a signature has been verified. It lives here, beside the readiness rules it has
to agree with, and is called by `attestation import`, never by a command.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agentloop import common, dag, dag_trace, digests, event_chain, models
from agentloop import repo as repo_mod
from agentloop import store as store_mod

logger = logging.getLogger(__name__)

#: The document each gate approves, for the receipt's `artifact_digest`.
GATE_ARTIFACT: dict[str, str] = {
    "requirements": "docs/10-requirements.md",
    "design": "docs/20-design.md",
}

#: gate → the attestation type that opens it (the inverse of models.ATTESTATION_GATE).
GATE_ATTESTATION: dict[str, str] = {gate: type_ for type_, gate in models.ATTESTATION_GATE.items()}


class ApprovalError(RuntimeError):
    """The gate cannot be approved, or an approval cannot be recorded."""


# --- readiness ------------------------------------------------------------------


def _chain_blockers(state: models.State, gate: str) -> list[str]:
    pending = state.pending_upstream(gate)
    if pending:
        return [
            f"gate '{pending}' is still pending — approving '{gate}' now would leave a decision "
            "standing on one that was never made (gates open in order)"
        ]
    if state.gate_status(gate) == "approved":
        return [f"gate '{gate}' is already approved"]
    return []


def _plan_blockers(plan: models.Plan | None, gate: str) -> list[str]:
    if plan is None:
        return [f"no plan at .agentloop/plan.yaml — there is nothing for gate '{gate}' to approve"]
    blockers: list[str] = list(dag_trace.trace(plan).errors)

    # A plan with nothing in it passes every consistency check trivially. Requiring content is
    # the difference between "no contradictions found" and "there is something here to approve".
    if not plan.claims:
        blockers.append(
            "the plan states no claims — there is nothing to approve. /req turns the brief into "
            "claims, each with the evidence that settles it."
        )
    for claim in plan.claims:
        if not claim.obligation_ids:
            blockers.append(
                f"{claim.id}: no evidence obligation — a claim nobody has to produce evidence for is "
                "an opinion with an id"
            )

    if gate in {"design", "tasks", "build", "release"}:
        for solution in plan.solutions:
            if not solution.alternatives:
                blockers.append(
                    f"{solution.id}: no alternatives recorded — a design decision with no stated "
                    "alternative is a decision nobody actually made"
                )
    if gate in {"tasks", "build", "release"}:
        if not plan.tasks:
            blockers.append("the plan declares no tasks")
        for oracle in plan.oracles:
            if not oracle.bundle_digest:
                blockers.append(f"{oracle.id}: the oracle bundle is not frozen (no digest)")
    return blockers


def _task_blockers(plan: models.Plan | None, state: models.State | None, gate: str) -> list[str]:
    if gate not in {"tasks", "build", "release"} or plan is None:
        return []
    try:
        graph = dag.join(plan, state)
    except dag.DagError as exc:
        return [str(exc)]
    blockers = [f"{cid}: no task is answerable for this claim" for cid in graph.claims_without_a_task(plan)]
    if gate in {"build", "release"}:
        unfinished = sorted(t.id for t in graph.tasks if not t.is_done)
        if unfinished:
            blockers.append(f"tasks not done: {', '.join(unfinished)}")
    return blockers


def _review_blockers(review: models.Review | None, gate: str) -> list[str]:
    """Gate ④/⑤ preconditions carried by the machine review (plan §16.8).

    A readiness check that passes because a stage has not been implemented yet is worse than
    no check at all, so an absent review is a blocker rather than a shrug.
    """
    if gate not in {"build", "release"}:
        return []
    if review is None or not review.is_generated:
        return [
            "no machine review has been generated — run `agentloop review generate`. "
            "Gate 4 approves a grounded review, not a green test run."
        ]
    blockers: list[str] = []
    if not review.coverage_sufficient:
        blockers.append("the coverage manifest is insufficient — extra-behaviour counts are undeterminable, not zero")
    blocking_extras = [e for e in review.extra_behaviors if e.get("blocking") is True]
    if blocking_extras:
        blockers.append(
            "blocking extra behaviours the plan does not account for: "
            + ", ".join(str(e.get("id")) for e in blocking_extras)
        )
    if review.blocking_security_findings:
        blockers.append(
            "blocking security findings: " + ", ".join(str(f.get("id")) for f in review.blocking_security_findings)
        )
    if review.human_status != "frozen":
        blockers.append(
            f"the human review is '{review.human_status}', not 'frozen' — "
            "complete it in the review UI (`agentloop review complete`)"
        )
    return blockers


def readiness(repo: repo_mod.Repo, gate: str) -> list[str]:
    """Every mechanical reason `gate` cannot be approved. Empty means a request may be issued.

    Deliberately exhaustive rather than short-circuiting: being handed one blocker, fixing it,
    and being handed the next is exactly the review friction plan §2.6 budgets against.
    """
    if gate not in models.GATE_VALUES:
        raise ApprovalError(f"unknown gate {gate!r} (one of {', '.join(models.GATE_ORDER)})")

    store = store_mod.Store(repo)
    try:
        state = store.read_state()
        plan = store.read_plan()
        review = store.read_review()
    except models.DocumentError as exc:
        return [str(exc)]

    if state is None:
        return ["no .agentloop/state.yaml — run `agentloop init` first"]

    blockers: list[str] = []
    _, defects = event_chain.scan(repo.events)
    if defects:
        blockers.append(
            f"the audit chain has {len(defects)} defect(s) — a receipt binds the chain root, so it "
            "cannot be issued against a damaged log (see `agentloop events --verify`)"
        )
    blockers += _chain_blockers(state, gate)
    blockers += _plan_blockers(plan, gate)
    blockers += _task_blockers(plan, state, gate)
    blockers += _review_blockers(review, gate)
    return blockers


# --- the attestation request ------------------------------------------------------


def _repository_id(repo: repo_mod.Repo) -> str:
    """The repository's identity in the attestation subject: its origin URL, else its path.

    Binding a signature to a repository is what stops it being lifted into a fork (E2E-14).
    The origin URL is the meaningful identity when there is one; the resolved path is the
    honest fallback for a local-only repository.
    """
    return repo._git("config", "--get", "remote.origin.url") or str(repo.root)


def _role_for(gate: str) -> str:
    return sorted(models.REQUIRED_ROLE[GATE_ATTESTATION[gate]])[0]


def request_envelope(repo: repo_mod.Repo, gate: str) -> dict[str, object]:
    """The unsigned envelope a human signs to open `gate`.

    Every digest the approval would *cover* goes in the subject, including the event chain root
    at this moment — so a signature can never be presented for a plan, a review, or a log other
    than the one the human actually read (plan §7.5, §7.6).
    """
    store = store_mod.Store(repo)
    state = store.read_state()
    plan = store.read_plan()
    review = store.read_review()
    config = store.read_config()
    events, _ = event_chain.scan(repo.events)

    subject: dict[str, object] = {
        "repository_id": _repository_id(repo),
        "cycle_id": state.cycle_id if state else "",
        "event_chain_root_before": event_chain.chain_root(events),
    }
    if plan is not None:
        subject["plan_digest"] = plan.digest()
    if config is not None:
        subject["config_digest"] = config.digest()
    if review is not None and review.is_generated:
        subject["machine_digest"] = review.machine_digest()
        subject["human_digest"] = review.human_digest()
    artifact = GATE_ARTIFACT.get(gate)
    if artifact and repo.path(artifact).exists():
        subject["artifact_digest"] = digests.of_file(repo.path(artifact))
    subject["validation_digest"] = digests.of({"gate": gate, "readiness": "clear"})

    return {
        "id": f"ATT-{gate.upper()}-{event_chain.new_id()[:8].upper()}",
        "type": GATE_ATTESTATION[gate],
        "subject": subject,
        "actor": {"principal": "unresolved@signing-key", "role": _role_for(gate)},
        "issued_at": event_chain.now_iso(),
    }


# --- recording an approval (called by `attestation import`) ---------------------


def record_approval(repo: repo_mod.Repo, gate: str, attestation: models.Attestation) -> None:
    """Write the gate receipt in one Central Store transaction. The signature is already verified.

    Deliberately not reachable from the CLI: the only route to an approved gate is a verified
    signature, and a command that opened a gate without one would *be* an alternative route.
    """
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ApprovalError("no .agentloop/state.yaml to record the approval in")

    events, defects = event_chain.scan(repo.events)
    if defects:
        raise ApprovalError("refusing to record an approval against a damaged audit chain")
    root_before = event_chain.chain_root(events)
    if not digests.matches(attestation.subject_digest("event_chain_root_before"), root_before):
        raise ApprovalError(
            "the attestation was issued against a different audit-chain root — events were appended, "
            "removed, or regenerated since it was signed. Re-run `agentloop approve` and sign again."
        )

    raw = json.loads(json.dumps(state.raw))  # plain deep copy; state.raw stays untouched
    receipt: dict[str, object] = {
        "attestation_id": attestation.id,
        "validation_digest": attestation.subject_digest("validation_digest"),
        "attested_chain_root": root_before,
        "result_chain_root": root_before,
    }
    for key in ("artifact_digest", "plan_digest", "toolchain_digest", "oracle_bundle_set_digest"):
        value = attestation.subject_digest(key)
        if value:
            receipt[key] = value

    raw["gates"][gate] = {"status": "approved", "receipt": receipt}
    raw["current_phase"] = models.PHASE_AFTER_GATE[gate]
    raw["updated_at"] = event_chain.now_iso()

    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=store.document_digest("state"))
        tx.append(
            "gate_approved",
            cycle_id=state.cycle_id,
            actor=attestation.principal,
            subject_ids=[gate, attestation.id],
            detail={"attested_chain_root": root_before},
        )


# --- CLI -------------------------------------------------------------------------


def render_blockers(gate: str, blockers: list[str]) -> str:
    body = "\n".join(f"  - {b}" for b in blockers)
    return f"gate '{gate}' is not ready ({len(blockers)} blocker(s)):\n{body}"


def render_instructions(gate: str, request_path: str) -> str:
    signed = request_path.replace(".json", ".signed.json")
    return (
        f"Attestation request for gate '{gate}' written to {request_path}\n"
        "\n"
        "Nothing is approved yet. A gate opens only on a signature from a key the external\n"
        "Trust Manifest authorizes for this role:\n"
        "\n"
        f"  agentloop attestation sign {request_path}\n"
        f"  agentloop attestation import {signed}\n"
        "\n"
        "Read what you are signing first — the subject block names every digest the approval\n"
        "will cover. If any of them moves afterwards, the approval stops applying."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check a gate's readiness and produce an attestation request (never opens a gate)"
    )
    parser.add_argument("gate", help=f"one of: {', '.join(models.GATE_ORDER)}")
    parser.add_argument("--out", default="", help="where to write the request (default: <gate>-attestation.json)")
    parser.add_argument("--check", action="store_true", help="readiness only; write no request")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
        repo.require_supported_layout()
    except (repo_mod.RepoNotFoundError, repo_mod.UnsupportedLayoutError) as exc:
        logger.error(str(exc))
        return 1

    try:
        blockers = readiness(repo, args.gate)
    except ApprovalError as exc:
        logger.error(str(exc))
        return 2
    if blockers:
        logger.error(render_blockers(args.gate, blockers))
        return 1
    if args.check:
        print(f"gate '{args.gate}' is ready for a signed attestation")
        return 0

    envelope = request_envelope(repo, args.gate)
    problems = models.schema_errors(envelope, "attestation")
    if problems:  # a request we cannot validate is a request nobody should sign
        logger.error(f"the generated request is not a valid attestation envelope: {'; '.join(problems)}")
        return 1
    out = args.out or f"{args.gate}-attestation.json"
    repo.path(out).write_text(json.dumps(envelope, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(render_instructions(args.gate, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
