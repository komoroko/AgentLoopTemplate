"""Tests for providers.py — where evidence comes from, and why none of it is trusted (plan §30.5).

The theme is that a provider fetches and the harness decides authority. The bytes are always
untrusted; the authority class is always derived, never taken from a value someone wrote; and
a command provider's executable is pinned so a fake on PATH is harmless.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from agentloop import digests, models, providers
from agentloop import repo as repo_mod
from tests._support import seed_repo

# --- authority derivation -----------------------------------------------------


def test_a_repository_source_is_descriptive_never_normative() -> None:
    """Existing code says what the system does, never what it should do (E2E-06)."""
    assert providers.derive_authority("repository", "repository_code") == "descriptive"
    assert providers.derive_authority("repository", "repository_test") == "descriptive"
    assert "descriptive" not in models.NORMATIVE_AUTHORITY


def test_an_official_external_spec_is_normative() -> None:
    assert providers.derive_authority("official_external", "official_external_spec") == "normative"


def test_an_unrecognized_pair_falls_to_inferred_not_normative() -> None:
    """Defaulting an unknown source to normative is how it silently becomes something a claim
    can lean on. `inferred` is the honest 'we cannot vouch for this'."""
    assert providers.derive_authority("mystery", "official_external_spec") == "inferred"
    assert "inferred" not in models.NORMATIVE_AUTHORITY


# --- the repository provider --------------------------------------------------


def git_repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, git=True)
    for name, value in (("user.email", "t@e.x"), ("user.name", "T")):
        subprocess.run(["git", "-C", str(tmp_path), "config", name, value], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "client.py").write_text("def charge():\n    ...\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_client.py").write_text("def test_charge():\n    ...\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True, capture_output=True)
    return repo_mod.Repo(tmp_path)


@pytest.mark.integration
def test_the_repository_provider_reads_a_committed_blob(tmp_path: Path) -> None:
    provider = providers.RepositoryProvider(git_repo(tmp_path))
    fetched = provider.fetch("repo://src/client.py")
    assert b"def charge" in fetched.content
    assert fetched.kind == "repository_code"


@pytest.mark.integration
def test_the_repository_provider_reads_the_commit_not_the_working_tree(tmp_path: Path) -> None:
    """The point of the whole provider: uncommitted content nobody attested is not evidence."""
    repo = git_repo(tmp_path)
    (repo.root / "src" / "client.py").write_text("uncommitted rewrite\n", encoding="utf-8")
    fetched = providers.RepositoryProvider(repo).fetch("repo://src/client.py")
    assert b"uncommitted" not in fetched.content
    assert b"def charge" in fetched.content


@pytest.mark.integration
def test_a_test_file_is_recognized_as_a_test_source(tmp_path: Path) -> None:
    fetched = providers.RepositoryProvider(git_repo(tmp_path)).fetch("repo://tests/test_client.py")
    assert fetched.kind == "repository_test"


def test_a_path_escaping_the_repo_is_refused(tmp_path: Path) -> None:
    seed_repo(tmp_path, git=True)
    with pytest.raises(providers.ProviderError, match="not a safe repo-relative path"):
        providers.RepositoryProvider(repo_mod.Repo(tmp_path)).fetch("repo://../../etc/passwd")


@pytest.mark.integration
def test_make_source_derives_authority_and_stores_a_snapshot(tmp_path: Path) -> None:
    repo = git_repo(tmp_path)
    provider = providers.RepositoryProvider(repo)
    entry = providers.make_source(repo, provider, provider.fetch("repo://src/client.py"), source_id="SRC-001")

    assert entry["authority"] == {"class": "descriptive", "derived_by_policy": True}
    assert digests.is_digest(entry["content_digest"])
    assert entry["locator"] == "repo://src/client.py"
    # The plan carries a digest and an opaque locator, not the bytes.
    snapshot = entry["snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["cache_locator"].startswith("evidence://sha256/")
    assert models.schema_errors({**_min_plan(entry), "sources": [entry]}, "plan") == []


def _min_plan(source_entry: dict[str, object]) -> dict[str, object]:
    from tests._support import make_claim, make_plan

    return make_plan(claims=[make_claim("C-001", source_ids=["SRC-001"])])


# --- the human decision provider ----------------------------------------------


def test_a_human_decision_without_an_attestation_is_inferred_not_normative() -> None:
    """A decision nobody signed for is a note, not evidence."""
    entry = providers.HumanDecisionProvider().decision(
        source_id="SRC-010", statement="do not auto-retry", scope="payment retries", rationale="risk"
    )
    assert entry["authority"] == {"class": "inferred", "derived_by_policy": True}
    assert entry["verification"]["status"] == "unavailable"  # type: ignore[index]


def test_a_signed_human_decision_is_normative() -> None:
    entry = providers.HumanDecisionProvider().decision(
        source_id="SRC-010",
        statement="do not auto-retry",
        scope="payment retries",
        rationale="risk",
        attestation_id="ATT-DECISION-001",
    )
    assert entry["authority"]["class"] == "normative"  # type: ignore[index]


# --- the command provider -----------------------------------------------------


def _adapter(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "adapter"
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _entry(path: Path, *, digest: str | None = None) -> providers.ProviderManifestEntry:
    return providers.ProviderManifestEntry(
        name="vendor-docs",
        executable=str(path),
        executable_digest=digest if digest is not None else digests.of_file(path),
        trust_class="official_external",
        timeout_sec=30,
        max_output_bytes=1_000_000,
        env_allowlist=("VENDOR_TOKEN",),
    )


_ECHO_ADAPTER = (
    "import json, sys\n"
    "json.load(sys.stdin)\n"
    "out = {'kind': 'official_external_spec', 'title': 'Idempotency', 'content': 'retry with one key'}\n"
    "print(json.dumps(out))\n"
)


@pytest.mark.integration
def test_a_command_provider_runs_a_pinned_adapter(tmp_path: Path) -> None:
    fetched = providers.CommandProvider(_entry(_adapter(tmp_path, _ECHO_ADAPTER))).fetch("vendor-doc://idempotency")
    assert fetched.kind == "official_external_spec"
    assert b"retry with one key" in fetched.content


@pytest.mark.integration
def test_a_fake_adapter_on_path_is_refused_by_the_digest(tmp_path: Path) -> None:
    """A substituted binary fails the digest check before it is ever executed (E2E-15)."""
    adapter = _adapter(tmp_path, "print('{}')\n")
    entry = _entry(adapter, digest="sha256:" + "0" * 64)  # the manifest pins different bytes
    with pytest.raises(providers.ProviderError, match="digest mismatch"):
        providers.CommandProvider(entry).fetch("vendor-doc://x")


def test_a_relative_executable_path_is_refused(tmp_path: Path) -> None:
    entry = providers.ProviderManifestEntry(
        name="p",
        executable="./adapter",
        executable_digest="sha256:" + "0" * 64,
        trust_class="official_external",
        timeout_sec=1,
        max_output_bytes=1,
        env_allowlist=(),
    )
    with pytest.raises(providers.ProviderError, match="must be absolute"):
        entry.verify_executable()


@pytest.mark.integration
def test_malformed_adapter_output_is_a_provider_error_not_a_source(tmp_path: Path) -> None:
    """An adapter that cannot speak the protocol has told us nothing; inventing a source from
    garbage is the failure."""
    adapter = _adapter(tmp_path, "print('not json at all')\n")
    with pytest.raises(providers.ProviderError, match="unusable adapter output"):
        providers.CommandProvider(_entry(adapter)).fetch("vendor-doc://x")


@pytest.mark.integration
def test_the_adapter_runs_with_only_the_allowlisted_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VENDOR_TOKEN", "let-me-in")
    monkeypatch.setenv("SECRET_UNRELATED", "should-not-leak")
    # The adapter reports both an allowlisted var and an un-allowlisted one; only the first
    # should reach it.
    body = (
        "import json, os, sys\n"
        "json.load(sys.stdin)\n"
        "tok = os.environ.get('VENDOR_TOKEN')\n"
        "leak = os.environ.get('SECRET_UNRELATED')\n"
        "print(json.dumps({'kind': 'official_external_spec', 'title': 't', "
        "'content': f'tok={tok} leak={leak}'}))\n"
    )
    fetched = providers.CommandProvider(_entry(_adapter(tmp_path, body))).fetch("vendor-doc://x")
    assert b"tok=let-me-in" in fetched.content
    assert b"leak=None" in fetched.content  # the un-allowlisted var did not reach the adapter


def test_load_provider_manifest_is_empty_when_absent(tmp_path: Path) -> None:
    assert providers.load_provider_manifest(tmp_path / "nope.yaml") == {}


def test_load_provider_manifest_parses_entries(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text(
        "providers:\n"
        "  vendor-docs:\n"
        "    executable: /opt/agentloop/bin/vendor-docs\n"
        "    executable_digest: sha256:" + "a" * 64 + "\n"
        "    trust_class: official_external\n"
        "    timeout_sec: 60\n"
        "    env_allowlist: [VENDOR_DOCS_PROFILE]\n",
        encoding="utf-8",
    )
    manifest = providers.load_provider_manifest(path)
    assert "vendor-docs" in manifest
    assert manifest["vendor-docs"].executable == "/opt/agentloop/bin/vendor-docs"
    assert manifest["vendor-docs"].env_allowlist == ("VENDOR_DOCS_PROFILE",)
