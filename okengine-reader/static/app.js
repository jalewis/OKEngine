"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const esc = s => (s ?? "").toString().replace(/[&<>"]/g, m => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
const dlLinks = qs => `<span class="dl">⬇ <a href="/api/download?fmt=md&${qs}">md</a> · <a href="/api/download?fmt=docx&${qs}">docx</a> · <a href="/api/download?fmt=pdf&${qs}">pdf</a></span>`;
const j = u => fetch(u).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });

// ── clock ──────────────────────────────────────────────────────────────────
function tick() {
  const d = new Date();
  $("#clock").textContent = d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}
tick(); setInterval(tick, 30000);

// ── browse: directories → pages ──────────────────────────────────────────────
let CURRENT_DIR = null;
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
    const initial = new URLSearchParams(location.search).get("dir") || (all[0] && all[0].dir);
    const node = [...$$(".dir", abox), ...$$(".dir", box)].find(r => $(".d-name", r).textContent === initial);
    if (initial) selectDir(initial, node);
  } catch (e) { box.innerHTML = `<div class="empty" style="padding:14px">failed (${e.message})</div>`; }
}
function renderPages(pane, heading, pages) {
  if (!pages.length) { pane.innerHTML = `<div class="empty">no pages in ${esc(heading)}</div>`; return; }
  const rows = pages.map(p =>
    `<tr data-page="${esc(p.path)}">
       <td class="pg-title">${esc(p.title)}</td>
       <td class="pg-type">${esc(p.type || "")}</td>
       <td class="pg-upd num">${esc(p.updated || "")}</td>
     </tr>`).join("");
  pane.innerHTML = `<div class="list-head"><h1>${esc(heading)}</h1><span class="meta">${pages.length} pages</span></div>` +
    `<table class="ledger"><thead><tr><th>title</th><th>type</th><th class="num">updated</th></tr></thead><tbody>${rows}</tbody></table>`;
  $$("table.ledger tbody tr", pane).forEach(tr => tr.onclick = e => { if (e.target.closest("a.wl")) return; openPage(tr.dataset.page); });
}
async function selectDir(dir, node) {
  CURRENT_DIR = dir;
  $$(".dir.sel").forEach(n => n.classList.remove("sel"));
  if (node) node.classList.add("sel");
  const pane = $("#list-pane");
  pane.innerHTML = `<div class="empty">Loading ${esc(dir)}…</div>`;
  try { renderPages(pane, dir, (await j(`/api/pages?dir=${encodeURIComponent(dir)}`)).pages); }
  catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

// ── browse BY KIND: pack-declared display groups (label -> related page types) ─
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
  $$(".dir.sel").forEach(n => n.classList.remove("sel"));
  if (node) node.classList.add("sel");
  const pane = $("#list-pane");
  pane.innerHTML = `<div class="empty">Loading ${esc(label)}…</div>`;
  try { renderPages(pane, label, (await j(`/api/pages?group=${encodeURIComponent(label)}`)).pages); }
  catch (e) { pane.innerHTML = `<div class="empty">failed (${e.message})</div>`; }
}

// ── page overlay (click-through to any wiki page) ────────────────────────────
const pageStack = [];
// Structured frontmatter, split into the page's intel (aliases, origin, refs, … — SURFACED
// visibly below the body) and record-keeping (tlp, dates, provenance — collapsed).
function _metaRows(meta) {
  return meta.map(m => {
    const single = m.values.length === 1 && !m.values[0].url;
    const vals = single
      ? `<span class="mtext">${esc(m.values[0].text)}</span>`
      : m.values.map(v => v.url
          ? `<a class="mlink" href="${esc(v.url)}" target="_blank" rel="noopener noreferrer">${esc(v.text)}</a>`
          : `<span class="mchip">${esc(v.text)}</span>`).join("");
    return `<div class="mrow"><div class="mk">${esc(m.label)}</div><div class="mv">${vals}</div></div>`;
  }).join("");
}
// the knowledge fields — visible (this IS the entity's profile)
function factPanel(meta) {
  return (meta && meta.length) ? `<div class="meta-panel meta-facts">${_metaRows(meta)}</div>` : "";
}
// record-keeping/provenance — collapsed so it doesn't bury the intel
function auxPanel(meta) {
  return (meta && meta.length)
    ? `<details class="meta-details"><summary>Record details</summary><div class="meta-panel">${_metaRows(meta)}</div></details>` : "";
}
// ── multi-source provenance (okengine#42): "what each source says" + drill-down ──
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
          (v.is_headline ? `<span class="cf-pick">chosen</span>` : "") + `</div>`).join("") +
        `</div>`;
    });
  }
  if (obs.length) {
    h += `<div class="prov-head">Per-source records</div><div class="obs-list">` +
      obs.map(o => `<a class="wl obs-item" data-page="${esc(o.key)}">${esc(o.source || o.key.split("/").pop())} ↗</a>`).join("") +
      `</div>`;
  }
  return h + `</div>`;
}
// ≥B filter: hide any conflicting value whose best source reliability ranks below B (4).
function wireProvFilter(root) {
  const cb = $("#prov-bfilter", root); if (!cb) return;
  cb.onchange = () => $$(".cf-val", root).forEach(el =>
    el.classList.toggle("dim", cb.checked && (+el.dataset.rank) < 4));
}

async function openPage(path, push = true) {
  const ov = $("#page-overlay"), c = $("#ov-content");
  ov.hidden = false; c.innerHTML = "<div class='empty'>Loading…</div>";
  try {
    const d = await j(`/api/page?path=${encodeURIComponent(path)}`);
    if (push) pageStack.push(path);
    $("#ov-title").textContent = d.title || path;
    $("#ov-path").textContent = (d.type ? d.type + " · " : "") + (d.rel || path);
    $("#ov-dl").innerHTML = dlLinks(`path=${encodeURIComponent(d.rel || path)}`);
    c.innerHTML = d.html + factPanel(d.meta) + provPanel(d) + auxPanel(d.meta_aux) + `<div id="backlinks" class="backlinks"></div>`; c.scrollTop = 0;
    wireProvFilter(c);
    $("#ov-back").style.visibility = pageStack.length > 1 ? "visible" : "hidden";
    loadBacklinks(d.rel || path);
  } catch (e) { c.innerHTML = `<div class='empty'>page not found: ${esc(path)}</div>`; }
}
// IWE knowledge-graph: "what links here". The first call warms the server-side
// graph; subsequent ones are instant.
async function loadBacklinks(path) {
  const box = $("#backlinks"); if (!box) return;
  box.innerHTML = `<div class="bl-head">↩ Backlinks</div><div class="bl-empty">finding references…</div>`;
  try {
    const d = await j(`/api/backlinks?path=${encodeURIComponent(path)}`);
    if (!d.count) { box.innerHTML = `<div class="bl-head">↩ Backlinks <span class="bl-n">0</span></div><div class="bl-empty">No pages link here.</div>`; return; }
    box.innerHTML = `<div class="bl-head">↩ Backlinks <span class="bl-n">${d.count}</span></div>` +
      `<div class="bl-list">` + d.backlinks.map(b =>
        `<a class="bl-item" data-page="${esc(b.key)}"><span class="bl-dir">${esc((b.key.split("/")[0]) || "")}</span><span class="bl-title">${esc(b.title)}</span></a>`).join("") +
      `</div>`;
    $$(".bl-item", box).forEach(a => a.onclick = () => openPage(a.dataset.page));
  } catch (e) { box.innerHTML = `<div class="bl-head">↩ Backlinks</div><div class="bl-empty">unavailable</div>`; }
}
function closeOverlay() { $("#page-overlay").hidden = true; pageStack.length = 0; }
$("#ov-close").onclick = closeOverlay;
$("#ov-back").onclick = () => { pageStack.pop(); const prev = pageStack[pageStack.length - 1]; prev ? openPage(prev, false) : closeOverlay(); };
document.addEventListener("click", e => { const a = e.target.closest("a.wl"); if (a) { e.preventDefault(); openPage(a.dataset.page); } });

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
    if (!d.results.length) { gresults.innerHTML = `<div class="gempty">no matches for “${esc(q)}”</div>`; gresults.hidden = false; return; }
    gresults.innerHTML = d.results.map(r =>
      `<div class="gres" data-path="${esc(r.path)}"><span class="gdir">${esc(r.dir)}</span>` +
      `<span class="gtitle">${esc(r.title)}</span><span class="gsnip">${esc(r.snippet)}</span></div>`).join("") +
      (d.total > d.results.length ? `<div class="gmore">${d.total} matches — showing ${d.results.length}</div>` : "");
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

// ── about panel ──────────────────────────────────────────────────────────────
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
    // Project/repo link is deployment-configured (OKENGINE_PROJECT_URL / pack.yaml);
    // show it only when set — the engine ships no hardcoded repo URL.
    if (a.project_url) {
      const r = $("#about-repo");
      r.href = a.project_url;
      $("#about-repo-li").hidden = false;
    }
    _aboutLoaded = true;
  } catch (e) { /* leave the meta line empty */ }
}
const openAbout = () => { aboutOv.hidden = false; loadAbout(); };
const closeAbout = () => { aboutOv.hidden = true; };
$("#about-btn").onclick = openAbout;
$("#about-close").onclick = closeAbout;
aboutOv.addEventListener("click", e => { if (e.target === aboutOv) closeAbout(); });

// ── keyboard ───────────────────────────────────────────────────────────────
document.addEventListener("keydown", e => {
  if (e.target.matches("input,select,textarea")) { if (e.key === "Escape") e.target.blur(); return; }
  if (e.key === "Escape" && !aboutOv.hidden) { closeAbout(); return; }
  if (e.key === "Escape" && !$("#page-overlay").hidden) { closeOverlay(); return; }
  if (e.key === "/") { gsearch.focus(); e.preventDefault(); }
});

// ── tabs (Browse / Chat) ─────────────────────────────────────────────────────
function showView(name) {
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + name));
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === name));
  if (name === "chat") $("#chat-input").focus();
}
$$(".tab").forEach(t => t.onclick = () => showView(t.dataset.view));

// ── agent chat ───────────────────────────────────────────────────────────────
// Relays to THE Hermes agent (/api/chat → its OpenAI-compatible API). The agent answers
// by navigating the vault via its graph tools; we render its markdown and turn page paths
// and [[wikilinks]] into clickable links (reusing the a.wl → openPage handler).
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
  return h.replace(/\n/g, "<br>");
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
  if (acc) CHAT.push({ role: "assistant", content: acc });
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
async function initChat() {
  try { if ((await j("/api/about")).chat_enabled) $("#tab-chat").hidden = false; }
  catch (e) { /* leave the tab hidden */ }
}

// ── boot ─────────────────────────────────────────────────────────────────────
loadTree();
loadGroups();
initChat();
const _pageParam = new URLSearchParams(location.search).get("page");
if (_pageParam) openPage(_pageParam);
