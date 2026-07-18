"use strict";
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
      out.textContent = "$ " + data.argv.join(" ") + "\n(exit " + data.exit_code + ")\n\n"
        + (data.stdout || "") + (data.stderr ? "\n[stderr]\n" + data.stderr : "");
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

// ---- project switcher (populated from /api/projects; switching persists the active target) ----
async function loadProjects() {
  const sel = document.getElementById("projectSelect");
  try {
    const res = await fetch("/api/projects");
    const d = await res.json();
    if (!d.projects || !d.projects.length) { sel.style.display = "none"; return; }
    sel.innerHTML = d.projects.map(p =>
      '<option value="' + esc(p.name) + '"' + (p.active ? " selected" : "") +
      (p.exists ? "" : " disabled") + ">" + esc(p.name) + (p.exists ? "" : " (missing)") + "</option>"
    ).join("");
    // Switching writes the active target to the user registry, so it needs the write path; a
    // read-only dashboard shows the current target but cannot change it.
    sel.disabled = READ_ONLY;
    sel.title = READ_ONLY ? "Target project (read-only: cannot switch)" : "Switch target project";
    sel.style.display = "";
  } catch (e) { sel.style.display = "none"; }
}
async function selectProject(name) {
  try {
    const res = await fetch("/api/project/select", { method:"POST",
      headers:{ "Content-Type":"application/json", "X-AgentLoop-Token":TOKEN },
      body: JSON.stringify({ name }) });
    const d = await res.json();
    if (d.error) { toast(d.error, "err"); loadProjects(); return; }
    toast("→ " + name, "ok");
    lastPayload = ""; await refresh(); loadProjects();
  } catch (e) { toast("switch failed", "err"); }
}

function approveGate(name, index) {
  if (confirm("Record HUMAN approval for gate " + index + " (" + name + ") in state.md?\n" +
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
  const onc = clickable ? ' onclick="showTaskDetail(\'' + id + '\')"' : "";
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
  const awaiting = (d.gates.find(g => g.status !== "approved") || {}).name;  // the gate the human is at now
  const idx = d.phase_order.indexOf(d.current_phase);
  const rail = d.phase_order.map((p, i) => {
    const g = gatesByPhase[p];
    let gate = "";
    if (g) {
      const cls = g.status === "approved" ? "approved" : (g.name === awaiting ? "await" : "");
      const mark = g.status === "approved" ? "✓" : (g.name === awaiting ? "◆" : "○");
      gate = '<span class="rgate ' + cls + '">' + mark + " g" + g.index + "</span>";
    }
    const cls = i === idx ? "live" : (idx >= 0 && i < idx ? "past" : "future");
    return '<div class="rphase ' + cls + '"><span class="rnode"></span><span class="rname">' +
      esc(p) + "</span>" + gate + "</div>";
  }).join("");
  document.getElementById("stepper").innerHTML =
    rail + '<span class="rloop" title="delta cycle → agentloop cycle-close">↻</span>';
}

function renderNext(d) {
  const n = d.next || {};
  const also = (n.also || []).map(a => '<span class="chip">' + esc(a) + "</span>").join(" ");
  document.getElementById("next").innerHTML =
    '<div class="console"><span class="prompt">▸</span><code class="cmd">' + esc(n.command) + "</code>" +
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
    nodes += '<g onclick="showTaskDetail(\'' + id + '\')">' +
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
      t.frontier.map(f => '<tr class="clk" onclick="showTaskDetail(\'' + f.id + '\')"><td class="mono">' +
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
    ? '<button class="primary" onclick="approveGate(\'' + pending.name + '\',' + pending.index +
      ')">Approve gate ' + pending.index + " (" + esc(pending.name) + ")</button>"
    : '<span class="empty">all gates approved</span>';
  const phases = ["requirements", "design", "tasks", "build"].map(p => "<option>" + p + "</option>").join("");
  document.getElementById("ops").innerHTML =
    '<div class="ops">' + gateBtn + '<button onclick="runDoctor()">agentloop doctor</button></div>' +
    '<div class="ops" style="margin-top:.6rem">' +
    '<select id="revPhase">' + phases + "</select>" +
    '<input id="revReason" placeholder="revise reason" size="28">' +
    '<button class="danger" onclick="runRevise()">agentloop revise</button>' +
    '<input id="closeSlug" placeholder="cycle slug" size="16">' +
    '<button class="danger" onclick="runCycleClose()">agentloop cycle-close</button></div>';
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
document.getElementById("projectSelect").onchange = (e) => selectProject(e.target.value);
refresh();
loadProjects();
setInterval(refresh, 3000);
setInterval(tickAgo, 1000);
