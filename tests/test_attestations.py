"""Tests for attestations.py — the request → sign → import flow that opens a gate (plan §30.4).

The centerpiece is the end-to-end integration test: a real ssh-keygen key, a Trust Manifest
that lists it, and a gate that opens only because a valid signature over the right digests was
imported. Everything else is a refusal — an unlisted key, a stale subject, a wrong repository,
a forged principal — because each is a way a signature could be valid and still not authorize
this approval.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentloop import approve, attestations, models, trust
from agentloop import repo as repo_mod
from agentloop import store as store_mod
from tests._support import make_claim, make_plan, make_source, make_state, seed_repo

HAS_SSH = shutil.which("ssh-keygen") is not None
requires_ssh = pytest.mark.skipif(not HAS_SSH, reason="needs ssh-keygen")

PENDING = dict.fromkeys(models.GATE_ORDER, "pending")


# --- a fixture repo whose requirements gate is ready to approve ----------------


def ready_repo(tmp_path: Path, *, repository_id: str = "") -> repo_mod.Repo:
    seed_repo(
        tmp_path,
        state=make_state(gates=PENDING, phase="requirements", plan_status="draft", cycle_id="cycle-1"),
        plan=make_plan(
            cycle_id="cycle-1",
            claims=[make_claim("C-001", requirement_ids=["R-1"], source_ids=["SRC-001"])],
            sources=[make_source("SRC-001")],
        ),
    )
    repo = repo_mod.Repo(tmp_path)
    if repository_id:
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", repository_id], check=True)
    return repo


# --- key + manifest helpers (real ssh-keygen) ---------------------------------


def make_key(tmp_path: Path, principal: str = "maintainer@example.com") -> tuple[Path, str]:
    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", principal, "-q"], check=True)
    out = subprocess.run(["ssh-keygen", "-l", "-f", str(key)], capture_output=True, text=True, check=True).stdout
    fingerprint = next(tok for tok in out.split() if tok.startswith("SHA256:"))
    return key, fingerprint


def write_manifest(
    tmp_path: Path,
    fingerprint: str,
    key: Path,
    *,
    principal: str = "maintainer@example.com",
    roles: str = "[gate_reviewer, release_approver]",
    repository_id: str = "github.com/komoroko/AgentLoopTemplate",
) -> Path:
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"{principal} {key.with_suffix('.pub').read_text().strip()}\n", encoding="utf-8")
    manifest = tmp_path / "trust.yaml"
    manifest.write_text(
        f"project: {{repository_id: {repository_id}}}\n"
        f"attestation: {{namespace: agentloop, allowed_signers_file: {allowed}}}\n"
        "identities:\n"
        f"  - principal: {principal}\n"
        f"    key_fingerprint: {fingerprint}\n"
        f"    roles: {roles}\n"
        "    domains: []\n",
        encoding="utf-8",
    )
    return manifest


# --- the end-to-end gate opening ----------------------------------------------


@requires_ssh
@pytest.mark.integration
def test_a_signature_opens_the_gate_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = ready_repo(tmp_path, repository_id="github.com/komoroko/AgentLoopTemplate")
    key, fingerprint = make_key(tmp_path)
    manifest_path = write_manifest(tmp_path, fingerprint, key)
    monkeypatch.setenv("AGENTLOOP_TRUST_MANIFEST", str(manifest_path))
    monkeypatch.setenv("AGENTLOOP_SIGNING_KEY", str(key))

    request = tmp_path / "req.json"
    request.write_text(json.dumps(approve.request_envelope(repo, "requirements")) + "\n", encoding="utf-8")

    signed = attestations.sign_envelope(request)
    summary = attestations.import_attestation(repo, signed)
    assert "gate 'requirements' opened by maintainer@example.com" in summary

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.gate_status("requirements") == "approved"
    assert state.current_phase == "design"
    # The signed envelope is committed alongside the receipt that names it.
    assert attestations.list_attestations(repo)
    assert "signature valid" in attestations.verify_stored(repo, attestations.list_attestations(repo)[0])


@requires_ssh
@pytest.mark.integration
def test_the_signer_principal_is_resolved_from_the_key_not_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`approve` writes a placeholder principal; the identity that signs is whoever the manifest
    binds this key to. A key signing another principal's name never gets that far."""
    repo = ready_repo(tmp_path)
    key, fingerprint = make_key(tmp_path)
    monkeypatch.setenv("AGENTLOOP_TRUST_MANIFEST", str(write_manifest(tmp_path, fingerprint, key)))
    monkeypatch.setenv("AGENTLOOP_SIGNING_KEY", str(key))

    request = tmp_path / "req.json"
    request.write_text(json.dumps(approve.request_envelope(repo, "requirements")), encoding="utf-8")
    signed = attestations.sign_envelope(request)
    envelope = json.loads(signed.read_text())
    assert envelope["actor"]["principal"] == "maintainer@example.com"


@requires_ssh
@pytest.mark.integration
def test_a_key_not_in_the_manifest_cannot_sign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = ready_repo(tmp_path)
    key, _ = make_key(tmp_path)
    (tmp_path / "b").mkdir()
    other_key, other_fp = make_key(tmp_path / "b")
    # The manifest lists `other_key`, but we sign with `key`.
    monkeypatch.setenv("AGENTLOOP_TRUST_MANIFEST", str(write_manifest(tmp_path, other_fp, other_key)))
    monkeypatch.setenv("AGENTLOOP_SIGNING_KEY", str(key))

    request = tmp_path / "req.json"
    request.write_text(json.dumps(approve.request_envelope(repo, "requirements")), encoding="utf-8")
    with pytest.raises(attestations.AttestationError, match="not in the Trust Manifest"):
        attestations.sign_envelope(request)


@requires_ssh
@pytest.mark.integration
def test_a_stale_subject_is_refused_at_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan moved after signing: the signature approved an earlier state (E2E-08)."""
    repo = ready_repo(tmp_path, repository_id="github.com/komoroko/AgentLoopTemplate")
    key, fingerprint = make_key(tmp_path)
    monkeypatch.setenv("AGENTLOOP_TRUST_MANIFEST", str(write_manifest(tmp_path, fingerprint, key)))
    monkeypatch.setenv("AGENTLOOP_SIGNING_KEY", str(key))

    request = tmp_path / "req.json"
    request.write_text(json.dumps(approve.request_envelope(repo, "requirements")), encoding="utf-8")
    signed = attestations.sign_envelope(request)

    # Edit the plan after signing.
    plan = make_plan(
        cycle_id="cycle-1",
        claims=[make_claim("C-001", requirement_ids=["R-1"], source_ids=["SRC-001"], risk="high")],
        sources=[make_source("SRC-001")],
    )
    store_mod.atomic_write(repo.plan, store_mod.dump_yaml(plan), mode=0o644)

    with pytest.raises(attestations.AttestationError, match="have since moved"):
        attestations.import_attestation(repo, signed)


@requires_ssh
@pytest.mark.integration
def test_an_attestation_for_another_repository_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A signature cannot be lifted into a fork (E2E-14)."""
    repo = ready_repo(tmp_path, repository_id="github.com/komoroko/AgentLoopTemplate")
    key, fingerprint = make_key(tmp_path)
    # The manifest is for a *different* repository than the envelope's origin.
    manifest = write_manifest(tmp_path, fingerprint, key, repository_id="github.com/attacker/fork")
    monkeypatch.setenv("AGENTLOOP_TRUST_MANIFEST", str(manifest))
    monkeypatch.setenv("AGENTLOOP_SIGNING_KEY", str(key))

    request = tmp_path / "req.json"
    request.write_text(json.dumps(approve.request_envelope(repo, "requirements")), encoding="utf-8")
    signed = attestations.sign_envelope(request)
    with pytest.raises(attestations.AttestationError, match="another repository"):
        attestations.import_attestation(repo, signed)


# --- refusals that do not need a real signature -------------------------------


def test_an_unsigned_envelope_is_refused(tmp_path: Path) -> None:
    manifest = trust.TrustManifest("repo", "agentloop", "/dev/null", (), tmp_path / "m.yaml")
    envelope: dict[str, object] = {"id": "ATT-X-1", "type": "human_review_approval"}
    with pytest.raises(attestations.AttestationError, match="unsigned"):
        attestations.verify_signature(envelope, manifest)


def test_a_tampered_payload_digest_is_caught(tmp_path: Path) -> None:
    identity = trust.Identity("m@example.com", "SHA256:" + "A" * 43, frozenset({"gate_reviewer"}), frozenset())
    manifest = trust.TrustManifest("repo", "agentloop", str(tmp_path / "signers"), (identity,), tmp_path / "m.yaml")
    (tmp_path / "signers").write_text("m@example.com ssh-ed25519 AAAA\n", encoding="utf-8")
    envelope: dict[str, object] = {
        "id": "ATT-BUILD-1",
        "type": "human_review_approval",
        "subject": {"repository_id": "r", "cycle_id": "c1"},
        "actor": {"principal": "m@example.com", "role": "gate_reviewer"},
        "issued_at": "2026-07-24T10:00:00+09:00",
        "signature": {
            "method": "ssh-signature",
            "namespace": "agentloop",
            "key_fingerprint": "SHA256:" + "A" * 43,
            "payload_digest": "sha256:" + "0" * 64,  # does not match the real payload
            "value": "-----BEGIN SSH SIGNATURE-----\nx\n-----END SSH SIGNATURE-----\n",
        },
    }
    with pytest.raises(attestations.AttestationError, match="payload_digest does not match"):
        attestations.verify_signature(envelope, manifest)


def test_store_attestation_writes_canonical_bytes(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    repo = repo_mod.Repo(tmp_path)
    att = models.Attestation(
        id="ATT-BUILD-001",
        type="human_review_approval",
        subject={"repository_id": "r", "cycle_id": "c1"},
        actor={"principal": "m@example.com", "role": "gate_reviewer"},
        issued_at="2026-07-24T10:00:00+09:00",
        signature={
            "method": "ssh-signature",
            "namespace": "agentloop",
            "key_fingerprint": "SHA256:" + "A" * 43,
            "payload_digest": "sha256:" + "0" * 64,
            "value": "x",
        },
    )
    path = attestations.store_attestation(repo, att)
    assert path.exists()
    assert attestations.list_attestations(repo) == ["ATT-BUILD-001"]


def test_verify_stored_reports_a_missing_attestation(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    with pytest.raises(attestations.AttestationError, match="cannot read"):
        attestations.verify_stored(repo_mod.Repo(tmp_path), "ATT-NOPE-1", manifest=None)


def test_the_namespace_is_pinned() -> None:
    assert attestations.NAMESPACE == "agentloop"


def test_the_cli_exposes_only_the_four_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        attestations.main(["--help"])
    helptext = capsys.readouterr().out
    for verb in ("sign", "import", "verify", "list"):
        assert verb in helptext
