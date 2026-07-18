// Overview: the lifecycle rail, the next recommended command, and what needs attention.

import { READ_ONLY, chip, esc } from "/assets/api.js";

export function renderStepper(d) {
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
      // The awaiting gate is a link into the review pane — "read, then approve" starts here.
      const inner = mark + " g" + g.index;
      gate = g.name === awaiting
        ? '<a class="rgate await" href="#review" title="Open the gate review">' + inner + "</a>"
        : '<span class="rgate ' + cls + '">' + inner + "</span>";
    }
    const cls = i === idx ? "live" : (idx >= 0 && i < idx ? "past" : "future");
    return '<div class="rphase ' + cls + '"><span class="rnode"></span><span class="rname">' +
      esc(p) + "</span>" + gate + "</div>";
  }).join("");
  document.getElementById("stepper").innerHTML =
    rail + '<span class="rloop" title="delta cycle → agentloop cycle-close">↻</span>';
}

export function renderNext(d) {
  const n = d.next || {};
  const also = (n.also || []).map(a => '<span class="chip">' + esc(a) + "</span>").join(" ");
  const review = n.kind === "run_phase" || n.kind === "close"
    ? "" : ' <a class="chip" href="#review">open review →</a>';
  document.getElementById("next").innerHTML =
    '<div class="console"><span class="prompt">▸</span><code class="cmd">' + esc(n.command) + "</code>" +
    '<button onclick="copyCmd(' + JSON.stringify(n.command || "").replace(/"/g, "&quot;") +
    ', this)">copy</button></div>' +
    '<div class="reason">' + esc(n.reason) + "</div>" +
    (also || review ? '<div style="margin-top:.4rem">' + (also ? "also: " + also : "") + review + "</div>" : "");
}

export function renderAttention(d) {
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
