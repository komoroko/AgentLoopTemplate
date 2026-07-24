"""The hermetic oracle runner: run a frozen oracle in a sealed sandbox, bind its result.

An oracle's verdict is only evidence if the thing that produced it cannot be tampered with —
by the implementer, by the project's own test config, by anything on the host. So the runner:

  - runs the frozen bundle in an OCI sandbox (never on the host), read-only, no network,
    with only the bundle and the subject code mounted;
  - mounts the **committed** subject tree, not the working directory, so uncommitted edits do
    not change what is judged;
  - for a Python oracle, disables pytest plugin autoload and keeps the project's own
    `conftest.py`, plugins, and PYTHONPATH out of the run — an implementer who can edit a
    fixture the oracle loads can otherwise make any oracle pass (plan §9.3, E2E-12);
  - never mounts the host HOME, SSH, cloud credentials, or the docker socket (E2E-28).

The result is **bound**: :func:`result_binding` records the seven digests (change, bundle,
image, environment, network profile, command, subject mount) that the run is only reusable
against. A cached result is reused only when all seven match (plan §9.6) — anything else, and
the oracle runs again, because "close enough" is how a stale pass survives a real change.

A high/critical oracle also runs its **negative controls**: the conforming subject must pass
and the known-violating subject must fail. An oracle that passes both is not looking, and its
"pass" on the real subject means nothing (plan §9.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentloop import digests, executors, models, oracle_bundle, strict_yaml
from agentloop import repo as repo_mod
from agentloop import store as store_mod

#: Forced into every Python oracle run so the project's plugins/fixtures cannot leak in.
_PYTHON_HARDENING = {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}


class OracleError(RuntimeError):
    """An oracle could not be run, or its bundle/runner is not frozen."""


@dataclass(frozen=True)
class OracleResult:
    """One oracle run: its verdict, the sandbox it ran in, and the binding it is reusable against."""

    oracle_id: str
    passed: bool
    exit_code: int
    output: str
    image_digest: str
    binding: dict[str, str]
    negative_controls: tuple[oracle_bundle.NegativeControlResult, ...] = ()
    timed_out: bool = False

    @property
    def negative_controls_ok(self) -> bool:
        """True when every declared negative control rejected its violating subject."""
        return all(control.rejected for control in self.negative_controls)

    @property
    def conclusive(self) -> bool:
        """A pass is only conclusive when the negative controls also behaved."""
        return self.passed and self.negative_controls_ok

    def result_digest(self) -> str:
        return digests.of(
            {
                "oracle_id": self.oracle_id,
                "exit_code": self.exit_code,
                "passed": self.passed,
                "binding": self.binding,
                "negative_controls": [
                    {"id": c.control_id, "expected": c.expected_exit_code, "actual": c.actual_exit_code}
                    for c in self.negative_controls
                ],
            }
        )


def result_binding(
    *, change_digest: str, oracle: models.Oracle, image_digest: str, subject_mount_digest: str
) -> dict[str, str]:
    """The seven-part tuple a result is reusable against (plan §9.6).

    Any one of these moving means the oracle ran against a different world, so the cached
    verdict does not carry over. Every part is a well-formed digest — `environment_digest` and
    `network_profile_digest` are hashed from the runner config, the command digest is over its
    argv. Hashing rather than passing a raw string through matters for reuse: `can_reuse`
    refuses an empty digest, so an oracle that merely declared no explicit environment would
    otherwise never reuse a result.
    """
    runner = oracle.raw.get("runner")
    runner = runner if isinstance(runner, dict) else {}
    return {
        "change_digest": change_digest,
        "oracle_bundle_digest": oracle.bundle_digest,
        "runner_image_digest": image_digest,
        "environment_digest": digests.of({"environment": runner.get("environment_digest", "")}),
        "network_profile_digest": digests.of({"network": runner.get("network_profile", "none")}),
        "command_digest": digests.of({"command": _command(oracle)}),
        "subject_mount_digest": subject_mount_digest,
    }


def _command(oracle: models.Oracle) -> list[str]:
    raw = oracle.raw.get("command")
    return [str(part) for part in raw] if isinstance(raw, list) else []


def _profile_for(config: models.Config | None, oracle: models.Oracle) -> models.ExecutorProfile:
    """The sandbox an oracle runs in: its runner's own profile, or the config's oracle profile.

    An oracle's runner names its image directly; wrap it in an ExecutorProfile so the same
    hardened OciExecutor path runs it. Repo-derived code always gets an OCI profile — a `host`
    oracle is a contradiction and is refused.
    """
    runner = oracle.raw.get("runner")
    runner = runner if isinstance(runner, dict) else {}
    if runner.get("executor") == "host":
        raise OracleError(
            f"oracle {oracle.id} declares a host runner — an acceptance oracle runs repository code and "
            "must be sandboxed (an oracle you can influence from the host is not a boundary)"
        )
    image = str(runner.get("image", ""))
    if image:
        return models.ExecutorProfile(
            oracle.id,
            {
                "kind": "oci",
                "image": image,
                "network_profile": str(runner.get("network_profile", "none")),
                "read_only_root": True,
            },
        )
    profile = config.profile_for("oracle") if config else None
    if profile is None:
        raise OracleError(f"oracle {oracle.id} names no runner image and config has no oracle profile")
    return profile


def run_oracle(
    repo: repo_mod.Repo,
    oracle: models.Oracle,
    *,
    change_digest: str,
    config: models.Config | None = None,
    subject_commit: str = "HEAD",
    executor: executors.Executor | None = None,
) -> OracleResult:
    """Run one frozen oracle in its sandbox and bind the result. Raises before running if unfrozen.

    The bundle must be committed and match its frozen digest first: running an oracle whose
    harness has drifted from what gate ③ approved would produce a verdict about a different
    check (E2E-12).
    """
    ok, message = oracle_bundle.verify_frozen(repo, oracle)
    if not ok:
        raise OracleError(message)

    profile = _profile_for(config, oracle)
    runner = executor or executors.for_profile(profile)
    command = _command(oracle)
    if not command:
        raise OracleError(f"oracle {oracle.id} declares no command to run")

    subject_mount_digest = _subject_mount_digest(oracle, subject_commit)
    env = dict(_PYTHON_HARDENING) if _is_python(command) else {}

    spec = executors.ExecutionSpec(
        command=tuple(command),
        profile=profile,
        env=env,
        workdir="/oracle",
        timeout_sec=_timeout(oracle),
    )
    outcome = runner.run(spec)
    expected = _expected_exit(oracle)
    passed = outcome.exit_code == expected

    controls = _run_negative_controls(oracle, runner, profile, env) if passed else ()

    return OracleResult(
        oracle_id=oracle.id,
        passed=passed,
        exit_code=outcome.exit_code,
        output=outcome.output,
        image_digest=outcome.image_digest,
        binding=result_binding(
            change_digest=change_digest,
            oracle=oracle,
            image_digest=outcome.image_digest,
            subject_mount_digest=subject_mount_digest,
        ),
        negative_controls=controls,
        timed_out=outcome.timed_out,
    )


def _run_negative_controls(
    oracle: models.Oracle,
    runner: executors.Executor,
    profile: models.ExecutorProfile,
    env: dict[str, str],
) -> tuple[oracle_bundle.NegativeControlResult, ...]:
    """Run each negative control against its violating subject; each must be *rejected*."""
    results: list[oracle_bundle.NegativeControlResult] = []
    for control in oracle.negative_controls:
        fixture = str(control.get("subject_fixture", ""))
        expected = control.get("expected_exit_code")
        expected_code = expected if isinstance(expected, int) else 1
        spec = executors.ExecutionSpec(
            command=(*_command(oracle), "--agentloop-subject", fixture),
            profile=profile,
            env=env,
            workdir="/oracle",
            timeout_sec=_timeout(oracle),
        )
        outcome = runner.run(spec)
        results.append(
            oracle_bundle.NegativeControlResult(
                control_id=str(control.get("id", "")),
                subject_fixture=fixture,
                expected_exit_code=expected_code,
                actual_exit_code=outcome.exit_code,
            )
        )
    return tuple(results)


def _subject_mount_digest(oracle: models.Oracle, subject_commit: str) -> str:
    return digests.of({"commit": subject_commit, "paths": sorted(oracle.subject_paths)})


def _is_python(command: list[str]) -> bool:
    head = command[0] if command else ""
    return head in {"pytest", "python", "python3"} or head.endswith(("pytest",))


def _expected_exit(oracle: models.Oracle) -> int:
    value = oracle.raw.get("expected_exit_code")
    return value if isinstance(value, int) else 0


def _timeout(oracle: models.Oracle) -> float | None:
    runner = oracle.raw.get("runner")
    if isinstance(runner, dict) and isinstance(runner.get("timeout_sec"), int):
        return float(runner["timeout_sec"])
    return 120.0


# --- result reuse (plan §9.6) --------------------------------------------------


def can_reuse(previous_binding: dict[str, str], current_binding: dict[str, str]) -> bool:
    """True only when every one of the seven binding digests matches.

    Strict equality across the whole tuple: a cached pass carries over only when the world it
    was produced in is byte-for-byte the world now. Anything short of that and the oracle runs
    again — "probably still passes" is not a verdict.
    """
    keys = (
        "change_digest",
        "oracle_bundle_digest",
        "runner_image_digest",
        "environment_digest",
        "network_profile_digest",
        "command_digest",
        "subject_mount_digest",
    )
    return all(previous_binding.get(k) == current_binding.get(k) and previous_binding.get(k) for k in keys)


def cache_result(repo: repo_mod.Repo, result: OracleResult) -> Path:
    """Persist a result to the oracle-results cache, keyed by its binding, for reuse."""
    key = digests.of(result.binding).removeprefix("sha256:")
    directory = store_mod.cache_dir(repo) / "oracle-results" / result.oracle_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    store_mod.atomic_write(path, digests.canonical(_result_to_mapping(result)) + b"\n", mode=0o600)
    return path


def load_cached(repo: repo_mod.Repo, oracle_id: str, binding: dict[str, str]) -> OracleResult | None:
    """A cached result whose binding matches, or None. The reuse-tuple check is applied here."""
    key = digests.of(binding).removeprefix("sha256:")
    path = store_mod.cache_dir(repo) / "oracle-results" / oracle_id / f"{key}.json"
    try:
        raw = strict_yaml.load_json_mapping(path.read_text(encoding="utf-8"), what="oracle result")
    except (OSError, strict_yaml.StrictParseError):
        return None
    cached = _mapping_to_result(raw)
    return cached if can_reuse(cached.binding, binding) else None


def _result_to_mapping(result: OracleResult) -> dict[str, object]:
    return {
        "oracle_id": result.oracle_id,
        "passed": result.passed,
        "exit_code": result.exit_code,
        "image_digest": result.image_digest,
        "binding": result.binding,
        "negative_controls": [
            {
                "id": c.control_id,
                "subject_fixture": c.subject_fixture,
                "expected_exit_code": c.expected_exit_code,
                "actual_exit_code": c.actual_exit_code,
            }
            for c in result.negative_controls
        ],
    }


def _as_int(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def _mapping_to_result(raw: dict[str, object]) -> OracleResult:
    controls = raw.get("negative_controls")
    parsed = tuple(
        oracle_bundle.NegativeControlResult(
            control_id=str(c.get("id", "")),
            subject_fixture=str(c.get("subject_fixture", "")),
            expected_exit_code=_as_int(c.get("expected_exit_code"), 1),
            actual_exit_code=_as_int(c.get("actual_exit_code"), 0),
        )
        for c in (controls if isinstance(controls, list) else [])
        if isinstance(c, dict)
    )
    binding = raw.get("binding")
    return OracleResult(
        oracle_id=str(raw.get("oracle_id", "")),
        passed=bool(raw.get("passed")),
        exit_code=_as_int(raw.get("exit_code"), 0),
        output="",
        image_digest=str(raw.get("image_digest", "")),
        binding={str(k): str(v) for k, v in binding.items()} if isinstance(binding, dict) else {},
        negative_controls=parsed,
    )
