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
  make ui                 # serve on 127.0.0.1:8765 and open the browser
  make ui ARGS=--no-open  # print the URL only
  make ui ARGS=--read-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentloop import approve, revise, status_api
from agentloop import repo as repo_mod

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

    def __init__(self, status: int, message: str) -> None:
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
            raise UiActionError(400, "events_resolve needs an integer 'id'") from None
        note = str(params.get("note") or "")
        return ["make", "events", f"ARGS=--resolve {event_id} --note {shlex.quote(note)}"]
    if action == "revise":
        phase = str(params.get("phase") or "")
        if phase not in revise._PHASE_GATE:
            raise UiActionError(400, f"revise 'phase' must be one of {', '.join(sorted(revise._PHASE_GATE))}")
        reason = str(params.get("reason") or "").strip()
        if not reason:
            raise UiActionError(400, "revise needs a non-empty 'reason'")
        return ["make", "revise", f"ARGS=--to {phase} --reason {shlex.quote(reason)}"]
    if action == "cycle_close":
        slug = str(params.get("slug") or "")
        if not _SLUG_RE.match(slug):
            raise UiActionError(400, "cycle_close 'slug' must match [a-z0-9][a-z0-9-]*")
        return ["make", "cycle-close", f"NAME={slug}"]
    raise UiActionError(400, f"unknown action '{action}'")


class DashboardServer(ThreadingHTTPServer):
    """The HTTP server plus the per-start context the handler needs (root, token, read_only)."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, root: Path, read_only: bool) -> None:
        super().__init__(address, DashboardHandler)
        self.root = root
        self.read_only = read_only
        self.token = secrets.token_hex(16)


def _tail(text: str) -> str:
    return text if len(text) <= _OUTPUT_LIMIT else "…(truncated)…\n" + text[-_OUTPUT_LIMIT:]


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer  # narrowed: only DashboardServer constructs this handler

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the terminal quiet; the page itself is the monitor

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict[str, object]) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path == "/":
            try:
                page = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
            except OSError as exc:
                self._send_json(500, {"error": f"cannot read ui_assets/index.html: {exc}"})
                return
            # The per-start token and mode are the only server-rendered values in the page.
            page = page.replace("__TOKEN__", self.server.token).replace(
                "__READ_ONLY__", "true" if self.server.read_only else "false"
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/assets/"):
            name = self.path[len("/assets/") :]
            ctype = _ASSET_TYPES.get(name)
            if ctype is None:  # exact-name allowlist — anything else (including ../) is a 404
                self._send_json(404, {"error": "not found"})
                return
            try:
                body = (ASSETS_DIR / name).read_bytes()
            except OSError as exc:
                self._send_json(500, {"error": f"cannot read ui_assets/{name}: {exc}"})
                return
            self._send(200, body, ctype)
        elif self.path == "/api/status":
            status: dict[str, object]
            try:
                status = status_api.collect_status(self.server.root)
            except Exception as exc:  # the dashboard must stay up even over a broken SSOT
                status = {"error": f"{type(exc).__name__}: {exc}"}
            self._send_json(200, status)
        else:
            self._send_json(404, {"error": "not found"})

    def _post_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise UiActionError(400, "request body must be JSON") from None
        if not isinstance(raw, dict):
            raise UiActionError(400, "request body must be a JSON object")
        return raw

    def do_POST(self) -> None:
        if self.server.read_only:
            self._send_json(405, {"error": "server is running with --read-only"})
            return
        if self.headers.get("X-AgentLoop-Token") != self.server.token:
            self._send_json(403, {"error": "missing or invalid X-AgentLoop-Token"})
            return
        try:
            if self.path == "/api/gate/approve":
                self._approve_gate(self._post_body())
            elif self.path == "/api/run":
                self._run_action(self._post_body())
            else:
                self._send_json(404, {"error": "not found"})
        except UiActionError as exc:
            self._send_json(exc.status, {"error": exc.message})

    def _approve_gate(self, body: dict[str, object]) -> None:
        # Delegates to approve.py — the single sanctioned pending→approved write path — so the
        # UI click stamps the gate line, advances current_phase, and records the gate_approved
        # event exactly as `make approve` does. ApproveError already carries the HTTP status.
        gate = str(body.get("gate") or "")
        try:
            today = approve.record_approval(
                gate,
                "(via ui)",
                state_path=str(self.server.root / ".agentloop" / "state.md"),
                events_path=str(self.server.root / ".agentloop" / "events.ndjson"),
            )
        except approve.ApproveError as exc:
            raise UiActionError(exc.status, exc.message) from None
        self._send_json(200, {"ok": True, "gate": gate, "date": today})

    def _run_action(self, body: dict[str, object]) -> None:
        action = str(body.get("action") or "")
        params = body.get("params")
        argv = action_argv(action, params if isinstance(params, dict) else {})
        try:
            proc = subprocess.run(
                argv, cwd=self.server.root, capture_output=True, text=True, timeout=ACTION_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            raise UiActionError(504, f"'{' '.join(argv)}' timed out after {ACTION_TIMEOUT_SEC}s") from None
        except OSError as exc:
            raise UiActionError(500, f"cannot launch '{argv[0]}': {exc}") from None
        self._send_json(
            200,
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
    try:
        root = repo_mod.get(args.root or None).root
    except repo_mod.RepoNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.once:
        print(json.dumps(status_api.collect_status(root), ensure_ascii=False, indent=2))
        return 0

    if args.host not in _LOOPBACK_HOSTS and not args.read_only and not args.allow_remote:
        print(
            f"refusing to bind {args.host}: the write endpoints (gate approval, actions) would be"
            " exposed beyond this machine. Add --read-only, or --allow-remote if that is intended.",
            file=sys.stderr,
        )
        return 2

    server = DashboardServer((args.host, args.port), root=root, read_only=args.read_only)
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
