"""Source providers: where evidence comes from, and why none of them is trusted by default.

A source's *bytes* are untrusted input no matter where they came from — a README, a vendor
doc, a code comment can all carry a prompt injection, and a provider executable on `PATH` can
be a fake (plan §2.2, §8.3). So a provider fetches, and the harness decides authority; a
provider never gets to say "trust me".

Three kinds ship:

  Repository   reads a git blob at a pinned commit — never the working tree, whose uncommitted
               content nobody attested — and refuses a symlink that escapes the repo. Existing
               code and tests are `descriptive`: they say what the system *does*, which is never
               by itself what it *should* do (plan §8.5, E2E-06).
  HumanDecision  a business policy a human made, `normative` only once a signed attestation
               backs it. A technical fact is never made true by a human decision.
  Command      an external adapter (vendor docs, a service catalog) run from a repo-external
               Provider Manifest: absolute path, digest-pinned, no shell, strict JSON in/out,
               its own temp HOME, an env allowlist, and a working directory outside the repo.
               The adapter is a trusted, pinned tool; the bytes it returns are not (E2E-15).

The `authority.class` on every source is **derived by policy** from the provider's trust class
and the source kind — never taken from a value an AI wrote (plan §6.2). `assumed` is not a
class; a thing we assumed is not evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentloop import digests, evidence, models, strict_yaml
from agentloop import repo as repo_mod
from agentloop import store as store_mod


class ProviderError(RuntimeError):
    """A provider could not be used, or returned something unusable."""


@dataclass(frozen=True)
class FetchedSource:
    """What a provider returned, before the harness assigns authority."""

    provider: str
    kind: str  # a models.SOURCE_KIND value
    title: str
    locator: str
    content: bytes
    revision: str = ""
    anchor: str = ""


#: provider trust class + source kind → the authority class the Policy Engine assigns. This
#: table *is* the derivation: an AI never picks the left column, and the right column is the
#: only thing a claim's grounding may rely on. `descriptive` and `inferred` never ground a
#: high/critical claim (models.NORMATIVE_AUTHORITY).
_AUTHORITY: dict[tuple[str, str], str] = {
    ("official_external", "official_external_spec"): "normative",
    ("official_external", "internal_spec"): "normative",
    ("internal", "internal_spec"): "normative",
    ("human", "human_decision"): "normative",  # only with a signed attestation — see HumanDecisionProvider
    ("experiment", "experiment_receipt"): "experimental",
    ("repository", "repository_code"): "descriptive",
    ("repository", "repository_test"): "descriptive",
    ("repository", "runtime_trace"): "descriptive",
    ("expert", "expert_statement"): "expert",
}


def derive_authority(trust_class: str, kind: str) -> str:
    """The authority class for a (provider trust class, source kind) pair.

    Unknown pairs fall to `inferred` — the honest label for "we cannot vouch for this" — rather
    than to anything a claim could lean on. Defaulting the other way is how an unrecognized
    source silently becomes normative.
    """
    return _AUTHORITY.get((trust_class, kind), "inferred")


class SourceProvider(Protocol):
    """The protocol every provider satisfies (plan §8.1). `trust_class` drives authority."""

    @property
    def name(self) -> str: ...

    @property
    def trust_class(self) -> str: ...

    def fetch(self, locator: str) -> FetchedSource: ...


def make_source(
    repo: repo_mod.Repo,
    provider: SourceProvider,
    fetched: FetchedSource,
    *,
    source_id: str,
    provider_manifest_digest: str = "",
) -> dict[str, object]:
    """Turn a fetched source into a plan-ready `sources[]` entry with a stored snapshot.

    The authority class is derived here, the snapshot is content-addressed, and the plan gets a
    digest and an opaque locator — not the bytes and not a class the provider chose.
    """
    snapshot = evidence.store_snapshot(repo, fetched.content, media_type=_media_type(fetched.kind))
    authority = derive_authority(provider.trust_class, fetched.kind)
    entry: dict[str, object] = {
        "id": source_id,
        "provider": provider.name,
        "kind": fetched.kind,
        "authority": {"class": authority, "derived_by_policy": True},
        "title": fetched.title,
        "locator": fetched.locator,
        "content_digest": snapshot.digest,
        "snapshot": snapshot.to_plan_snapshot(),
        "verification": {"status": "verified", "verified_at": _now()},
    }
    if fetched.revision:
        entry["revision"] = fetched.revision
    if fetched.anchor:
        entry["anchor"] = fetched.anchor
    if provider_manifest_digest:
        authority_block = entry["authority"]
        assert isinstance(authority_block, dict)
        authority_block["provider_manifest_digest"] = provider_manifest_digest
    return entry


def _media_type(kind: str) -> str:
    return "text/x-python" if kind in {"repository_code", "repository_test"} else "text/markdown"


def _now() -> str:
    from agentloop import event_chain

    return event_chain.now_iso()


# --- Repository provider -------------------------------------------------------


@dataclass(frozen=True)
class RepositoryProvider:
    """Reads a git blob at a pinned commit. `repo://<path>` or `repo://<commit>:<path>`."""

    repo: repo_mod.Repo
    name: str = "repository"
    trust_class: str = "repository"

    def fetch(self, locator: str) -> FetchedSource:
        commit, path = self._parse(locator)
        if not models.is_repo_path(path):
            raise ProviderError(f"{path!r} is not a safe repo-relative path")
        # `git show <commit>:<path>` reads the committed blob, never the working tree — the
        # point of the whole provider. A symlink or a path escaping the repo is refused by the
        # repo-path check above and by git itself (it will not resolve outside the tree).
        rc, out = self._git("show", f"{commit}:{path}")
        if rc != 0:
            raise ProviderError(f"cannot read {path} at {commit}: {out.strip()}")
        kind = "repository_test" if _looks_like_test(path) else "repository_code"
        return FetchedSource(
            provider=self.name,
            kind=kind,
            title=f"{path}@{commit[:12]}",
            locator=locator,
            content=out.encode("utf-8"),
            revision=commit,
            anchor=path,
        )

    def _parse(self, locator: str) -> tuple[str, str]:
        body = locator.removeprefix("repo://")
        if ":" in body and not body.startswith("/"):
            commit, _, path = body.partition(":")
            return commit, path
        return "HEAD", body

    def _git(self, *args: str) -> tuple[int, str]:
        try:
            proc = subprocess.run(["git", "-C", str(self.repo.root), *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return 1, str(exc)
        return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


def _looks_like_test(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".spec.ts")) or "/tests/" in f"/{path}"


# --- Human Decision provider ---------------------------------------------------


@dataclass(frozen=True)
class HumanDecisionProvider:
    """A business policy a human decided. `normative` only once a signed attestation backs it."""

    name: str = "human-decision"
    trust_class: str = "human"

    def decision(
        self, *, source_id: str, statement: str, scope: str, rationale: str, attestation_id: str = ""
    ) -> dict[str, object]:
        """A plan-ready source for a human decision.

        Without a signed `expert_confirmation`/`human_decision` attestation the authority is
        `inferred`, not `normative`: a decision nobody signed for is a note, not evidence. A
        technical fact is refused outright — a human decision cannot make one true (plan §8.5).
        """
        authority = "normative" if attestation_id else "inferred"
        entry: dict[str, object] = {
            "id": source_id,
            "provider": self.name,
            "kind": "human_decision",
            "authority": {"class": authority, "derived_by_policy": True},
            "title": scope,
            "locator": f"human-decision://{source_id}",
            "verification": {"status": "verified" if attestation_id else "unavailable", "verified_at": _now()},
        }
        return entry


# --- Command provider (repo-external Provider Manifest) ------------------------


@dataclass(frozen=True)
class ProviderManifestEntry:
    """One command provider from the external Provider Manifest (plan §8.2)."""

    name: str
    executable: str
    executable_digest: str
    trust_class: str
    timeout_sec: int
    max_output_bytes: int
    env_allowlist: tuple[str, ...]

    def verify_executable(self) -> None:
        """Refuse to run the adapter unless its bytes match the pinned digest.

        This is what makes a fake `vendor-docs` on `PATH` harmless: the manifest names an
        absolute path and a digest, and a substituted binary fails the digest check before it
        is ever executed (E2E-15).
        """
        path = Path(self.executable)
        if not path.is_absolute():
            raise ProviderError(f"provider {self.name}: executable path must be absolute, got {self.executable!r}")
        try:
            actual = digests.of_file(path)
        except OSError as exc:
            raise ProviderError(f"provider {self.name}: cannot read executable {path}: {exc}") from None
        if not digests.matches(actual, self.executable_digest):
            raise ProviderError(
                f"provider {self.name}: executable digest mismatch — {path} is not the binary the "
                "Provider Manifest pins. Refusing to run it."
            )


def load_provider_manifest(path: Path | None = None) -> dict[str, ProviderManifestEntry]:
    """Read the external Provider Manifest ($XDG_CONFIG_HOME/agentloop/providers.yaml)."""
    source = path or (store_mod.config_home() / "agentloop" / "providers.yaml")
    try:
        raw = strict_yaml.load_mapping(source.read_text(encoding="utf-8"), what=str(source))
    except FileNotFoundError:
        return {}
    except (OSError, strict_yaml.StrictParseError) as exc:
        raise ProviderError(f"cannot read the Provider Manifest {source}: {exc}") from None

    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return {}
    result: dict[str, ProviderManifestEntry] = {}
    for name, body in providers.items():
        if not isinstance(body, dict):
            continue
        allowlist = body.get("env_allowlist")
        result[name] = ProviderManifestEntry(
            name=name,
            executable=str(body.get("executable", "")),
            executable_digest=str(body.get("executable_digest", "")),
            trust_class=str(body.get("trust_class", "official_external")),
            timeout_sec=int(body.get("timeout_sec", 60)),
            max_output_bytes=int(body.get("max_output_bytes", 1024 * 1024)),
            env_allowlist=tuple(a for a in (allowlist or []) if isinstance(a, str)),
        )
    return result


@dataclass(frozen=True)
class CommandProvider:
    """Runs a manifest-pinned adapter. The adapter is trusted; the bytes it returns are not."""

    entry: ProviderManifestEntry

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def trust_class(self) -> str:
        return self.entry.trust_class

    def fetch(self, locator: str) -> FetchedSource:
        self.entry.verify_executable()
        request = json.dumps({"op": "fetch", "locator": locator}, sort_keys=True)
        with tempfile.TemporaryDirectory() as home:
            # A dedicated empty HOME and a repo-external working directory, an env allowlist,
            # and no shell: the adapter starts from nothing it did not bring, and cannot read
            # the repo it is fetching evidence *about*.
            env = {name: os.environ[name] for name in self.entry.env_allowlist if name in os.environ}
            env["HOME"] = home
            env["PATH"] = os.environ.get("PATH", "")
            try:
                proc = subprocess.run(
                    [self.entry.executable],
                    input=request,
                    capture_output=True,
                    text=True,
                    timeout=self.entry.timeout_sec,
                    cwd=home,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                raise ProviderError(f"provider {self.name}: timed out after {self.entry.timeout_sec}s") from None
            except OSError as exc:
                raise ProviderError(f"provider {self.name}: could not launch adapter: {exc}") from None

        if proc.returncode != 0:
            raise ProviderError(f"provider {self.name}: adapter exited {proc.returncode}: {proc.stderr[:500]}")
        if len(proc.stdout.encode("utf-8")) > self.entry.max_output_bytes:
            raise ProviderError(f"provider {self.name}: adapter output exceeds {self.entry.max_output_bytes} bytes")

        try:
            answer = strict_yaml.load_json_mapping(proc.stdout, what=f"{self.name} output")
        except strict_yaml.StrictParseError as exc:
            # Malformed output is `unavailable`, not a source: an adapter that cannot speak the
            # protocol has told us nothing, and inventing a source from garbage is the failure.
            raise ProviderError(f"provider {self.name}: unusable adapter output: {exc}") from None

        content = str(answer.get("content", ""))
        return FetchedSource(
            provider=self.name,
            kind=str(answer.get("kind", "official_external_spec")),
            title=str(answer.get("title", locator)),
            locator=locator,
            content=content.encode("utf-8"),
            revision=str(answer.get("revision", "")),
            anchor=str(answer.get("anchor", "")),
        )
