/* content.js — overlay on robinhood.com prediction-market event pages.
 *
 * Reads nothing private: the event id/contracts come from the public page, quotes from the
 * public quotes API (via the background worker). Renders a panel with, per outcome:
 * fair value, each venue's ask / all-in cost (fees included), the edge, and the max price
 * to pay here (taker and resting/maker) so that hedging the other side elsewhere still
 * locks in the configured margin. Never places orders.
 */
(function arbEngineOverlay() {
  "use strict";
  const PANEL_ID = "arbe-panel";
  const EVENT_RE = /\/prediction-markets\/[^/]+\/events\/[^/]+\/?/;
  let lastUrl = null, timer = null, refreshSeconds = 15, collapsed = false, lastAnalysis = null;

  const fmtP = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "¢");
  const fmtPct = (x, signed) => (x == null ? "–" : (signed && x > 0 ? "+" : "") + (x * 100).toFixed(1) + "%");
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function eventUrl() {
    const m = EVENT_RE.exec(location.pathname);
    return m ? location.origin + m[0].replace(/\/?$/, "/") : null;
  }

  function ensurePanel() {
    let p = document.getElementById(PANEL_ID);
    if (p) return p;
    p = document.createElement("div");
    p.id = PANEL_ID;
    p.innerHTML = `<div class="arbe-head"><span class="arbe-title">Arb Engine</span><span class="arbe-status"></span><button class="arbe-btn arbe-refresh" title="Refresh">↻</button><button class="arbe-btn arbe-collapse" title="Collapse">–</button></div><div class="arbe-body"><div class="arbe-muted">Open a game / match page to see fair value and max-buy prices.</div></div>`;
    document.body.appendChild(p);
    p.querySelector(".arbe-refresh").addEventListener("click", () => run(true));
    p.querySelector(".arbe-collapse").addEventListener("click", () => { collapsed = !collapsed; p.classList.toggle("arbe-collapsed", collapsed); });
    return p;
  }

  function setStatus(text, cls) {
    const el = ensurePanel().querySelector(".arbe-status");
    el.textContent = text;
    el.className = "arbe-status " + (cls || "");
  }

  function venueName(v) {
    if (v.venue === "robinhood") return "Robinhood" + (v.exchange ? " · " + v.exchange : "");
    return v.venue === "polymarket" ? "Polymarket" : v.venue.charAt(0).toUpperCase() + v.venue.slice(1);
  }

  function render(res) {
    const body = ensurePanel().querySelector(".arbe-body");
    if (!res.ok) { body.innerHTML = `<div class="arbe-err">${esc(res.error)}</div>`; return; }
    if (!res.analysis) { body.innerHTML = `<div class="arbe-muted">${esc(res.note || "Nothing to analyse on this page.")}</div>`; return; }
    const a = res.analysis;
    lastAnalysis = a;
    if (a.lines) { renderLines(res); return; }
    let html = "";
    if (a.arb) {
      const cls = a.arb.isArb ? "arbe-good" : "arbe-bad";
      const legs = a.arb.legs.map((l) => `${esc(l.label || l.outcome)} @ ${fmtP(l.price)} on ${esc(l.venue)}`).join(" + ");
      html += `<div class="arbe-arb ${cls}"><b>${a.arb.isArb ? "ARB" : "No arb"}</b> — cheapest legs sum to ${(a.arb.grossSum * 100).toFixed(1)}¢; fee-adjusted margin <b>${fmtPct(a.arb.margin, true)}</b> per $1 (${a.arb.contracts} contracts: ${a.arb.profit >= 0 ? "+" : ""}$${a.arb.profit.toFixed(2)})<div class="arbe-muted">${legs}</div></div>`;
    }
    for (const row of a.rows) {
      html += `<div class="arbe-row"><div class="arbe-rowhead"><span class="arbe-outcome">${esc(row.label)}</span><span>fair <b>${fmtP(row.fair)}</b></span><span class="${row.edge > 0 ? "arbe-good" : "arbe-bad"}">edge ${fmtPct(row.edge, true)} @ ${esc(row.best || "–")}</span></div><table class="arbe-table"><thead><tr><th>venue</th><th>ask</th><th>bid</th><th>fee/ct</th><th>all-in</th><th>max buy (take)</th><th>max buy (rest)</th></tr></thead><tbody>`;
      for (const v of row.venues) {
        const tag = v.mirror ? ` <span class="arbe-tag" title="Robinhood resells this exchange's order book; same prices, higher fees">= ${esc(v.mirror)} book</span>` : "";
        const link = v.url ? `<a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(venueName(v))}</a>` : esc(venueName(v));
        html += `<tr class="${v.venue === "robinhood" ? "arbe-here" : ""}"><td title="${esc(v.feeNote)}">${link}${tag}</td><td>${fmtP(v.ask)}</td><td>${fmtP(v.bid)}</td><td>${fmtP(v.feePerContract)}</td><td><b>${fmtP(v.allIn)}</b></td><td>${fmtP(v.maxBuyTaker)}</td><td>${fmtP(v.maxBuyMaker)}</td></tr>`;
      }
      html += `</tbody></table></div>`;
    }
    if (a.errors && a.errors.length) html += `<div class="arbe-muted arbe-small">${a.errors.map(esc).join("<br>")}</div>`;
    html += `<div class="arbe-muted arbe-small">Size ${a.contracts} contracts · target margin ${fmtPct(a.targetMargin)} · fees: Robinhood commission (Gold ${a.gold ? "on" : "off"}) + $0.01/ct exchange; Kalshi 7% taker / 1.75% maker × p(1−p); Polymarket 5% taker × p(1−p). Max buy = highest price on this venue that still locks the margin after hedging the other side at its cheapest current ask. Informational only — verify before trading.</div>`;
    body.innerHTML = html;
    decorateTabs(a);
  }

  // Spread / total pages: one row per line, arbs first.
  function renderLines(res) {
    const a = res.analysis;
    const body = ensurePanel().querySelector(".arbe-body");
    const arbs = a.lines.filter((l) => l.arb && l.arb.isArb && l.fillable);
    let html = `<div class="arbe-arb ${arbs.length ? "arbe-good" : "arbe-muted"}"><b>${esc(res.event.name)}</b> — ${a.lines.length} ${esc(a.marketType)} lines, <b>${arbs.length}</b> fee-adjusted arb${arbs.length === 1 ? "" : "s"} fillable ≥ 1 contract</div>`;
    html += `<table class="arbe-table"><thead><tr><th>line</th><th>side</th><th>here</th><th>all-in</th><th>max buy</th><th>best hedge</th><th>margin</th></tr></thead><tbody>`;
    for (const l of a.lines) {
      const m = l.arb ? l.arb.margin : null;
      const cls = l.arb && l.arb.isArb ? (l.fillable ? "arbe-good" : "arbe-thin") : "";
      l.rows.forEach((row, i) => {
        const here = row.venues.find((v) => v.venue === "robinhood");
        const hedgeRow = l.rows.find((r) => r !== row);
        const hedge = hedgeRow ? hedgeRow.venues.find((v) => v.allIn != null && !v.mirror) : null;
        html += `<tr class="${cls}">${i === 0 ? `<td rowspan="2"><b>${esc(l.title)}</b>${l.arb && l.arb.isArb ? `<div class="arbe-small">${l.fillable ? "fillable " + (l.sizedContracts || "?") + " ct" : "thin"}</div>` : ""}</td>` : ""}<td>${esc(row.label)}</td><td>${fmtP(here ? here.ask : null)}</td><td>${fmtP(here ? here.allIn : null)}</td><td><b>${fmtP(here ? here.maxBuyTaker : null)}</b></td><td>${hedge ? esc(hedgeRow.label) + " @ " + fmtP(hedge.allIn) + " " + esc(hedge.venue) : "–"}</td>${i === 0 ? `<td rowspan="2" class="${m != null && m > 0 ? "arbe-good" : "arbe-bad"}">${fmtPct(m, true)}</td>` : ""}</tr>`;
      });
    }
    html += `</tbody></table>`;
    if (a.errors && a.errors.length) html += `<div class="arbe-muted arbe-small">${a.errors.map(esc).join("<br>")}</div>`;
    html += `<div class="arbe-muted arbe-small">here = Robinhood ask for that side; all-in includes commission + exchange fee; max buy = highest price here that still locks the margin against the cheapest hedge of the other side. Half-point lines cannot push. Informational only.</div>`;
    body.innerHTML = html;
    decorateLineTabs(a);
  }
  function decorateLineTabs(a) {
    const tabs = document.querySelectorAll('[role="tablist"][aria-label="Contracts"] [role="tab"]');
    const byContract = new Map(a.lines.map((l) => [l.contractId, l]));
    tabs.forEach((tab) => {
      const text = (tab.textContent || "").toLowerCase();
      // Tab text is the contract's long name, e.g. "Buffalo wins by over 1.5 points 65¢" / "Over 49.5 points 64¢".
      const l = a.lines.find((x) => { const yes = x.rows[0]; return yes && text.indexOf(String(x.line)) >= 0 && (x.marketType === "total" || text.indexOf(yes.label.split(" ")[0].toLowerCase()) >= 0); });
      let badge = tab.querySelector(".arbe-badge");
      if (!l) { if (badge) badge.remove(); return; }
      const here = l.rows[0].venues.find((v) => v.venue === "robinhood");
      if (!badge) { badge = document.createElement("span"); badge.className = "arbe-badge"; tab.appendChild(badge); }
      const good = l.arb && l.arb.isArb && l.fillable;
      badge.className = "arbe-badge " + (good ? "arbe-good" : "arbe-bad");
      badge.textContent = `max ${fmtP(here ? here.maxBuyTaker : null)}` + (good ? ` · arb ${fmtPct(l.arb.margin, true)}` : "");
    });
  }

  // Badge the contract tabs ("Philadelphia 77¢") with fair value and the max-buy price.
  function decorateTabs(a) {
    const tabs = document.querySelectorAll('[role="tablist"][aria-label="Contracts"] [role="tab"]');
    tabs.forEach((tab) => {
      const text = tab.textContent || "";
      const row = a.rows.find((r) => text.toLowerCase().indexOf(String(r.label).toLowerCase()) >= 0);
      let badge = tab.querySelector(".arbe-badge");
      if (!row) { if (badge) badge.remove(); return; }
      const here = row.venues.find((v) => v.venue === "robinhood");
      if (!badge) { badge = document.createElement("span"); badge.className = "arbe-badge"; tab.appendChild(badge); }
      const edge = row.edge;
      badge.className = "arbe-badge " + (edge != null && edge > 0 ? "arbe-good" : "arbe-bad");
      badge.textContent = `fair ${fmtP(row.fair)} · all-in ${fmtP(here ? here.allIn : null)} · max ${fmtP(here ? here.maxBuyTaker : null)}`;
      badge.title = "Arb Engine: consensus fair value · your all-in cost here incl. fees · max price to pay here to lock an arb vs the cheapest hedge elsewhere";
    });
  }

  function run(force) {
    const url = eventUrl();
    if (!url) { if (document.getElementById(PANEL_ID)) ensurePanel().querySelector(".arbe-body").innerHTML = `<div class="arbe-muted">Open a game / match page to see fair value and max-buy prices.</div>`; return; }
    if (!force && url === lastUrl && lastAnalysis && Date.now() - lastAnalysis.fetchedAt < refreshSeconds * 1000) return;
    lastUrl = url;
    setStatus("loading…", "");
    chrome.runtime.sendMessage({ type: "analyze", url }, (res) => {
      if (chrome.runtime.lastError) { setStatus("error", "arbe-bad"); render({ ok: false, error: chrome.runtime.lastError.message }); return; }
      if (!res) { setStatus("no response", "arbe-bad"); return; }
      setStatus(res.ok ? new Date().toLocaleTimeString() + (res.analysis && res.analysis.source === "bridge" ? " · engine" : "") : "error", res.ok ? "arbe-good" : "arbe-bad");
      render(res);
    });
  }

  function start() {
    ensurePanel();
    chrome.runtime.sendMessage({ type: "settings" }, (s) => { if (s && s.refreshSeconds) refreshSeconds = Math.max(5, Number(s.refreshSeconds)); });
    run(true);
    if (timer) clearInterval(timer);
    timer = setInterval(() => run(false), 2000);  // cheap tick; actual refresh gated by refreshSeconds
    // SPA navigations: watch the URL and re-run.
    let href = location.href;
    setInterval(() => { if (location.href !== href) { href = location.href; lastAnalysis = null; run(true); } }, 500);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
