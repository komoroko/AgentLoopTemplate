""".agentloop/agentloop.lock — the machine-written record of what the harness installed.

The old copy-distribution model recorded provenance in `.agentloop/adopt-manifest.yaml`; with
the harness living in an installed package, the repo needs a lock that answers three questions
without a network call: **which agentloop version last wrote this repo's artifacts** (and from
which source), **which schema versions the SSOT YAMLs follow**, and **which files the tool owns**
(the materialized prompts/schema under `.agentloop/`, the per-agent integration surfaces, and
the one-shot seeds) — each with a content hash, so `sync`/`upgrade`/`uninstall` can tell a
pristine file (safe to refresh/remove) from a locally-modified one (never touched silently).

Structure (YAML mapping, `sort_keys=False` so the file reads top-down):

  version: 1                    # lock-format version — a tool refuses formats newer than it knows
  agentloop: {version, source}  # the tool release that last wrote the lock, and where it came from
  schema: {config: 1, tasks: 1} # schema_version each SSOT YAML was written at
  prompts: {version, files:}    # the materialized artifacts (.agentloop/prompts|schema, rules)
  integrations: {claude: {...}} # present only for installed agent surfaces (install.py)
  seeded: {path: hash}          # one-shot seeds the repo owns from then on (uninstall check only)
  created_at / updated_at

Writers: `init` (creates), `sync`/`upgrade` (prompts section), `install`/`uninstall`
(integrations). The lock is always rewritten *last* in an operation, so a crash leaves
behind at worst an under-recorded lock that the next run reconverges. Readers: every CLI
invocation runs :func:`startup_warning` (cheap: parse + version compare); `doctor` digs
deeper (pristine hash comparison, schema_version match).
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from agentloop import repo as repo_mod

FORMAT_VERSION = 1
# The schema_version each SSOT YAML is written at by this tool release (see data/schema/*.json).
SCHEMA_VERSIONS = {"config": 1, "tasks": 1}
LOCK_NAME = ".agentloop/agentloop.lock"
_HEADER = (
    "# .agentloop/agentloop.lock — machine-written by `agentloop init|sync|install|uninstall|upgrade`.\n"
    "# Records the tool version/source, schema versions, and a hash per installed file so\n"
    "# upgrades never overwrite local edits. Do not edit by hand.\n"
)


class LockError(RuntimeError):
    """An unusable lock: unparseable, not a mapping, or written by a newer lock format."""


def norm_hash(blob: bytes) -> str:
    """sha256 of the CRLF-normalized bytes — a checkout's line-ending conversion is not an edit.

    The same normalization adopt-manifest used, so hashes stay comparable across platforms.
    """
    return "sha256:" + hashlib.sha256(blob.replace(b"\r\n", b"\n")).hexdigest()


def new(version: str, source: str) -> dict[str, Any]:
    """A fresh lock skeleton for `init` to fill."""
    today = date.today().isoformat()
    return {
        "version": FORMAT_VERSION,
        "agentloop": {"version": version, "source": source},
        "schema": dict(SCHEMA_VERSIONS),
        "prompts": {"version": version, "files": {}},
        "integrations": {},
        "seeded": {},
        "created_at": today,
        "updated_at": today,
    }


def read(path: Path) -> dict[str, Any] | None:
    """The lock mapping, or None when the file does not exist. LockError when unusable.

    A lock whose `version` is newer than this tool knows is refused outright — its semantics
    are unknown, and "proceed and guess" is how a newer repo gets silently corrupted by an
    older tool. The fix is upgrading the tool, and the message says so.
    """
    import yaml  # lazy: keep `import lock` stdlib-only for the hook path

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LockError(f"cannot read {path}: {exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LockError(f"{path} is not valid YAML: {exc} — machine-written; restore it from git") from None
    if not isinstance(data, dict):
        raise LockError(f"{path} is not a mapping — machine-written; restore it from git")
    fmt = data.get("version")
    if not isinstance(fmt, int) or fmt < 1:
        raise LockError(f"{path} has no usable lock-format `version`")
    if fmt > FORMAT_VERSION:
        raise LockError(
            f"{path} was written by a newer agentloop (lock format {fmt} > {FORMAT_VERSION}) — "
            "upgrade the tool: `uv tool upgrade agentloop`"
        )
    return data


def write(path: Path, data: dict[str, Any]) -> None:
    """Write the lock (stamping updated_at), with the do-not-edit header."""
    import yaml  # lazy (see read)

    data = dict(data)
    data["updated_at"] = date.today().isoformat()
    data.setdefault("created_at", data["updated_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def tool_version_of(data: dict[str, Any]) -> str:
    """The agentloop version recorded in a lock mapping ("" when unrecorded)."""
    agentloop = data.get("agentloop")
    return str(agentloop.get("version", "")) if isinstance(agentloop, dict) else ""


def startup_warning(repo: repo_mod.Repo, running_version: str) -> str | None:
    """The cheap per-invocation check: one warning line, or None when all is well.

    A missing lock is silent (pre-lock repos and mid-init states are legitimate); an unusable
    or newer-format lock raises LockError (the caller turns that into a hard error); a version
    skew between the running tool and the lock's writer gets one actionable stderr line.
    """
    data = read(repo.lock)
    if data is None:
        return None
    recorded = tool_version_of(data)
    if not recorded or recorded == running_version:
        return None
    if _version_tuple(recorded) > _version_tuple(running_version):
        return (
            f"agentloop {running_version} is older than the {recorded} that wrote {LOCK_NAME} — "
            "upgrade the tool (`uv tool upgrade agentloop`)"
        )
    return (
        f"agentloop {running_version} is newer than the {recorded} recorded in {LOCK_NAME} — "
        "run `agentloop sync` to refresh the materialized artifacts (and the lock)"
    )


def _version_tuple(version: str) -> tuple[int, ...]:
    """Best-effort numeric ordering of an x.y.z string (non-numeric parts compare as 0)."""
    parts: list[int] = []
    for chunk in version.split("+")[0].split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)
