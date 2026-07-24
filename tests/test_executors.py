"""The sandbox hardening is unconditional — these tests assert the argv proves it.

Most of this runs without a container runtime: the `docker run` argv is built the same way
whether or not docker is installed, so we can read every hardening flag out of it. The one
test that actually builds an image is behind the `integration` marker.
"""

from __future__ import annotations

import pytest

from agentloop import executors, models


def _oci_profile(**overrides: object) -> models.ExecutorProfile:
    raw = {
        "kind": "oci",
        "image": "localhost/agentloop-python@sha256:" + "a" * 64,
        "network_profile": "none",
        "read_only_root": True,
    }
    raw.update(overrides)
    return models.ExecutorProfile("oracle", raw)


def _spec(profile: models.ExecutorProfile, **overrides: object) -> executors.ExecutionSpec:
    kwargs: dict[str, object] = {"command": ("pytest", "-q"), "profile": profile}
    kwargs.update(overrides)
    return executors.ExecutionSpec(**kwargs)  # type: ignore[arg-type]


def test_argv_carries_every_hardening_flag() -> None:
    argv = executors.OciExecutor(runtime="docker")._argv(_spec(_oci_profile()))
    joined = " ".join(argv)
    # The flags the module docstring promises are unconditional — each must be present.
    assert "--network none" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--cap-drop ALL" in joined
    assert "--read-only" in joined
    assert "--user 1000:1000" in joined
    assert "--pids-limit" in joined
    assert "--memory" in joined
    assert "--cpus" in joined
    # An ephemeral HOME so the container cannot read the host's ~/.ssh, ~/.aws, etc.
    assert "HOME=/tmp" in joined
    # The image is the pinned digest reference, and the command comes after it.
    assert argv[-3:] == [_oci_profile().image, "pytest", "-q"]


def test_argv_never_mounts_host_secrets() -> None:
    """No mount is added unless the spec asks for one — the host filesystem is not exposed."""
    argv = executors.OciExecutor(runtime="docker")._argv(_spec(_oci_profile()))
    joined = " ".join(argv)
    for forbidden in ("/var/run/docker.sock", ".ssh", ".aws", "/root", "HOME=/home"):
        assert forbidden not in joined


def test_argv_honors_declared_mounts_readonly_flag() -> None:
    from pathlib import Path

    spec = _spec(_oci_profile(), mounts=((Path("/repo"), "/work", "ro"), (Path("/out"), "/out", "rw")))
    joined = " ".join(executors.OciExecutor(runtime="docker")._argv(spec))
    assert "type=bind,src=/repo,dst=/work,readonly=true" in joined
    assert "type=bind,src=/out,dst=/out,readonly=false" in joined


def test_argv_env_allowlist_only_passes_named_vars() -> None:
    profile = _oci_profile(env_allowlist=["CI"])
    spec = _spec(profile, env={"CI": "1", "SECRET_TOKEN": "leak"})
    joined = " ".join(executors.OciExecutor(runtime="docker")._argv(spec))
    assert "CI=1" in joined
    assert "SECRET_TOKEN" not in joined


def test_read_only_root_can_be_disabled_by_profile() -> None:
    argv = executors.OciExecutor(runtime="docker")._argv(_spec(_oci_profile(read_only_root=False)))
    assert "--read-only" not in argv


def test_oci_executor_refuses_unpinned_image() -> None:
    profile = _oci_profile(image="localhost/agentloop-python:latest")
    with pytest.raises(executors.ExecutorError, match="digest-pinned"):
        executors.OciExecutor(runtime="docker").run(_spec(profile))


def test_oci_executor_refuses_host_profile() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    with pytest.raises(executors.ExecutorError, match="host profile"):
        executors.OciExecutor(runtime="docker").run(_spec(host))


def test_host_executor_refuses_sandboxed_profile() -> None:
    with pytest.raises(executors.ExecutorError, match="OCI profile"):
        executors.HostExecutor().run(_spec(_oci_profile()))


def test_for_profile_dispatches_host_without_a_runtime() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    assert isinstance(executors.for_profile(host), executors.HostExecutor)


def test_host_executor_runs_a_trusted_command() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    result = executors.HostExecutor().run(_spec(host, command=("true",)))
    assert result.exit_code == 0
    assert result.image_digest == "host"


def test_containerfile_names_lists_the_packaged_profiles() -> None:
    names = executors.containerfile_names()
    assert {"python", "reviewer", "implementer"} <= set(names)


def test_verify_pinned_host_profile_is_a_noop() -> None:
    host = models.ExecutorProfile("t", {"kind": "host"})
    ok, message = executors.verify_pinned(host)
    assert ok
    assert "nothing to pin" in message


def test_verify_pinned_reports_unpinned_profile() -> None:
    profile = _oci_profile(image="localhost/agentloop-python:latest")
    ok, message = executors.verify_pinned(profile)
    assert not ok
    assert "pins no image digest" in message


def test_verify_pinned_reports_missing_local_image() -> None:
    """A pinned digest with no matching local image says 'build it', not a cryptic runtime error."""
    ok, message = executors.verify_pinned(_oci_profile(), runtime="docker")
    assert not ok
    # Either no runtime, or the image is genuinely absent — both are actionable messages.
    assert "oci build" in message or "no local image" in message or "no container runtime" in message


@pytest.mark.integration
def test_build_image_produces_a_pinned_digest() -> None:
    if executors.container_runtime() is None:
        pytest.skip("no container runtime on PATH")
    digest = executors.build_image("python")
    assert digest.startswith("sha256:")
    profile = _oci_profile(image=f"localhost/agentloop-python@{digest}")
    ok, _ = executors.verify_pinned(profile)
    assert ok
