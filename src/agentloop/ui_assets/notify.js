// Approval-wait notifications: the human's response latency is the loop's bottleneck, so the
// dashboard actively signals "it's your turn" instead of waiting to be looked at. Three layers:
// browser notifications (opt-in via the bell — permission needs a user gesture), the tab title
// badge, and a canvas-drawn favicon (offline self-contained: no image assets, no external URLs).
// Transitions are detected client-side by diffing consecutive /api/status payloads — no new API.

import { state, toast } from "/assets/api.js";

let enabled = localStorage.getItem("agentloop-notify") === "on";
let prev = null;  // last snapshot; transitions only fire within the same project

function snapshot(d) {
  const awaiting = (d.gates || []).find(g => g.status !== "approved") || null;
  const counts = ((d.tasks || {}).counts) || {};
  return {
    project: d.project || "",
    awaiting: awaiting ? awaiting.name : null,
    awaitingIndex: awaiting ? awaiting.index : 0,
    openEsc: (d.escalations || {}).total_open || 0,
    inProgress: counts.in_progress || 0,
    done: counts.done || 0,
    needsRevision: counts["needs-revision"] || 0,
  };
}

function notify(body) {
  if (!enabled || typeof Notification === "undefined" || Notification.permission !== "granted") return;
  try { new Notification("AgentLoop — " + ((state.data || {}).project || "dashboard"), { body }); }
  catch (e) { /* headless/denied environments: the title/favicon badges still carry the signal */ }
}

function favicon(s) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 32;
  const g = canvas.getContext("2d");
  if (!g) return;
  // teal = quiet loop, amber = a gate waits on the human, red = an escalation waits on the human
  const styles = getComputedStyle(document.documentElement);
  const color = s.openEsc > 0 ? (styles.getPropertyValue("--bad") || "#c23b2f")
    : s.awaiting ? (styles.getPropertyValue("--gate") || "#b3760f")
    : (styles.getPropertyValue("--accent") || "#0c7d73");
  g.beginPath();
  g.arc(16, 16, 13, 0, Math.PI * 2);
  g.fillStyle = color.trim();
  g.fill();
  let link = document.querySelector('link[rel="icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.href = canvas.toDataURL("image/png");
}

function badges(s) {
  const flag = s.openEsc > 0 ? "(!" + s.openEsc + ") " : (s.awaiting ? "(◆g" + s.awaitingIndex + ") " : "");
  document.title = flag + "AgentLoop — " + (s.project || "dashboard");
  favicon(s);
}

function onStatus(d) {
  const s = snapshot(d);
  if (prev && prev.project === s.project) {  // a project switch resets the baseline silently
    if (s.awaiting && s.awaiting !== prev.awaiting)
      notify("gate " + s.awaitingIndex + " (" + s.awaiting + ") is now the gate under decision");
    if (s.openEsc > prev.openEsc)
      notify("escalation opened — " + s.openEsc + " now waiting on you");
    if (s.needsRevision > prev.needsRevision)
      notify("a task went needs-revision — reconcile via /tasks");
    if (prev.inProgress > 0 && s.inProgress === 0 && s.done > prev.done)
      notify("build tasks finished — the implementation is ready for review");
  }
  prev = s;
  badges(s);
}

async function toggle() {
  if (!enabled) {
    if (typeof Notification !== "undefined" && Notification.permission !== "granted") {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast("browser notifications are blocked", "err"); return; }
    }
    enabled = true;
    localStorage.setItem("agentloop-notify", "on");
    toast("notifications on", "ok");
  } else {
    enabled = false;
    localStorage.setItem("agentloop-notify", "off");
    toast("notifications off");
  }
  paintBell();
}

function paintBell() {
  const btn = document.getElementById("bellBtn");
  btn.textContent = enabled ? "🔔" : "🔕";
  btn.title = enabled ? "Notifications on (click to disable)" : "Notify me when a gate or escalation waits";
}

document.getElementById("bellBtn").onclick = toggle;
document.addEventListener("agentloop:status", e => onStatus(e.detail));
paintBell();
