"""The Trust Manifest: who may approve what, kept deliberately outside the repository.

Every other identity in the system is untrusted input (plan §2.2): the CODEOWNERS in the PR
head, a `git config user.name`, the OS user, a `principal:` field an AI wrote into an
attestation. A pull request that could add its own approver is a pull request that approves
itself, so the one place authority is *defined* has to be somewhere the PR cannot reach:

    $XDG_CONFIG_HOME/agentloop/trust.yaml     (or $AGENTLOOP_TRUST_MANIFEST)

In CI it is a secret or a protected volume; nothing in the checkout points at it. This module
reads it and answers one question — *is this principal authorized for this action, in this
domain?* — and answers it from key fingerprints, never from a name. A name is a claim; a
signature over the right payload with a key the manifest lists is proof, and only the second
opens a gate (the signature check itself lives in :mod:`agentloop.attestations`).

The manifest is read strictly (:mod:`agentloop.strict_yaml`) and its absence is a hard
failure, never a default: "no manifest" means "no authorized principal", which means no gate
can open — reporting that as fine would describe a repository in which nothing can ever be
approved as healthy (`doctor` says FAIL, loudly).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from agentloop import models, strict_yaml
from agentloop import store as store_mod

MANIFEST_ENV = "AGENTLOOP_TRUST_MANIFEST"

_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/=]{20,}$")
_PRINCIPAL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


class TrustError(RuntimeError):
    """The Trust Manifest is missing, unreadable, malformed, or does not authorize an action."""


@dataclass(frozen=True)
class Identity:
    """One authorized signer: a principal, the key that proves them, and what they may do."""

    principal: str
    key_fingerprint: str
    roles: frozenset[str]
    domains: frozenset[str]

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def covers_domain(self, domain: str) -> bool:
        """True when this identity may act in `domain`. An empty `domains` means all domains.

        The default is deliberately permissive for roles that are not domain-scoped (a gate
        reviewer approves the cycle, not a domain), and deliberately explicit for experts: an
        expert with `domains: [payment]` cannot attest an idempotency claim they were never
        vouched for.
        """
        return not self.domains or domain in self.domains


@dataclass(frozen=True)
class TrustManifest:
    """The parsed manifest: the repository it is for, the signing config, and the identities."""

    repository_id: str
    namespace: str
    allowed_signers_file: str
    identities: tuple[Identity, ...]
    source: Path

    def identity_for_fingerprint(self, fingerprint: str) -> Identity | None:
        """The identity whose key fingerprint matches, or None. The binding lookup — by key."""
        for identity in self.identities:
            if identity.key_fingerprint == fingerprint:
                return identity
        return None

    def authorizes(self, *, fingerprint: str, principal: str, role: str, domains: tuple[str, ...] = ()) -> Identity:
        """The identity authorized for this action, or raise :class:`TrustError` saying why.

        The checks, in order: the key is listed at all; the principal the envelope claims is
        the one this key belongs to (a mismatch means a real key was used to sign somebody
        else's name); the identity holds the role; and, for a domain-scoped action, it covers
        every domain. Each failure names itself, because "unauthorized" with no reason is how
        a misconfigured manifest becomes an afternoon.
        """
        identity = self.identity_for_fingerprint(fingerprint)
        if identity is None:
            raise TrustError(
                f"key {fingerprint} is not in the Trust Manifest — a signature from an unlisted key "
                "carries no authority, however valid the signature itself is"
            )
        if identity.principal != principal:
            raise TrustError(
                f"key {fingerprint} belongs to {identity.principal!r}, but the envelope claims "
                f"{principal!r} — a real key was used to sign another principal's name"
            )
        if not identity.has_role(role):
            raise TrustError(
                f"{principal} is not authorized as '{role}' (has: {', '.join(sorted(identity.roles)) or 'none'})"
            )
        uncovered = [d for d in domains if not identity.covers_domain(d)]
        if uncovered:
            raise TrustError(
                f"{principal} is not vouched for in domain(s) {', '.join(uncovered)} "
                f"(covers: {', '.join(sorted(identity.domains)) or 'all'})"
            )
        return identity


# --- loading -------------------------------------------------------------------


def manifest_path() -> Path:
    """Where the Trust Manifest is expected. Outside the repository, always."""
    override = os.environ.get(MANIFEST_ENV, "").strip()
    if override:
        return Path(override)
    return store_mod.config_home() / "agentloop" / "trust.yaml"


def load(path: Path | None = None) -> TrustManifest:
    """Read and validate the manifest. Raises :class:`TrustError` for every failure mode."""
    source = path or manifest_path()
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise TrustError(
            f"no Trust Manifest at {source} — no principal is authorized, so no gate can open. "
            f"Create it (or point {MANIFEST_ENV} at it); it must stay OUTSIDE the repository so a "
            "pull request cannot add its own approvers."
        ) from None
    except OSError as exc:
        raise TrustError(f"cannot read the Trust Manifest {source}: {exc}") from None

    try:
        raw = strict_yaml.load_mapping(text, what=str(source))
    except strict_yaml.StrictParseError as exc:
        raise TrustError(f"the Trust Manifest is malformed: {exc}") from None

    project = raw.get("project")
    repository_id = str(project.get("repository_id", "")) if isinstance(project, dict) else ""

    attestation = raw.get("attestation")
    attestation = attestation if isinstance(attestation, dict) else {}
    namespace = str(attestation.get("namespace", "agentloop"))
    allowed_signers = str(attestation.get("allowed_signers_file", ""))

    raw_identities = raw.get("identities")
    if not isinstance(raw_identities, list) or not raw_identities:
        raise TrustError(f"the Trust Manifest {source} lists no identities — no principal can approve anything")

    identities: list[Identity] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_identities):
        identities.append(_parse_identity(entry, index, source))
        if identities[-1].key_fingerprint in seen:
            raise TrustError(f"the Trust Manifest lists key {identities[-1].key_fingerprint} twice")
        seen.add(identities[-1].key_fingerprint)

    return TrustManifest(
        repository_id=repository_id,
        namespace=namespace,
        allowed_signers_file=allowed_signers,
        identities=tuple(identities),
        source=source,
    )


def _parse_identity(entry: object, index: int, source: Path) -> Identity:
    if not isinstance(entry, dict):
        raise TrustError(f"identities[{index}] in {source} is not a mapping")
    principal = str(entry.get("principal", ""))
    if not _PRINCIPAL_RE.match(principal):
        raise TrustError(f"identities[{index}]: {principal!r} is not a valid principal (expected an email address)")
    fingerprint = str(entry.get("key_fingerprint", ""))
    if not _FINGERPRINT_RE.match(fingerprint):
        raise TrustError(f"identities[{index}] ({principal}): {fingerprint!r} is not a SHA256 key fingerprint")

    roles = _string_set(entry.get("roles"), f"identities[{index}].roles")
    unknown_roles = roles - models.ROLE_VALUES
    if unknown_roles:
        raise TrustError(
            f"identities[{index}] ({principal}): unknown role(s) {', '.join(sorted(unknown_roles))} "
            f"(one of {', '.join(sorted(models.ROLE_VALUES))})"
        )
    if not roles:
        raise TrustError(f"identities[{index}] ({principal}): no roles — an identity that may do nothing is noise")
    domains = _string_set(entry.get("domains"), f"identities[{index}].domains")
    return Identity(principal=principal, key_fingerprint=fingerprint, roles=roles, domains=domains)


def _string_set(value: object, what: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TrustError(f"{what} must be a list of strings")
    return frozenset(value)


def digest(manifest: TrustManifest) -> str:
    """A canonical digest of the authority the manifest grants — for the toolchain binding.

    Covers who may do what, so a change to the authorized set moves the toolchain digest and
    (plan §17.5) makes a review generated under the old set stale. Deliberately does *not*
    cover the source path or file mtime: where the manifest lives is not part of what it says.
    """
    from agentloop import digests as digests_mod

    payload = {
        "repository_id": manifest.repository_id,
        "namespace": manifest.namespace,
        "identities": [
            {
                "principal": i.principal,
                "key_fingerprint": i.key_fingerprint,
                "roles": sorted(i.roles),
                "domains": sorted(i.domains),
            }
            for i in sorted(manifest.identities, key=lambda i: i.key_fingerprint)
        ],
    }
    return digests_mod.of(payload)
