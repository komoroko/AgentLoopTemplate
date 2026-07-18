// Entry module: hash-routed tabs, the 3-second status poll, theme, and the project switcher.
// Rendering lives in the view-* modules; shared plumbing in api.js.

import { READ_ONLY, TOKEN, esc, state, toast } from "/assets/api.js";
import { renderAttention, renderNext, renderStepper } from "/assets/view-overview.js";
import { renderReview } from "/assets/view-review.js";
import { renderTasks, renderTrace } from "/assets/view-tasks.js";
import { renderLogs, renderOps } from "/assets/view-activity.js";

// ---- tabs (location.hash is the router; unknown/empty hash lands on overview) ----
const VIEWS = ["overview", "review", "tasks", "activity"];
export function currentView() {
  const h = location.hash.replace("#", "");
  return VIEWS.includes(h) ? h : "overview";
}
function showView() {
  const v = currentView();
  VIEWS.forEach(name => {
    document.getElementById("view-" + name).style.display = name === v ? "" : "none";
    document.querySelector('#tabs a[data-view="' + name + '"]').classList.toggle("active", name === v);
  });
  document.dispatchEvent(new CustomEvent("agentloop:view", { detail: v }));
}

function updateReviewBadge(d) {
  const badge = document.getElementById("reviewBadge");
  const awaiting = (d.gates || []).find(g => g.status !== "approved");
  if (awaiting) { badge.textContent = "◆ g" + awaiting.index; badge.style.display = ""; }
  else badge.style.display = "none";
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
    state.lastPayload = ""; await refresh(); loadProjects();
  } catch (e) { toast("switch failed", "err"); }
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

// ---- the status poll ----
function tickAgo() {
  const el = document.getElementById("ago");
  if (!state.lastGen) { el.textContent = "—"; return; }
  const secs = Math.max(0, Math.round((Date.now() - new Date(state.lastGen).getTime()) / 1000));
  el.textContent = secs < 60 ? ("updated " + secs + "s ago") : ("updated " + Math.round(secs / 60) + "m ago");
}

async function refresh() {
  const dot = document.getElementById("dot");
  try {
    const res = await fetch("/api/status");
    const text = await res.text();
    dot.classList.remove("off");
    if (text === state.lastPayload) return;  // unchanged: skip the re-render (keeps inputs alive)
    state.lastPayload = text;
    const d = JSON.parse(text); state.data = d; state.lastGen = d.generated_at;
    if (d.error) { document.getElementById("meta").textContent = "status error: " + d.error; return; }
    document.getElementById("meta").textContent =
      (d.project || "(no project)") + " · " + (d.branch || "-") + " · phase " + (d.current_phase || "-");
    updateReviewBadge(d);
    renderStepper(d); renderNext(d); renderTasks(d); renderTrace(d); renderAttention(d);
    renderLogs(d); renderReview(d);
    const a = document.activeElement;  // don't clobber an ops input mid-typing
    if (!(a && a.closest && a.closest("#ops") && a.tagName === "INPUT")) renderOps(d);
    tickAgo();
  } catch (e) {
    dot.classList.add("off");
    document.getElementById("ago").textContent = "disconnected";
  }
}

document.getElementById("themeBtn").onclick = toggleTheme;
document.getElementById("refreshBtn").onclick = () => { state.lastPayload = ""; refresh(); };
document.getElementById("projectSelect").onchange = (e) => selectProject(e.target.value);
document.addEventListener("agentloop:refresh", () => refresh());
window.addEventListener("hashchange", showView);
showView();
refresh();
loadProjects();
setInterval(refresh, 3000);
setInterval(tickAgo, 1000);
