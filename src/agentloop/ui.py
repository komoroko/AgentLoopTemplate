"""Local web dashboard for the AgentLoop SSOT — state, gates, tasks, and the next recommended command.

Serves the page in `ui_assets/` (index.html + app.css + app.js — real files so the frontend is
lintable and diffable, not a string inside Python) over stdlib `http.server`. Everything is served
same-origin with zero external references, so the dashboard stays offline-safe; the data comes from
status_api.collect_status(). Guidance-first: the page shows where the lifecycle stands and which
command to run next. A small **fixed whitelist** of safe operations can also be executed from the
page (gate-approval recording — a human privilege exercised by a human clicking — plus doctor /
events-resolve / revise / cycle-close). The client only ever sends an action id and typed
parameters; command lines are built server-side (`action_argv`), so arbitrary command execution is
structurally impossible. Outward-facing operations (push / PR / merge) are deliberately absent.

Safety layers: binds 127.0.0.1 by default, and a non-loopback bind with the write endpoints enabled
requires an explicit `--allow-remote`; every POST requires the `X-AgentLoop-Token` header whose value
is generated per server start and embedded only in the served page (a cross-origin page cannot set a
custom header without a CORS preflight, and no CORS headers are ever sent); `--read-only` disables
POST entirely.

Usage:
  agentloop ui                 # serve on 127.0.0.1:8765 and open the browser
  agentloop ui --no-open  # print the URL only
  agentloop ui --read-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentloop import approve, common, review_api, revise, status_api
from agentloop import registry as registry_mod
from agentloop import repo as repo_mod

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_SEC = 900
_OUTPUT_LIMIT = 8000  # tail shown per stream (failures are summarized, not dumped)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# The frontend, shipped beside this module. Served by exact name only (no traversal surface);
# read per request so an edit shows up on reload during development.
ASSETS_DIR = Path(__file__).resolve().parent / "ui_assets"
_ASSET_TYPES = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class UiActionError(Exception):
    """A rejected UI action, carrying the HTTP status to answer with."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def action_argv(action: str, params: dict[str, object]) -> list[str]:
    """Build the command line for a whitelisted action id. Everything else is rejected (400).

    The argv mirrors the documented make targets one-to-one, so what the button runs is exactly what
    the human would have typed; typed parameters are validated and shell-quoted here, never client-side.
    """
    if action == "doctor":
        return ["make", "doctor"]
    if action == "events_resolve":
        try:
            event_id = int(str(params.get("id")))
        except (TypeError, ValueError):
            raise UiActionError(HTTPStatus.BAD_REQUEST, "events_resolve needs an integer 'id'") from None
        note = str(params.get("note") or "")
        return ["make", "events", f"ARGS=--resolve {event_id} --note {shlex.quote(note)}"]
    if action == "revise":
        phase = str(params.get("phase") or "")
        if phase not in revise._PHASE_GATE:
            raise UiActionError(
                HTTPStatus.BAD_REQUEST, f"revise 'phase' must be one of {', '.join(sorted(revise._PHASE_GATE))}"
            )
        reason = str(params.get("reason") or "").strip()
        if not reason:
            raise UiActionError(HTTPStatus.BAD_REQUEST, "revise needs a non-empty 'reason'")
        return ["make", "revise", f"ARGS=--to {phase} --reason {shlex.quote(reason)}"]
    if action == "cycle_close":
        slug = str(params.get("slug") or "")
        if not _SLUG_RE.match(slug):
            raise UiActionError(HTTPStatus.BAD_REQUEST, "cycle_close 'slug' must match [a-z0-9][a-z0-9-]*")
        return ["make", "cycle-close", f"NAME={slug}"]
    raise UiActionError(HTTPStatus.BAD_REQUEST, f"unknown action '{action}'")


class DashboardServer(ThreadingHTTPServer):
    """The HTTP server plus the per-start context the handler needs (root, token, read_only).

    ``registry_path`` points at the user-global project registry so the dashboard can enumerate and
    switch targets. When it is None the server runs in *pinned* single-project mode: an ephemeral
    one-entry registry built from ``root`` (used by the direct-construction unit tests, and whenever
    no registry file backs the session), and ``/api/project/select`` is refused.
    """

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], *, root: Path, read_only: bool, registry_path: Path | None = None
    ) -> None:
        super().__init__(address, DashboardHandler)
        self.root = Path(root)
        self.read_only = read_only
        self.registry_path = registry_path
        self.token = secrets.token_hex(16)

    def registry(self) -> registry_mod.Registry:
        """The live registry: the backing file when one is set and non-empty, else a pinned view of root."""
        if self.registry_path is not None:
            reg = registry_mod.load(self.registry_path)
            if reg.projects:
                return reg
        name = registry_mod.slug_for(self.root)
        return registry_mod.Registry(projects={name: self.root}, active=name)

    def active_root(self) -> Path:
        """The repository every read/action currently targets — the registry's active project, or root."""
        reg = self.registry()
        if reg.active and reg.active in reg.projects:
            return reg.projects[reg.active]
        return self.root


def _tail(text: str) -> str:
    return text if len(text) <= _OUTPUT_LIMIT else "…(truncated)…\n" + text[-_OUTPUT_LIMIT:]


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer  # narrowed: only DashboardServer constructs this handler

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the terminal quiet; the page itself is the monitor

    def _send(self, code: HTTPStatus, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: HTTPStatus, obj: dict[str, object]) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path == "/":
            try:
                page = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
            except OSError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"cannot read ui_assets/index.html: {exc}"})
                return
            # The per-start token and mode are the only server-rendered values in the page.
            page = page.replace("__TOKEN__", self.server.token).replace(
                "__READ_ONLY__", "true" if self.server.read_only else "false"
            )
            self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/assets/"):
            name = self.path[len("/assets/") :]
            ctype = _ASSET_TYPES.get(name)
            if ctype is None:  # exact-name allowlist — anything else (including ../) is a 404
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                body = (ASSETS_DIR / name).read_bytes()
            except OSError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"cannot read ui_assets/{name}: {exc}"})
                return
            self._send(HTTPStatus.OK, body, ctype)
        elif self.path == "/api/status":
            status: dict[str, object]
            try:
                status = status_api.collect_status(self.server.active_root())
            except Exception as exc:  # the dashboard must stay up even over a broken SSOT
                status = {"error": f"{type(exc).__name__}: {exc}"}
            self._send_json(HTTPStatus.OK, status)
        elif self.path == "/api/projects":
            reg = self.server.registry()
            self._send_json(HTTPStatus.OK, {"projects": reg.entries(), "active": reg.active})
        elif self.path.startswith("/api/review/"):
            # The path carries only a gate *name*; review_api maps it to a fixed set of repo files
            # server-side, so a traversal-shaped suffix is just an unknown gate (404).
            gate = self.path[len("/api/review/") :]
            try:
                payload = review_api.collect_review(self.server.active_root(), gate)
            except review_api.ReviewError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            except Exception as exc:  # same posture as /api/status: a broken repo must not 500 the pane
                payload = {"error": f"{type(exc).__name__}: {exc}"}
            self._send_json(HTTPStatus.OK, payload)
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _post_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise UiActionError(HTTPStatus.BAD_REQUEST, "request body must be JSON") from None
        if not isinstance(raw, dict):
            raise UiActionError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return raw

    def do_POST(self) -> None:
        if self.server.read_only:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "server is running with --read-only"})
            return
        if self.headers.get("X-AgentLoop-Token") != self.server.token:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "missing or invalid X-AgentLoop-Token"})
            return
        try:
            if self.path == "/api/gate/approve":
                self._approve_gate(self._post_body())
            elif self.path == "/api/run":
                self._run_action(self._post_body())
            elif self.path == "/api/project/select":
                self._select_project(self._post_body())
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except UiActionError as exc:
            self._send_json(exc.status, {"error": exc.message})

    def _approve_gate(self, body: dict[str, object]) -> None:
        # Delegates to approve.py — the single sanctioned pending→approved write path — so the
        # UI click stamps the gate line, advances current_phase, and records the gate_approved
        # event exactly as `agentloop approve` does. ApproveError already carries the HTTP status.
        gate = str(body.get("gate") or "")
        root = self.server.active_root()
        try:
            today = approve.record_approval(
                gate,
                "(via ui)",
                state_path=str(root / ".agentloop" / "state.md"),
                events_path=str(root / ".agentloop" / "events.ndjson"),
            )
        except approve.ApproveError as exc:
            raise UiActionError(exc.status, exc.message) from None
        self._send_json(HTTPStatus.OK, {"ok": True, "gate": gate, "date": today})

    def _select_project(self, body: dict[str, object]) -> None:
        # The client sends only a registered *name*; the server maps it to a root through the
        # registry, never a browser-supplied path — so switching cannot widen the dashboard's reach.
        if self.server.registry_path is None:
            raise UiActionError(
                HTTPStatus.CONFLICT, "this dashboard is pinned to a single repository — no project registry"
            )
        name = str(body.get("name") or "")
        reg = registry_mod.load(self.server.registry_path)
        try:
            reg.set_active(name)
        except registry_mod.RegistryError as exc:
            raise UiActionError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        registry_mod.save(reg, self.server.registry_path)
        self._send_json(HTTPStatus.OK, {"ok": True, "active": name, "root": str(reg.projects[name])})

    def _run_action(self, body: dict[str, object]) -> None:
        action = str(body.get("action") or "")
        params = body.get("params")
        argv = action_argv(action, params if isinstance(params, dict) else {})
        try:
            proc = subprocess.run(
                argv, cwd=self.server.active_root(), capture_output=True, text=True, timeout=ACTION_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            raise UiActionError(
                HTTPStatus.GATEWAY_TIMEOUT, f"'{' '.join(argv)}' timed out after {ACTION_TIMEOUT_SEC}s"
            ) from None
        except OSError as exc:
            raise UiActionError(HTTPStatus.INTERNAL_SERVER_ERROR, f"cannot launch '{argv[0]}': {exc}") from None
        self._send_json(
            HTTPStatus.OK,
            {
                "action": action,
                "argv": argv,
                "exit_code": proc.returncode,
                "stdout": _tail(proc.stdout),
                "stderr": _tail(proc.stderr),
            },
        )


def open_mode(no_open: bool, term_program: str | None) -> str:
    """Decide how to surface the URL at startup (kept pure so the choice is unit-testable).

    Inside VS Code's integrated terminal (`TERM_PROGRAM=vscode`) opening the system browser is the
    wrong target — under WSL it launches the Windows browser, away from the editor — so we point the
    user at the built-in Simple Browser / Ports preview instead. `--no-open` overrides both.
    """
    if no_open:
        return "none"
    if term_program == "vscode":
        return "vscode"
    return "browser"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local dashboard for the AgentLoop SSOT")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765; 0 = ephemeral)")
    parser.add_argument("--root", "--repo", dest="root", default="", help="repository root (default: discovered)")
    parser.add_argument(
        "--no-open", action="store_true", help="do not open the browser automatically (VS Code: use Simple Browser)"
    )
    parser.add_argument("--read-only", action="store_true", help="disable the action endpoints (view only)")
    parser.add_argument("--once", action="store_true", help="print the status JSON and exit (no server)")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="required to bind a non-loopback host while the write endpoints are enabled",
    )
    args = parser.parse_args(argv)
    common.configure_logging()
    reg_path = registry_mod.registry_path()
    try:
        root = repo_mod.get(args.root or None).root
        discovered = True
    except repo_mod.RepoNotFoundError as exc:
        # Not inside a repo — but a populated registry still lets us serve a picked project, so the
        # dashboard can be launched from anywhere once projects have been registered.
        reg = registry_mod.load(reg_path)
        picked = reg.active or next(iter(reg.projects), None)
        if picked is None:
            logger.error(str(exc))
            return 1
        root = reg.projects[picked]
        discovered = False

    if args.once:  # a scripting/inspection path — never mutate the registry
        print(json.dumps(status_api.collect_status(root), ensure_ascii=False, indent=2))
        return 0

    if args.host not in _LOOPBACK_HOSTS and not args.read_only and not args.allow_remote:
        logger.error(
            f"refusing to bind {args.host}: the write endpoints (gate approval, actions) would be"
            " exposed beyond this machine. Add --read-only, or --allow-remote if that is intended."
        )
        return 2

    if discovered:
        # Record the launched-from repo and make it the active target (least surprise: `agentloop ui`
        # inside repo B shows repo B), while keeping every other registered project in the switcher.
        reg = registry_mod.load(reg_path)
        reg.active = registry_mod.record_use(reg, root)
        registry_mod.save(reg, reg_path)

    server = DashboardServer((args.host, args.port), root=root, read_only=args.read_only, registry_path=reg_path)
    url = f"http://{args.host}:{server.server_address[1]}/"
    mode = " (read-only)" if args.read_only else ""
    print(f"AgentLoop dashboard{mode}: {url}  — Ctrl+C to stop")
    if open_mode(args.no_open, os.environ.get("TERM_PROGRAM")) == "vscode":
        print("  VS Code detected — open it inside the editor: Ctrl+Shift+P → 'Simple Browser: Show'")
        print("  and paste the URL above (or use the PORTS panel's 'Preview in Editor').")
    elif open_mode(args.no_open, os.environ.get("TERM_PROGRAM")) == "browser":
        webbrowser.open(url)  # best-effort; under WSL the printed URL is the fallback
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
