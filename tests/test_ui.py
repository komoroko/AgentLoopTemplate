"""Verify ui.py: the action whitelist and the HTTP surface (deterministic, offline).

The gate-approval rewrite itself lives in approve.py (the single sanctioned write path) and is
unit-tested in test_approve.py; here only the endpoint's delegation behavior is asserted."""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentloop import registry, ui

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


@pytest.fixture(autouse=True)
def isolate_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every ui.main / registry write off the developer's real ~/.config during tests."""
    monkeypatch.setenv("AGENTLOOP_CONFIG_HOME", str(tmp_path / "cfg"))


def _seed_repo(base: Path, project: str) -> Path:
    loop = base / ".agentloop"
    loop.mkdir(parents=True)
    (loop / "state.md").write_text(_STATE.replace('project: "demo"', f'project: "{project}"'), encoding="utf-8")
    (loop / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    return base


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
    assert status == 200 and "AgentLoop" in page
    assert server.token in page  # the POST token is delivered only via the page
    # Offline canary across the page AND its same-origin assets: no external reference anywhere.
    css = _request(server, "GET", "/assets/app.css")[1].decode("utf-8")
    js = _request(server, "GET", "/assets/app.js")[1].decode("utf-8")
    for name, text in (("index.html", page), ("app.css", css), ("app.js", js)):
        assert "http://" not in text and "https://" not in text, name
        assert "//cdn" not in text and "@import" not in text, name
    # the page pulls only the two shipped assets, nothing else
    assert re.findall(r'(?:src|href)="([^"]+)"', page) == ["/assets/app.css", "/assets/app.js"]
    # the sections live in the page; their renderers and the theme machinery in the assets
    for marker in ('id="stepper"', 'id="trace"', 'id="logs"', 'id="toasts"'):
        assert marker in page, marker
    for marker in ("buildDag", "showTaskDetail", "data-theme"):
        assert marker in js, marker
    assert "data-theme" in css


def test_assets_are_served_with_their_types_and_nothing_else(server: ui.DashboardServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", "/assets/app.css")
    res = conn.getresponse()
    assert res.status == 200 and res.getheader("Content-Type", "").startswith("text/css")
    res.read()
    conn.close()
    # anything off the exact-name allowlist is a 404 (including traversal shapes)
    for path in ("/assets/nope.js", "/assets/../ui.py", "/assets/app.js.bak"):
        assert _request(server, "GET", path)[0] == 404, path


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
    # The delegation to approve.py carries its full behavior: phase advance + the event record.
    assert "current_phase: design" in state
    events = (repo / ".agentloop" / "events.ndjson").read_text(encoding="utf-8")
    assert '"event": "gate_approved"' in events and '"gate": "requirements"' in events
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


def test_main_refuses_non_loopback_bind_with_writes_enabled(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ui.main(["--host", "0.0.0.0", "--root", str(repo)]) == 2
    assert "refusing to bind" in capsys.readouterr().err
    # --once starts no server, so the guard does not apply to it
    assert ui.main(["--host", "0.0.0.0", "--once", "--root", str(repo)]) == 0


def test_open_mode_targets_vscode_over_external_browser() -> None:
    assert ui.open_mode(no_open=False, term_program="vscode") == "vscode"
    assert ui.open_mode(no_open=False, term_program=None) == "browser"
    assert ui.open_mode(no_open=False, term_program="Apple_Terminal") == "browser"
    assert ui.open_mode(no_open=True, term_program="vscode") == "none"  # --no-open overrides detection


# --- review endpoint ------------------------------------------------------------


def test_get_review_serves_rendered_deliverable(server: ui.DashboardServer, repo: Path) -> None:
    doc = repo / "docs" / "10-requirements.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# Requirements\n<script>steal(TOKEN)</script>\n\n## Self-assessment\n- **Confidence**: low\n",
        encoding="utf-8",
    )
    status, data = _request(server, "GET", "/api/review/requirements")
    assert status == 200
    payload = json.loads(data)
    assert payload["is_awaiting"] is True and payload["index"] == 1
    (main,) = payload["deliverables"]
    assert "<h1>Requirements</h1>" in main["html"]
    assert "<script" not in main["html"]  # XSS regression: agent markup arrives inert
    assert main["self_assessment"]["confidence"] == "low"


@pytest.mark.parametrize("path", ["/api/review/nope", "/api/review/../state", "/api/review/", "/api/review/Build"])
def test_get_review_unknown_gate_is_404(server: ui.DashboardServer, path: str) -> None:
    assert _request(server, "GET", path)[0] == 404


def test_review_is_readable_on_a_read_only_server(repo: Path) -> None:
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _request(srv, "GET", "/api/review/requirements")[0] == 200  # reviewing is view-only
    finally:
        srv.shutdown()
        srv.server_close()


# --- project switcher -----------------------------------------------------------


@pytest.fixture
def multi_server(tmp_path: Path) -> Iterator[tuple[ui.DashboardServer, dict[str, Path]]]:
    """A server backed by a real registry with two projects (alpha active, beta second)."""
    alpha = _seed_repo(tmp_path / "alpha", "alpha")
    beta = _seed_repo(tmp_path / "beta", "beta")
    reg_path = registry.registry_path()
    reg = registry.Registry()
    reg.add("alpha", alpha)
    reg.add("beta", beta)
    reg.set_active("alpha")
    registry.save(reg, reg_path)

    srv = ui.DashboardServer(("127.0.0.1", 0), root=alpha, read_only=False, registry_path=reg_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv, {"alpha": alpha, "beta": beta}
    finally:
        srv.shutdown()
        srv.server_close()


def test_get_projects_lists_registry_with_active(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    status, data = _request(srv, "GET", "/api/projects")
    assert status == 200
    payload = json.loads(data)
    assert payload["active"] == "alpha"
    by_name = {p["name"]: p for p in payload["projects"]}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"]["active"] is True and by_name["alpha"]["exists"] is True


def test_status_follows_the_active_project(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    assert json.loads(_request(srv, "GET", "/api/status")[1])["project"] == "alpha"
    status, _ = _request(srv, "POST", "/api/project/select", {"name": "beta"}, token=srv.token)
    assert status == 200
    # the switch persisted, so /api/status now reports the beta repo
    assert json.loads(_request(srv, "GET", "/api/status")[1])["project"] == "beta"
    assert json.loads(_request(srv, "GET", "/api/projects")[1])["active"] == "beta"


def test_select_unknown_project_is_400(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    status, _ = _request(srv, "POST", "/api/project/select", {"name": "ghost"}, token=srv.token)
    assert status == 400


def test_select_without_token_is_403(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    status, _ = _request(srv, "POST", "/api/project/select", {"name": "beta"})
    assert status == 403


def test_pinned_server_reports_single_project_and_refuses_select(server: ui.DashboardServer) -> None:
    # The `server` fixture builds a registry_path-less (pinned) server from a single repo.
    payload = json.loads(_request(server, "GET", "/api/projects")[1])
    assert len(payload["projects"]) == 1 and payload["projects"][0]["active"] is True
    status, _ = _request(server, "POST", "/api/project/select", {"name": "whatever"}, token=server.token)
    assert status == 409  # no registry backs a pinned server


def test_main_once_does_not_touch_the_registry(repo: Path) -> None:
    # --once is a scripting/inspection path: it prints status and must not mutate user-global state.
    assert ui.main(["--once", "--root", str(repo)]) == 0
    assert not registry.registry_path().exists()
