// Activity: the state.md log tables and the operations console (kept away from the review flow —
// destructive actions live on this tab, approval lives on the Review tab).

import { READ_ONLY, post, tableFrom } from "/assets/api.js";

export function resolveEsc(id) {
  const note = prompt("Resolution note for escalation #" + id + ":");
  if (note !== null) post("/api/run", { action:"events_resolve", params:{ id:id, note:note } });
}
function runDoctor() { post("/api/run", { action:"doctor", params:{} }); }
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

export function renderLogs(d) {
  const sec = document.getElementById("logsSection"), lg = d.logs || {};
  const spec = lg.speculative || [], rb = lg.rollback || [];
  if (!spec.length && !rb.length) { sec.style.display = "none"; return; }
  sec.style.display = "";
  let html = "";
  if (spec.length)
    html += '<div class="subhead">SPECULATIVE WORK</div>' +
      tableFrom(["Date", "Gate", "Content", "Deliverable", "Adopt?"], spec);
  if (rb.length)
    html += '<div class="subhead" style="margin-top:.6rem">ROLL-BACK HISTORY</div>' +
      tableFrom(["Date", "Target", "Gates reset", "Reason"], rb);
  document.getElementById("logs").innerHTML = html;
}

export function renderOps() {
  if (READ_ONLY) {
    document.getElementById("ops").innerHTML =
      '<div class="empty">Running with --read-only; actions are disabled.</div>';
    return;
  }
  const phases = ["requirements", "design", "tasks", "build"].map(p => "<option>" + p + "</option>").join("");
  document.getElementById("ops").innerHTML =
    '<div class="ops"><button onclick="runDoctor()">agentloop doctor</button></div>' +
    '<div class="ops" style="margin-top:.6rem">' +
    '<select id="revPhase">' + phases + "</select>" +
    '<input id="revReason" placeholder="revise reason" size="28">' +
    '<button class="danger" onclick="runRevise()">agentloop revise</button>' +
    '<input id="closeSlug" placeholder="cycle slug" size="16">' +
    '<button class="danger" onclick="runCycleClose()">agentloop cycle-close</button></div>';
}

// Named by generated onclick= handlers (module scope is not global scope).
window.resolveEsc = resolveEsc;
window.runDoctor = runDoctor;
window.runRevise = runRevise;
window.runCycleClose = runCycleClose;
