// Shared plumbing for every view: token-carrying POST, escaping, toasts, and the last status.
// ES module — loaded only via app.js; nothing here touches the DOM except #out and #toasts.

export const TOKEN = window.TOKEN;
export const READ_ONLY = window.READ_ONLY;

// The single mutable snapshot the views render from (app.js writes it on every poll).
export const state = { data: null, lastPayload: "", lastGen: null };

export const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

export function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 3200);
}

// POSTs land their outcome in #out and ask app.js for a fresh status via a DOM event
// (no circular import between the entry module and this one).
export async function post(path, body) {
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
    state.lastPayload = "";  // force a re-render with the fresh state
    document.dispatchEvent(new CustomEvent("agentloop:refresh"));
  } catch (e) { out.textContent = "request failed: " + e; toast("request failed", "err"); }
}

export function copyCmd(cmd, btn) {
  if (navigator.clipboard) navigator.clipboard.writeText(cmd);
  if (btn) { const o = btn.textContent; btn.textContent = "✓ copied"; setTimeout(() => btn.textContent = o, 1200); }
}

export function taskById(id) {
  return (state.data && state.data.tasks) ? state.data.tasks.tasks.find(x => x.id === id) : null;
}

export function chip(id, status, critical, clickable) {
  const clk = clickable ? " clk" : "";
  const onc = clickable ? ' onclick="showTaskDetail(\'' + id + '\')"' : "";
  return '<span class="chip ' + esc(status) + (critical ? " critical" : "") + clk + '" title="' +
    esc(status) + '"' + onc + ">" + esc(id) + "</span>";
}

export function tableFrom(headers, rows) {
  const th = "<tr>" + headers.map(h => "<th>" + esc(h) + "</th>").join("") + "</tr>";
  const tr = rows.map(r => "<tr>" + r.map(c => "<td>" + esc(c) + "</td>").join("") + "</tr>").join("");
  return '<div class="scroll"><table>' + th + tr + "</table></div>";
}

// Generated HTML uses inline onclick= handlers; modules are not global scope, so the few
// functions those handlers name are published on window explicitly (here and in the views).
window.copyCmd = copyCmd;
