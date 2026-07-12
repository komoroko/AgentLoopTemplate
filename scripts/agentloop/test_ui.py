"""Verify ui.py: gate-approval rewrite, the action whitelist, and the HTTP surface (deterministic, offline)."""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
import ui

_STATE = """---
project: "demo"
branch: "build/demo"
current_phase: requirements
gates:
  requirements: pending       # c1
  design: pending             # c2
  tasks: pending
  build: pending
  release: pending
updated_at: "2026-07-01"
---
# board

Body example that must never be rewritten: `tasks: pending`.
"""

_CONFIG = """gates:
  template_mode: false
github:
  enabled: false
"""


# --- approve_gate_text: surgical front-matter rewrite --------------------------


def test_approve_gate_rewrites_only_the_gate_line() -> None:
    out = ui.approve_gate_text(_STATE, "requirements", "2026-07-12")
    assert re.search(r"requirements: approved\s+# 2026-07-12 \(via ui\)", out)
    assert re.search(r"design: pending\s+# c2", out)  # downstream untouched
    assert "Body example that must never be rewritten: `tasks: pending`." in out  # body untouched


def test_approve_gate_enforces_chain_order() -> None:
    with pytest.raises(ui.UiActionError) as exc:
        ui.approve_gate_text(_STATE, "design", "2026-07-12")  # requirements still pending
    assert exc.value.status == 409
    approved = ui.approve_gate_text(_STATE, "requirements", "2026-07-12")
    out = ui.approve_gate_text(approved, "design", "2026-07-12")  # now legal
    assert re.search(r"design: approved\s+# 2026-07-12 \(via ui\)", out)


def test_approve_gate_rejects_already_approved_and_unknown() -> None:
    approved = ui.approve_gate_text(_STATE, "requirements", "2026-07-12")
    with pytest.raises(ui.UiActionError) as exc:
        ui.approve_gate_text(approved, "requirements", "2026-07-12")
    assert exc.value.status == 409
    with pytest.raises(ui.UiActionError) as exc2:
        ui.approve_gate_text(_STATE, "verify", "2026-07-12")
    assert exc2.value.status == 400


def test_approve_gate_requires_frontmatter() -> None:
    with pytest.raises(ui.UiActionError) as exc:
        ui.approve_gate_text("# no front-matter here", "requirements", "2026-07-12")
    assert exc.value.status == 500


# --- action_argv: the fixed whitelist ------------------------------------------


def test_action_argv_whitelist() -> None:
    assert ui.action_argv("doctor", {}) == ["make", "doctor"]
    argv = ui.action_argv("events_resolve", {"id": 3, "note": "fixed; it's done"})
    assert argv[:2] == ["make", "events"] and "--resolve 3" in argv[2]
    assert "'fixed; it'\"'\"'s done'" in argv[2]  # note is shell-quoted server-side
    argv = ui.action_argv("revise", {"phase": "design", "reason": "rethink auth"})
    assert argv[:2] == ["make", "revise"] and "--to design" in argv[2]
    assert ui.action_argv("cycle_close", {"slug": "payment-refactor"}) == [
        "make",
        "cycle-close",
        "NAME=payment-refactor",
    ]


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("rm_rf", {}),  # not on the whitelist
        ("events_resolve", {"id": "abc"}),  # non-integer id
        ("revise", {"phase": "verify", "reason": "x"}),  # not a roll-back target
        ("revise", {"phase": "design", "reason": "  "}),  # empty reason
        ("cycle_close", {"slug": "Bad Slug!"}),  # invalid slug characters
        ("cycle_close", {"slug": "x; rm -rf /"}),  # injection attempt
    ],
)
def test_action_argv_rejects_invalid(action: str, params: dict[str, object]) -> None:
    with pytest.raises(ui.UiActionError) as exc:
        ui.action_argv(action, params)
    assert exc.value.status == 400


# --- HTTP surface ---------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    loop = tmp_path / ".agentloop"
    loop.mkdir()
    (loop / "state.md").write_text(_STATE, encoding="utf-8")
    (loop / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    return tmp_path


@pytest.fixture
def server(repo: Path) -> Iterator[ui.DashboardServer]:
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _request(
    srv: ui.DashboardServer, method: str, path: str, body: dict[str, object] | None = None, token: str | None = None
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-AgentLoop-Token"] = token
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


def test_get_status_returns_next_command(server: ui.DashboardServer) -> None:
    status, data = _request(server, "GET", "/api/status")
    assert status == 200
    payload = json.loads(data)
    assert payload["next"]["command"] == "/req" and payload["project"] == "demo"


def test_get_page_is_offline_self_contained(server: ui.DashboardServer) -> None:
    status, data = _request(server, "GET", "/")
    page = data.decode("utf-8")
    assert status == 200 and "AgentLoop dashboard" in page
    assert "http://" not in page and "https://" not in page  # no external fetches (offline canary)
    assert server.token in page  # the POST token is delivered only via the page


def test_get_unknown_path_is_404(server: ui.DashboardServer) -> None:
    assert _request(server, "GET", "/nope")[0] == 404


def test_post_without_token_is_403(server: ui.DashboardServer) -> None:
    status, _ = _request(server, "POST", "/api/run", {"action": "doctor", "params": {}})
    assert status == 403


def test_post_unknown_action_is_400(server: ui.DashboardServer) -> None:
    status, _ = _request(server, "POST", "/api/run", {"action": "nope", "params": {}}, token=server.token)
    assert status == 400


def test_post_gate_approve_updates_state(server: ui.DashboardServer, repo: Path) -> None:
    status, _ = _request(server, "POST", "/api/gate/approve", {"gate": "requirements"}, token=server.token)
    assert status == 200
    state = (repo / ".agentloop" / "state.md").read_text(encoding="utf-8")
    assert re.search(r"requirements: approved\s+# \d{4}-\d{2}-\d{2} \(via ui\)", state)
    assert re.search(r"design: pending\s+# c2", state)
    # The chain check answers 409 through HTTP too (tasks needs design first).
    status2, _ = _request(server, "POST", "/api/gate/approve", {"gate": "tasks"}, token=server.token)
    assert status2 == 409


def test_read_only_server_refuses_posts(repo: Path) -> None:
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _ = _request(srv, "POST", "/api/gate/approve", {"gate": "requirements"}, token=srv.token)
        assert status == 405
        assert _request(srv, "GET", "/api/status")[0] == 200  # reads still work
    finally:
        srv.shutdown()
        srv.server_close()


def test_main_once_prints_parseable_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ui.main(["--once", "--root", str(repo)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next"]["command"] == "/req"
