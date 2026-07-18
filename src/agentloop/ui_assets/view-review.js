// Review: read the gate's deliverables — self-assessment pinned on top — then approve, in one pane.
// Deliverable HTML arrives pre-rendered from the server (mdlite, escape-first); the diff arrives as
// raw text and is escaped here line by line. Nothing in this module puts unescaped input in the DOM.

import { READ_ONLY, esc, post, state } from "/assets/api.js";

const DIFF_ID = "__diff__";  // the synthetic "change set" entry on the build gate's list
let current = null;    // selected gate name
let review = null;     // last /api/review payload for `current`
let selected = null;   // selected deliverable id
let tabVisible = false;
let fetching = false;      // one review fetch in flight at a time (polls must not stack them)
let reviewProject = null;  // which project `review` was fetched for (switcher invalidation)
const readSets = {};   // "project:gate" -> Set of deliverable ids the human opened (client-side only)

function readSet() {
  const key = ((state.data || {}).project || "") + ":" + current;
  return (readSets[key] = readSets[key] || new Set());
}

function defaultGate() {
  const gates = (state.data || {}).gates || [];
  return ((gates.find(g => g.status !== "approved") || gates[0]) || {}).name || null;
}

async function fetchReview() {
  if (!current || fetching) return;
  fetching = true;
  try {
    const res = await fetch("/api/review/" + current);
    review = await res.json();
  } catch (e) { review = { error: "request failed: " + e }; }
  finally { fetching = false; }
  reviewProject = (state.data || {}).project || null;
  if (!review.error) {
    const items = mainEntries();
    if (!items.some(x => x.id === selected)) selected = (items[0] || {}).id || null;
    if (selected) readSet().add(selected);
  }
  paint();
}

function selectGate(name) {
  if (name === current) return;
  current = name; review = null; selected = null;
  paint();  // bar feedback right away…
  fetchReview();  // …content when it lands
}

function selectDeliverable(id) {
  selected = id;
  readSet().add(id);
  paint();
}

function approveCurrent() {
  const unread = mainEntries().filter(x => !readSet().has(x.id)).map(x => x.label);
  let msg = "Record HUMAN approval for gate " + review.index + " (" + current + ") in state.md?";
  if (unread.length) msg += "\n\nNot opened here yet:\n  " + unread.join("\n  ");
  msg += "\n\nOnly approve after actually reviewing the deliverables.";
  if (confirm(msg)) post("/api/gate/approve", { gate: current });
}

// The reviewable entries on the left rail: the gate's deliverables, plus the synthetic change-set
// entry on the build gate (the diff IS gate ④'s main deliverable).
function mainEntries() {
  if (!review || review.error) return [];
  const items = [];
  if (review.gate === "build" && review.diff)
    items.push({ id: DIFF_ID, label: "change set (git diff)", exists: !review.diff.error });
  return items.concat(review.deliverables || []);
}

function listHtml() {
  const rs = readSet();
  const item = (e, isCtx) =>
    '<div class="rv-item' + (e.id === selected ? " active" : "") + (e.exists === false ? " missing" : "") +
    '" onclick="revSelect(' + JSON.stringify(e.id).replace(/"/g, "&quot;") + ')">' +
    (isCtx ? "" : '<span class="rv-read">' + (rs.has(e.id) ? "✓" : "·") + "</span>") +
    esc(e.label) + (e.exists === false ? " (missing)" : "") + "</div>";
  let html = '<div class="subhead">DELIVERABLES</div>' + mainEntries().map(e => item(e, false)).join("");
  if ((review.context || []).length)
    html += '<div class="subhead" style="margin-top:.6rem">CONTEXT</div>' +
      review.context.map(e => item(e, true)).join("");
  return html;
}

function saHtml(sa) {
  if (!sa) return "";
  const conf = sa.confidence
    ? '<span class="conf ' + esc(sa.confidence) + '">' + esc(sa.confidence) + "</span>"
    : '<span class="conf unset">unset</span>';
  return '<div class="sa"><div class="subhead">SELF-ASSESSMENT ' + conf + "</div>" + sa.html + "</div>";
}

function diffHtml(diff, meta) {
  if (diff.error) return '<div class="warn">' + esc(diff.error) + "</div>";
  let badge = "";
  if (meta) {
    badge = meta.fresh
      ? '<div class="okline">✓ security review is bound to this HEAD (' + esc((meta.head || "").slice(0, 12)) + ")</div>"
      : '<div class="warn">security review is missing or stale (reviewed: ' +
        esc(meta.reviewed_head ? meta.reviewed_head.slice(0, 12) : "none") + ", HEAD: " +
        esc((meta.head || "").slice(0, 12)) + ") — run /security-review before approving</div>";
  }
  if (diff.log)
    return badge + '<div class="empty">' + esc(diff.note || "") + '</div><pre class="patch">' +
      diff.log.map(esc).join("\n") + "</pre>";
  const files = (diff.name_status || []).map(r =>
    '<tr><td class="mono">' + esc(r[0]) + '</td><td class="mono">' + esc(r[1]) + "</td></tr>").join("");
  const patch = diff.patch.split("\n").map(line => {
    const cls = line.startsWith("+++") || line.startsWith("---") ? "file"
      : line.startsWith("@@") ? "hunk"
      : line.startsWith("+") ? "add"
      : line.startsWith("-") ? "del" : "";
    return '<span class="dl ' + cls + '">' + esc(line) + "</span>";
  }).join("\n");
  return badge +
    '<div class="subhead">FILES (base ' + esc((diff.base || "").slice(0, 12)) + " on " + esc(diff.base_ref || "") +
    ')</div><div class="scroll"><table>' + files + "</table></div>" +
    (diff.truncated ? '<div class="warn">patch truncated at 200KB — review the rest in your editor</div>' : "") +
    '<div class="subhead" style="margin-top:.6rem">PATCH</div><pre class="patch">' + patch + "</pre>";
}

function bodyHtml() {
  if (selected === DIFF_ID) return diffHtml(review.diff, review.review_meta);
  const e = mainEntries().concat(review.context || []).find(x => x.id === selected);
  if (!e) return '<div class="empty">Select a deliverable.</div>';
  if (e.exists === false)
    return '<div class="warn">' + esc(e.label) + " does not exist yet — the phase has not produced it.</div>";
  return saHtml(e.self_assessment) +
    (e.truncated ? '<div class="warn">truncated at 300KB — open the file for the rest</div>' : "") +
    '<div class="md">' + e.html + "</div>" +
    (e.mtime ? '<div class="empty" style="margin-top:.5rem">last modified ' + esc(e.mtime) + "</div>" : "");
}

function footerHtml() {
  if (READ_ONLY) return '<span class="empty">read-only dashboard — approval happens elsewhere</span>';
  if (review.status === "approved") return '<span class="okline">✓ gate ' + review.index + " already approved</span>";
  if (!review.is_awaiting)
    return '<span class="empty">not the gate under decision (awaiting: ' + esc(review.awaiting || "none") +
      ")</span>";
  let warn = "";
  if (review.gate === "release" && review.open_escalations)
    warn = '<span class="warn" style="margin-right:.6rem">' + review.open_escalations +
      " open escalation(s) — resolve before the release decision</span>";
  return warn + '<button class="primary" onclick="revApprove()">Approve gate ' + review.index +
    " (" + esc(current) + ")</button>";
}

function barHtml() {
  const gates = (state.data || {}).gates || [];
  const awaiting = defaultGate();
  return '<div class="gatebar">' + gates.map(g => {
    const mark = g.status === "approved" ? "✓" : (g.name === awaiting ? "◆" : "○");
    return '<button class="gatebtn' + (g.name === current ? " active" : "") + '" onclick="revGate(\'' +
      g.name + '\')">' + mark + " g" + g.index + " " + esc(g.name) + "</button>";
  }).join("") + "</div>";
}

function paint() {
  const el = document.getElementById("review");
  if (!state.data) { el.innerHTML = '<div class="empty">waiting for status…</div>'; return; }
  if (!current) current = defaultGate();
  let inner = barHtml();
  if (!review) inner += '<div class="empty">loading…</div>';
  else if (review.error) inner += '<div class="warn">' + esc(review.error) + "</div>";
  else inner +=
    '<div class="rv-grid"><aside class="rv-list">' + listHtml() + '</aside>' +
    '<div class="rv-body">' + bodyHtml() + "</div></div>" +
    '<div class="approvebar">' + footerHtml() + "</div>";
  el.innerHTML = inner;
}

// Status polls repaint cheaply (awaiting/approved may have moved); deliverables refetch only on
// tab entry, gate switch, or after a POST — never on the 3-second poll. The one exception: when
// the tab opened before the first status arrived, the first poll completes the deferred fetch.
export function renderReview() {
  if (!tabVisible) return;
  if (!review || reviewProject !== ((state.data || {}).project || null)) {
    if (!current) current = defaultGate();
    if (current) fetchReview();
    return;
  }
  paint();
}

document.addEventListener("agentloop:view", e => {
  tabVisible = e.detail === "review";
  if (tabVisible) { if (!current) current = defaultGate(); fetchReview(); }
});
document.addEventListener("agentloop:refresh", () => { if (tabVisible) fetchReview(); });

window.revGate = selectGate;
window.revSelect = selectDeliverable;
window.revApprove = approveCurrent;
