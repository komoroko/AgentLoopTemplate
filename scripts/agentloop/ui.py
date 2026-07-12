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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local dashboard for the AgentLoop SSOT")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765; 0 = ephemeral)")
    parser.add_argument("--root", default=".", help="repository root holding .agentloop/ (default: cwd)")
    parser.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
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
    if not args.no_open:
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
  :root { --border:#d0d4da; --muted:#667085; --accent:#3b82f6; }
  * { box-sizing:border-box; }
  body { font-family:system-ui,sans-serif; margin:0; background:#f5f6f8; color:#1f2430; }
  header { background:#1f2430; color:#fff; padding:.7rem 1.2rem; display:flex; gap:1.2rem;
           align-items:baseline; flex-wrap:wrap; }
  header h1 { font-size:1.05rem; margin:0; font-weight:600; }
  header .meta { color:#aab2c0; font-size:.85rem; }
  main { max-width:1080px; margin:1rem auto; padding:0 1rem; display:grid; gap:1rem; }
  section { background:#fff; border:1px solid var(--border); border-radius:8px; padding:.9rem 1.1rem; }
  h2 { font-size:.8rem; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 .6rem; }
  /* phase stepper */
  .stepper { display:flex; flex-wrap:wrap; gap:.25rem; align-items:center; }
  .step { padding:.35rem .7rem; border-radius:999px; border:1px solid var(--border); font-size:.85rem;
          background:#fafbfc; color:var(--muted); }
  .step.current { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
  .step.past { background:#d7f5dd; border-color:#22a04b; color:#0b3d1d; }
  .arrow { color:var(--muted); font-size:.8rem; }
  .gatemark { font-size:.75rem; margin-left:.35rem; }
  /* next-action card */
  .next { border-left:4px solid var(--accent); }
  .cmdrow { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; margin:.3rem 0 .5rem; }
  code.cmd { background:#1f2430; color:#e7ecf3; padding:.45rem .8rem; border-radius:6px; font-size:1rem; }
  button { border:1px solid var(--border); background:#fff; border-radius:6px; padding:.35rem .7rem;
           cursor:pointer; font-size:.85rem; }
  button:hover { border-color:var(--accent); color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.danger:hover { border-color:#ee2233; color:#ee2233; }
  .reason { color:var(--muted); font-size:.9rem; }
  .chip { display:inline-block; padding:.15rem .55rem; border-radius:999px; border:1px solid #999;
          background:#eee; color:#333; font-size:.8rem; margin:.12rem; }
  .chip.in_progress { background:#cfe8ff; border-color:#3b82f6; color:#06325e; }
  .chip.blocked { background:#ffd6d6; border-color:#ee2233; color:#7a0010; }
  .chip.needs-revision { background:#ffe9c7; border-color:#f59e0b; color:#7a4a00; }
  .chip.done { background:#d7f5dd; border-color:#22a04b; color:#0b3d1d; }
  .chip.critical { border-width:2.5px; font-weight:600; }
  table { border-collapse:collapse; width:100%; font-size:.85rem; }
  th, td { border-bottom:1px solid var(--border); text-align:left; padding:.35rem .5rem; }
  th { color:var(--muted); font-weight:600; }
  .warn { color:#7a4a00; background:#ffe9c7; border-radius:6px; padding:.4rem .6rem; font-size:.85rem;
          margin:.25rem 0; }
  .ops { display:flex; gap:.6rem; flex-wrap:wrap; align-items:center; }
  .ops input, .ops select { border:1px solid var(--border); border-radius:6px; padding:.3rem .5rem;
                            font-size:.85rem; }
  pre.out { background:#1f2430; color:#e7ecf3; border-radius:6px; padding:.7rem; font-size:.78rem;
            overflow-x:auto; white-space:pre-wrap; max-height:20rem; overflow-y:auto; }
  .empty { color:var(--muted); font-size:.85rem; }
  .layer { margin:.15rem 0; font-size:.85rem; }
  .layer b { color:var(--muted); font-weight:600; margin-right:.4rem; }
</style>
</head>
<body>
<header>
  <h1>AgentLoop dashboard</h1>
  <span class="meta" id="meta">loading…</span>
</header>
<main>
  <section><h2>Lifecycle</h2><div class="stepper" id="stepper"></div></section>
  <section class="next"><h2>Next action</h2><div id="next"></div></section>
  <section><h2>Tasks</h2><div id="tasks"></div></section>
  <section><h2>Needs attention</h2><div id="attention"></div></section>
  <section id="opsSection"><h2>Operations</h2><div id="ops"></div>
    <pre class="out" id="out" style="display:none"></pre></section>
</main>
<script>
"use strict";
const TOKEN = "__TOKEN__";
const READ_ONLY = __READ_ONLY__;
let lastPayload = "";

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function post(path, body) {
  const out = document.getElementById("out");
  out.style.display = "block";
  out.textContent = "running…";
  try {
    const res = await fetch(path, { method:"POST",
      headers:{ "Content-Type":"application/json", "X-AgentLoop-Token":TOKEN },
      body: JSON.stringify(body) });
    const data = await res.json();
    if (data.error) { out.textContent = "ERROR: " + data.error; return; }
    if ("exit_code" in data) {
      out.textContent = "$ " + data.argv.join(" ") + "\\n(exit " + data.exit_code + ")\\n\\n"
        + (data.stdout || "") + (data.stderr ? "\\n[stderr]\\n" + data.stderr : "");
    } else {
      out.textContent = JSON.stringify(data, null, 2);
    }
    lastPayload = "";  // force a re-render with the fresh state
    refresh();
  } catch (e) { out.textContent = "request failed: " + e; }
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
function copyCmd(cmd) { navigator.clipboard && navigator.clipboard.writeText(cmd); }

function renderStepper(d) {
  const gatesByPhase = {};
  d.gates.forEach(g => { gatesByPhase[g.phase] = g; });
  const idx = d.phase_order.indexOf(d.current_phase);
  const parts = d.phase_order.map((p, i) => {
    const g = gatesByPhase[p];
    const mark = g ? '<span class="gatemark">' + (g.status === "approved" ? "✓" : "○") +
      "gate" + g.index + "</span>" : "";
    const cls = i === idx ? "current" : (idx >= 0 && i < idx ? "past" : "");
    return '<span class="step ' + cls + '">' + esc(p) + mark + "</span>";
  });
  document.getElementById("stepper").innerHTML = parts.join('<span class="arrow">→</span>');
}

function renderNext(d) {
  const n = d.next || {};
  const also = (n.also || []).map(a =>
    '<span class="chip">' + esc(a) + "</span>").join(" ");
  document.getElementById("next").innerHTML =
    '<div class="cmdrow"><code class="cmd">' + esc(n.command) + "</code>" +
    '<button onclick="copyCmd(' + JSON.stringify(n.command || "").replace(/"/g, "&quot;") +
    ')">copy</button></div>' +
    '<div class="reason">' + esc(n.reason) + "</div>" +
    (also ? '<div style="margin-top:.4rem">also: ' + also + "</div>" : "");
}

function chip(id, status, critical) {
  return '<span class="chip ' + esc(status) + (critical ? " critical" : "") + '" title="' +
    esc(status) + '">' + esc(id) + "</span>";
}

function renderTasks(d) {
  const el = document.getElementById("tasks");
  const t = d.tasks;
  if (!t) { el.innerHTML = '<div class="empty">No tasks.yaml yet (created by /tasks).</div>'; return; }
  const byId = {}; t.tasks.forEach(x => { byId[x.id] = x; });
  const crit = new Set(t.critical_path);
  const counts = Object.entries(t.counts).map(([k, v]) => esc(k) + "=" + v).join(" / ");
  const layers = t.layers.map((ids, i) =>
    '<div class="layer"><b>L' + i + "</b>" +
    ids.map(id => chip(id, byId[id] ? byId[id].status : "todo", crit.has(id))).join(" ") + "</div>").join("");
  const frontier = t.frontier.length
    ? "<table><tr><th>ID</th><th>Title</th><th>Kind</th><th>fan-out</th></tr>" +
      t.frontier.map(f => "<tr><td>" + esc(f.id) + "</td><td>" + esc(f.title) + "</td><td>" +
        esc(f.kind) + "</td><td>" + f.fan_out + "</td></tr>").join("") + "</table>"
    : '<div class="empty">(no startable todo)</div>';
  el.innerHTML = "<div>" + counts + " (total " + t.total + ")</div>" +
    '<div style="margin:.5rem 0">' + layers + "</div>" +
    "<div><b style='font-size:.8rem;color:var(--muted)'>FRONTIER (optimal order)</b>" + frontier + "</div>";
}

function renderAttention(d) {
  const el = document.getElementById("attention");
  let html = "";
  (d.warnings || []).forEach(w => { html += '<div class="warn">' + esc(w) + "</div>"; });
  const open = (d.escalations || {}).open || [];
  if (open.length) {
    html += "<table><tr><th>ID</th><th>Date</th><th>Event</th><th>Task</th><th>Detail</th>" +
      (READ_ONLY ? "" : "<th></th>") + "</tr>" +
      open.map(e => "<tr><td>" + e.id + "</td><td>" + esc(e.date) + "</td><td>" + esc(e.event) +
        "</td><td>" + esc(e.task || "-") + "</td><td>" + esc(e.detail || "-") + "</td>" +
        (READ_ONLY ? "" : '<td><button onclick="resolveEsc(' + e.id + ')">resolve</button></td>') +
        "</tr>").join("") + "</table>";
  }
  const t = d.tasks || {};
  if ((t.needs_revision || []).length)
    html += "<div>needs-revision: " + t.needs_revision.map(id => chip(id, "needs-revision")).join(" ") + "</div>";
  if ((t.blocked || []).length)
    html += "<div>blocked: " + t.blocked.map(id => chip(id, "blocked")).join(" ") + "</div>";
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
  const phases = ["requirements", "design", "tasks", "build"].map(p =>
    "<option>" + p + "</option>").join("");
  document.getElementById("ops").innerHTML =
    '<div class="ops">' + gateBtn +
    '<button onclick="runDoctor()">make doctor</button></div>' +
    '<div class="ops" style="margin-top:.6rem">' +
    '<select id="revPhase">' + phases + "</select>" +
    '<input id="revReason" placeholder="revise reason" size="28">' +
    '<button class="danger" onclick="runRevise()">make revise</button>' +
    '<input id="closeSlug" placeholder="cycle slug" size="16">' +
    '<button class="danger" onclick="runCycleClose()">make cycle-close</button></div>';
}

async function refresh() {
  try {
    const res = await fetch("/api/status");
    const text = await res.text();
    if (text === lastPayload) return;  // unchanged: skip the re-render (keeps inputs alive)
    lastPayload = text;
    const d = JSON.parse(text);
    if (d.error) {
      document.getElementById("meta").textContent = "status error: " + d.error;
      return;
    }
    document.getElementById("meta").textContent =
      (d.project || "(no project)") + " · branch " + (d.branch || "-") +
      " · phase " + (d.current_phase || "-") + " · updated " + (d.updated_at || "-");
    renderStepper(d); renderNext(d); renderTasks(d); renderAttention(d); renderOps(d);
  } catch (e) {
    document.getElementById("meta").textContent = "connection lost: " + e;
  }
}
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
