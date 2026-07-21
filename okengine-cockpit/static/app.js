"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => (s ?? "").toString().replace(/[&<>"]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
const dlLinks = qs => `<span class="dl">⬇ <a href="/api/download?fmt=md&${qs}">md</a> · <a href="/api/download?fmt=docx&${qs}">docx</a> · <a href="/api/download?fmt=pdf&${qs}">pdf</a></span>`;
const j = u => fetch(u).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

const BLOCKS = "▁▂▃▄▅▆▇█";
function spark(traj) {
  if (!traj || traj.length < 2) return "<span class='trend flat'>─</span>";
  return traj.map(c => BLOCKS[Math.max(0, Math.min(7, Math.floor(c * 8)))]).join("");
}
const GLYPH = { open:"○", confirmed:"✓", refuted:"✗", expired:"⊘", "expired-ungraded":"⊘", partial:"◐", active:"▸", resolved:"✓", unvalidated:"?" };
const SORDER = ["open","active","confirmed","partial","refuted","expired","expired-ungraded"];
const allStatuses = () => SORDER.filter(s => SUMMARY[s]).concat(Object.keys(SUMMARY).filter(s => !SORDER.includes(s) && s !== "?"));
const sClass = s => /^expired/.test(s) ? "expired" : /^confirm/.test(s) ? "confirmed" : (["open","refuted","partial","active"].includes(s) ? s : "open");
const sGlyph = s => GLYPH[s] || (/^expired/.test(s) ? "⊘" : "·");

// ── clock ──────────────────────────────────────────────────────────────────
// Renders in the DEPLOYMENT timezone (okengine#301), set from /api/config `tz`; defaults UTC until
// config loads (and if Intl doesn't know the zone). Shows the zone abbreviation (EDT/UTC/…), not a
// hardcoded "UTC".
let CLOCK_TZ = "UTC";
function tick() {
  const d = new Date();
  let out;
  try {
    const p = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
      timeZone: CLOCK_TZ, hour12: false, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", timeZoneName: "short",
    }).formatToParts(d).map(x => [x.type, x.value]));
    out = `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ${p.timeZoneName}`;
  } catch (e) {
    out = d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
  $("#clock").textContent = out;
}
tick(); setInterval(tick, 30000);

// ── tabs (built at runtime from /api/config — domain-agnostic) ───────────────
const TAB_LABELS = { home: "Home", briefings: "Briefings", dashboards: "Dashboards", predictions: "Predictions", ops: "Ops", competitors: "Competitors", watchlist: "Watchlist", browse: "Browse", chat: "Chat" };
let TABS = [];
let REVIEW_ENABLED = false;
let TAB_DEF_LABELS = {};   // pack-defined dataset tabs (from /api/config tab_labels)
function buildTabs(tabs) {
  TABS = (tabs && tabs.length) ? tabs.slice() : ["briefings"];
  const nav = $("#tabs"); nav.innerHTML = "";
  // pack-defined dataset tabs get their pane created dynamically (no static markup)
  TABS.forEach(tname => {
    if (TAB_LABELS[tname] || $("#view-" + tname)) return;
    const sec = document.createElement("section");
    sec.id = "view-" + tname; sec.className = "view";
    sec.innerHTML = `<div class="pane dgrid" id="dpane-${tname}"><div class="empty">Loading…</div></div>`;
    document.querySelector("main").appendChild(sec);
  });
  TABS.forEach((t, i) => {
    const b = el("button", i === 0 ? "active" : "", esc(TAB_LABELS[t] || TAB_DEF_LABELS[t] || t));
    b.dataset.tab = t;
    if (t === "predictions") b.insertAdjacentHTML("beforeend", '<span id="due-badge" class="badge" hidden></span>');
    b.onclick = () => showTab(t);
    nav.appendChild(b);
  });
}
function showTab(name) {
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "home" && !homeLoaded) loadHome();
  if (name === "dashboards" && !dashLoaded) loadDashboards();
  if (name === "ops" && !opsLoaded) loadOps();
  if (name === "predictions" && !predLoaded) loadPredictions();
  if (name === "competitors" && !compLoaded) loadCompetitors();
  if (name === "watchlist" && !watchLoaded) loadWatchlist();
  if (name === "browse" && !browseLoaded) loadBrowse();
  if (TAB_DEF_LABELS[name] && !DTAB_LOADED[name]) loadDataTab(name);
  if (name === "chat") { const ci = $("#chat-input"); if (ci) ci.focus(); }
  if (location.hash.slice(1) !== name) location.hash = name;
}

// ── briefings ──────────────────────────────────────────────────────────────
const STREAM_PDF = {}, STREAM_LABEL = {};
async function loadStreams() {
  const { streams } = await j("/api/streams");
  const box = $("#streams"); box.innerHTML = "";
  streams.forEach((s, i) => {
    STREAM_PDF[s.key] = !!s.has_pdf; STREAM_LABEL[s.key] = s.label;
    const div = el("div", "stream" + (i === 0 ? " open" : ""));
    const head = el("div", "s-head", `<span>${i === 0 ? "▾" : "▸"} ${esc(s.label)}</span>`);
    if (s.has_pdf) head.appendChild(el("span", "pdf", "⧉pdf"));
    head.onclick = () => { div.classList.toggle("open"); head.firstChild.textContent = (div.classList.contains("open") ? "▾ " : "▸ ") + s.label; };
    div.appendChild(head);
    const dates = el("div", "dates");
    const show = s.dates.slice(0, 12);
    show.forEach(dt => {
      const d = el("div", "date", esc(dt));
      d.onclick = e => { e.stopPropagation(); selectDoc(s.key, dt, d); };
      dates.appendChild(d);
    });
    if (s.dates.length > show.length)
      dates.appendChild(el("div", "date more", `… ${s.dates.length - show.length} more`));
    div.appendChild(dates);
    box.appendChild(div);
    if (i === 0 && s.latest && !/[?&](stream|page)=/.test(location.search)) setTimeout(() => selectDoc(s.key, s.latest, dates.firstChild), 0);
  });
}
async function selectDoc(stream, date, node) {
  if (date === "__latest__") { try { const st = await j("/api/streams"); date = (st.streams.find(x => x.key === stream) || {}).latest; } catch (e) {} if (!date) return; }
  $$(".date.sel").forEach(n => n.classList.remove("sel"));
  if (node) node.classList.add("sel");
  const pane = $("#brief-pane");
  if (STREAM_PDF[stream]) {
    const u = `/api/stream.pdf?stream=${encodeURIComponent(stream)}&date=${esc(date)}`;
    pane.innerHTML = `<div class="doc-head"><h1>${esc(STREAM_LABEL[stream] || stream)} — ${esc(date)}</h1>
      <span class="meta"><a href="${u}" target="_blank">open pdf ⧉</a></span></div>
      <iframe class="deck" src="${u}"></iframe>`;
    return;
  }
  pane.innerHTML = `<div class="empty">Loading ${esc(date)}…</div>`;
  try {
    const d = await j(`/api/doc?stream=${stream}&date=${date}`);
    pane.innerHTML = `<div class="doc-head"><h1>${esc(d.title)}</h1>
      <span class="meta">${d.generated_at ? "generated " + esc(d.generated_at) : ""}</span></div>
      <div class="md">${d.html}</div>
      <div class="doc-actions"><a id="copy">[c]opy</a> · ${dlLinks(`stream=${stream}&date=${date}`)} · <span>${esc(stream)}/${esc(date)}</span></div>`;
    $("#copy").onclick = () => navigator.clipboard?.writeText(pane.innerText);
  } catch (e) { pane.innerHTML = `<div class="empty">failed to load (${e.message})</div>`; }
}

// ── dashboards ───────────────────────────────────────────────────────────────
// Domain-agnostic: the grid is served by /api/dashboards. A pack may supply a
// curated, grouped reading order via the `cockpit.dashboards:` schema block;
// with none, every page under wiki/dashboards/ is auto-listed. Each card opens
// in the page overlay via /api/page.
let dashLoaded = false;
async function loadDashboards() {
  dashLoaded = true;
  const pane = $("#dash-pane");
  try {
    const { groups } = await j("/api/dashboards");
    if (!groups || !groups.length) { pane.innerHTML = `<div class="empty">no dashboards</div>`; return; }
    pane.innerHTML = groups.map(g =>
      `<div class="dash-group">${g.group ? `<div class="dash-h">${esc(g.group)}</div>` : ""}` +
      `<div class="dash-grid">` + (g.items || []).map(it =>
        `<a class="dash-card" data-page="${esc(it.path)}">` +
        `<span class="dash-t">${esc(it.title || it.path)}</span>` +
        (it.desc ? `<span class="dash-d">${esc(it.desc)}</span>` : "") + `</a>`).join("") +
      `</div></div>`).join("");
    $$(".dash-card", pane).forEach(a => a.onclick = () => openPage(a.dataset.page));
  } catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

// ── ops (engine health/audit surface) ────────────────────────────────────────
// Read-only grid over the operational/health pages the engine crons generate
// (fleet-health, kb-health, conformance, review queue, grounding, operator, …).
// Auto-appended to the nav by /api/config when that content exists; cards open
// the underlying page in the overlay. Reuses the dashboards grid styling.
let opsLoaded = false;
async function loadOps() {
  opsLoaded = true;
  const pane = $("#ops-pane");
  try {
    const { groups } = await j("/api/ops");
    if (!groups || !groups.length) { pane.innerHTML = `<div class="empty">no operational pages</div>`; return; }
    pane.innerHTML = groups.map(g =>
      `<div class="dash-group">${g.group ? `<div class="dash-h">${esc(g.group)}</div>` : ""}` +
      `<div class="dash-grid">` + (g.items || []).map(it =>
        `<a class="dash-card" ${it.action ? `data-action="${esc(it.action)}"` : `data-page="${esc(it.path)}"`}>` +
        `<span class="dash-t">${esc(it.title || it.path)}</span>` +
        (it.desc ? `<span class="dash-d">${esc(it.desc)}</span>` : "") +
        (it.updated ? `<span class="dash-d dim">updated ${esc(it.updated)}</span>` : "") + `</a>`).join("") +
      `</div></div>`).join("");
    $$(".dash-card", pane).forEach(a => a.onclick = () =>
        a.dataset.action === "application" ? openApplicationContract() :
        a.dataset.action === "reviews" ? openReviewQueue(0, "") : openPage(a.dataset.page));
  } catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

const reviewFilters = {reason:"", page_type:"", page_types:"", state:"", assignment:"", source_resolution:"", age:""};
async function openReviewQueue(offset = 0, presetTypes = null) {
  if (presetTypes !== null) {
    Object.keys(reviewFilters).forEach(k => reviewFilters[k] = "");
    reviewFilters.page_types = presetTypes;
  }
  const ov = $("#page-overlay"), c = $("#ov-content");
  ov.hidden = false; c.innerHTML = "<div class='empty'>Loading review queue…</div>";
  pageStack.length = 0; $("#ov-title").textContent = "Human review"; $("#ov-path").textContent = "operator worklist";
  $("#ov-dl").innerHTML = ""; $("#ov-back").style.visibility = "hidden";
  try {
    const qs = new URLSearchParams({offset, limit:50, ...reviewFilters});
    const d = await j(`/api/reviews?${qs}`);
    const opts = (values, selected) => `<option value="">all</option>` + values.map(v => `<option value="${esc(v)}" ${v===selected?"selected":""}>${esc(v)}</option>`).join("");
    c.innerHTML = `<div class="review-queue-head"><strong>${d.total} awaiting review</strong><span>showing ${d.total ? d.offset + 1 : 0}–${Math.min(d.total,d.offset+d.items.length)}</span></div>` +
      `<div class="review-metrics"><span>oldest ${d.metrics.oldest_days ?? "—"}d</span><span>assigned ${d.metrics.assigned}</span><span>decisions 30d ${d.metrics.throughput_30d}</span><span>reopened ${d.metrics.reopened}</span></div>` +
      `<div class="review-filters">` +
      `<label>reason<select data-review-filter="reason">${opts(Object.keys(d.facets.reasons||{}),reviewFilters.reason)}</select></label>` +
      `<label>type<select data-review-filter="page_type">${opts(Object.keys(d.facets.types||{}),reviewFilters.page_type)}</select></label>` +
      `<label>state<select data-review-filter="state">${opts(Object.keys(d.facets.states||{}),reviewFilters.state)}</select></label>` +
      `<label>assignment<select data-review-filter="assignment">${opts(["assigned","unassigned"],reviewFilters.assignment)}</select></label>` +
      `<label>sources<select data-review-filter="source_resolution">${opts(["complete","partial","none"],reviewFilters.source_resolution)}</select></label>` +
      `<label>age<select data-review-filter="age">${opts(["0-30","31-90","91+"],reviewFilters.age)}</select></label></div>` +
      `<div class="review-queue">` + d.items.map(row => `<a class="review-row" data-page="${esc(row.subject)}">` +
        `<span><b>${esc(row.title)}</b><small>${esc(row.subject)}</small></span><span>${esc(row.type)}</span>` +
        `<span>${(row.reasons||[]).map(r=>esc(r.code)).join(", ")}</span><span>${row.evidence_resolved}/${row.evidence_total} sources · ${row.age_days ?? "—"}d</span></a>`).join("") + `</div>` +
      `<div class="review-pages">${offset>0?`<button data-review-offset="${Math.max(0,offset-50)}">Previous</button>`:""}` +
      `${offset+d.items.length<d.total?`<button data-review-offset="${offset+50}">Next</button>`:""}</div>`;
    $$(".review-row", c).forEach(a => a.onclick = () => openPage(a.dataset.page));
    $$("[data-review-offset]", c).forEach(b => b.onclick = () => openReviewQueue(+b.dataset.reviewOffset));
    $$("[data-review-filter]", c).forEach(s => s.onchange = () => {
      reviewFilters[s.dataset.reviewFilter] = s.value; openReviewQueue(0);
    });
  } catch (e) { c.innerHTML = `<div class="empty">review queue failed: ${esc(e.message)}</div>`; }
}

async function openApplicationContract() {
  const ov = $("#page-overlay"), c = $("#ov-content");
  ov.hidden = false; c.innerHTML = "<div class='empty'>Loading…</div>";
  try {
    const d = await j("/api/application");
    pageStack.length = 0;
    $("#ov-title").textContent = d.title || "Application";
    $("#ov-path").textContent = `${d.profile || "application"} · v${d.profile_version || "—"}`;
    $("#ov-dl").innerHTML = "";
    $("#ov-back").style.visibility = "hidden";
    c.innerHTML = d.html; c.scrollTop = 0;
  } catch (e) { c.innerHTML = `<div class='empty'>application contract unavailable</div>`; }
}

// ── predictions ────────────────────────────────────────────────────────────
let predLoaded = false, PRED = [], SUMMARY = {};
// Clean an entity subject PATH for display: drop the single-letter shard dir
// (entities/a/anthropic -> the "a"), dim the namespace, keep the slug prominent.
// Raw path stays in the cell title tooltip.
function subjCell(s) {
  const parts = (s || "").split("/").filter(p => p && p.length > 1);
  if (!parts.length) return esc(s || "—");
  const slug = parts.pop();
  const ns = parts.length ? `<span class="subj-ns">${esc(parts.join("/"))}/</span>` : "";
  return ns + esc(slug);
}

async function loadPredictions() {
  predLoaded = true;
  const data = await j("/api/predictions");
  PRED = data.rows; SUMMARY = data.summary;
  if (data.due_soon > 0) { const b = $("#due-badge"); b.textContent = data.due_soon; b.hidden = false; }
  // summary strip
  const order = allStatuses();
  $("#pred-summary").innerHTML =
    `<span class="tot">${data.total} predictions</span>` +
    order.map(s => `<span class="s">${sGlyph(s)} ${SUMMARY[s]} ${esc(s)}</span>`).join("") +
    `<span class="due">⚠ ${data.due_soon} due ≤7d</span>` +
    (data.idle ? `<span class="due">⏳ ${data.idle} idle</span>` : "");
  // filters
  const fs = $("#f-status");
  fs.innerHTML = `<option value="">all</option>` + order.map(s => `<option value="${esc(s)}">${esc(s)} (${SUMMARY[s]})</option>`).join("")
    + `<option value="due">due ≤7d</option>` + (data.idle ? `<option value="idle">idle ≥60d, no evidence (${data.idle})</option>` : "");
  fs.value = "open";
  const hs = [...new Set(PRED.map(p => p.horizon).filter(Boolean))].sort();
  $("#f-horizon").innerHTML = `<option value="">all</option>` + hs.map(h => `<option>${esc(h)}</option>`).join("");
  const fsets = data.forecast_sets || [];
  $("#f-forecast").innerHTML = `<option value="">all</option>` + fsets.map(s => `<option>${esc(s)}</option>`).join("");
  $("#f-forecast").parentElement.style.display = fsets.length ? "" : "none";
  ["#f-status", "#f-horizon", "#f-forecast", "#f-subject", "#f-sort", "#f-search"].forEach(s => $(s).oninput = renderLedger);
  renderLedger();
}
function renderLedger() {
  const st = $("#f-status").value, hz = $("#f-horizon").value, fset = $("#f-forecast").value,
    subj = $("#f-subject").value.toLowerCase(), q = $("#f-search").value.toLowerCase(), sort = $("#f-sort").value;
  let rows = PRED.filter(p => {
    const due = p.status === "open" && p.days_to_resolve != null && p.days_to_resolve >= 0 && p.days_to_resolve <= 7;
    if (st === "due") { if (!due) return false; }
    else if (st === "idle") { if (!p.idle) return false; }
    else if (st && p.status !== st) return false;
    if (hz && p.horizon !== hz) return false;
    if (fset && p.forecast_set !== fset) return false;
    if (subj && !(p.subject || "").toLowerCase().includes(subj)) return false;
    if (q && !((p.claim || "") + (p.subject || "")).toLowerCase().includes(q)) return false;
    return true;
  });
  const big = 1e9;
  // "due" = what resolves SOONEST among upcoming; past-due (negative days) and
  // undated never sort to the top — they go after all upcoming items.
  const dueKey = p => p.days_to_resolve == null ? 2e9
                    : (p.days_to_resolve < 0 ? 1e9 - p.days_to_resolve : p.days_to_resolve);
  const cmp = {
    due: (a, b) => dueKey(a) - dueKey(b),
    conf: (a, b) => (b.confidence ?? -1) - (a.confidence ?? -1),
    moved: (a, b) => Math.abs(b.last_move ?? 0) - Math.abs(a.last_move ?? 0) || (b.updated || "").localeCompare(a.updated || ""),
    made: (a, b) => (b.made_on || "").localeCompare(a.made_on || ""),
  }[sort];
  rows.sort(cmp);
  const head = `<thead><tr><th></th><th>subject</th><th>claim</th><th class="num">conf</th><th>trend</th><th class="num">ev</th><th>resolves</th><th class="num">in</th><th>updated</th></tr></thead>`;
  const body = rows.map(p => {
    const due = p.status === "open" && p.days_to_resolve != null && p.days_to_resolve >= 0 && p.days_to_resolve <= 7;
    const cls = due ? "due" : sClass(p.status);
    const gl = due ? "⚠" : sGlyph(p.status);
    const inCell = p.days_to_resolve == null ? "—" : (p.days_to_resolve < 0 ? "past" : p.days_to_resolve + "d");
    const tr = p.last_move == null ? "flat" : (p.last_move > 0 ? "up" : p.last_move < 0 ? "down" : "flat");
    const arrow = tr === "up" ? "↑" : tr === "down" ? "↓" : "→";
    const ed = p.ev_dir || {}, en = p.evidence_n || 0;
    const evTitle = `${en} evidence — reinforces ${ed.reinforces || 0} · contradicts ${ed.contradicts || 0} · partial ${ed.partial || 0} · neutral ${ed.neutral || 0}`;
    const evCell = en === 0 ? "—" : `${en}${ed.contradicts ? ` <span class="trend down">✗${ed.contradicts}</span>` : ""}`;
    return `<tr data-id="${esc(p.id)}">
      <td class="st ${cls}">${gl}</td>
      <td class="subj" title="${esc(p.subject || "")}">${subjCell(p.subject)}${p.np ? " <span class='trend flat'>np</span>" : ""}${p.idle ? " <span class='trend flat' title='open ≥60d with no evidence'>⏳</span>" : ""}</td>
      <td class="claim" title="${esc(p.claim)}">${p.claim_html || esc(p.claim || "")}</td>
      <td class="num">${p.confidence != null ? p.confidence.toFixed(2) : "—"}</td>
      <td><span class="spark">${spark(p.trajectory)}</span> <span class="trend ${tr}">${arrow}</span></td>
      <td class="num" title="${esc(evTitle)}">${evCell}</td>
      <td class="num">${esc(p.resolves_by || "—")}</td>
      <td class="num ${due ? "due-in" : ""}">${inCell}</td>
      <td class="num">${esc(p.updated || "—")}</td></tr>`;
  }).join("");
  const t = $("#pred-table");
  t.innerHTML = rows.length ? `<table class="ledger">${head}<tbody>${body}</tbody></table>` +
    `<div class="empty" style="text-align:left;padding:8px 4px">${rows.length} shown · j/k move · Enter expand</div>`
    : `<div class="empty">no predictions match</div>`;
  $$("table.ledger tbody tr").forEach(tr => tr.onclick = (e) => { if (e.target.closest("a.wl")) return; showDetail(tr.dataset.id, tr); });
}
// Evidence drilldown: the actual regrade log behind the ledger's ev tally — each row's
// date, direction glyph, confidence move, note, and source link. Source paths open in the
// page overlay (a.wl delegation); http(s) sources open externally.
function evidenceHtml(entries) {
  if (!entries || !entries.length) return "";
  const cls = { reinforces: "up", contradicts: "down", partial: "flat", neutral: "flat" };
  const glyph = { reinforces: "↑", contradicts: "✗", partial: "≈", neutral: "•" };
  const rows = entries.map(e => {
    const dc = cls[e.direction] || "flat", dg = glyph[e.direction] || "•";
    const cb = e.confidence_before, ca = e.confidence_after;
    const conf = (cb != null || ca != null)
      ? `<span class="ev-conf">${cb != null ? cb.toFixed(2) : "?"}→${ca != null ? ca.toFixed(2) : "?"}</span>` : "";
    let src = "";
    if (e.source) {
      src = /^https?:\/\//.test(e.source)
        ? `<a class="ev-src" href="${esc(e.source)}" target="_blank" rel="noopener">source ↗</a>`
        : `<a class="ev-src wl" data-page="${esc(e.source)}" title="${esc(e.source)}">${esc(e.source.split("/").pop())}</a>`;
    }
    return `<li class="ev-row"><span class="ev-dir trend ${dc}" title="${esc(e.tag || e.direction || "note")}">${dg}</span>` +
      `<span class="ev-date">${esc(e.date || "—")}</span>${conf}` +
      `<span class="ev-note">${esc(e.note || "")}</span>${src}</li>`;
  }).join("");
  return `<ul class="ev-list">${rows}</ul>`;
}
async function showDetail(id, tr) {
  $$("table.ledger tr.sel").forEach(n => n.classList.remove("sel"));
  if (tr) tr.classList.add("sel");
  const d = $("#pred-detail"); d.hidden = false; d.innerHTML = "loading…";
  try {
    const p = await j(`/api/prediction?id=${encodeURIComponent(id)}`);
    const traj = p.trajectory && p.trajectory.length
      ? p.trajectory.map(c => c.toFixed(2)).join(" ─▶ ") + "   <span class='spark'>" + spark(p.trajectory) + "</span>" : "—";
    const fm = p.fm || {};
    const row = PRED.find(x => x.id === id) || {}, ed = row.ev_dir || {};
    const evLine = (row.evidence_n || 0) === 0
      ? `<div class="traj">evidence: none${row.idle ? " · <span class='trend flat'>⏳ idle (open ≥60d)</span>" : ""}</div>`
      : `<div class="traj">evidence (${row.evidence_n}): <span class="trend up">↑${ed.reinforces || 0} reinforce</span> · <span class="trend down">✗${ed.contradicts || 0} contradict</span> · ${ed.partial || 0} partial · ${ed.neutral || 0} neutral</div>`;
    d.innerHTML = `<span class="x" id="x">✕ Esc</span>
      <h3>${esc(id)} · ${esc(fm.status || "")} · ${esc(fm.horizon || "")} · resolves ${esc(fm.resolves_by || fm.target_date || "—")}</h3>
      <div class="claimq">"${p.claim_html || esc(p.claim || "")}"</div>
      <div class="traj">trajectory: ${traj}</div>
      ${evLine}
      ${evidenceHtml(p.evidence)}
      <div class="meta">${row.forecast_set ? "set: " + esc(row.forecast_set) + "  ·  " : ""}${fm.measurement_method ? "measurement: " + esc(fm.measurement_method) + "  ·  " : ""}made ${esc(fm.made_on || fm.created || "—")} · updated ${esc(fm.updated || "—")} · confidence ${esc(fm.confidence || "—")}</div>`;
    $("#x").onclick = () => { d.hidden = true; };
  } catch (e) { d.innerHTML = `failed (${e.message})`; }
}

// ── competitors ────────────────────────────────────────────────────────────
let compLoaded = false;
async function loadCompetitors() {
  compLoaded = true;
  const pane = $("#comp-pane");
  try {
    const { views } = await j("/api/competitors");
    pane.innerHTML = views.length ? views.map(v =>
      `<div class="cview"><div class="h">${esc(v.title)}${v.updated ? " · updated " + esc(v.updated) : ""}</div>
       <div class="b md">${v.html}</div></div>`).join("") : `<div class="empty">no competitor views found</div>`;
  } catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

// ── pack-defined dataset tabs (declarative boxes rendered by /api/tab/<key>) ──
const DTAB_LOADED = {};
async function loadDataTab(name) {
  DTAB_LOADED[name] = true;
  const pane = $("#dpane-" + name);
  try {
    const { boxes } = await j("/api/tab/" + encodeURIComponent(name));
    let lastSection = null;
    pane.innerHTML = boxes.map(b => {
      // partial labels-map drift: a compact header warning listing the raw codes (okengine#188)
      const um = (b.unmapped && b.unmapped.length)
        ? `<span class="um-warn" title="unmapped codes: ${esc(b.unmapped.join(", "))}">⚠ ${b.unmapped.length} unmapped</span>`
        : "";
      // provenance affordance: mark a corpus/coverage metric so it isn't read as a threat measure (#259)
      const prov = (b.provenance && b.provenance.label)
        ? `<span class="prov-badge" title="${esc(b.provenance.note || "Measures our reporting/collection, not threat level.")}">◷ ${esc(b.provenance.label)}</span>`
        : "";
      const section = b.section && b.section !== lastSection
        ? `<h2 class="dsection">${esc(b.section)}</h2>` : "";
      if (b.section) lastSection = b.section;
      return section + `<section class="dbox s${Math.min(12, Math.max(3, b.span || 6))}">` +
        `<header><span class="eb">${esc(b.title)}${prov}</span>` +
        `<span class="dmeta">${um}${esc(b.meta || "")}</span></header>` +
        `<div class="db">${b.html}</div></section>`;
    }).join("")
      || `<div class="empty">nothing to show yet — boxes appear as their lanes produce data</div>`;
    $$(".operation-control", pane).forEach(bindOperationControl);
  } catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

function operationArguments(panel) {
  try { return JSON.parse(panel.dataset.arguments || "[]"); }
  catch (_) { return []; }
}
function bindOperationControl(panel) {
  const plan = $("[data-operation-plan]", panel), run = $("[data-operation-run]", panel);
  if (plan) plan.onclick = () => planOperation(panel);
  if (run) run.onclick = () => startOperation(panel);
}
async function planOperation(panel) {
  const name = panel.dataset.operation, result = $(".operation-result", panel);
  const plan = $("[data-operation-plan]", panel), run = $("[data-operation-run]", panel);
  plan.disabled = true; run.disabled = true; result.textContent = "Planning frozen scope…";
  try {
    const response = await fetch(`/api/operations/${encodeURIComponent(name)}/plan`, {
      method:"POST", headers:{"Content-Type":"application/json","X-OKEngine-Operation":"1"},
      body:JSON.stringify({arguments:operationArguments(panel)})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || response.status);
    const actors = (data.actor_inventory || []).length, questions = (data.counts || {}).actor_questions;
    const dimensions = (data.dimensions || []).length;
    panel.dataset.planDigest = data.snapshot_digest || "";
    result.innerHTML = `<strong>Planned:</strong> ${actors.toLocaleString()} actors · ` +
      `${Number(questions || 0).toLocaleString()} questions · ${dimensions} dimensions` +
      (data.snapshot_digest ? `<br><code>${esc(data.snapshot_digest)}</code>` : "");
    run.disabled = false;
  } catch (e) { result.textContent = `Planning failed: ${e.message}`; }
  finally { plan.disabled = false; }
}
async function startOperation(panel) {
  const name = panel.dataset.operation, result = $(".operation-result", panel);
  if (!panel.dataset.planDigest) { result.textContent = "Plan the scope before starting."; return; }
  if (!confirm(`Start ${name} against the planned full scope?`)) return;
  $$('button', panel).forEach(button => button.disabled = true);
  result.textContent = "Submitting operation…";
  try {
    const response = await fetch(`/api/operations/${encodeURIComponent(name)}/run`, {
      method:"POST", headers:{"Content-Type":"application/json","X-OKEngine-Operation":"1"},
      body:JSON.stringify({arguments:operationArguments(panel), plan_digest:panel.dataset.planDigest})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || response.status);
    result.textContent = `Started request ${data.request_id}; waiting for receipt…`;
    pollOperation(panel, data.request_id);
  } catch (e) {
    result.textContent = `Start failed: ${e.message}`;
    $("[data-operation-plan]", panel).disabled = false;
  }
}
async function pollOperation(panel, requestId) {
  const result = $(".operation-result", panel);
  try {
    const data = await j(`/api/operations/requests/${encodeURIComponent(requestId)}`);
    const progress = data.progress || {};
    const lanes = progress.lanes_total ? ` · ${progress.lanes_complete || 0}/${progress.lanes_total} lanes` : "";
    const actors = progress.actors ? ` · ${Number(progress.actors).toLocaleString()} actors` : "";
    result.innerHTML = `<strong>${esc(data.status || "running")}</strong>${actors}${lanes}` +
      (data.run_id ? `<br>Run <code>${esc(data.run_id)}</code>` : "");
    if (["succeeded","failed","canceled"].includes(data.status)) {
      $("[data-operation-plan]", panel).disabled = false;
      if (data.status === "succeeded") setTimeout(() => location.reload(), 1000);
      return;
    }
    setTimeout(() => pollOperation(panel, requestId), 2000);
  } catch (e) {
    result.textContent = `Status unavailable: ${e.message}`;
    $("[data-operation-plan]", panel).disabled = false;
  }
}

// ── analyst home (the flow: latest briefs → what moved → predictions → gaps) ──
let homeLoaded = false;
async function loadHome() {
  homeLoaded = true;
  const pane = $("#home-pane");
  try {
    const { sections } = await j("/api/home");
    let html = "", lastGroup = null;
    sections.forEach(s => {
      if (s.group !== lastGroup) { html += `<h2 class="watch-group">${esc(s.group)}</h2>`; lastGroup = s.group; }
      html += `<div class="cview"><div class="h">${esc(s.title)}</div><div class="b md">${s.html}</div></div>`;
    });
    pane.innerHTML = html || `<div class="empty">nothing to show yet — surfaces appear as their lanes produce data</div>`;
    // a stream row jumps to the briefings tab (a.wl data-page links use the global handler)
    $$("[data-stream]", pane).forEach(a => a.onclick = (e) => { e.preventDefault(); showTab("briefings"); });
  } catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

// ── watchlist & trends ───────────────────────────────────────────────────────
let watchLoaded = false;
async function loadWatchlist() {
  watchLoaded = true;
  const pane = $("#watch-pane");
  try {
    const { sections } = await j("/api/watchlist");
    let html = "", lastGroup = null;
    sections.forEach(s => {
      if (s.group !== lastGroup) { html += `<h2 class="watch-group">${esc(s.group)}</h2>`; lastGroup = s.group; }
      html += `<div class="cview"><div class="h">${esc(s.title)}</div><div class="b md">${s.html}</div></div>`;
    });
    pane.innerHTML = html || `<div class="empty">no data</div>`;
  } catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

// ── browse (generic namespaces → pages, ported from okengine-reader) ─────────
let browseLoaded = false, CURRENT_DIR = null;
function loadBrowse() { browseLoaded = true; loadTree(); loadGroups(); }
function _railRows(container, items) {
  container.innerHTML = "";
  items.forEach(d => {
    const row = el("div", "dir", `<span class="d-name">${esc(d.dir)}</span><span class="d-n">${d.count}</span>`);
    row.onclick = () => selectDir(d.dir, row);
    container.appendChild(row);
  });
}
async function loadTree() {
  const box = $("#dirs"), abox = $("#analysis"), atitle = $("#analysis-title");
  try {
    const { dirs, top_section } = await j("/api/tree");
    if (!dirs.length) { box.innerHTML = `<div class="empty" style="padding:14px">empty vault</div>`; $("#list-pane").innerHTML = `<div class="empty">No pages found under wiki/.</div>`; return; }
    const topNs = (top_section && top_section.namespaces) || [];
    const featured = topNs.map(n => dirs.find(d => d.dir === n)).filter(Boolean);   // ordered, present
    const rest = dirs.filter(d => !topNs.includes(d.dir));
    if (featured.length && top_section.label) { atitle.textContent = top_section.label; atitle.hidden = false; _railRows(abox, featured); }
    else { atitle.hidden = true; abox.innerHTML = ""; }
    _railRows(box, rest);
    const all = [...featured, ...rest];
    const dirParam = new URLSearchParams(location.search).get("dir");
    const initial = dirParam || (all[0] && all[0].dir);
    const node = [...$$(".dir", abox), ...$$(".dir", box)].find(r => $(".d-name", r).textContent === initial);
    if (initial) selectDir(initial, node);
  } catch (e) { box.innerHTML = `<div class="empty" style="padding:14px">failed (${e.message})</div>`; }
}
function renderPages(pane, heading, pages, about = "") {
  // namespace description card (wiki/<ns>/_about.md, server-rendered) — shown even when empty
  const aboutHtml = about ? `<div class="ns-about md">${about}</div>` : "";
  if (!pages.length) { pane.innerHTML = aboutHtml + `<div class="empty">no pages in ${esc(heading)}</div>`; return; }
  const rows = pages.map(p =>
    `<tr data-page="${esc(p.path)}">
       <td class="pg-title">${esc(p.title)}</td>
       <td class="pg-type">${esc(p.type || "")}</td>
       <td class="pg-upd num">${esc(p.updated || "")}</td>
     </tr>`).join("");
  pane.innerHTML = aboutHtml + `<div class="list-head"><h1>${esc(heading)}</h1><span class="meta">${pages.length} pages</span></div>` +
    `<table class="ledger"><thead><tr><th>title</th><th>type</th><th class="num">updated</th></tr></thead><tbody>${rows}</tbody></table>`;
  $$("table.ledger tbody tr", pane).forEach(tr => tr.onclick = e => { if (e.target.closest("a.wl")) return; openPage(tr.dataset.page); });
}
async function selectDir(dir, node) {
  CURRENT_DIR = dir;
  $$("#dir-rail .dir.sel").forEach(n => n.classList.remove("sel"));
  if (node) node.classList.add("sel");
  const pane = $("#list-pane");
  pane.innerHTML = `<div class="empty">Loading ${esc(dir)}…</div>`;
  try { const d = await j(`/api/pages?dir=${encodeURIComponent(dir)}`); renderPages(pane, dir, d.pages, d.about); }
  catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}
// browse BY KIND: pack-declared display groups (label -> related page types)
async function loadGroups() {
  const box = $("#groups"), title = $("#groups-title");
  try {
    const { groups } = await j("/api/groups");
    if (!groups || !groups.length) return;            // pack declares none → stay hidden
    title.hidden = false; box.innerHTML = "";
    groups.forEach(g => {
      const row = el("div", "dir", `<span class="d-name">${esc(g.label)}</span><span class="d-n">${g.count}</span>`);
      row.onclick = () => selectGroup(g.label, row);
      box.appendChild(row);
    });
  } catch (e) { /* leave the section hidden */ }
}
async function selectGroup(label, node) {
  CURRENT_DIR = null;
  $$("#dir-rail .dir.sel").forEach(n => n.classList.remove("sel"));
  if (node) node.classList.add("sel");
  const pane = $("#list-pane");
  pane.innerHTML = `<div class="empty">Loading ${esc(label)}…</div>`;
  try { renderPages(pane, label, (await j(`/api/pages?group=${encodeURIComponent(label)}`)).pages); }
  catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

// ── agent chat (relay to THE Hermes agent via /api/chat) ─────────────────────
// The agent answers by navigating the vault via its graph tools; we render its markdown
// and turn page paths and [[wikilinks]] into clickable links (reusing a.wl → openPage).
const CHAT = [];
let _chatBusy = false;
function mdLite(s) {
  let h = esc(s);
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/\[\[\s*([^\]|#]+?)\s*(?:\|\s*([^\]]+?))?\s*\]\]/g,
    (m, t, a) => `<a class="wl" data-page="${esc(t)}">${esc(a || t)}</a>`);
  h = h.replace(/`([^`]+)`/g, (m, c) => {
    const v = c.trim();
    return /^[a-z][\w-]*\/[\w./-]+$/i.test(v)
      ? `<a class="wl" data-page="${esc(v.replace(/\.md$/, ""))}">${c}</a>`
      : `<code>${c}</code>`;
  });
  h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (m, t, u) => `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  // internal linked-title citation `[APT41](entities/a/apt41)` -> clickable page link (openPage);
  // http links already became <a> above so they won't match here.
  h = h.replace(/\[([^\]]+)\]\(([^\s)]+)\)/g,
    (m, t, p) => `<a class="wl" data-page="${esc(p.replace(/\.md$/, ""))}">${t}</a>`);
  return h.replace(/\n/g, "<br>");
}
// Export the finished assistant report through the server clean+pandoc pipeline (internal vault
// links flattened to text). One file per format; the browser downloads it.
async function downloadChatExport(text, fmt, btn) {
  const title = ((text.match(/^#{1,6}\s*(.+)$/m) || [])[1]
    || (text.split("\n").find(l => l.trim()) || "report")).replace(/[*_`#]/g, "").slice(0, 80).trim();
  const old = btn.textContent; btn.disabled = true; btn.textContent = "…";
  try {
    const res = await fetch(`/api/chat_export?fmt=${encodeURIComponent(fmt)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, title })
    });
    if (!res.ok) throw new Error(res.status);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const fn = decodeURIComponent((cd.match(/filename="?([^"]+)"?/) || [])[1] || `report.${fmt}`);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = fn; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  } catch (e) { btn.textContent = "err"; setTimeout(() => (btn.textContent = old), 1500); return; }
  finally { btn.disabled = false; if (btn.textContent === "…") btn.textContent = old; }
}
function chatExportBar(text) {
  const bar = el("div", "chat-dl", `<span class="chat-dl-l">Export report</span>`);
  ["md", "docx", "pdf"].forEach(f => {
    const b = el("button", "chat-dl-b"); b.type = "button"; b.textContent = f;
    b.onclick = () => downloadChatExport(text, f, b);
    bar.appendChild(b);
  });
  return bar;
}
function appendBubble(role, text) {
  const log = $("#chat-log");
  const hint = $(".chat-hint", log); if (hint) hint.remove();
  const b = el("div", "chat-msg chat-" + role, role === "user" ? esc(text) : mdLite(text));
  log.appendChild(b); log.scrollTop = log.scrollHeight; return b;
}
async function sendChat(text) {
  if (_chatBusy) return;
  _chatBusy = true; $("#chat-send").disabled = true;
  CHAT.push({ role: "user", content: text });
  appendBubble("user", text);
  const bubble = appendBubble("assistant", ""); bubble.classList.add("streaming");
  const log = $("#chat-log");
  let acc = "";
  try {
    const res = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: CHAT })
    });
    if (!res.ok || !res.body) {
      bubble.classList.remove("streaming");
      bubble.innerHTML = `<span class="chat-err">agent unavailable (${res.status})</span>`;
      _chatBusy = false; $("#chat-send").disabled = false; return;
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (; ;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (!data || data === "[DONE]") continue;
        try {
          const o = JSON.parse(data);
          if (o.error) { acc += `\n\n_(${o.error})_`; }
          const ch = o.choices && o.choices[0];
          const delta = ch && ((ch.delta && ch.delta.content) || (ch.message && ch.message.content));
          if (delta) acc += delta;
        } catch (e) { /* ignore keepalive / partial */ }
        bubble.innerHTML = mdLite(acc); log.scrollTop = log.scrollHeight;
      }
    }
  } catch (e) {
    bubble.innerHTML = mdLite(acc) + `<span class="chat-err"> — connection lost</span>`;
  }
  bubble.classList.remove("streaming");
  if (acc) {
    CHAT.push({ role: "assistant", content: acc });
    if (acc.length > 400) bubble.appendChild(chatExportBar(acc));   // offer export on substantial replies
  }
  log.scrollTop = log.scrollHeight;
  _chatBusy = false; $("#chat-send").disabled = false;
}
(function wireChat() {
  const form = $("#chat-form"), input = $("#chat-input");
  if (!form) return;
  form.addEventListener("submit", e => {
    e.preventDefault();
    const t = input.value.trim(); if (!t) return;
    input.value = ""; input.style.height = "auto";
    sendChat(t);
  });
  input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto"; input.style.height = Math.min(160, input.scrollHeight) + "px";
  });
})();

// UI extension panels (okengine#160, ported from the reader): a cockpit-OWNED renderer for
// declarative kinds. The extension supplies only data (coords/fields, server-extracted).
function panelHtml(panel) {
  if (!panel || !panel.kind) return "";
  if (panel.kind === "fields") {
    const items = (panel.items || []).map(i =>
      `<span class="ep-field"><b>${esc(i.label)}</b> ${esc(i.value)}</span>`).join("");
    return items ? `<div class="ext-panel"><div class="ep-title">${esc(panel.title || "")}</div>${items}</div>` : "";
  }
  if (panel.kind === "two-axis") {
    const W = 640, H = 430, P = 46, nodes = panel.nodes || [];
    const px = v => P + (+v || 0) * (W - 2 * P), py = v => H - P - (+v || 0) * (H - 2 * P);
    // x_bands: labeled stage regions + dividers (e.g. Wardley genesis/custom/product/commodity)
    const bands = (panel.x_bands || []).map(b => {
      let s = "";
      if (+b.from > 0) s += `<line x1="${px(b.from).toFixed(1)}" y1="${P}" x2="${px(b.from).toFixed(1)}" y2="${H - P}" class="ep-band"/>`;
      return s + `<text x="${px(((+b.from || 0) + (+b.to || 1)) / 2).toFixed(1)}" y="${H - P + 14}" text-anchor="middle" class="ep-bandlbl">${esc(b.label || "")}</text>`;
    }).join("");
    // edges: value-chain dependency lines between named nodes, beneath the dots
    const bySlug = {};
    nodes.forEach(n => { if (n.slug) bySlug[n.slug] = n; });
    const edges = (panel.edges || []).map(e => {
      const a = bySlug[e[0]], b = bySlug[e[1]];
      if (!a || !b) return "";
      return `<line x1="${px(a.x).toFixed(1)}" y1="${py(a.y).toFixed(1)}" x2="${px(b.x).toFixed(1)}" y2="${py(b.y).toFixed(1)}" class="ep-edge"/>`;
    }).join("");
    const dots = nodes.map(n => {
      const x = px(n.x), y = py(n.y);
      return `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4" class="ep-dot"/>`
        + `<text x="${(x + 6).toFixed(1)}" y="${(y + 3).toFixed(1)}" class="ep-lbl">${esc(n.label || "")}</text>`;
    }).join("");
    return `<div class="ext-panel"><svg viewBox="0 0 ${W} ${H}" class="ep-svg" preserveAspectRatio="xMidYMid meet">` +
      `<line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" class="ep-axis"/>` +
      `<line x1="${P}" y1="${P}" x2="${P}" y2="${H - P}" class="ep-axis"/>` +
      `<text x="${W - P}" y="${H - P + 28}" text-anchor="end" class="ep-axlbl">${esc(panel.x_label || "")}</text>` +
      `<text x="${P - 6}" y="${P - 14}" class="ep-axlbl">${esc(panel.y_label || "")}</text>` +
      bands + edges + dots + `</svg></div>`;
  }
  return "";
}

// ── page overlay (click-through to any wiki page) ────────────────────────────
const pageStack = [];
// Trust/provenance strip (okengine, ported from the reader): a compact "can I trust this?" band
// at the top of every page overlay, from fields the trust lanes + write path already stamp.
function provenanceHtml(p) {
  if (!p || !Object.keys(p).length) return "";
  // clip free-text values so a malformed/garbage field (e.g. a run-on `reliability`) can't blow the
  // strip layout; the full value stays available on hover via title=.
  const clip = (s, n = 40) => { s = String(s); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  const chip = (v, cls = "") => `<span class="pv-chip ${cls}" title="${esc(v)}">`;
  const chips = [];
  if (p.needs_review) chips.push(`<span class="pv-chip pv-warn">⚠ Needs review</span>`);
  if (p.sources) chips.push(`<span class="pv-chip">🔗 ${p.sources} source${p.sources === 1 ? "" : "s"}${p.source_pages ? ` · ${p.source_pages} linked` : ""}</span>`);
  if (p.grounding) {
    const s = p.grounding.supported || 0, u = p.grounding.unsupported || 0;
    chips.push(`<span class="pv-chip ${u ? "pv-warn" : "pv-ok"}">grounding ✓${s}${u ? ` ✗${u}` : ""}</span>`);
  }
  if (p.reviewed_by) chips.push(chip(p.reviewed_by, "pv-ok") + `✓ ${esc(clip(p.reviewed_by))}${p.reviewed_on ? ` · ${esc(clip(p.reviewed_on, 12))}` : ""}</span>`);
  if (p.tlp) chips.push(`<span class="pv-chip pv-tlp pv-tlp-${esc(String(p.tlp).toLowerCase().replace(/[^a-z]/g, ""))}">TLP:${esc(clip(p.tlp, 16))}</span>`);
  if (p.sensitivity) chips.push(chip(p.sensitivity) + `${esc(clip(p.sensitivity))}</span>`);
  if (p.reliability || p.credibility) chips.push(chip(`${p.reliability || ""} ${p.credibility || ""}`.trim()) + `${p.reliability ? `Rel ${esc(clip(p.reliability))}` : ""}${p.reliability && p.credibility ? " · " : ""}${p.credibility ? `Cred ${esc(clip(p.credibility))}` : ""}</span>`);
  if (p.maintained_by) chips.push(chip(p.maintained_by, "pv-dim") + `maintained: ${esc(clip(p.maintained_by))}</span>`);
  if (p.discovered_by) chips.push(chip(p.discovered_by, "pv-dim") + `discovered: ${esc(clip(p.discovered_by))}</span>`);
  return chips.length ? `<div class="provenance">${chips.join("")}</div>` : "";
}

// Page quality/status badges: a problem-only flag row at the very top of the overlay (needs-review,
// no-sources, ungrounded, conflicting, stale, thin, missing-required). Server computes them from
// data already present; a clean page yields none (no row). level -> colour.
function qualityHtml(badges) {
  if (!badges || !badges.length) return "";
  return `<div class="qbadges">` + badges.map(b =>
    `<span class="qb qb-${esc(b.level || "warn")}" title="${esc(b.title || b.label)}">${esc(b.label)}</span>`).join("") + `</div>`;
}
function trustGatedHtml(trust, content, path, reviewEnabled) {
  if (!trust || trust.state === "normal" || trust.state === "verified") return content;
  if (trust.state === "retired") {
    const target = trust.redirect_to
      ? `<a class="wl" data-page="${esc(trust.redirect_to)}">Open canonical record →</a>` : "";
    return `<div class="trust-gate trust-retired"><strong>Retired duplicate</strong>` +
      `<span>This record is retained for audit history and is not a current profile.</span>${target}</div>`;
  }
  const why = (trust.reasons || []).map(esc).join(" · ");
  return `<div class="trust-gate"><strong>Unverified draft — quarantined</strong>` +
    `<span>This entity has not cleared the source and review gates${why ? `: ${why}` : ""}.</span>` +
    (reviewEnabled ? `<button class="review-open" data-review-path="${esc(path)}">Review this page</button>` : "") +
    `</div><div id="review-workspace"></div>` +
    `<details class="quarantined-content"><summary>View unverified content</summary>${content}</details>`;
}

function reviewAffordance(d) {
  if (!d.review_enabled || !d.provenance?.needs_review || d.trust?.state === "quarantined") return "";
  return `<div class="review-affordance"><strong>Human review required</strong>` +
    `<span>This current version is awaiting an evidence decision.</span>` +
    `<button class="review-open" data-review-path="${esc(d.path)}">Review this page</button></div>` +
    `<div id="review-workspace"></div>`;
}

function reviewReasons(rows) {
  return (rows || []).map(r => `<li><b>${esc(r.code || "review")}</b>${r.field ? ` · ${esc(r.field)}` : ""}` +
    `<span>${esc(r.detail || "")}</span></li>`).join("");
}
function reviewEvidence(rows) {
  return (rows || []).map(e => `<div class="review-ev ${e.page ? "resolved" : "unresolved"}">` +
    (e.page ? `<a class="wl" data-page="${esc(e.page)}">${esc(e.name)}</a>` : `<span>${esc(e.name)}</span>`) +
    `<span>${e.page ? "linked" : "unresolved"}${e.reliability ? ` · reliability ${esc(e.reliability)}` : ""}${e.date ? ` · ${esc(e.date)}` : ""}</span></div>`).join("");
}
async function openReview(path) {
  const box = $("#review-workspace"); if (!box) return;
  box.innerHTML = `<div class="review-panel"><div class="empty">Loading review…</div></div>`;
  try {
    const d = await j(`/api/review?path=${encodeURIComponent(path)}`);
    const machine = (d.machine_checks || []).map(c => `<li>${esc(c.outcome)} · ${esc(c.evaluator)} · ${esc(c.checked_at || "")}${c.note ? ` — ${esc(c.note)}` : ""}</li>`).join("");
    const history = (d.history || []).map(h => `<li>${esc(h.decision || h.action || h.state)} · ${esc(h.decision_by || h.assigned_to || "")} · ${esc(h.decision_at || h.at || "")}${h.decision_note ? ` — ${esc(h.decision_note)}` : ""}</li>`).join("");
    const dc = d.decision_context || {}, noun = dc.noun || "record";
    const decisionContext = `<div class="review-decision-context"><h3>Decision to make</h3>` +
      `<div class="review-question">${esc(dc.question || "Is this record supported by its cited evidence?")}</div>` +
      `<blockquote>${esc(dc.proposition || d.title || path)}</blockquote>` +
      `<div class="review-scope"><b>Scope:</b> ${esc(dc.scope || "Decide only the proposition as written.")}</div>` +
      `<dl><dt>Approve ${esc(noun)}</dt><dd>${esc(dc.approve || "The evidence supports it as written.")}</dd>` +
      `<dt>Reject ${esc(noun)}</dt><dd>${esc(dc.reject || "The evidence does not support it as written; this does not prove the opposite.")}</dd>` +
      `<dt>Request changes</dt><dd>${esc(dc.request_changes || "The proposition or evidence needs correction.")}</dd>` +
      `<dt>Defer</dt><dd>${esc(dc.defer || "More evidence is required.")}</dd>` +
      `<dt>Dismiss</dt><dd>${esc(dc.dismiss || "Duplicate, out of scope, or not applicable.")}</dd></dl></div>`;
    box.innerHTML = `<div class="review-panel" data-path="${esc(path)}" data-version="${d.version}" data-hash="${esc(d.hash)}" data-review-id="${esc(d.review_id || "")}">` +
      `<div class="review-head"><strong>Review current version ${d.version}</strong><span>${esc(d.state)}</span></div>` +
      decisionContext +
      `<h3>Why this needs review</h3><ul class="review-reasons">${reviewReasons(d.reasons)}</ul>` +
      `<h3>Evidence · ${d.evidence_resolved}/${d.evidence_total} linked</h3><div class="review-evidence">${reviewEvidence(d.evidence)}</div>` +
      (machine ? `<h3>Machine checks (not human approval)</h3><ul>${machine}</ul>` : "") +
      (history ? `<h3>Decision history</h3><ul>${history}</ul>` : "") +
      `<label class="review-confirm"><input type="checkbox" id="review-confirm"> I examined the scoped content and cited evidence.</label>` +
      `<textarea id="review-note" placeholder="Decision note (required except approval)"></textarea>` +
      `<div class="review-actions"><button data-review-assign>Assign to me</button><button data-decision="approve">Approve ${esc(noun)}</button><button data-decision="request-changes">Request changes</button>` +
      `<button data-decision="reject">Reject ${esc(noun)}</button><button data-decision="dismiss">Dismiss / not applicable</button><button data-decision="defer">Defer</button></div>` +
      `<div class="review-result"></div></div>`;
  } catch (e) { box.innerHTML = `<div class="review-panel review-error">Unable to load review: ${esc(e.message)}</div>`; }
}
async function assignReview(button) {
  const panel = button.closest(".review-panel"), result = $(".review-result", panel);
  button.disabled = true; result.textContent = "Assigning…";
  try {
    const response = await fetch("/api/review/assign", {method:"POST", headers:{"Content-Type":"application/json","X-OKEngine-Review":"1"},
      body:JSON.stringify({path:panel.dataset.path, expected_version:+panel.dataset.version,
        expected_hash:panel.dataset.hash, review_id:panel.dataset.reviewId || ""})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || response.status);
    await openReview(panel.dataset.path);
  } catch (e) { result.textContent = `Assignment failed: ${e.message}`; button.disabled = false; }
}
async function submitReview(button) {
  const panel = button.closest(".review-panel"), decision = button.dataset.decision;
  const result = $(".review-result", panel), note = $("#review-note", panel).value.trim();
  if (!$("#review-confirm", panel).checked) { result.textContent = "Confirm that you examined the scoped content and evidence."; return; }
  if (decision !== "approve" && !note) { result.textContent = "This disposition requires a decision note."; return; }
  $$("button", panel).forEach(b => b.disabled = true); result.textContent = "Saving decision…";
  try {
    const response = await fetch("/api/review/decision", {method:"POST", headers:{"Content-Type":"application/json","X-OKEngine-Review":"1"},
      body:JSON.stringify({path:panel.dataset.path, decision, note, expected_version:+panel.dataset.version,
        expected_hash:panel.dataset.hash, review_id:panel.dataset.reviewId || ""})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || response.status);
    await openPage(panel.dataset.path, false);
  } catch (e) { result.textContent = `Decision failed: ${e.message}`; $$("button", panel).forEach(b => b.disabled = false); }
}
// Fact panel + record details (ported from the reader): the SURFACED frontmatter is the page's
// profile (aliases, origin, refs, …) shown below the body; record-keeping/provenance is tucked
// into a collapsed disclosure. A value resolving to a vault page -> internal a.wl link (the global
// delegated handler opens it); a url-valued field -> external link; otherwise a plain chip.
function _metaRows(meta) {
  const chip = v => v.page
    ? `<a class="wl" data-page="${esc(v.page)}">${esc(v.text)}</a>`
    : v.url
      ? `<a class="mlink" href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">${esc(v.text)}</a>`
      : `<span class="mchip">${esc(v.text)}</span>`;
  return meta.map(m => {
    const single = m.values.length === 1 && !m.values[0].url && !m.values[0].page;
    const vals = single ? `<span class="mtext">${esc(m.values[0].text)}</span>` : m.values.map(chip).join("");
    return `<div class="mrow"><div class="mk">${esc(m.label)}</div><div class="mv">${vals}</div></div>`;
  }).join("");
}
function factPanel(meta) {
  return (meta && meta.length) ? `<div class="meta-panel meta-facts">${_metaRows(meta)}</div>` : "";
}
function auxPanel(meta) {
  return (meta && meta.length)
    ? `<details class="meta-details"><summary>Record details</summary><div class="meta-panel">${_metaRows(meta)}</div></details>` : "";
}
// Multi-source provenance (okengine#42): the assembler's per-field "what each source says" +
// per-source observation records, each source tagged with Admiralty reliability.
const relBadge = s => s.reliability
  ? ` <b class="rel rel-${esc(s.reliability)}" title="Admiralty reliability ${esc(s.reliability)}">${esc(s.reliability)}</b>` : "";
function provPanel(d) {
  const conflicts = d.conflicts || [], obs = d.observations || [];
  if (!conflicts.length && !obs.length) return "";
  let h = `<div class="prov">`;
  if (conflicts.length) {
    h += `<div class="prov-head"><span>⚠ Sources disagree${d.needs_review ? ` <span class="nr">needs review</span>` : ""}</span>` +
      `<label class="prov-filter"><input type="checkbox" id="prov-bfilter"> ≥ B-reliability only</label></div>`;
    conflicts.forEach(c => {
      h += `<div class="cf"><div class="cf-field">${esc(c.field)}</div>` +
        c.values.map(v =>
          `<div class="cf-val${v.is_headline ? " cf-head" : ""}" data-rank="${v.rank}">` +
          `<span class="cf-v">${esc(v.value)}</span>` +
          `<span class="cf-srcs">` + (v.sources.map(s => `<span class="cf-src">${esc(s.name)}${relBadge(s)}</span>`).join("") || "—") + `</span>` +
          (v.is_headline ? `<span class="cf-pick">chosen</span>` : "") + `</div>`).join("") + `</div>`;
    });
  }
  if (obs.length) {
    h += `<div class="prov-head">Per-source records</div><div class="obs-list">` +
      obs.map(o => `<a class="wl obs-item" data-page="${esc(o.key)}">${esc(o.source || o.key.split("/").pop())} ↗</a>`).join("") +
      `</div>`;
  }
  return h + `</div>`;
}
// ≥B filter: dim any conflicting value whose best source reliability ranks below B (4).
function wireProvFilter(root) {
  const cb = $("#prov-bfilter", root); if (!cb) return;
  cb.onchange = () => $$(".cf-val", root).forEach(el =>
    el.classList.toggle("dim", cb.checked && (+el.dataset.rank) < 4));
}

// Evidence section: the page's cited sources as graded, dated citations (not a bare list) —
// name (linked if it resolves to a source page), Admiralty reliability badge, recency date.
function citationsHtml(cites) {
  if (!cites || !cites.length) return "";
  const rows = cites.map(c => {
    const nm = c.page
      ? `<a class="wl cite-n" data-page="${esc(c.page)}">${esc(c.name)}</a>`
      : `<span class="cite-n">${esc(c.name)}</span>`;
    const rel = c.reliability
      ? ` <b class="rel rel-${esc(c.reliability)}" title="Admiralty reliability ${esc(c.reliability)}">${esc(c.reliability)}</b>` : "";
    const dt = c.date ? `<span class="cite-d">${esc(c.date)}</span>` : "";
    return `<div class="cite">${nm}${rel}${dt}</div>`;
  }).join("");
  return `<div class="prov"><div class="prov-head">Evidence <span class="bl-n">${cites.length}</span></div>` +
    `<div class="cite-list">${rows}</div></div>`;
}

function assessmentPanel(rows) {
  if (!rows || !rows.length) return "";
  return `<section class="actor-assessments"><div class="prov-head">CHE actor assessments <span class="bl-n">${rows.length}</span></div>` +
    rows.map(row => `<a class="actor-assessment wl" data-page="${esc(row.path)}">` +
      `<span><b>${esc(row.title)}</b><small>${esc(row.claim || "")}</small></span>` +
      `<span class="assessment-state">${esc(row.status || "—")} · ${row.confidence == null ? "—" : row.confidence.toFixed(2)}${row.confidence_band ? ` (${esc(row.confidence_band)})` : ""}` +
      `${row.needs_review ? ` · <em>needs review</em>` : ""}</span></a>`).join("") + `</section>`;
}

async function openPage(path, push = true) {
  const ov = $("#page-overlay"), c = $("#ov-content");
  ov.hidden = false; c.innerHTML = "<div class='empty'>Loading…</div>";
  try {
    const d = await j(`/api/page?path=${encodeURIComponent(path)}`);
    if (push) pageStack.push(path);
    $("#ov-title").textContent = d.title || path;
    $("#ov-path").textContent = (d.type ? d.type + " · " : "") + (d.rel || path);
    $("#ov-dl").innerHTML = dlLinks(`path=${encodeURIComponent(path)}`);
    // The prose body always leads. The fact panel is reference detail (aliases, techniques,
    // metrics) and follows the body; record-keeping (meta_aux) trails it. The `profiled` flag
    // still governs which fields are primary (fact panel) vs secondary (Record details) — it no
    // longer moves the panel above the body.
    const facts = factPanel(d.meta);
    const profile = panelHtml(d.panel) + d.html + facts + citationsHtml(d.citations) +
      provPanel(d) + auxPanel(d.meta_aux);
    c.innerHTML = qualityHtml(d.quality) + provenanceHtml(d.provenance) + assessmentPanel(d.assessments) +
      reviewAffordance(d) +
      trustGatedHtml(d.trust, profile, d.path || path, d.review_enabled) +
      `<div id="backlinks" class="backlinks"></div>`; c.scrollTop = 0;
    wireProvFilter(c);
    $("#ov-back").style.visibility = pageStack.length > 1 ? "visible" : "hidden";
    loadBacklinks(d.rel || path);
  } catch (e) { c.innerHTML = `<div class='empty'>page not found: ${esc(path)}</div>`; }
}
// IWE knowledge-graph: "what links here". The first call in a 10-min window
// warms the server-side graph (~20s); subsequent ones are instant.
async function loadBacklinks(path) {
  const box = $("#backlinks"); if (!box) return;
  box.innerHTML = `<div class="bl-head">↩ Backlinks</div><div class="bl-empty">finding references…</div>`;
  try {
    const d = await j(`/api/backlinks?path=${encodeURIComponent(path)}`);
    if (!d.count) { box.innerHTML = `<div class="bl-head">↩ Related <span class="bl-n">0</span></div><div class="bl-empty">No pages reference this.</div>`; return; }
    // typed rail: one section per referrer namespace (predictions/findings/entities/…), each with
    // its true count; items capped server-side with a "+N more" tail.
    const groups = (d.groups && d.groups.length) ? d.groups : [{ label: "", count: d.count, items: d.backlinks }];
    box.innerHTML = `<div class="bl-head">↩ Related <span class="bl-n">${d.count}</span></div>` +
      groups.map(g =>
        `<div class="bl-group"><div class="bl-gh">${esc(g.label || g.ns)}<span class="bl-n">${g.count}</span></div>` +
        `<div class="bl-list">` + (g.items || []).map(b =>
          `<a class="bl-item" data-page="${esc(b.key)}"><span class="bl-title">${esc(b.title)}</span></a>`).join("") +
        (g.count > (g.items || []).length ? `<div class="bl-more">+${g.count - (g.items || []).length} more</div>` : "") +
        `</div></div>`).join("");
    $$(".bl-item", box).forEach(a => a.onclick = () => openPage(a.dataset.page));
  } catch (e) { box.innerHTML = `<div class="bl-head">↩ Backlinks</div><div class="bl-empty">unavailable</div>`; }
}
function closeOverlay() { $("#page-overlay").hidden = true; pageStack.length = 0; }
$("#ov-close").onclick = closeOverlay;
$("#ov-back").onclick = () => { pageStack.pop(); const prev = pageStack[pageStack.length - 1]; prev ? openPage(prev, false) : closeOverlay(); };
document.addEventListener("click", e => { const a = e.target.closest("a.wl"); if (a) { e.preventDefault(); openPage(a.dataset.page); } });
document.addEventListener("click", e => {
  const scoped = e.target.closest("[data-review-types]");
  if (scoped) { e.preventDefault(); openReviewQueue(0, scoped.dataset.reviewTypes || ""); return; }
  const open = e.target.closest("[data-review-path]"); if (open) { e.preventDefault(); openReview(open.dataset.reviewPath); return; }
  const assign = e.target.closest("[data-review-assign]"); if (assign) { e.preventDefault(); assignReview(assign); return; }
  const decision = e.target.closest("[data-decision]"); if (decision) { e.preventDefault(); submitReview(decision); }
});

// ── drilldown: a cockpit aggregate (bar/chip/bignum) → its filtered page list (okengine#189) ──
async function openDrill(tab, box, qs) {
  const ov = $("#page-overlay"), c = $("#ov-content");
  ov.hidden = false; c.innerHTML = "<div class='empty'>Loading…</div>";
  try {
    const d = await j(`/api/drill/${encodeURIComponent(tab)}/${encodeURIComponent(box)}?${qs}`);
    pageStack.length = 0;
    $("#ov-title").textContent = d.title || "Matches";
    $("#ov-path").textContent = `${d.count} page${d.count === 1 ? "" : "s"}`;
    $("#ov-dl").innerHTML = "";
    $("#ov-back").style.visibility = "hidden";
    c.innerHTML = d.pages.length
      ? `<div class="drill-list">` + d.pages.map(p =>
          `<a class="wl drow" data-page="${esc(p.path)}"><span class="drow-t">${esc(p.title)}</span>` +
          (p.type ? `<span class="drow-ty">${esc(p.type)}</span>` : "") + `</a>`).join("") + `</div>`
      : `<div class="empty">no matching pages</div>`;
    c.scrollTop = 0;
  } catch (e) { c.innerHTML = `<div class='empty'>drilldown failed: ${esc(e.message)}</div>`; }
}
document.addEventListener("click", e => {
  const el = e.target.closest("[data-drill]");
  if (!el) return;
  e.preventDefault();
  if (el.dataset.dpage) { openPage(el.dataset.dpage); return; }   // value_field bar -> its page
  const qs = el.dataset.ditem != null
    ? "item=" + encodeURIComponent(el.dataset.ditem)
    : "value=" + encodeURIComponent(el.dataset.dval || "");
  openDrill(el.dataset.dtab, el.dataset.dbox, qs);
});

// ── global search ──────────────────────────────────────────────────────────
let _searchTimer;
const gsearch = $("#gsearch"), gresults = $("#gresults");
gsearch.addEventListener("input", () => {
  clearTimeout(_searchTimer);
  const q = gsearch.value.trim();
  if (q.length < 2) { gresults.hidden = true; gresults.innerHTML = ""; return; }
  _searchTimer = setTimeout(() => runSearch(q), 220);
});
async function runSearch(q) {
  try {
    const d = await j(`/api/search?q=${encodeURIComponent(q)}`);
    if (!d.results.length) { gresults.innerHTML = `<div class="gempty">no matches for \u201c${esc(q)}\u201d</div>`; gresults.hidden = false; return; }
    gresults.innerHTML = d.results.map(r =>
      `<div class="gres" data-path="${esc(r.path)}"><span class="gdir">${esc(r.dir)}</span>` +
      `<span class="gtitle">${esc(r.title)}</span><span class="gsnip">${esc(r.snippet)}</span></div>`).join("") +
      (d.total > d.results.length ? `<div class="gmore">${d.total} matches \u2014 showing ${d.results.length}</div>` : "");
    gresults.hidden = false;
    $$(".gres", gresults).forEach(elm => elm.onclick = () => { openPage(elm.dataset.path); closeSearch(); });
  } catch (e) { gresults.innerHTML = `<div class="gempty">search failed</div>`; gresults.hidden = false; }
}
function closeSearch() { gresults.hidden = true; }
gsearch.addEventListener("keydown", e => {
  const items = $$(".gres", gresults);
  let i = items.findIndex(x => x.classList.contains("sel"));
  if (e.key === "ArrowDown") { i = Math.min(items.length - 1, i + 1); items.forEach(x => x.classList.remove("sel")); if (items[i]) { items[i].classList.add("sel"); items[i].scrollIntoView({ block: "nearest" }); } e.preventDefault(); }
  else if (e.key === "ArrowUp") { i = Math.max(0, i - 1); items.forEach(x => x.classList.remove("sel")); if (items[i]) { items[i].classList.add("sel"); items[i].scrollIntoView({ block: "nearest" }); } e.preventDefault(); }
  else if (e.key === "Enter") { const sel = items[i] || items[0]; if (sel) { openPage(sel.dataset.path); closeSearch(); gsearch.blur(); } }
  else if (e.key === "Escape") { closeSearch(); gsearch.blur(); }
});
document.addEventListener("click", e => { if (!e.target.closest(".gsearch-wrap")) closeSearch(); });

// ── keyboard ───────────────────────────────────────────────────────────────
document.addEventListener("keydown", e => {
  if (e.target.matches("input,select,textarea")) { if (e.key === "Escape") e.target.blur(); return; }
  if (e.key === "Escape" && !$("#page-overlay").hidden) { closeOverlay(); return; }
  if (e.key === "/") { gsearch.focus(); e.preventDefault(); return; }
  const rows = $$("table.ledger tbody tr");
  if (e.key === "Escape") { $("#pred-detail").hidden = true; return; }
  if (!rows.length) return;
  let i = rows.findIndex(r => r.classList.contains("sel"));
  if (e.key === "j" || e.key === "ArrowDown") { i = Math.min(rows.length - 1, i + 1); rows[i].click(); rows[i].scrollIntoView({ block: "nearest" }); e.preventDefault(); }
  if (e.key === "k" || e.key === "ArrowUp") { i = Math.max(0, i - 1); rows[i].click(); rows[i].scrollIntoView({ block: "nearest" }); e.preventDefault(); }
});

// ── bootstrap (pull pack config first, then build the shell) ─────────────────
async function bootstrap() {
  let cfg = { title: "cockpit", tabs: ["briefings"] };
  try { cfg = await j("/api/config"); } catch (e) { /* fall back to generic shell */ }
  TAB_DEF_LABELS = cfg.tab_labels || {};
  REVIEW_ENABLED = !!cfg.review_enabled;
  if (cfg.tz) { CLOCK_TZ = cfg.tz; tick(); }   // clock -> deployment timezone (okengine#301)
  document.title = cfg.title || "cockpit";
  $("#brand").innerHTML = `⬢ <span>${esc(cfg.short_title || cfg.title || "cockpit")}</span>`;
  // the cockpit's function tabs (pack-driven) + the two general-purpose tabs from
  // okengine-reader: Browse is always present; Chat only when an agent is configured.
  const tabs = (cfg.tabs && cfg.tabs.length ? cfg.tabs.slice() : ["briefings"]);
  // general-purpose tabs: append only when the pack config didn't place them itself
  if (!tabs.includes("browse")) tabs.push("browse");
  if (cfg.chat_enabled && !tabs.includes("chat")) tabs.push("chat");
  buildTabs(tabs);

  loadStreams();
  window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (TABS.includes(t)) showTab(t); });
  const _q = new URLSearchParams(location.search);
  const _initTab = location.hash.slice(1);
  if (TABS.includes(_initTab)) showTab(_initTab);
  else showTab(TABS[0]);
  const _pageParam = _q.get("page");
  if (_pageParam) openPage(_pageParam);
  const _streamParam = _q.get("stream");
  if (_streamParam) { showTab("briefings"); selectDoc(_streamParam, _q.get("date") || "__latest__", null); }
}
bootstrap();

/* ── Text-size control (A−/A+ header) ── the cockpit's content (dashboards, ledger,
   detail panes) is styled in px, not rem, so scaling the root font-size only grew the
   rem-based chrome (tabs/clock) and left the content untouched. Scale the whole UI with
   `zoom` (relative to DEF) so px and rem alike grow together. Persisted per-browser. */
(function () {
  var KEY = "okengine.cockpit.fontPx", MIN = 12, MAX = 24, STEP = 1, DEF = 14;
  var root = document.documentElement;
  function clamp(v) { return Math.max(MIN, Math.min(MAX, v)); }
  function cur() { var s = parseFloat(localStorage.getItem(KEY)); return clamp(isNaN(s) ? DEF : s); }
  function refresh(v) {
    var d = document.getElementById("fs-dec"), i = document.getElementById("fs-inc");
    if (d) d.disabled = v <= MIN;
    if (i) i.disabled = v >= MAX;
  }
  function apply(v) { root.style.zoom = (v / DEF).toFixed(4); try { localStorage.setItem(KEY, v); } catch (e) {} refresh(v); }
  root.style.zoom = (cur() / DEF).toFixed(4);
  function wire() {
    refresh(cur());
    var d = document.getElementById("fs-dec"), i = document.getElementById("fs-inc");
    if (d) d.addEventListener("click", function () { apply(clamp(cur() - STEP)); });
    if (i) i.addEventListener("click", function () { apply(clamp(cur() + STEP)); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire); else wire();
})();

// ── about panel (mirrors okengine-reader/static/app.js — keep the pair in sync) ──
const aboutOv = $("#about-overlay");
let _aboutLoaded = false;
async function loadAbout() {
  if (_aboutLoaded) return;
  try {
    const a = await j("/api/about");
    const bits = [];
    if (a.vault) bits.push(`Vault: <strong>${esc(a.vault)}${a.vault_version ? " " + esc(a.vault_version) : ""}</strong>`);
    if (a.engine_version) bits.push(`OKEngine ${esc(a.engine_version)}`);
    if (a.hermes_pin) bits.push(`Hermes ${esc(a.hermes_pin)}`);
    $("#about-meta").innerHTML = bits.join(" · ");
    const dep = [];
    if (a.description) dep.push(`<p class="about-desc"><strong>${esc(a.description)}</strong></p>`);
    if (a.mission) dep.push(`<p class="about-mission">${esc(a.mission)}</p>`);
    if (a.installed_domains && a.installed_domains.length)
      dep.push(`<p class="about-installed"><strong>Installed alongside:</strong></p><ul class="about-installed-list">` +
               a.installed_domains.map(d => `<li>${esc(d)}</li>`).join("") + `</ul>`);
    else if (a.sub_domains && a.sub_domains.length)
      dep.push(`<p class="about-installed"><strong>Sub-domains:</strong> ${a.sub_domains.map(esc).join(", ")}</p>`);
    if (a.extensions && a.extensions.length) {
      const short = a.extensions.map(x => esc((x.id || "").replace("okengine.", ""))).join(" · ");
      const rows = a.extensions.map(x =>
        `<li><strong>${esc((x.id || "").replace("okengine.", ""))}</strong>` +
        (x.description ? ` — ${esc(x.description)}` : "") + `</li>`).join("");
      dep.push(`<details class="about-ext"><summary><strong>Extensions:</strong> ${short}</summary>` +
               `<ul class="about-ext-list">${rows}</ul></details>`);
    }
    const depEl = $("#about-deploy");
    if (depEl) depEl.innerHTML = dep.join("");
    _aboutLoaded = true;
  } catch (e) { /* leave the meta line empty */ }
}
$("#about-btn").onclick = () => { aboutOv.hidden = false; loadAbout(); };
$("#about-close").onclick = () => { aboutOv.hidden = true; };
aboutOv.addEventListener("click", e => { if (e.target === aboutOv) aboutOv.hidden = true; });
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !aboutOv.hidden) aboutOv.hidden = true;
});
