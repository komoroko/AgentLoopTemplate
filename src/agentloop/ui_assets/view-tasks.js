// Tasks: the dependency DAG, the frontier order, task detail, and the traceability table.

import { chip, esc, taskById } from "/assets/api.js";

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

export function showTaskDetail(id) {
  const t = taskById(id), el = document.getElementById("taskDetail");
  if (!t || !el) return;
  el.innerHTML = '<div class="detail"><b class="mono">' + esc(t.id) + "</b> — " + esc(t.title) +
    "<dt>status / kind</dt><div>" + esc(t.status) + " / " + esc(t.kind) + "</div>" +
    '<dt>blockedBy</dt><div class="mono">' + (t.blocked_by.length ? esc(t.blocked_by.join(", ")) : "—") +
    "</div><dt>req</dt><div>" + (t.req ? esc(t.req) : "—") +
    '</div><dt>test</dt><div class="mono">' + (t.test ? esc(t.test) : "—") + "</div></div>";
}

export function renderTasks(d) {
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

export function renderTrace(d) {
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

window.showTaskDetail = showTaskDetail;  // named by generated onclick= handlers
