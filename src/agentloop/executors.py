"""Executors: where repository-derived code is allowed to run, and how it is boxed in.

The rule (plan §10.1): **anything that runs repository code, tests, or build scripts runs in an
OCI sandbox, regardless of risk.** A test file is code an agent wrote, and running it on the
host runs it with the host's credentials, its SSH agent, its cloud tokens, its docker socket.
`host` execution exists only for trusted, pinned tooling (a signature verification) — never for
anything the repository produced.

The sandbox is built from a config `executor_profile` and hardened the same way every time:

  network       deny by default (`--network none`); a profile that needs egress names a
                network profile, and only an experiment with a signed receipt gets one.
  filesystem    read-only root (`--read-only`), a size-capped writable tmpfs, the repo or
                worktree mounted read-only (a reviewer) or read-write (an implementer), and
                **nothing else** — no HOME, no ~/.ssh, no ~/.aws, no /var/run/docker.sock.
  privileges    `--security-opt no-new-privileges`, a non-root user, a pids limit, memory and
                cpu caps, so a runaway or a fork bomb cannot take the host with it.
  environment   an allowlist, passed explicitly; the container starts from nothing it did not
                bring.

The image is **digest-pinned**, always. A mutable tag would let the environment a review ran
in change after that review was signed (plan §10.2), so the profile carries
`image: <ref>@sha256:...` and this module refuses to run an un-pinned OCI profile.

Images are built locally from the Containerfiles the package ships (`data/oci/<profile>/`) via
:func:`build_image`, which prints the digest to pin. Nothing here reaches a registry: the
sandbox a review runs in is reproducible from the repository, not fetched.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from agentloop import common, data, digests, models

# Prefer docker; podman is a drop-in for the flags used here.
_RUNTIMES = ("docker", "podman")


class ExecutorError(RuntimeError):
    """A sandbox could not be prepared or run."""


@dataclass(frozen=True)
class ExecutionSpec:
    """One command to run in a sandbox, with the mounts and limits it is allowed."""

    command: tuple[str, ...]
    profile: models.ExecutorProfile
    mounts: tuple[tuple[Path, str, str], ...] = ()  # (host path, container path, "ro"|"rw")
    env: dict[str, str] = field(default_factory=dict)
    workdir: str = "/work"
    timeout_sec: float | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """What a run produced. `image_digest` records which sandbox it actually ran in."""

    exit_code: int
    output: str
    image_digest: str
    timed_out: bool = False


def container_runtime() -> str | None:
    """The first available container runtime, or None."""
    for runtime in _RUNTIMES:
        if shutil.which(runtime):
            return runtime
    return None


class Executor:
    """Runs an :class:`ExecutionSpec`. Two kinds — `oci` and `host` — chosen by the profile."""

    def run(self, spec: ExecutionSpec) -> ExecutionResult:  # pragma: no cover - dispatch only
        raise NotImplementedError


@dataclass(frozen=True)
class HostExecutor(Executor):
    """Runs on the host, for trusted pinned tooling only.

    Refuses any command that a caller might mistake for "run the repo's code here": the guard
    is not a substitute for reading the call site, but it turns the most likely mistake into an
    error instead of a host-level code execution.
    """

    def run(self, spec: ExecutionSpec) -> ExecutionResult:
        if spec.profile.is_sandboxed:
            raise ExecutorError("HostExecutor was handed an OCI profile — route it through OciExecutor")
        rc, out = common.run(
            list(spec.command),
            timeout=spec.timeout_sec,
            env={**spec.env} if spec.env else None,
        )
        return ExecutionResult(exit_code=rc, output=out, image_digest="host", timed_out=rc == common.RC_TIMEOUT)


@dataclass(frozen=True)
class OciExecutor(Executor):
    """Runs inside a digest-pinned container, hardened per the module docstring."""

    runtime: str

    @classmethod
    def create(cls) -> OciExecutor:
        runtime = container_runtime()
        if runtime is None:
            raise ExecutorError(
                "no container runtime (docker/podman) on PATH, but an OCI profile was requested — "
                "install one, or the code this would sandbox cannot run safely"
            )
        return cls(runtime=runtime)

    def run(self, spec: ExecutionSpec) -> ExecutionResult:
        profile = spec.profile
        if not profile.is_sandboxed:
            raise ExecutorError(f"profile {profile.name!r} is a host profile — route it through HostExecutor")
        digest = profile.image_digest
        if not digests.is_digest(digest):
            raise ExecutorError(
                f"profile {profile.name!r} has no digest-pinned image. A mutable tag would let the "
                "sandbox change after a review was signed — pin it with `agentloop oci build`."
            )

        argv = self._argv(spec)
        rc, out = common.run(argv, timeout=spec.timeout_sec)
        return ExecutionResult(
            exit_code=rc, output=out, image_digest=digest, timed_out=rc == common.RC_TIMEOUT
        )

    def _argv(self, spec: ExecutionSpec) -> list[str]:
        """The full `docker run` argv. Every hardening flag is unconditional, not a knob."""
        profile = spec.profile
        argv = [
            self.runtime,
            "run",
            "--rm",
            "--network",
            profile.network_profile or "none",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--user",
            "1000:1000",
            "--workdir",
            spec.workdir,
        ]
        if profile.raw.get("read_only_root", True):
            argv += ["--read-only"]
        tmp_mb = profile.raw.get("writable_tmp_mb", 512)
        argv += ["--tmpfs", f"/tmp:size={int(tmp_mb) if isinstance(tmp_mb, int) else 512}m,mode=1777"]
        argv += ["--pids-limit", str(_int(profile.raw.get("pids_limit"), 256))]
        argv += ["--memory", f"{_int(profile.raw.get('memory_mb'), 1024)}m"]
        argv += ["--cpus", str(_int(profile.raw.get("cpu_count"), 2))]
        # An empty, ephemeral HOME: the container cannot read the host's ~/.ssh, ~/.aws, etc.
        argv += ["--env", "HOME=/tmp"]

        for host_path, container_path, mode in spec.mounts:
            readonly = "true" if mode == "ro" else "false"
            argv += ["--mount", f"type=bind,src={host_path},dst={container_path},readonly={readonly}"]
        for name in profile.env_allowlist:
            if name in spec.env:
                argv += ["--env", f"{name}={spec.env[name]}"]
        argv.append(profile.image)
        argv += list(spec.command)
        return argv


def _int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def for_profile(profile: models.ExecutorProfile) -> Executor:
    """The executor a profile calls for. The one dispatch point, so the rule lives in one place."""
    return OciExecutor.create() if profile.is_sandboxed else HostExecutor()


# --- image building ------------------------------------------------------------


def containerfile_names() -> list[str]:
    """The Containerfiles the package ships, by profile name (the `data/oci/<name>/` dirs)."""
    names: set[str] = set()
    for rel, _ in data.iter_files("oci"):
        parts = rel.split("/")
        if len(parts) >= 2 and parts[-1] == "Containerfile":
            names.add(parts[-2])
    return sorted(names)


def build_image(name: str, *, tag: str | None = None, runtime: str | None = None) -> str:
    """Build the packaged Containerfile `name` locally and return its `sha256:` image digest.

    The digest is what a config profile pins. Building is a bootstrap convenience — nothing is
    fetched from a registry — and re-pinning after a rebuild is what keeps the sandbox a review
    ran in reproducible from the repository.
    """
    if name not in containerfile_names():
        raise ExecutorError(f"no packaged Containerfile named {name!r} (have: {', '.join(containerfile_names())})")
    engine = runtime or container_runtime()
    if engine is None:
        raise ExecutorError("no container runtime (docker/podman) on PATH")

    import tempfile

    image_tag = tag or f"localhost/agentloop-{name}:local"
    with tempfile.TemporaryDirectory() as workdir:
        context = Path(workdir)
        for rel, blob in data.iter_files(f"oci/{name}"):
            target = context / rel[len(f"oci/{name}/") :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        iid_file = context / "iid"
        rc, out = common.run(
            [
                engine,
                "build",
                "-t",
                image_tag,
                "--iidfile",
                str(iid_file),
                "-f",
                str(context / "Containerfile"),
                str(context),
            ],
            timeout=1800,
        )
        if rc != 0:
            raise ExecutorError(f"building {name} failed (rc={rc}):\n{out[-2000:]}")
        return _image_digest(engine, image_tag, iid_file)


def _image_digest(engine: str, image_tag: str, iid_file: Path) -> str:
    """The image's content digest (`sha256:...`), from the iidfile or an inspect."""
    try:
        raw = iid_file.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if raw.startswith("sha256:") and digests.is_digest(raw):
        return raw
    rc, out = common.run([engine, "inspect", "--format", "{{.Id}}", image_tag], timeout=60)
    candidate = out.strip()
    if rc == 0 and digests.is_digest(candidate):
        return candidate
    raise ExecutorError(f"could not determine the image digest of {image_tag} (got {candidate!r})")


def verify_pinned(profile: models.ExecutorProfile, *, runtime: str | None = None) -> tuple[bool, str]:
    """(ok, message): does a local image with the profile's pinned digest exist?

    Used by `agentloop oci verify`: a profile can pin a digest that no local image has (a
    stale pin, a machine that never built it), and running would then fail cryptically instead
    of saying "build it".
    """
    if not profile.is_sandboxed:
        return True, f"profile {profile.name!r} is a host profile — nothing to pin"
    digest = profile.image_digest
    if not digests.is_digest(digest):
        return False, f"profile {profile.name!r} pins no image digest"
    engine = runtime or container_runtime()
    if engine is None:
        return False, "no container runtime on PATH to check the pinned image against"
    rc, out = common.run([engine, "inspect", "--format", "{{.Id}}", profile.image], timeout=60)
    if rc != 0:
        return False, f"no local image {profile.image} — run `agentloop oci build --profile {profile.name}`"
    if out.strip() != digest:
        return False, f"local image digest {out.strip()} does not match the pinned {digest}"
    return True, f"profile {profile.name!r} pinned image is present ({digest[:19]}…)"
