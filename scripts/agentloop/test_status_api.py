"""Verify status_api's next-action decision table and tolerant SSOT aggregation (deterministic, offline)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import status_api

# --- next_action: one test per decision-table row (first match wins) -----------

_ALL_PENDING = {g: "pending" for g in status_api.GATE_ORDER}
_ALL_APPROVED = {g: "approved" for g in status_api.GATE_ORDER}


def _action(**overrides: object) -> status_api.Recommendation:
    """next_action with a healthy mid-lifecycle baseline; each test overrides its trigger."""
    kwargs: dict[str, object] = {
        "current_phase": "requirements",
        "gates": dict(_ALL_PENDING),
        "counts": None,
        "open_escalation_count": 0,
        "template_mode": False,
        "placeholders": False,
        "has_adopt_manifest": False,
    }
    kwargs.update(overrides)
    return status_api.next_action(**kwargs)  # type: ignore[arg-type]


def test_row1_template_mode_recommends_init() -> None:
    rec = _action(template_mode=True)
    assert rec.command.startswith("make init") and rec.kind == "setup"
    assert rec.also == ()


def test_row1_placeholders_recommend_init_with_onboard_when_adopted() -> None:
    rec = _action(placeholders=True, has_adopt_manifest=True)
    assert rec.command.startswith("make init")
    assert "/onboard" in rec.also


def test_row2_broken_gate_chain_recommends_doctor() -> None:
    gates = dict(_ALL_PENDING, design="approved")  # approved below a pending requirements gate
    rec = _action(gates=gates, current_phase="design")
    assert rec.command == "make doctor" and rec.kind == "fix"


def test_row3_needs_revision_recommends_tasks_reconcile() -> None:
    gates = dict(_ALL_APPROVED, build="pending", release="pending")
    rec = _action(current_phase="build", gates=gates, counts={"needs-revision": 2, "todo": 1})
    assert rec.command == "/tasks" and rec.kind == "reconcile"
    assert "make revise" in rec.also


def test_row4_open_escalations_block_verify() -> None:
    gates = dict(_ALL_APPROVED, release="pending")
    rec = _action(current_phase="verify", gates=gates, open_escalation_count=3)
    assert rec.kind == "resolve" and "--resolve" in rec.command
    # Outside verify, open escalations do not take over the primary recommendation.
    rec2 = _action(current_phase="requirements", open_escalation_count=3)
    assert rec2.kind == "run_phase"


def test_row5_brief_recommends_req() -> None:
    rec = _action(current_phase="brief")
    assert rec.command == "/req" and "docs/00-product-brief.md" in rec.reason


def test_row6_done_recommends_cycle_close() -> None:
    assert _action(current_phase="done", gates=dict(_ALL_APPROVED)).kind == "close"
    # All gates approved counts as done even if the phase was not flipped yet.
    assert _action(current_phase="verify", gates=dict(_ALL_APPROVED)).kind == "close"


@pytest.mark.parametrize(
    ("phase", "gates", "command"),
    [
        ("requirements", _ALL_PENDING, "/req"),  # own gate pending → run the phase
        ("requirements", {**_ALL_PENDING, "requirements": "approved"}, "/design"),  # approved → advance
        ("design", {**_ALL_PENDING, "requirements": "approved", "design": "approved"}, "/tasks"),
        ("build", {**_ALL_APPROVED, "build": "pending", "release": "pending"}, "/build"),
        ("build", {**_ALL_APPROVED, "release": "pending"}, "/verify"),
        ("verify", {**_ALL_APPROVED, "release": "pending"}, "/verify"),
    ],
)
def test_row7_phase_gate_progression(phase: str, gates: dict[str, str], command: str) -> None:
    rec = _action(current_phase=phase, gates=dict(gates))
    assert rec.command == command and rec.kind == "run_phase"


def test_row7_build_recommendation_offers_headless_loop() -> None:
    gates = dict(_ALL_APPROVED, build="pending", release="pending")
    assert "make build-loop" in _action(current_phase="build", gates=gates).also  # own phase
    gates2 = {**_ALL_PENDING, "requirements": "approved", "design": "approved", "tasks": "approved"}
    assert "make build-loop" in _action(current_phase="tasks", gates=gates2).also  # advancing into build


def test_unknown_phase_falls_back_to_doctor() -> None:
    rec = _action(current_phase="biuld")
    assert rec.command == "make doctor" and rec.kind == "fix"


# --- collect_status: tolerant aggregation over a fixture repo ------------------

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: build          # comment preserved
gates:
  requirements: approved      # 2026-07-01
  design: approved            # 2026-07-02
  tasks: approved             # 2026-07-03
  build: pending
  release: pending
updated_at: "2026-07-03"
---
# board
"""

_CONFIG = """gates:
  template_mode: false
github:
  enabled: false
"""

_TASKS = """tasks:
  - {id: T-001, title: base, kind: foundation, blockedBy: [], status: done, test: make test}
  - {id: T-002, title: leaf, kind: parallel, blockedBy: [T-001], status: todo, test: make test}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    loop = tmp_path / ".agentloop"
    loop.mkdir()
    (loop / "state.md").write_text(_STATE, encoding="utf-8")
    (loop / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (loop / "tasks.yaml").write_text(_TASKS, encoding="utf-8")
    return tmp_path


def test_collect_status_full_repo(repo: Path) -> None:
    status = status_api.collect_status(repo)
    assert status["project"] == "demo" and status["current_phase"] == "build"
    gates = status["gates"]
    assert isinstance(gates, list) and [g["name"] for g in gates] == list(status_api.GATE_ORDER)
    assert gates[3] == {"name": "build", "status": "pending", "index": 4, "phase": "build"}
    tasks = status["tasks"]
    assert isinstance(tasks, dict)
    assert tasks["counts"]["done"] == 1 and tasks["total"] == 2
    assert [f["id"] for f in tasks["frontier"]] == ["T-002"]
    assert tasks["layers"] == [["T-001"], ["T-002"]]
    next_rec = status["next"]
    assert isinstance(next_rec, dict) and next_rec["command"] == "/build"
    assert status["warnings"] == []
    json.dumps(status)  # the whole object must be JSON-serializable


def test_collect_status_without_tasks_yaml(repo: Path) -> None:
    (repo / ".agentloop" / "tasks.yaml").unlink()
    status = status_api.collect_status(repo)
    assert status["tasks"] is None and status["warnings"] == []  # normal before /tasks
    assert isinstance(status["next"], dict)


def test_collect_status_surfaces_corrupt_files_as_warnings(repo: Path) -> None:
    (repo / ".agentloop" / "config.yaml").write_text("gates: [broken", encoding="utf-8")
    (repo / ".agentloop" / "tasks.yaml").write_text("tasks:\n  - {id: T-001, kind: nope}\n", encoding="utf-8")
    status = status_api.collect_status(repo)
    warnings = status["warnings"]
    assert isinstance(warnings, list) and len(warnings) == 2
    assert isinstance(status["next"], dict)  # still recommends something
    json.dumps(status)


def test_collect_status_missing_state_md(tmp_path: Path) -> None:
    (tmp_path / ".agentloop").mkdir()
    status = status_api.collect_status(tmp_path)
    warnings = status["warnings"]
    assert isinstance(warnings, list) and any("state.md" in w for w in warnings)


def test_collect_status_counts_open_escalations(repo: Path) -> None:
    events = (
        '{"id": 1, "ts": "2026-07-03T10:00:00", "event": "blocked", "task": "T-002"}\n'
        '{"id": 2, "ts": "2026-07-03T11:00:00", "event": "blocked", "task": "T-001"}\n'
        '{"id": 3, "ts": "2026-07-03T12:00:00", "event": "resolve", "ref": 1}\n'
    )
    (repo / ".agentloop" / "events.ndjson").write_text(events, encoding="utf-8")
    status = status_api.collect_status(repo)
    esc = status["escalations"]
    assert isinstance(esc, dict) and esc["total_open"] == 1
    assert esc["open"][0]["id"] == 2 and esc["open"][0]["task"] == "T-001"


def test_main_prints_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert status_api.main(["--root", str(repo), "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["next"]["command"] == "/build"
