// Review: read the gate's deliverables — self-assessment pinned on top — then approve, in one pane.
// Deliverable HTML arrives pre-rendered from the server (mdlite, escape-first); the diff arrives as
// raw text and is escaped here line by line. Nothing in this module puts unescaped input in the DOM.

import { READ_ONLY, TOKEN, awaitingGate, esc, post, state, toast } from "/assets/api.js";

const DIFF_ID = "__diff__";  // the synthetic "change set" entry on the build gate's list
let current = null;    // selected gate name
let review = null;     // last /api/review payload for `current`
let session = null;    // last /api/review/session payload (the build gate's Challenge-first state)
let selected = null;   // selected deliverable id
let tabVisible = false;
let fetchSeq = 0;          // newest request wins; older responses are dropped on arrival
let reviewProject = null;  // which project `review` was fetched for (switcher invalidation)
const readSets = {};   // "project:gate" -> Set of deliverable ids the human opened (client-side only)

function readSet() {
  const key = ((state.data || {}).project || "") + ":" + current;
  return (readSets[key] = readSets[key] || new Set());
}

function defaultGate() {
  const gates = (state.data || {}).gates || [];
  return ((awaitingGate(state.data) || gates[0]) || {}).name || null;
}

// Every response is tagged with the request that asked for it. A gate clicked while an earlier
// fetch is still in flight must not be dropped (the pane would keep showing the old gate's
// deliverables under the new gate's name — and the approval footer is computed from this payload,
// so the human could approve one gate having read another). Newest request wins; stale responses
// are discarded, never painted.
async function fetchReview() {
  if (!current) return;
  const gate = current, seq = ++fetchSeq;
  let payload;
  try {
    const res = await fetch("/api/review/" + gate);
    payload = await res.json();
  } catch (e) { payload = { error: "request failed: " + e }; }
  if (seq !== fetchSeq) return;  // superseded by a later selection
  review = payload;
  reviewProject = (state.data || {}).project || null;
  if (!review.error) {
    const items = mainEntries();
    if (!items.some(x => x.id === selected)) selected = (items[0] || {}).id || null;
    if (selected) readSet().add(selected);
  }
  // Gate ④ is Challenge-first: the machine review decides whether the human may even see
  // Expected/Actual yet, and whether the review can be frozen. Fetch it alongside the deliverables.
  session = null;
  if (gate === "build" && !review.error) await fetchSession(seq);
  if (seq !== fetchSeq) return;
  paint();
}

async function fetchSession(seq) {
  try {
    const res = await fetch("/api/review/session");
    const payload = await res.json();
    if (seq === fetchSeq) session = payload;
  } catch (e) { if (seq === fetchSeq) session = { error: "request failed: " + e }; }
}

// Answer a challenge, then refetch the session so the panel (and the priming lock) update in place.
// A 409 means the machine review moved underneath the reviewer — reload rather than merge blind.
async function answerChallenge(id, choice) {
  if (!session || !session.machine_digest) return;
  try {
    const res = await fetch("/api/review/challenge", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-AgentLoop-Token": TOKEN },
      body: JSON.stringify({ challenge_id: id, choice: choice, confidence: "medium", machine_digest: session.machine_digest }),
    });
    if (res.status === 409) { toast("the machine review changed — reloading", "err"); fetchReview(); return; }
    const data = await res.json();
    if (data.error) { toast(data.error, "err"); return; }
    session = data.session || session;
    paint();
  } catch (e) { toast("request failed: " + e, "err"); }
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
  // The footer is drawn from `review`; refuse to act if it is not the payload for the selected
  // gate, so an approval can never be recorded against deliverables the human did not see.
  if (!review || review.error || review.gate !== current) return;
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
      ? '<div class="okline">✓ the machine review is bound to this HEAD (' + esc((meta.head || "").slice(0, 12)) + ")</div>"
      : '<div class="warn">the machine review is missing or stale (reviewed: ' +
        esc(meta.reviewed_head ? meta.reviewed_head.slice(0, 12) : "none") + ", HEAD: " +
        esc((meta.head || "").slice(0, 12)) + ") — regenerate it before approving</div>";
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
  }).join("");  // .dl spans are display:block — a "\n" separator would double the line height
  return badge +
    '<div class="subhead">FILES (base ' + esc((diff.base || "").slice(0, 12)) + " on " + esc(diff.base_ref || "") +
    ')</div><div class="scroll"><table>' + files + "</table></div>" +
    (diff.truncated ? '<div class="warn">patch truncated at 200KB — review the rest in your editor</div>' : "") +
    '<div class="subhead" style="margin-top:.6rem">PATCH</div><pre class="patch">' + patch + "</pre>";
}

// The Challenge-first panel for gate ④: the unprimed challenge comes before anything the reviewer
// could read the answer off, and the budget / expertise / blocker verdicts sit above the diff so a
// scope split or an unfamiliar domain is seen before the approve button, not after (plan §14).
function challengeHtml() {
  if (!session || session.error || !session.generated) return "";
  let html = "";
  const ch = session.next_challenge;
  if (ch) {
    const choices = (ch.choices || []).map(c =>
      '<button class="gatebtn" data-choice="' + esc(c.id) + '" data-ch="' + esc(ch.id) + '">' +
      esc(c.id) + ". " + esc(c.text) + "</button>").join("");
    html += '<div class="sa"><div class="subhead">CHALLENGE ' + esc(ch.id) +
      ' <span class="conf ' + esc(ch.risk) + '">' + esc(ch.risk) + '</span></div>' +
      '<p>' + esc(ch.scenario) + "</p>" +
      '<p class="empty">Answer before you see Expected/Actual — this is a forcing function, not a quiz.</p>' +
      '<div class="gatebar">' + choices + "</div></div>";
  }
  const list = (title, items) => items.length
    ? '<div class="warn"><b>' + esc(title) + "</b><ul>" + items.map(x => "<li>" + esc(x) + "</li>").join("") + "</ul></div>"
    : "";
  html += list("Scope split required (budget exceeded)", session.scope_split_required || []);
  html += list("Domains needing an expert / experiment / smaller scope",
    (session.expertise_gaps || []).map(g => g.domain + " (" + g.level + ")"));
  if (!ch) html += list("Blocking before this review can be frozen", session.completion_blockers || []);
  return html;
}

function bodyHtml() {
  if (selected === DIFF_ID) return challengeHtml() + diffHtml(review.diff, review.review_meta);
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
  // On gate ④ the button is a signature request, and it is disabled until the human review can be
  // frozen: an unanswered challenge, an expertise gap, a blown budget, or any machine blocker.
  if (review.gate === "build" && session && !session.error) {
    if (!session.can_freeze) {
      const n = (session.completion_blockers || []).length;
      return warn + '<span class="warn" style="margin-right:.6rem">human review not ready — ' + n +
        " blocker(s) above</span><button class=\"primary\" disabled>Approve gate " + review.index + "</button>";
    }
  }
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

// The pane is repainted in two grains. `paintChrome` (gate bar + approval footer) is cheap and
// tracks the status poll, since the awaiting/approved marks move underneath the human. `paint`
// additionally rebuilds the body, which holds the rendered deliverable or a patch up to 200KB —
// doing that on every poll threw away the reader's scroll position every three seconds, so it now
// happens only when the content itself can have changed: a fetch landing, a gate switch, or a
// deliverable selection.
function paintChrome() {
  document.getElementById("rvBar").innerHTML = state.data ? barHtml() : "";
  const foot = document.getElementById("rvFoot");
  foot.innerHTML = (review && !review.error && review.gate === current)
    ? '<div class="approvebar">' + footerHtml() + "</div>" : "";
}

function paint() {
  if (!current) current = defaultGate();
  paintChrome();
  const main = document.getElementById("rvMain");
  if (!state.data) { main.innerHTML = '<div class="empty">waiting for status…</div>'; return; }
  if (review && review.error) main.innerHTML = '<div class="warn">' + esc(review.error) + "</div>";
  else if (!review || review.gate !== current) main.innerHTML = '<div class="empty">loading…</div>';
  else main.innerHTML =
    '<div class="rv-grid"><aside class="rv-list">' + listHtml() + '</aside>' +
    '<div class="rv-body">' + bodyHtml() + "</div></div>";
}

// Status polls repaint only the chrome (awaiting/approved may have moved); deliverables refetch only
// on tab entry, gate switch, or after a POST — never on the poll. The one exception: when the tab
// opened before the first status arrived, the first poll completes the deferred fetch.
export function renderReview() {
  if (!tabVisible) return;
  if (!review || reviewProject !== ((state.data || {}).project || null)) {
    if (!current) current = defaultGate();
    if (current) fetchReview();
    return;
  }
  paintChrome();
}

document.addEventListener("agentloop:view", e => {
  tabVisible = e.detail === "review";
  if (tabVisible) { if (!current) current = defaultGate(); fetchReview(); }
});
document.addEventListener("agentloop:refresh", () => { if (tabVisible) fetchReview(); });

// Challenge choices carry their ids as escaped data attributes, read back by this delegated
// listener — never interpolated into a generated onclick, the same rule as task ids in api.js.
document.addEventListener("click", e => {
  const btn = e.target.closest && e.target.closest("[data-choice]");
  if (btn) answerChallenge(btn.getAttribute("data-ch"), btn.getAttribute("data-choice"));
});

window.revGate = selectGate;
window.revSelect = selectDeliverable;
window.revApprove = approveCurrent;
