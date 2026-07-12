"""Local web dashboard for the AgentLoop SSOT — state, gates, tasks, and the next recommended command.

Serves a single self-contained page (inline CSS/JS, no external fetches, offline-safe) over stdlib
`http.server`; the data comes from status_api.collect_status(). Guidance-first: the page shows where the
lifecycle stands and which command to run next. A small **fixed whitelist** of safe operations can also be
executed from the page (gate-approval recording — a human privilege exercised by a human clicking — plus
doctor / events-resolve / revise / cycle-close). The client only ever sends an action id and typed
parameters; command lines are built server-side (`action_argv`), so arbitrary command execution is
structurally impossible. Outward-facing operations (push / PR / merge) are deliberately absent.

Safety layers: binds 127.0.0.1 by default; every POST requires the `X-AgentLoop-Token` header whose value
is generated per server start and embedded only in the served page (a cross-origin page cannot set a custom
header without a CORS preflight, and no CORS headers are ever sent); `--read-only` disables POST entirely.

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
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import revise
import status_api
import yaml

ACTION_TIMEOUT_SEC = 900
_OUTPUT_LIMIT = 8000  # tail shown per stream (failures are summarized, not dumped)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class UiActionError(Exception):
    """A rejected UI action, carrying the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def approve_gate_text(text: str, gate: str, today: str) -> str:
    """Record a human gate approval in the state.md text (pure function).

    Rewrites only the gate's own front-matter line (`<gate>: pending …` → `approved   # <date> (via ui)`),
    the same surgical style as revise.py. Enforces the gate-chain invariant server-side: approving a gate
    whose upstream is still pending is refused, mirroring AGENTS.md gate rule 2's ordering.
    """
    if gate not in status_api.GATE_ORDER:
        raise UiActionError(400, f"unknown gate '{gate}' (one of {', '.join(status_api.GATE_ORDER)})")
    parts = text.split("---", 2)
    if not text.startswith("---") or len(parts) < 3:
        raise UiActionError(500, "state.md has no YAML front-matter")
    loaded = yaml.safe_load(parts[1]) or {}
    raw_gates = loaded.get("gates") if isinstance(loaded, dict) else None
    gates = {str(k): str(v) for k, v in raw_gates.items()} if isinstance(raw_gates, dict) else {}
    if gates.get(gate) == "approved":
        raise UiActionError(409, f"gate '{gate}' is already approved")
    for upstream in status_api.GATE_ORDER[: status_api.GATE_ORDER.index(gate)]:
        if gates.get(upstream) != "approved":
            raise UiActionError(409, f"cannot approve '{gate}': upstream gate '{upstream}' is still pending")
    pattern = re.compile(rf"^(\s*{re.escape(gate)}:\s*)pending\b.*$", re.MULTILINE)
    new_front, n = pattern.subn(rf"\g<1>approved   # {today} (via ui)", parts[1], count=1)
    if n == 0:
        raise UiActionError(500, f"gate line '{gate}: pending' not found in state.md front-matter")
    return f"---{new_front}---{parts[2]}"


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
            page = PAGE_HTML.replace("__TOKEN__", self.server.token).replace(
                "__READ_ONLY__", "true" if self.server.read_only else "false"
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
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
        gate = str(body.get("gate") or "")
        state_path = self.server.root / ".agentloop" / "state.md"
        try:
            text = state_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise UiActionError(500, f"cannot read state.md: {exc}") from None
        today = date.today().isoformat()
        state_path.write_text(approve_gate_text(text, gate, today), encoding="utf-8")
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
    parser.add_argument("--root", default=".", help="repository root holding .agentloop/ (default: cwd)")
    parser.add_argument(
        "--no-open", action="store_true", help="do not open the browser automatically (VS Code: use Simple Browser)"
    )
    parser.add_argument("--read-only", action="store_true", help="disable the action endpoints (view only)")
    parser.add_argument("--once", action="store_true", help="print the status JSON and exit (no server)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.once:
        print(json.dumps(status_api.collect_status(root), ensure_ascii=False, indent=2))
        return 0

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


# The whole page: one document, inline CSS/JS, zero external references (works offline). The status
# palette matches dag._STATUS_CLASSDEFS so the chips read the same as the Mermaid view.
PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentLoop dashboard</title>
<style>
  :root {
    --bg:#eef1f5; --panel:#ffffff; --panel2:#f7f9fc; --ink:#1c2230; --muted:#5b6472; --border:#dde2e9;
    --accent:#3b6ef5; --accent-ink:#ffffff; --code-bg:#1e2330; --code-ink:#e8edf6; --ok:#1f9d55; --bad:#e0243a;
    --todo-bg:#e9ecef; --todo-bd:#a2a9b2; --todo-ink:#3a3f47;
    --prog-bg:#cfe8ff; --prog-bd:#3b82f6; --prog-ink:#06325e;
    --blk-bg:#ffd6d6; --blk-bd:#ee2233; --blk-ink:#7a0010;
    --rev-bg:#ffe9c7; --rev-bd:#f59e0b; --rev-ink:#7a4a00;
    --done-bg:#d7f5dd; --done-bd:#22a04b; --done-ink:#0b3d1d;
    --shadow:0 1px 2px rgba(20,26,38,.06),0 2px 8px rgba(20,26,38,.05);
  }
  /* dark palette (duplicated for the two triggers): OS auto-dark unless the toggle forced light,
     and the explicit [data-theme="dark"] toggle which wins over the media query */
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
    --bg:#0f1218; --panel:#181c24; --panel2:#12151c; --ink:#e6ebf3; --muted:#98a2b3; --border:#2a313d;
    --accent:#6a97ff; --accent-ink:#0a0d13; --code-bg:#0c0f15; --code-ink:#e6ebf3; --ok:#41c980; --bad:#ff6472;
    --todo-bg:#2a2f38; --todo-bd:#525a67; --todo-ink:#c4cbd6;
    --prog-bg:#173456; --prog-bd:#3b82f6; --prog-ink:#bcd9ff;
    --blk-bg:#4a1620; --blk-bd:#ee2233; --blk-ink:#ffb9c0;
    --rev-bg:#463109; --rev-bd:#f59e0b; --rev-ink:#ffd894;
    --done-bg:#123723; --done-bd:#22a04b; --done-ink:#a6f0c1; --shadow:0 1px 2px rgba(0,0,0,.4);
  } }
  :root[data-theme="dark"] {
    --bg:#0f1218; --panel:#181c24; --panel2:#12151c; --ink:#e6ebf3; --muted:#98a2b3; --border:#2a313d;
    --accent:#6a97ff; --accent-ink:#0a0d13; --code-bg:#0c0f15; --code-ink:#e6ebf3; --ok:#41c980; --bad:#ff6472;
    --todo-bg:#2a2f38; --todo-bd:#525a67; --todo-ink:#c4cbd6;
    --prog-bg:#173456; --prog-bd:#3b82f6; --prog-ink:#bcd9ff;
    --blk-bg:#4a1620; --blk-bd:#ee2233; --blk-ink:#ffb9c0;
    --rev-bg:#463109; --rev-bd:#f59e0b; --rev-ink:#ffd894;
    --done-bg:#123723; --done-bd:#22a04b; --done-ink:#a6f0c1; --shadow:0 1px 2px rgba(0,0,0,.4);
  }
  * { box-sizing:border-box; }
  body { font-family:system-ui,-apple-system,"Segoe UI",sans-serif; margin:0; background:var(--bg);
         color:var(--ink); line-height:1.45; }
  header { position:sticky; top:0; z-index:5; background:var(--panel); border-bottom:1px solid var(--border);
           padding:.6rem 1.2rem; display:flex; gap:1rem; align-items:center; flex-wrap:wrap; box-shadow:var(--shadow); }
  header h1 { font-size:1rem; margin:0; font-weight:700; letter-spacing:-.01em; }
  header .meta { color:var(--muted); font-size:.85rem; }
  header .spacer { flex:1; }
  .dot { display:inline-block; width:.55rem; height:.55rem; border-radius:50%; background:var(--ok);
         margin-right:.3rem; vertical-align:middle; }
  .dot.off { background:var(--bad); }
  main { max-width:1160px; margin:1.1rem auto; padding:0 1rem; display:grid; gap:1rem;
         grid-template-columns:repeat(2,minmax(0,1fr)); }
  section { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:.9rem 1.1rem;
            box-shadow:var(--shadow); }
  .col-full { grid-column:1 / -1; }
  @media (max-width:820px){ main { grid-template-columns:1fr; } }
  h2 { font-size:.72rem; text-transform:uppercase; letter-spacing:.07em; color:var(--muted);
       margin:0 0 .7rem; font-weight:700; }
  /* phase stepper */
  .stepper { display:flex; flex-wrap:wrap; gap:.3rem; align-items:stretch; }
  .step { flex:1; min-width:5.2rem; padding:.4rem .5rem; border-radius:8px; border:1px solid var(--border);
          font-size:.82rem; background:var(--panel2); color:var(--muted); text-align:center; }
  .step .nm { font-weight:600; color:var(--ink); text-transform:capitalize; }
  .step.current { background:var(--accent); border-color:var(--accent); }
  .step.current .nm, .step.current .gatemark { color:var(--accent-ink); }
  .step.past { background:var(--done-bg); border-color:var(--done-bd); }
  .step.past .nm { color:var(--done-ink); }
  .gatemark { display:block; font-size:.72rem; margin-top:.15rem; }
  /* next-action card */
  .next { border-left:4px solid var(--accent); }
  .cmdrow { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; margin:.1rem 0 .5rem; }
  code.cmd { background:var(--code-bg); color:var(--code-ink); padding:.5rem .8rem; border-radius:7px;
             font-size:1rem; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  button { border:1px solid var(--border); background:var(--panel2); color:var(--ink); border-radius:7px;
           padding:.35rem .7rem; cursor:pointer; font-size:.85rem; transition:border-color .12s,color .12s; }
  button:hover { border-color:var(--accent); color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:var(--accent-ink); }
  button.primary:hover { color:var(--accent-ink); filter:brightness(1.06); }
  button.danger:hover { border-color:var(--bad); color:var(--bad); }
  button.ghost { background:transparent; }
  .reason { color:var(--muted); font-size:.9rem; }
  .chip { display:inline-block; padding:.15rem .55rem; border-radius:999px; border:1px solid var(--todo-bd);
          background:var(--todo-bg); color:var(--todo-ink); font-size:.8rem; margin:.12rem;
          font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .chip.clk { cursor:pointer; }
  .chip.clk:hover { filter:brightness(.96); }
  .chip.in_progress { background:var(--prog-bg); border-color:var(--prog-bd); color:var(--prog-ink); }
  .chip.blocked { background:var(--blk-bg); border-color:var(--blk-bd); color:var(--blk-ink); }
  .chip.needs-revision { background:var(--rev-bg); border-color:var(--rev-bd); color:var(--rev-ink); }
  .chip.done { background:var(--done-bg); border-color:var(--done-bd); color:var(--done-ink); }
  .chip.critical { border-width:2.5px; font-weight:700; }
  .pills { display:flex; gap:.35rem; flex-wrap:wrap; margin-bottom:.6rem; }
  .pill { font-size:.78rem; padding:.2rem .5rem; border-radius:6px; border:1px solid var(--border); }
  .pill b { font-variant-numeric:tabular-nums; }
  .scroll { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:.85rem; }
  th, td { border-bottom:1px solid var(--border); text-align:left; padding:.35rem .5rem; }
  th { color:var(--muted); font-weight:600; }
  tr.clk { cursor:pointer; }
  tr.clk:hover td { background:var(--panel2); }
  .mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .warn { color:var(--rev-ink); background:var(--rev-bg); border:1px solid var(--rev-bd); border-radius:7px;
          padding:.4rem .6rem; font-size:.85rem; margin:.25rem 0; }
  .bad { color:var(--blk-ink); background:var(--blk-bg); border:1px solid var(--blk-bd); border-radius:7px;
         padding:.35rem .6rem; font-size:.85rem; margin:.2rem 0; }
  .ops { display:flex; gap:.6rem; flex-wrap:wrap; align-items:center; }
  .ops input, .ops select { border:1px solid var(--border); background:var(--panel2); color:var(--ink);
                            border-radius:7px; padding:.3rem .5rem; font-size:.85rem; }
  pre.out { background:var(--code-bg); color:var(--code-ink); border-radius:7px; padding:.7rem; font-size:.78rem;
            overflow:auto; white-space:pre-wrap; max-height:20rem; margin-top:.7rem;
            font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  .detail { margin-top:.6rem; padding:.6rem .75rem; background:var(--panel2); border:1px solid var(--border);
            border-radius:8px; font-size:.85rem; }
  .detail dt { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.04em;
               margin-top:.4rem; }
  .empty { color:var(--muted); font-size:.85rem; }
  .layer { margin:.15rem 0; font-size:.85rem; }
  .layer b { color:var(--muted); font-weight:600; margin-right:.4rem; }
  /* dependency graph (inline SVG) */
  svg.dag { display:block; min-width:100%; }
  svg.dag .edge { stroke:var(--border); stroke-width:1.5; fill:none; }
  svg.dag .edge.crit { stroke:var(--accent); stroke-width:2.5; }
  svg.dag .nd { stroke:var(--todo-bd); stroke-width:1.5; rx:6; cursor:pointer; }
  svg.dag .nd.crit { stroke:var(--accent); stroke-width:2.5; }
  svg.dag text { font-size:12px; fill:var(--ink); pointer-events:none;
                 font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  svg.dag .nd.todo { fill:var(--todo-bg); } svg.dag .nd.in_progress { fill:var(--prog-bg); }
  svg.dag .nd.blocked { fill:var(--blk-bg); } svg.dag .nd.needs-revision { fill:var(--rev-bg); }
  svg.dag .nd.done { fill:var(--done-bg); }
  /* toast */
  #toasts { position:fixed; right:1rem; bottom:1rem; z-index:20; display:flex; flex-direction:column; gap:.4rem; }
  .toast { background:var(--panel); border:1px solid var(--border); border-left:4px solid var(--accent);
           border-radius:8px; padding:.5rem .8rem; font-size:.85rem; box-shadow:var(--shadow); max-width:22rem;
           animation:fade .25s ease; }
  .toast.ok { border-left-color:var(--ok); } .toast.err { border-left-color:var(--bad); }
  @keyframes fade { from { opacity:0; transform:translateY(6px); } }
</style>
</head>
<body>
<header>
  <h1>AgentLoop</h1>
  <span class="meta" id="meta">loading…</span>
  <span class="spacer"></span>
  <span class="meta"><span class="dot" id="dot"></span><span id="ago">—</span></span>
  <button class="ghost" id="refreshBtn" title="Refresh now">⟳</button>
  <button class="ghost" id="themeBtn" title="Toggle theme">◐</button>
</header>
<main>
  <section class="col-full"><h2>Lifecycle</h2><div class="stepper" id="stepper"></div></section>
  <section class="next col-full"><h2>Next action</h2><div id="next"></div></section>
  <section class="col-full"><h2>Tasks</h2><div id="tasks"></div></section>
  <section id="traceSection" style="display:none"><h2>Traceability (requirements → tasks)</h2>
    <div id="trace"></div></section>
  <section><h2>Needs attention</h2><div id="attention"></div></section>
  <section id="logsSection" style="display:none"><h2>Logs</h2><div id="logs"></div></section>
  <section class="col-full"><h2>Operations</h2><div id="ops"></div>
    <pre class="out" id="out" style="display:none"></pre></section>
</main>
<div id="toasts"></div>
<script>
"use strict";
const TOKEN = "__TOKEN__";
const READ_ONLY = __READ_ONLY__;
let lastPayload = "";
let DATA = null;       // the last parsed status (for click-to-detail lookups)
let lastGen = null;    // generated_at of the last status (drives the "updated Ns ago" label)

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 3200);
}

async function post(path, body) {
  const out = document.getElementById("out");
  out.style.display = "block";
  out.textContent = "running…";
  try {
    const res = await fetch(path, { method:"POST",
      headers:{ "Content-Type":"application/json", "X-AgentLoop-Token":TOKEN },
      body: JSON.stringify(body) });
    const data = await res.json();
    if (data.error) { out.textContent = "ERROR: " + data.error; toast(data.error, "err"); return; }
    if ("exit_code" in data) {
      out.textContent = "$ " + data.argv.join(" ") + "\\n(exit " + data.exit_code + ")\\n\\n"
        + (data.stdout || "") + (data.stderr ? "\\n[stderr]\\n" + data.stderr : "");
      toast((data.exit_code === 0 ? "✓ " : "✗ exit " + data.exit_code + " — ") + data.argv.join(" "),
        data.exit_code === 0 ? "ok" : "err");
    } else {
      out.textContent = JSON.stringify(data, null, 2);
      toast("✓ " + (data.gate ? ("gate " + data.gate + " approved") : "done"), "ok");
    }
    lastPayload = "";  // force a re-render with the fresh state
    refresh();
  } catch (e) { out.textContent = "request failed: " + e; toast("request failed", "err"); }
}

function approveGate(name, index) {
  if (confirm("Record HUMAN approval for gate " + index + " (" + name + ") in state.md?\\n" +
              "Only do this after reviewing the phase deliverable."))
    post("/api/gate/approve", { gate: name });
}
function runDoctor() { post("/api/run", { action:"doctor", params:{} }); }
function resolveEsc(id) {
  const note = prompt("Resolution note for escalation #" + id + ":");
  if (note !== null) post("/api/run", { action:"events_resolve", params:{ id:id, note:note } });
}
function runRevise() {
  const phase = document.getElementById("revPhase").value;
  const reason = document.getElementById("revReason").value.trim();
  if (!reason) { alert("revise needs a reason"); return; }
  if (confirm("Roll back to '" + phase + "'? Gates from there onward reset to pending."))
    post("/api/run", { action:"revise", params:{ phase:phase, reason:reason } });
}
function runCycleClose() {
  const slug = document.getElementById("closeSlug").value.trim();
  if (!slug) { alert("enter a cycle slug (e.g. payment-refactor)"); return; }
  if (confirm("Close the cycle as '" + slug + "'? Deliverables are archived and gates reset."))
    post("/api/run", { action:"cycle_close", params:{ slug:slug } });
}
function copyCmd(cmd, btn) {
  if (navigator.clipboard) navigator.clipboard.writeText(cmd);
  if (btn) { const o = btn.textContent; btn.textContent = "✓ copied"; setTimeout(() => btn.textContent = o, 1200); }
}

function taskById(id) { return (DATA && DATA.tasks) ? DATA.tasks.tasks.find(x => x.id === id) : null; }
function chip(id, status, critical, clickable) {
  const clk = clickable ? " clk" : "";
  const onc = clickable ? ' onclick="showTaskDetail(\\'' + id + '\\')"' : "";
  return '<span class="chip ' + esc(status) + (critical ? " critical" : "") + clk + '" title="' +
    esc(status) + '"' + onc + ">" + esc(id) + "</span>";
}

// ---- theme (auto → dark → light → auto), persisted in localStorage ----
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  const val = !cur ? "dark" : (cur === "dark" ? "light" : "");
  if (val) localStorage.setItem("agentloop-theme", val); else localStorage.removeItem("agentloop-theme");
  applyTheme(val);
}
applyTheme(localStorage.getItem("agentloop-theme") || "");

function renderStepper(d) {
  const gatesByPhase = {};
  d.gates.forEach(g => { gatesByPhase[g.phase] = g; });
  const idx = d.phase_order.indexOf(d.current_phase);
  document.getElementById("stepper").innerHTML = d.phase_order.map((p, i) => {
    const g = gatesByPhase[p];
    const mark = g ? '<span class="gatemark">' + (g.status === "approved" ? "✓" : "○") + " gate" +
      g.index + "</span>" : "";
    const cls = i === idx ? "current" : (idx >= 0 && i < idx ? "past" : "");
    return '<div class="step ' + cls + '"><span class="nm">' + esc(p) + "</span>" + mark + "</div>";
  }).join("");
}

function renderNext(d) {
  const n = d.next || {};
  const also = (n.also || []).map(a => '<span class="chip">' + esc(a) + "</span>").join(" ");
  document.getElementById("next").innerHTML =
    '<div class="cmdrow"><code class="cmd">' + esc(n.command) + "</code>" +
    '<button onclick="copyCmd(' + JSON.stringify(n.command || "").replace(/"/g, "&quot;") +
    ', this)">copy</button></div>' +
    '<div class="reason">' + esc(n.reason) + "</div>" +
    (also ? '<div style="margin-top:.4rem">also: ' + also + "</div>" : "");
}

// ---- dependency graph as inline SVG (no external namespace literal → stays offline-safe) ----
function buildDag(t, byId) {
  const crit = new Set(t.critical_path);
  const pos = {};
  t.layers.forEach((ids, c) => ids.forEach((id, r) => { pos[id] = { c:c, r:r }; }));
  const colW = 170, rowH = 52, nodeW = 132, nodeH = 34, padX = 12, padY = 12;
  const cols = t.layers.length || 1;
  const rows = Math.max(1, ...t.layers.map(l => l.length));
  const W = cols * colW + padX * 2, H = rows * rowH + padY * 2;
  const X = id => padX + pos[id].c * colW, Y = id => padY + pos[id].r * rowH;
  let edges = "", nodes = "";
  t.tasks.forEach(tk => (tk.blocked_by || []).forEach(dep => {
    if (!pos[dep] || !pos[tk.id]) return;
    const x1 = X(dep) + nodeW, y1 = Y(dep) + nodeH / 2, x2 = X(tk.id), y2 = Y(tk.id) + nodeH / 2;
    const cx = (x1 + x2) / 2;
    const c = (crit.has(dep) && crit.has(tk.id)) ? " crit" : "";
    edges += '<path class="edge' + c + '" d="M' + x1 + " " + y1 + " C" + cx + " " + y1 + " " +
      cx + " " + y2 + " " + x2 + " " + y2 + '"/>';
  }));
  t.layers.forEach(ids => ids.forEach(id => {
    const tk = byId[id] || { status:"todo" };
    const c = crit.has(id) ? " crit" : "", x = X(id), y = Y(id);
    nodes += '<g onclick="showTaskDetail(\\'' + id + '\\')">' +
      '<rect class="nd ' + esc(tk.status) + c + '" x="' + x + '" y="' + y + '" width="' + nodeW +
      '" height="' + nodeH + '" rx="6"/>' +
      '<text x="' + (x + 8) + '" y="' + (y + 21) + '">' + esc(id) + "</text></g>";
  }));
  return '<div class="scroll"><svg class="dag" viewBox="0 0 ' + W + " " + H + '" width="' + W +
    '" height="' + H + '">' + edges + nodes + "</svg></div>";
}

function showTaskDetail(id) {
  const t = taskById(id), el = document.getElementById("taskDetail");
  if (!t || !el) return;
  el.innerHTML = '<div class="detail"><b class="mono">' + esc(t.id) + "</b> — " + esc(t.title) +
    "<dt>status / kind</dt><div>" + esc(t.status) + " / " + esc(t.kind) + "</div>" +
    '<dt>blockedBy</dt><div class="mono">' + (t.blocked_by.length ? esc(t.blocked_by.join(", ")) : "—") +
    "</div><dt>req</dt><div>" + (t.req ? esc(t.req) : "—") +
    '</div><dt>test</dt><div class="mono">' + (t.test ? esc(t.test) : "—") + "</div></div>";
}

function renderTasks(d) {
  const el = document.getElementById("tasks");
  const t = d.tasks;
  if (!t) { el.innerHTML = '<div class="empty">No tasks.yaml yet (created by /tasks).</div>'; return; }
  const byId = {}; t.tasks.forEach(x => { byId[x.id] = x; });
  const order = ["todo", "in_progress", "blocked", "needs-revision", "done"];
  const pills = '<div class="pills">' + order.map(s => '<span class="chip ' + s + '">' + esc(s) + " " +
    (t.counts[s] || 0) + "</span>").join("") + '<span class="pill">total ' + t.total + "</span></div>";
  const graph = t.tasks.length ? buildDag(t, byId) : '<div class="empty">(no tasks)</div>';
  const frontier = t.frontier.length
    ? '<div class="scroll"><table><tr><th>ID</th><th>Title</th><th>Kind</th><th>fan-out</th></tr>' +
      t.frontier.map(f => '<tr class="clk" onclick="showTaskDetail(\\'' + f.id + '\\')"><td class="mono">' +
        esc(f.id) + "</td><td>" + esc(f.title) + "</td><td>" + esc(f.kind) + "</td><td>" + f.fan_out +
        "</td></tr>").join("") + "</table></div>"
    : '<div class="empty">(no startable todo)</div>';
  el.innerHTML = pills + graph +
    '<div style="margin-top:.6rem;font-size:.72rem;color:var(--muted);font-weight:700">' +
    "FRONTIER (optimal order)</div>" + frontier + '<div id="taskDetail"></div>';
}

function renderTrace(d) {
  const sec = document.getElementById("traceSection"), tr = d.trace;
  if (!tr) { sec.style.display = "none"; return; }
  sec.style.display = "";
  const rows = tr.requirements.map(r => {
    const dz = r.design === null ? "—"
      : (r.design ? '<span style="color:var(--ok)">✓</span>' : '<span style="color:var(--bad)">✗</span>');
    const tasks = r.tasks.length
      ? r.tasks.map(id => chip(id, (taskById(id) || {}).status || "todo", false, true)).join(" ")
      : '<span class="empty">(no task)</span>';
    return '<tr><td class="mono">' + esc(r.id) + (r.nfr ? ' <span class="empty">NFR</span>' : "") +
      "</td><td>" + dz + "</td><td>" + tasks + "</td></tr>";
  }).join("");
  const findings = tr.findings.length
    ? tr.findings.map(f => '<div class="bad">' + esc(f) + "</div>").join("")
    : '<div class="empty">Every requirement is linked to a task' + (tr.design_checked ? " and design." : ".") +
      "</div>";
  document.getElementById("trace").innerHTML =
    '<div class="scroll"><table><tr><th>Requirement</th><th>design</th><th>tasks</th></tr>' +
    rows + "</table></div>" + findings;
}

function tableFrom(headers, rows) {
  const th = "<tr>" + headers.map(h => "<th>" + esc(h) + "</th>").join("") + "</tr>";
  const tr = rows.map(r => "<tr>" + r.map(c => "<td>" + esc(c) + "</td>").join("") + "</tr>").join("");
  return '<div class="scroll"><table>' + th + tr + "</table></div>";
}
function renderLogs(d) {
  const sec = document.getElementById("logsSection"), lg = d.logs || {};
  const spec = lg.speculative || [], rb = lg.rollback || [];
  if (!spec.length && !rb.length) { sec.style.display = "none"; return; }
  sec.style.display = "";
  let html = "";
  if (spec.length)
    html += '<div style="font-size:.75rem;color:var(--muted);font-weight:700;margin:.1rem 0 .3rem">' +
      "SPECULATIVE WORK</div>" + tableFrom(["Date", "Gate", "Content", "Deliverable", "Adopt?"], spec);
  if (rb.length)
    html += '<div style="font-size:.75rem;color:var(--muted);font-weight:700;margin:.6rem 0 .3rem">' +
      "ROLL-BACK HISTORY</div>" + tableFrom(["Date", "Target", "Gates reset", "Reason"], rb);
  document.getElementById("logs").innerHTML = html;
}

function renderAttention(d) {
  const el = document.getElementById("attention");
  let html = "";
  (d.warnings || []).forEach(w => { html += '<div class="warn">' + esc(w) + "</div>"; });
  const open = (d.escalations || {}).open || [];
  if (open.length) {
    html += '<div class="scroll"><table><tr><th>ID</th><th>Date</th><th>Event</th><th>Task</th><th>Detail</th>' +
      (READ_ONLY ? "" : "<th></th>") + "</tr>" +
      open.map(e => "<tr><td>" + e.id + "</td><td>" + esc(e.date) + "</td><td>" + esc(e.event) +
        "</td><td>" + esc(e.task || "-") + "</td><td>" + esc(e.detail || "-") + "</td>" +
        (READ_ONLY ? "" : '<td><button onclick="resolveEsc(' + e.id + ')">resolve</button></td>') +
        "</tr>").join("") + "</table></div>";
  }
  const t = d.tasks || {};
  if ((t.needs_revision || []).length)
    html += '<div style="margin-top:.4rem">needs-revision: ' +
      t.needs_revision.map(id => chip(id, "needs-revision", false, true)).join(" ") + "</div>";
  if ((t.blocked || []).length)
    html += '<div style="margin-top:.4rem">blocked: ' +
      t.blocked.map(id => chip(id, "blocked", false, true)).join(" ") + "</div>";
  el.innerHTML = html || '<div class="empty">Nothing needs attention.</div>';
}

function renderOps(d) {
  if (READ_ONLY) {
    document.getElementById("ops").innerHTML =
      '<div class="empty">Running with --read-only; actions are disabled.</div>';
    return;
  }
  const pending = d.gates.find(g => g.status !== "approved");
  const gateBtn = pending
    ? '<button class="primary" onclick="approveGate(\\'' + pending.name + '\\',' + pending.index +
      ')">Approve gate ' + pending.index + " (" + esc(pending.name) + ")</button>"
    : '<span class="empty">all gates approved</span>';
  const phases = ["requirements", "design", "tasks", "build"].map(p => "<option>" + p + "</option>").join("");
  document.getElementById("ops").innerHTML =
    '<div class="ops">' + gateBtn + '<button onclick="runDoctor()">make doctor</button></div>' +
    '<div class="ops" style="margin-top:.6rem">' +
    '<select id="revPhase">' + phases + "</select>" +
    '<input id="revReason" placeholder="revise reason" size="28">' +
    '<button class="danger" onclick="runRevise()">make revise</button>' +
    '<input id="closeSlug" placeholder="cycle slug" size="16">' +
    '<button class="danger" onclick="runCycleClose()">make cycle-close</button></div>';
}

function tickAgo() {
  const el = document.getElementById("ago");
  if (!lastGen) { el.textContent = "—"; return; }
  const secs = Math.max(0, Math.round((Date.now() - new Date(lastGen).getTime()) / 1000));
  el.textContent = secs < 60 ? ("updated " + secs + "s ago") : ("updated " + Math.round(secs / 60) + "m ago");
}

async function refresh() {
  const dot = document.getElementById("dot");
  try {
    const res = await fetch("/api/status");
    const text = await res.text();
    dot.classList.remove("off");
    if (text === lastPayload) return;  // unchanged: skip the re-render (keeps inputs alive)
    lastPayload = text;
    const d = JSON.parse(text); DATA = d; lastGen = d.generated_at;
    if (d.error) { document.getElementById("meta").textContent = "status error: " + d.error; return; }
    document.getElementById("meta").textContent =
      (d.project || "(no project)") + " · " + (d.branch || "-") + " · phase " + (d.current_phase || "-");
    renderStepper(d); renderNext(d); renderTasks(d); renderTrace(d); renderAttention(d); renderLogs(d);
    const a = document.activeElement;  // don't clobber an ops input mid-typing
    if (!(a && a.closest && a.closest("#ops") && a.tagName === "INPUT")) renderOps(d);
    tickAgo();
  } catch (e) {
    dot.classList.add("off");
    document.getElementById("ago").textContent = "disconnected";
  }
}
document.getElementById("themeBtn").onclick = toggleTheme;
document.getElementById("refreshBtn").onclick = () => { lastPayload = ""; refresh(); };
refresh();
setInterval(refresh, 3000);
setInterval(tickAgo, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
