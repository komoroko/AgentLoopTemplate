"""Signed attestations: the request → sign → import flow that is the only way a gate opens.

A gate does not open because someone ran a command or clicked a button (plan §7.1: a TTY and a
localhost UI are for preventing fumbles, not for proving identity). It opens because a human
signed an envelope, with a key the external Trust Manifest authorizes, over the exact digests
being approved — and that signature verified.

The three steps, each a separate CLI verb so the signing key never passes through this process:

  request  `agentloop approve <gate>` (in :mod:`agentloop.approve`) writes an unsigned envelope
           whose `subject` binds every digest the approval will cover.
  sign     `agentloop attestation sign <request.json>` calls ``ssh-keygen -Y sign`` — shell-free,
           absolute path, over the envelope's canonical payload — and writes `<request>.signed.json`.
  import   `agentloop attestation import <signed.json>` verifies the signature against the
           Trust Manifest's allowed-signers file, checks the principal/role/domain, checks the
           subject still matches the repository's current digests, and only then records the
           gate receipt in a Store transaction.

What each check stops (plan §7.5): a signature from an unlisted key (Trust Manifest lookup); a
real key signing another principal's name (fingerprint↔principal match); a valid signature for
a *different* review, cycle, or repository (subject digest re-check); a signature replayed after
the artifacts moved (the digests no longer match); a signature over a stale event-chain root
(:mod:`agentloop.approve` binds `event_chain_root_before`). The signature is verified with the
external allowed-signers file, so nothing in the checkout can make a forged key look valid.

`ssh-keygen -Y verify` is invoked without a shell and its executable digest is part of the
toolchain binding, so the trusted tool that says "this signature is valid" is itself pinned.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentloop import common, digests, models, strict_yaml, trust
from agentloop import repo as repo_mod
from agentloop import store as store_mod

logger = logging.getLogger(__name__)

#: The SSH signature namespace. Must match what the signer passed to `-Y sign -n`.
NAMESPACE = "agentloop"

_SIGN_SUFFIX = ".signed.json"


class AttestationError(RuntimeError):
    """An attestation could not be signed, verified, or imported."""


# --- the canonical payload the signature covers --------------------------------


def _payload_bytes(envelope: dict[str, object]) -> bytes:
    """The exact bytes a signature is made over: the envelope minus its signature block.

    Taken over the canonical form so that reformatting the JSON file cannot invalidate a
    signature, and re-serializing it cannot forge one.
    """
    attestation = models.Attestation.from_mapping(envelope)
    return digests.canonical(attestation.payload())


def _ssh_keygen() -> str:
    path = shutil.which("ssh-keygen")
    if path is None:
        raise AttestationError("ssh-keygen is not on PATH — attestations cannot be signed or verified")
    return path


# --- signing (the `sign` verb) -------------------------------------------------


def sign_envelope(request_path: Path, *, key_path: Path | None = None) -> Path:
    """Sign the request at `request_path`, writing `<request>.signed.json`. Returns the new path.

    `ssh-keygen -Y sign` is called with an absolute executable path and no shell, over the
    canonical payload on stdin. The key stays with the user; this process only shells the
    signer, and only long enough to attach a signature.
    """
    try:
        envelope = strict_yaml.load_json_mapping(request_path.read_text(encoding="utf-8"), what=str(request_path))
    except (OSError, strict_yaml.StrictParseError) as exc:
        raise AttestationError(f"cannot read the attestation request {request_path}: {exc}") from None
    if "signature" in envelope:
        raise AttestationError(f"{request_path} already carries a signature — sign the unsigned request")

    identity_file = str(key_path) if key_path else os.environ.get("AGENTLOOP_SIGNING_KEY", "")
    if not identity_file:
        raise AttestationError(
            "no signing key — pass --key <path> or set AGENTLOOP_SIGNING_KEY to your private key "
            "(e.g. ~/.ssh/id_ed25519). The key never leaves your control; agentloop only shells ssh-keygen."
        )
    fingerprint = _key_fingerprint(identity_file)

    # The principal is resolved from the signing key, never typed. `approve` writes a
    # placeholder; the identity that actually signs is whoever the Trust Manifest binds this
    # key's fingerprint to. Signing with a key the manifest does not list is refused up front
    # rather than after a needless ssh-keygen call.
    try:
        manifest = trust.load()
    except trust.TrustError as exc:
        raise AttestationError(str(exc)) from None
    identity = manifest.identity_for_fingerprint(fingerprint)
    if identity is None:
        raise AttestationError(
            f"the signing key ({fingerprint}) is not in the Trust Manifest — signing with it would "
            "produce a signature no gate would accept"
        )
    actor = dict(envelope.get("actor") or {})
    if not identity.has_role(str(actor.get("role", ""))):
        raise AttestationError(
            f"{identity.principal} is not authorized as '{actor.get('role')}' for this attestation "
            f"(has: {', '.join(sorted(identity.roles)) or 'none'})"
        )
    actor["principal"] = identity.principal
    envelope["actor"] = actor
    payload = _payload_bytes(envelope)

    with tempfile.TemporaryDirectory() as workdir:
        message = Path(workdir) / "payload"
        message.write_bytes(payload)
        rc, out = common.run(
            [_ssh_keygen(), "-Y", "sign", "-n", NAMESPACE, "-f", identity_file, str(message)],
            timeout=60,
        )
        if rc != 0:
            raise AttestationError(f"ssh-keygen -Y sign failed (rc={rc}): {out.strip()}")
        signature_blob = (message.with_suffix(".sig")).read_text(encoding="utf-8")

    signed = dict(envelope)
    signed["signature"] = {
        "method": "ssh-signature",
        "namespace": NAMESPACE,
        "key_fingerprint": fingerprint,
        "payload_digest": digests.of_bytes(payload),
        "value": signature_blob,
    }
    problems = models.schema_errors(signed, "attestation")
    if problems:
        raise AttestationError(f"the signed envelope is not valid: {'; '.join(problems)}")

    out_path = request_path.with_name(request_path.stem + _SIGN_SUFFIX)
    out_path.write_text(json.dumps(signed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return out_path


def _key_fingerprint(identity_file: str) -> str:
    """The SHA256 fingerprint of a private (or public) key, via `ssh-keygen -l`."""
    rc, out = common.run([_ssh_keygen(), "-l", "-f", identity_file], timeout=30)
    if rc != 0:
        raise AttestationError(f"cannot read the key fingerprint of {identity_file}: {out.strip()}")
    for token in out.split():
        if token.startswith("SHA256:"):
            return token
    raise AttestationError(f"ssh-keygen -l gave no SHA256 fingerprint for {identity_file}")


# --- verification --------------------------------------------------------------


def verify_signature(envelope: dict[str, object], manifest: trust.TrustManifest) -> trust.Identity:
    """Verify the envelope's SSH signature and resolve its authorized identity.

    Uses the manifest's *external* allowed-signers file, so no key the checkout controls can
    be made to look valid. Returns the :class:`trust.Identity` the signature authorizes, or
    raises :class:`AttestationError` / :class:`trust.TrustError`.
    """
    attestation = models.Attestation.from_mapping(envelope)
    signature = attestation.signature
    if not signature:
        raise AttestationError("the envelope is unsigned")
    if signature.get("namespace") != NAMESPACE:
        raise AttestationError(f"signature namespace {signature.get('namespace')!r} is not {NAMESPACE!r}")

    payload = digests.canonical(attestation.payload())
    if not digests.matches(signature.get("payload_digest"), digests.of_bytes(payload)):
        raise AttestationError(
            "the signature's payload_digest does not match the envelope — the subject was edited after signing"
        )

    if not manifest.allowed_signers_file:
        raise AttestationError(
            "the Trust Manifest names no allowed_signers_file, so a signature cannot be verified against it"
        )
    allowed = Path(manifest.allowed_signers_file)
    if not allowed.exists():
        raise AttestationError(f"the allowed-signers file {allowed} does not exist")

    fingerprint = str(signature.get("key_fingerprint", ""))
    identity = manifest.identity_for_fingerprint(fingerprint)
    if identity is None:
        raise trust.TrustError(f"key {fingerprint} is not in the Trust Manifest")

    with tempfile.TemporaryDirectory() as workdir:
        sig_file = Path(workdir) / "payload.sig"
        sig_file.write_text(str(signature.get("value", "")), encoding="utf-8")
        # `-Y verify` reads the signed message from stdin and needs the signer's principal to
        # find them in the allowed-signers file. The principal is the manifest's, not the
        # envelope's — the envelope's is untrusted until this check passes.
        rc, out = common.run(
            [
                _ssh_keygen(),
                "-Y",
                "verify",
                "-f",
                str(allowed),
                "-I",
                identity.principal,
                "-n",
                NAMESPACE,
                "-s",
                str(sig_file),
            ],
            timeout=60,
            input_text=payload.decode("utf-8"),
        )
    if rc != 0:
        raise AttestationError(f"ssh-keygen -Y verify rejected the signature (rc={rc}): {out.strip()}")

    return manifest.authorizes(
        fingerprint=fingerprint,
        principal=attestation.principal,
        role=attestation.role,
        domains=attestation.domains,
    )


# --- subject freshness ---------------------------------------------------------


@dataclass(frozen=True)
class SubjectCheck:
    """One `subject` digest checked against what the repository currently holds."""

    name: str
    signed: str
    current: str

    @property
    def fresh(self) -> bool:
        return digests.matches(self.signed, self.current)


def subject_checks(repo: repo_mod.Repo, attestation: models.Attestation) -> list[SubjectCheck]:
    """Compare every digest the signature covers against the repository's current state.

    A signature is only meaningful for the artifacts it was made over; if the plan, the
    config, the review, or the event chain has moved since, the approval no longer applies to
    what is now on disk (plan §7.5, E2E-08).
    """
    # `event_chain_root_before` is a point-in-time checkpoint, not a current-state digest: the
    # approval itself appends an event, so it is *expected* to differ afterwards. That binding
    # is validated against the pre-approval root by `approve.record_approval`, inside the same
    # transaction; checking it here would flag every imported attestation as stale forever.
    store = store_mod.Store(repo)
    candidates: dict[str, str] = {}
    try:
        plan = store.read_plan()
        config = store.read_config()
        review = store.read_review()
    except (models.DocumentError, store_mod.StoreError, strict_yaml.StrictParseError):
        plan = config = review = None
    if plan is not None:
        candidates["plan_digest"] = plan.digest()
    if config is not None:
        candidates["config_digest"] = config.digest()
    if review is not None and review.is_generated:
        candidates["machine_digest"] = review.machine_digest()
        candidates["human_digest"] = review.human_digest()

    checks: list[SubjectCheck] = []
    for name, current in candidates.items():
        signed = attestation.subject_digest(name)
        if signed:
            checks.append(SubjectCheck(name=name, signed=signed, current=current))
    return checks


# --- import (the `import` verb) ------------------------------------------------


def import_attestation(repo: repo_mod.Repo, signed_path: Path, *, manifest: trust.TrustManifest | None = None) -> str:
    """Verify a signed envelope and, if it opens a gate, record the receipt. Returns a summary.

    This is the *only* path from a signature to an approved gate. Everything it checks is a way
    the signature could be valid and still not authorize this approval — an unlisted key, a
    mismatched principal, a missing role, a stale subject, a different repository.
    """
    try:
        envelope = strict_yaml.load_json_mapping(signed_path.read_text(encoding="utf-8"), what=str(signed_path))
    except (OSError, strict_yaml.StrictParseError) as exc:
        raise AttestationError(f"cannot read {signed_path}: {exc}") from None
    problems = models.schema_errors(envelope, "attestation")
    if problems:
        raise AttestationError(f"{signed_path} is not a valid attestation: {'; '.join(problems)}")
    attestation = models.Attestation.from_mapping(envelope)

    resolved_manifest = manifest or trust.load()
    _check_repository(repo, attestation, resolved_manifest)

    identity = verify_signature(envelope, resolved_manifest)

    stale = [c for c in subject_checks(repo, attestation) if not c.fresh]
    if stale:
        listed = ", ".join(c.name for c in stale)
        raise AttestationError(
            f"the signature covers digests that have since moved ({listed}): it approved an earlier state, "
            "not what is on disk now. Re-run `agentloop approve` against the current state and sign again."
        )

    store_attestation(repo, attestation)

    gate = attestation.gate
    if not gate:
        return f"imported {attestation.id} ({attestation.type}, signed by {identity.principal}) — opens no gate"

    from agentloop import approve

    blockers = approve.readiness(repo, gate)
    # The gate's own "already approved" blocker is expected here — this call IS the approval.
    blockers = [b for b in blockers if "already approved" not in b]
    if blockers:
        raise AttestationError(
            f"the signature is valid, but gate '{gate}' is not ready to open:\n"
            + "\n".join(f"  - {b}" for b in blockers)
        )
    approve.record_approval(repo, gate, attestation)
    return f"gate '{gate}' opened by {identity.principal} ({attestation.id})"


def _check_repository(repo: repo_mod.Repo, attestation: models.Attestation, manifest: trust.TrustManifest) -> None:
    """The signature must be for this repository and this cycle — not lifted from a fork."""
    subject_repo = attestation.subject_digest("repository_id") or str(attestation.subject.get("repository_id", ""))
    if manifest.repository_id and subject_repo and subject_repo != manifest.repository_id:
        raise AttestationError(
            f"the attestation is for repository {subject_repo!r}, but this manifest is for "
            f"{manifest.repository_id!r} — a signature cannot be lifted into another repository"
        )
    store = store_mod.Store(repo)
    state = store.read_state()
    cycle_id = str(attestation.subject.get("cycle_id", ""))
    if state is not None and cycle_id and cycle_id != state.cycle_id:
        raise AttestationError(
            f"the attestation is for cycle {cycle_id!r}, but the repository is on {state.cycle_id!r}"
        )


def store_attestation(repo: repo_mod.Repo, attestation: models.Attestation) -> Path:
    """Write the signed envelope into `.agentloop/attestations/<id>.json` (git-managed history).

    A gate receipt is worthless if the signature it names is not in the tree a reviewer can
    fetch, so the envelope is committed alongside the receipt that binds it.
    """
    repo.attestations.mkdir(parents=True, exist_ok=True)
    path = repo.attestations / f"{attestation.id}.json"
    path.write_bytes(digests.canonical(attestation.to_mapping()) + b"\n")
    return path


# --- verify / list (read-only) -------------------------------------------------


def verify_stored(repo: repo_mod.Repo, attestation_id: str, *, manifest: trust.TrustManifest | None = None) -> str:
    """Re-verify a stored attestation against the manifest and the current repository state."""
    path = repo.attestations / f"{attestation_id}.json"
    try:
        envelope = strict_yaml.load_json_mapping(path.read_text(encoding="utf-8"), what=str(path))
    except (OSError, strict_yaml.StrictParseError) as exc:
        raise AttestationError(f"cannot read {path}: {exc}") from None
    attestation = models.Attestation.from_mapping(envelope)
    resolved = manifest or trust.load()
    identity = verify_signature(envelope, resolved)
    stale = [c.name for c in subject_checks(repo, attestation) if not c.fresh]
    freshness = "current" if not stale else f"STALE ({', '.join(stale)})"
    return f"{attestation_id}: signature valid ({identity.principal}, {attestation.role}); subject {freshness}"


def list_attestations(repo: repo_mod.Repo) -> list[str]:
    """Every stored attestation id, sorted."""
    if not repo.attestations.exists():
        return []
    return sorted(p.stem for p in repo.attestations.glob("*.json"))


# --- CLI -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentloop attestation", description="sign, import, and verify attestations")
    sub = parser.add_subparsers(dest="command", required=True)

    sign = sub.add_parser("sign", help="sign an attestation request with ssh-keygen")
    sign.add_argument("request", help="the request JSON written by `agentloop approve`")
    sign.add_argument("--key", default=None, help="private key path (default: $AGENTLOOP_SIGNING_KEY)")

    imp = sub.add_parser("import", help="verify a signed attestation and, if it opens a gate, record it")
    imp.add_argument("signed", help="the signed attestation JSON")

    ver = sub.add_parser("verify", help="re-verify a stored attestation")
    ver.add_argument("id", help="the attestation id (e.g. ATT-BUILD-001)")

    sub.add_parser("list", help="list stored attestations")

    for name in ("import", "verify", "list", "sign"):
        sub.choices[name].add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")

    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
        repo.require_supported_layout()
    except (repo_mod.RepoNotFoundError, repo_mod.UnsupportedLayoutError) as exc:
        logger.error(str(exc))
        return 1

    try:
        if args.command == "sign":
            out = sign_envelope(repo.path(args.request), key_path=Path(args.key) if args.key else None)
            print(f"signed → {out}\nImport it to open the gate:\n  agentloop attestation import {out}")
            return 0
        if args.command == "import":
            print(import_attestation(repo, repo.path(args.signed)))
            return 0
        if args.command == "verify":
            print(verify_stored(repo, args.id))
            return 0
        if args.command == "list":
            found = list_attestations(repo)
            print("\n".join(found) if found else "(no attestations)")
            return 0
    except (AttestationError, trust.TrustError, models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
