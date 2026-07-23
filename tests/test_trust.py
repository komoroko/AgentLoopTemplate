"""Tests for trust.py — who may approve what, defined outside the repository (plan §30.4).

The property the whole module protects: authority is bound to a key fingerprint, never to a
name a pull request could write. So the tests are mostly about refusals — an unlisted key, a
key signing another principal's name, a role that is not held, a domain that is not vouched
for — each of which is a way a valid signature could still fail to authorize an action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import trust

FP_A = "SHA256:" + "A" * 43
FP_B = "SHA256:" + "B" * 43

MANIFEST = f"""\
project:
  repository_id: github.com/komoroko/AgentLoopTemplate
attestation:
  namespace: agentloop
  allowed_signers_file: /etc/agentloop/allowed_signers
identities:
  - principal: maintainer@example.com
    key_fingerprint: {FP_A}
    roles: [gate_reviewer, release_approver]
    domains: []
  - principal: security@example.com
    key_fingerprint: {FP_B}
    roles: [expert, security_approver]
    domains: [security, sandbox]
"""


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "trust.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def load(tmp_path: Path, body: str = MANIFEST) -> trust.TrustManifest:
    return trust.load(write(tmp_path, body))


# --- loading ------------------------------------------------------------------


def test_a_valid_manifest_loads(tmp_path: Path) -> None:
    manifest = load(tmp_path)
    assert manifest.repository_id == "github.com/komoroko/AgentLoopTemplate"
    assert manifest.namespace == "agentloop"
    assert {i.principal for i in manifest.identities} == {"maintainer@example.com", "security@example.com"}


def test_the_manifest_lives_outside_the_repository(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTLOOP_TRUST_MANIFEST", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert trust.manifest_path() == tmp_path / "cfg" / "agentloop" / "trust.yaml"


def test_the_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLOOP_TRUST_MANIFEST", "/secret/trust.yaml")
    assert trust.manifest_path() == Path("/secret/trust.yaml")


def test_a_missing_manifest_is_a_hard_failure(tmp_path: Path) -> None:
    """ "No manifest" means "no authorized principal", which means no gate can open."""
    with pytest.raises(trust.TrustError, match="no principal is authorized"):
        trust.load(tmp_path / "nope.yaml")


def test_a_manifest_with_no_identities_is_refused(tmp_path: Path) -> None:
    with pytest.raises(trust.TrustError, match="lists no identities"):
        load(tmp_path, "identities: []\n")


def test_a_bad_fingerprint_is_refused(tmp_path: Path) -> None:
    body = MANIFEST.replace(FP_A, "not-a-fingerprint")
    with pytest.raises(trust.TrustError, match="not a SHA256 key fingerprint"):
        load(tmp_path, body)


def test_a_bad_principal_is_refused(tmp_path: Path) -> None:
    body = MANIFEST.replace("maintainer@example.com", "not an email", 1)
    with pytest.raises(trust.TrustError, match="not a valid principal"):
        load(tmp_path, body)


def test_an_unknown_role_is_refused(tmp_path: Path) -> None:
    body = MANIFEST.replace("[gate_reviewer, release_approver]", "[emperor]")
    with pytest.raises(trust.TrustError, match="unknown role"):
        load(tmp_path, body)


def test_an_identity_with_no_roles_is_refused(tmp_path: Path) -> None:
    body = MANIFEST.replace("roles: [gate_reviewer, release_approver]", "roles: []")
    with pytest.raises(trust.TrustError, match="no roles"):
        load(tmp_path, body)


def test_a_duplicate_fingerprint_is_refused(tmp_path: Path) -> None:
    body = MANIFEST.replace(FP_B, FP_A)
    with pytest.raises(trust.TrustError, match="twice"):
        load(tmp_path, body)


def test_a_malformed_manifest_is_refused(tmp_path: Path) -> None:
    with pytest.raises(trust.TrustError, match="malformed"):
        load(tmp_path, "identities: [\n")


# --- authorization ------------------------------------------------------------


def test_a_listed_key_with_the_role_is_authorized(tmp_path: Path) -> None:
    manifest = load(tmp_path)
    identity = manifest.authorizes(fingerprint=FP_A, principal="maintainer@example.com", role="gate_reviewer")
    assert identity.principal == "maintainer@example.com"


def test_an_unlisted_key_carries_no_authority(tmp_path: Path) -> None:
    """A signature from an unlisted key is worthless however valid the signature itself is."""
    manifest = load(tmp_path)
    with pytest.raises(trust.TrustError, match="not in the Trust Manifest"):
        manifest.authorizes(fingerprint="SHA256:" + "Z" * 43, principal="x@example.com", role="gate_reviewer")


def test_a_key_signing_another_principal_s_name_is_refused(tmp_path: Path) -> None:
    """A real key used to sign somebody else's name."""
    manifest = load(tmp_path)
    with pytest.raises(trust.TrustError, match="another principal's name"):
        manifest.authorizes(fingerprint=FP_A, principal="attacker@example.com", role="gate_reviewer")


def test_a_role_that_is_not_held_is_refused(tmp_path: Path) -> None:
    manifest = load(tmp_path)
    with pytest.raises(trust.TrustError, match="not authorized as 'expert'"):
        manifest.authorizes(fingerprint=FP_A, principal="maintainer@example.com", role="expert")


def test_an_expert_is_scoped_to_their_domains(tmp_path: Path) -> None:
    """An expert vouched for `security` cannot attest an idempotency claim."""
    manifest = load(tmp_path)
    manifest.authorizes(fingerprint=FP_B, principal="security@example.com", role="expert", domains=("security",))
    with pytest.raises(trust.TrustError, match="not vouched for in domain"):
        manifest.authorizes(fingerprint=FP_B, principal="security@example.com", role="expert", domains=("payment",))


def test_an_unscoped_identity_covers_every_domain(tmp_path: Path) -> None:
    # An empty `domains` means all domains: a gate reviewer approves the cycle, not a domain.
    manifest = load(tmp_path)
    manifest.authorizes(
        fingerprint=FP_A, principal="maintainer@example.com", role="gate_reviewer", domains=("anything",)
    )


# --- digest -------------------------------------------------------------------


def test_the_digest_covers_the_authorized_set(tmp_path: Path) -> None:
    base = trust.digest(load(tmp_path))
    # Adding a role to an identity changes who may do what, so the digest must move — a review
    # generated under the old authority set goes stale (plan §17.5).
    widened = MANIFEST.replace(
        "roles: [gate_reviewer, release_approver]", "roles: [gate_reviewer, release_approver, expert]"
    )
    assert trust.digest(load(tmp_path, widened)) != base


def test_the_digest_ignores_where_the_manifest_lives(tmp_path: Path) -> None:
    # Where the manifest is stored is not part of what it says.
    a = trust.load(write(tmp_path, MANIFEST))
    other = tmp_path / "sub"
    other.mkdir()
    b = trust.load(write(other, MANIFEST))
    assert trust.digest(a) == trust.digest(b)


def test_identity_order_does_not_change_the_digest(tmp_path: Path) -> None:
    swapped = f"""\
project: {{repository_id: github.com/komoroko/AgentLoopTemplate}}
attestation: {{namespace: agentloop, allowed_signers_file: /etc/agentloop/allowed_signers}}
identities:
  - principal: security@example.com
    key_fingerprint: {FP_B}
    roles: [expert, security_approver]
    domains: [security, sandbox]
  - principal: maintainer@example.com
    key_fingerprint: {FP_A}
    roles: [gate_reviewer, release_approver]
    domains: []
"""
    assert trust.digest(load(tmp_path, swapped)) == trust.digest(load(tmp_path))
