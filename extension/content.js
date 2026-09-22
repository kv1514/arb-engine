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
  let lastUrl = null, timer = null, refreshSeconds = 1, collapsed = false, lastAnalysis = null, inflight = false;
  // Freshness / mode state for the header and the 500 ms ticker (never touches the tables).
  let lastFetchedAt = 0, lastMode = null, bridgeRetryAt = 0, bridgeDown = false, quoteAges = [];
  // Render diffing: last HTML string per section and last text per flagged value (data-k).
  const SECTIONS = ["msg", "inplay", "arb", "rows", "lines", "errors", "foot"];
  let lastHtml = {}, lastVals = {};

  const fmtP = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "¢");
  const fmtC = (x) => (x == null ? "–" : Math.round(x * 100) + "¢");  // whole cents for the small card badges
  const fmtPct = (x, signed) => (x == null ? "–" : (signed && x > 0 ? "+" : "") + (x * 100).toFixed(1) + "%");
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // A value that may move between renders: `flashChanged` compares its text with the last render
  // and adds .arbe-flash (a one-shot CSS animation) when it differs. Keys are stable per page.
  const val = (k, text) => `<span class="arbe-v" data-k="${esc(k)}">${text}</span>`;
  const fmtAge = (s) => (s < 10 ? s.toFixed(1) : String(Math.round(s))) + " s";

  // Pure: the freshness line. Amber after 3 s, red after 10 s (scaled up when the user asked
  // for a slower refresh, so a 5 s cadence is not "stale" by design), with the live mode and,
  // when the engine reports them, per-venue quote ages advanced by the time since the fetch.
  function freshness(ageS, mode, opts) {
    opts = opts || {};
    const every = Math.max(1, opts.refreshSeconds || 1);
    const amberAfter = Math.max(3, every + 2), redAfter = Math.max(10, every * 3);
    const cls = ageS >= redAfter ? "arbe-bad" : ageS >= amberAfter ? "arbe-amber" : "arbe-good";
    let text = `updated ${fmtAge(ageS)} ago · ${mode || "–"}`;
    if (ageS >= redAfter) text += ` — stale — ${mode === "engine" ? "bridge" : "worker"} not answering?`;
    if (opts.bridgeDown) text += ` · bridge down${opts.retryInS != null ? `, retry in ${Math.max(0, Math.ceil(opts.retryInS))} s` : ""}`;
    const ages = (opts.quoteAges || []).filter((v) => v.quoteAge != null);
    if (ages.length) text += " · quotes: " + ages.map((v) => `${v.venue} ${fmtAge(v.quoteAge + ageS)}`).join(", ");
    return { text, cls };
  }

  const APP_RE = /^\/events\/([^/?#]+)/;  // logged-in trading route: /events/<slug>?contract=<id>
  function eventUrl() {
    const m = EVENT_RE.exec(location.pathname);
    if (m) return location.origin + m[0].replace(/\/?$/, "/");
    const a = APP_RE.exec(location.pathname);
    return a ? location.origin + "/events/" + a[1] + "/" : null;
  }

  function ensurePanel() {
    let p = document.getElementById(PANEL_ID);
    if (p) return p;
    p = document.createElement("div");
    p.id = PANEL_ID;
    p.innerHTML = `<div class="arbe-head"><span class="arbe-title">Arb Engine</span><span class="arbe-status"></span><button class="arbe-btn arbe-refresh" title="Refresh">↻</button><button class="arbe-btn arbe-collapse" title="Collapse">–</button><div class="arbe-fresh arbe-muted"></div></div><div class="arbe-body">${SECTIONS.map((n) => `<div class="arbe-sec" data-sec="${n}"></div>`).join("")}</div>`;
    document.body.appendChild(p);
    p.querySelector(".arbe-refresh").addEventListener("click", () => run(true));
    p.querySelector(".arbe-collapse").addEventListener("click", () => { collapsed = !collapsed; p.classList.toggle("arbe-collapsed", collapsed); });
    paint({ msg: `<div class="arbe-muted">Open a game / match page to see fair value and max-buy prices.</div>` });
    return p;
  }

  function setStatus(text, cls) {
    const el = ensurePanel().querySelector(".arbe-status");
    if (el.textContent !== text) el.textContent = text;
    const c = "arbe-status " + (cls || "");
    if (el.className !== c) el.className = c;
  }

  // Assign innerHTML per section only when its HTML string changed since the last paint, so a
  // quiet market re-renders nothing (hover tooltips survive, no visible thrash). `partial`
  // leaves the sections not named alone (an error beside the last good tables).
  function paint(parts, partial) {
    const body = ensurePanel().querySelector(".arbe-body");
    for (const name of SECTIONS) {
      if (partial && !(name in parts)) continue;
      const html = parts[name] || "";
      if (lastHtml[name] === html) continue;
      const el = body.querySelector(`.arbe-sec[data-sec="${name}"]`);
      el.innerHTML = html;
      lastHtml[name] = html;
      flashChanged(el);
    }
  }
  function flashChanged(root) {
    root.querySelectorAll(".arbe-v[data-k]").forEach((el) => {
      const k = el.dataset.k, t = el.textContent;
      if (lastVals[k] !== undefined && lastVals[k] !== t) el.classList.add("arbe-flash");
      lastVals[k] = t;
    });
  }
  function resetRender() { lastHtml = {}; lastVals = {}; lastAnalysis = null; lastFetchedAt = 0; lastMode = null; quoteAges = []; }

  // 500 ms ticker: only the freshness line (text + class) — never the tables.
  function tickFresh() {
    const el = ensurePanel().querySelector(".arbe-fresh");
    if (!el) return;
    if (!lastFetchedAt) { const t = inflight ? "loading…" : ""; if (el.textContent !== t) el.textContent = t; return; }
    const now = Date.now();
    const f = freshness((now - lastFetchedAt) / 1000, lastMode, { refreshSeconds, quoteAges, bridgeDown, retryInS: bridgeDown && bridgeRetryAt ? (bridgeRetryAt - now) / 1000 : null });
    if (el.textContent !== f.text) el.textContent = f.text;
    const cls = "arbe-fresh " + f.cls;
    if (el.className !== cls) el.className = cls;
  }
  // Remember what the worker said about the bridge so the header and ticker can say which mode is live.
  function noteBridge(res) {
    const b = res && res.bridge;
    bridgeDown = !!(b && b.up === false);
    bridgeRetryAt = bridgeDown && b.retryInMs ? Date.now() + b.retryInMs : 0;
  }
  function modeLabel(res) {
    const src = res && res.analysis && res.analysis.source;
    return src === "bridge" ? "engine" : src === "direct" ? "direct" : (res && res.bridge && res.bridge.mode === "bridge" ? "engine" : "direct");
  }

  function venueName(v) {
    // A NO-side row is the OTHER contract's NO (Rothera pays $1 on a tie for it): label it so it
    // is never mistaken for the YES contract on this page.
    const side = v.side === "no" ? " · NO" : "";
    if (v.venue === "robinhood") return "Robinhood" + (v.exchange ? " · " + v.exchange : "") + side;
    return (v.venue === "polymarket" ? "Polymarket" : v.venue.charAt(0).toUpperCase() + v.venue.slice(1)) + side;
  }
  // The contract quoted on this page: Robinhood's YES side (NO rows are the other contract).
  function hereOf(venues) { return (venues || []).find((v) => v.venue === "robinhood" && v.side !== "no") || null; }

  function render(res) {
    noteBridge(res);
    if (!res.ok) {
      // Keep the last good tables on a transient error: the freshness line goes amber, then red.
      if (lastAnalysis && res.error) paint({ errors: `<div class="arbe-err arbe-small">${esc(res.error)}</div>` }, true);
      else paint({ msg: `<div class="arbe-err">${esc(res.error)}</div>` });
      return;
    }
    if (!res.analysis) { paint({ msg: `<div class="arbe-muted">${esc(res.note || "Nothing to analyse on this page.")}</div>` }); return; }
    const a = res.analysis;
    lastAnalysis = a;
    lastFetchedAt = Date.now();
    lastMode = modeLabel(res);
    quoteAges = (a.venues || []).filter((v) => v && typeof v === "object" && v.quoteAge != null);
    if (a.lines) { renderLines(res); return; }
    const parts = { inplay: a.inplay ? renderInplay(a.inplay) : "", arb: "", rows: "", errors: "", foot: "" };
    if (a.arb) {
      const cls = a.arb.isArb ? "arbe-good" : "arbe-bad";
      const legs = a.arb.legs.map((l) => `${esc(l.label || l.outcome)} @ ${fmtP(l.price)} on ${esc(l.venue)}`).join(" + ");
      const sized = a.arb.isArb && a.arb.sizedContracts ? `<div class="arbe-good"><b>Buy ${a.arb.sizedContracts} contracts</b> (what the books hold at these prices): ${(a.arb.sizedLegs || []).map((l) => `${l.contracts} × ${esc(l.label)} @ ${fmtP(l.price)} on ${esc(l.venue)}`).join(" + ")} → locked ${a.arb.sizedProfit >= 0 ? "+" : ""}$${Number(a.arb.sizedProfit).toFixed(2)} after fees</div>` : (a.arb.isArb ? `<div class="arbe-muted arbe-small">arb, but no depth at these prices (top-of-book size 0)</div>` : "");
      parts.arb = `<div class="arbe-arb ${cls}"><b>${a.arb.isArb ? "ARB" : "No arb"}</b> — cheapest legs sum to ${val("arb:gross", (a.arb.grossSum * 100).toFixed(1) + "¢")}; fee-adjusted margin <b>${val("arb:margin", fmtPct(a.arb.margin, true))}</b> per $1 (${a.arb.contracts} contracts: ${a.arb.profit >= 0 ? "+" : ""}$${a.arb.profit.toFixed(2)})<div class="arbe-muted">${legs}</div>${sized}</div>`;
    }
    // LAG: one venue has repriced and an executable one has not yet — the dip that is cheap
    // (docs/MODEL.md "The first live Sunday": the laggard caught up within ~23 s on 80% of moves).
    if (a.lags && a.lags.length) {
      parts.arb += a.lags.map((l) => `<div class="arbe-arb arbe-good arbe-lag"><b>LAG</b> — ${esc(l.leader)} moved ${(l.lead_move * 100).toFixed(0) >= 0 ? "+" : ""}${(l.lead_move * 100).toFixed(0)}¢, ${esc(l.follower)} has not: <b>buy ${esc(l.label)} on ${l.url ? `<a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.follower)}</a>` : esc(l.follower)} at ${fmtP(l.ask)}</b> (all-in ${fmtP(l.all_in)}) vs ${esc(l.leader)} mid ${fmtP(l.leader_mid)} — edge <b>${fmtPct(l.edge, true)}</b>${l.suggested_contracts ? ` → ${l.suggested_contracts} contracts` : ""}${l.depth != null ? ` (${Math.floor(l.depth)} offered)` : ""}<div class="arbe-muted arbe-small">act within ~20 s; the laggard usually catches up, and a leader that reverses means the print was wrong</div></div>`).join("");
    }
    let html = "";
    for (const row of a.rows) {
      const rk = "row:" + row.outcome;
      html += `<div class="arbe-row"><div class="arbe-rowhead"><span class="arbe-outcome">${esc(row.label)}</span><span>fair <b>${val(rk + ":fair", fmtP(row.fair))}</b></span><span class="${row.edge > 0 ? "arbe-good" : "arbe-bad"}">edge ${val(rk + ":edge", fmtPct(row.edge, true))} @ ${esc(row.best || "–")}</span></div><table class="arbe-table"><thead><tr><th>venue</th><th>ask</th><th>size</th><th>bid</th><th>fee/ct</th><th>all-in</th><th>max buy (take)</th><th>max buy (rest)</th></tr></thead><tbody>`;
      for (const v of row.venues) {
        const vk = rk + ":" + v.venue + (v.side === "no" ? "#no" : "") + (v.exchange ? "@" + v.exchange : "");
        const tag = (v.mirror ? ` <span class="arbe-tag" title="Robinhood resells this exchange's order book; same prices, higher fees">= ${esc(v.mirror)} book</span>` : "") + (v.ineligible ? ` <span class="arbe-tag arbe-signal" title="${esc(v.ineligible)}: priced into the fair value, never an arb leg (US accounts cannot trade there)">signal only</span>` : "") + (v.quoteAge != null && v.quoteAge >= 2 ? ` <span class="arbe-tag arbe-age" title="this venue's quote was already this old when the engine priced it (whole seconds; exact ages tick in the header)">quote ${Math.round(v.quoteAge)} s old</span>` : "");
        const link = v.url ? `<a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(venueName(v))}</a>` : esc(venueName(v));
        html += `<tr class="${v.venue === "robinhood" && v.side !== "no" ? "arbe-here" : ""}${v.ineligible ? " arbe-ineligible" : ""}"><td title="${esc(v.feeNote)}">${link}${tag}</td><td>${val(vk + ":ask", fmtP(v.ask))}</td><td>${val(vk + ":size", v.askSize == null ? "–" : String(Math.floor(v.askSize)))}</td><td>${val(vk + ":bid", fmtP(v.bid))}</td><td>${fmtP(v.feePerContract)}</td><td><b>${val(vk + ":allin", fmtP(v.allIn))}</b></td><td>${val(vk + ":maxt", fmtP(v.maxBuyTaker))}</td><td>${val(vk + ":maxm", fmtP(v.maxBuyMaker))}</td></tr>`;
      }
      html += `</tbody></table></div>`;
    }
    parts.rows = html;
    if (a.errors && a.errors.length) parts.errors = `<div class="arbe-muted arbe-small">${a.errors.map(esc).join("<br>")}</div>`;
    parts.foot = `<div class="arbe-muted arbe-small">Reference size ${a.contracts} contracts · target margin ${fmtPct(a.targetMargin)} · size = contracts offered at that ask · fees: Robinhood commission (Gold ${a.gold ? "on" : "off"}) + $0.01/ct exchange; Kalshi 7% taker / 1.75% maker × p(1−p); Polymarket 5% taker × p(1−p). Max buy = highest price on this venue that still locks the margin after hedging the other side at its cheapest current ask. Informational only — verify before trading.</div>`;
    paint(parts);
    decorateTabs(a);
  }

  // In-play strip: game state, model vs market fair, and LOCK / STEAL actions (bridge only).
  function renderInplay(v) {
    const live = v.live ? "LIVE" : "PRE";
    const gs = v.game_state || {};
    const fr = v.freshness || {};
    // Event-level gate reasons and the FeedFreshness flags behind them, on hover of the strip
    // head. Nothing that moves every poll goes in here: freshness.frozen / freshness.polls are
    // counters FeedFreshness.observe bumps on every quiet tick (pre-game, timeouts, halftime),
    // so the frozen clock is only ever named through the engine's own `clock-frozen` gate —
    // the strip's HTML must be byte-identical on a quiet tick or the diffing re-renders it
    // every second and the hover tooltip closes under the mouse.
    const eventReasons = v.gated_reasons || [];
    const headTitle = (eventReasons.length ? "gates: " + eventReasons.join(", ") : "no feed gates") + (fr.score_pending ? " · score pending" : "") + (v.spread_source ? ` · spread from ${v.spread_source}` : "");
    let html = `<div class="arbe-arb ${v.live ? "arbe-live" : ""}${eventReasons.length ? " arbe-gated" : ""}"><b title="${esc(headTitle)}">${live}${eventReasons.length ? " ⏸" : ""}</b> ${val("ip:game", esc(v.game_line || ""))}${v.fair_line ? `<div class="arbe-muted">${val("ip:fair", esc(v.fair_line))}</div>` : ""}`;
    const play = gs.last_play_text || v.last_play_text;
    if (play) html += `<div class="arbe-play arbe-muted arbe-small" title="last play (ESPN)${gs.last_play_type ? " · " + esc(gs.last_play_type) : ""}">▶ ${val("ip:play", esc(play))}</div>`;
    const gatedTag = (reasons) => ` <span class="arbe-tag arbe-signal" title="the feed cannot be trusted right now: ${esc((reasons || []).join(", "))}">GATED · wait</span>`;
    for (const sv of v.sides || []) {
      const k = "ip:" + sv.outcome;
      const held = sv.held ? ` · held ${sv.held} @ ${fmtP(sv.avg_all_in)}` : "";
      // A gated LOCK / STEAL is shown as such — never as an actionable NOW — mirroring the CLI's
      // "GATED … wait: <reasons>" (bridge /inplay keeps a FeedFreshness per event across polls).
      const lockNow = sv.lock_available && !sv.lock_gated;
      const lock = sv.need ? ` · <b class="${lockNow ? "arbe-good" : ""}">LOCK ≤ ${val(k + ":lock", fmtP(sv.lock_price))}</b>${lockNow ? " NOW" : ""}${sv.lock_gated ? gatedTag(sv.gated_reasons) : ""}` : "";
      const bestNote = sv.best_ineligible ? ` <span class="arbe-tag arbe-signal" title="${esc(sv.best_ineligible)}">signal only</span>` : "";
      const steal = sv.steal && !sv.best_ineligible ? ` · <b class="arbe-good">STEAL +${val(k + ":steal", fmtPct(sv.steal_edge))}${sv.suggested_contracts ? ` → buy ${sv.suggested_contracts} ct` : ""}</b>` : (sv.steal_gated ? ` · STEAL +${val(k + ":steal", fmtPct(sv.steal_edge))}${gatedTag(sv.gated_reasons)}` : "");
      html += `<div class="arbe-small">${esc(sv.label)}: fair ${val(k + ":fair", fmtP(sv.fair))} (mkt ${val(k + ":mkt", fmtP(sv.market_p))} / model ${val(k + ":model", fmtP(sv.model_p))}${sv.espn_p != null ? " / espn " + val(k + ":espn", fmtP(sv.espn_p)) : ""}) · best ${esc(sv.best_venue || "–")}${bestNote} all-in ${val(k + ":allin", fmtP(sv.best_all_in))}${held}${lock}${steal}</div>`;
    }
    for (const act of (v.actions || []).filter((x) => /^(LOCK NOW|STEAL|FLAT)/.test(x))) html += `<div class="arbe-good arbe-small">→ ${esc(act)}</div>`;
    for (const act of (v.actions || []).filter((x) => /^GATED/.test(x))) html += `<div class="arbe-muted arbe-small" title="${esc(eventReasons.join(", ") || "see the side's GATED tag")}">⏸ ${esc(act)}</div>`;
    html += `</div>`;
    return html;
  }

  // Spread / total pages: one row per line, arbs first.
  function renderLines(res) {
    const a = res.analysis;
    const arbs = a.lines.filter((l) => l.arb && l.arb.isArb && l.fillable);
    const parts = { arb: `<div class="arbe-arb ${arbs.length ? "arbe-good" : "arbe-muted"}"><b>${esc(res.event.name)}</b> — ${a.lines.length} ${esc(a.marketType)} lines, <b>${val("lines:arbs", String(arbs.length))}</b> fee-adjusted arb${arbs.length === 1 ? "" : "s"} fillable ≥ 1 contract</div>`, lines: "", errors: "", foot: "" };
    let html = `<table class="arbe-table"><thead><tr><th>line</th><th>side</th><th>here</th><th>all-in</th><th>max buy</th><th>best hedge</th><th>margin</th></tr></thead><tbody>`;
    for (const l of a.lines) {
      const m = l.arb ? l.arb.margin : null;
      const cls = l.arb && l.arb.isArb ? (l.fillable ? "arbe-good" : "arbe-thin") : "";
      l.rows.forEach((row, i) => {
        const k = "line:" + (l.key || l.title) + ":" + row.outcome;
        const here = hereOf(row.venues);
        const hedgeRow = l.rows.find((r) => r !== row);
        const hedge = hedgeRow ? hedgeRow.venues.find((v) => v.allIn != null && !v.mirror) : null;
        html += `<tr class="${cls}">${i === 0 ? `<td rowspan="2"><b>${esc(l.title)}</b>${l.arb && l.arb.isArb ? `<div class="arbe-small">${l.fillable ? "fillable " + (l.sizedContracts || "?") + " ct" : "thin"}</div>` : ""}</td>` : ""}<td>${esc(row.label)}</td><td>${val(k + ":ask", fmtP(here ? here.ask : null))}</td><td>${val(k + ":allin", fmtP(here ? here.allIn : null))}</td><td><b>${val(k + ":max", fmtP(here ? here.maxBuyTaker : null))}</b></td><td>${hedge ? esc(hedgeRow.label) + " @ " + val(k + ":hedge", fmtP(hedge.allIn)) + " " + esc(hedge.venue) : "–"}</td>${i === 0 ? `<td rowspan="2" class="${m != null && m > 0 ? "arbe-good" : "arbe-bad"}">${val(k + ":margin", fmtPct(m, true))}</td>` : ""}</tr>`;
      });
    }
    parts.lines = html + `</tbody></table>`;
    if (a.errors && a.errors.length) parts.errors = `<div class="arbe-muted arbe-small">${a.errors.map(esc).join("<br>")}</div>`;
    parts.foot = `<div class="arbe-muted arbe-small">here = Robinhood ask for that side; all-in includes commission + exchange fee; max buy = highest price here that still locks the margin against the cheapest hedge of the other side. Half-point lines cannot push. Informational only.</div>`;
    paint(parts);
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
      const here = hereOf(l.rows[0].venues);
      if (!badge) { badge = document.createElement("span"); badge.className = "arbe-badge"; tab.appendChild(badge); }
      const good = l.arb && l.arb.isArb && l.fillable;
      setBadge(badge, "arbe-badge " + (good ? "arbe-good" : "arbe-bad"), `max ${fmtP(here ? here.maxBuyTaker : null)}` + (good ? ` · arb ${fmtPct(l.arb.margin, true)}` : ""), null);
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
      const here = hereOf(row.venues);
      if (!badge) { badge = document.createElement("span"); badge.className = "arbe-badge"; tab.appendChild(badge); }
      const edge = row.edge;
      setBadge(badge, "arbe-badge " + (edge != null && edge > 0 ? "arbe-good" : "arbe-bad"), `fair ${fmtP(row.fair)} · all-in ${fmtP(here ? here.allIn : null)} · max ${fmtP(here ? here.maxBuyTaker : null)}`, "Arb Engine: consensus fair value · your all-in cost here incl. fees · max price to pay here to lock an arb vs the cheapest hedge elsewhere");
    });
  }
  // Badges live inside Robinhood's own DOM: touch them only when the text really changed, so
  // the host page sees no mutation on a quiet tick (and a hovered badge keeps its tooltip).
  function setBadge(badge, cls, text, title) {
    if (badge.className !== cls) badge.className = cls;
    if (badge.textContent !== text) badge.textContent = text;
    if (title != null && badge.title !== title) badge.title = title;
  }

  // --- category pages: badge every game card ("PHI - 77¢") with fair / max-buy --------------
  const CATEGORY_RE = /^(?:\/us\/en)?\/prediction-markets\/([^/?#]+)\/?$/;
  const CONTRACT_RE = /^\s*([A-Z]{2,4})\s*-\s*(\d{1,2})¢\s*$/;
  const CATEGORY_MAX = 16;
  let lastCategory = null, lastCategoryAt = 0, categoryBusy = false;
  function categoryLinks() {
    const groups = new Map();  // href -> [contract links]
    document.querySelectorAll('a[href*="/events/"]').forEach((a) => {
      const href = a.getAttribute("href") || "";
      const slug = (/\/events\/([^/?#]+)/.exec(href) || [])[1];
      if (!slug || /spread|total|points/.test(slug) || !/-vs-/.test(slug)) return;   // moneyline cards only
      if (!CONTRACT_RE.test(a.textContent || "")) return;                              // the two contract buttons
      if (!groups.has(href)) groups.set(href, []);
      groups.get(href).push(a);
    });
    return groups;
  }
  function decorateCategory(groups, res) {
    let arbs = 0, badged = 0, best = null;
    for (const [href, links] of groups) {
      const url = location.origin + href;
      const r = res.results[url];
      links.forEach((a) => {
        let badge = a.querySelector(".arbe-badge");
        const m = CONTRACT_RE.exec(a.textContent.replace(badge ? badge.textContent : "", "") || "");
        if (!r || !r.ok || !m) { if (badge) badge.remove(); return; }
        const code = m[1];
        const row = r.rows.find((x) => String(x.outcome).toUpperCase() === code || String(x.label || "").toUpperCase().startsWith(code));
        if (!row) { if (badge) badge.remove(); return; }
        if (!badge) { badge = document.createElement("span"); badge.className = "arbe-badge arbe-cat-badge"; a.classList.add("arbe-cat-host"); a.appendChild(badge); }
        const good = row.edge != null && row.edge > 0;
        setBadge(badge, "arbe-badge arbe-cat-badge " + (good ? "arbe-good" : "arbe-bad") + (r.arb && r.arb.isArb ? " arbe-arbflag" : ""), `fair ${fmtC(row.fair)} · max ${fmtC(row.here ? row.here.maxBuyTaker : null)}` + (r.arb && r.arb.isArb ? ` · ARB ${fmtPct(r.arb.margin, true)}` : ""), `Arb Engine: consensus fair ${fmtP(row.fair)} · all-in here ${fmtP(row.here ? row.here.allIn : null)} · max price here that still locks the margin vs the cheapest hedge (${row.best || "–"})`);
        badged++;
        if (best == null || (row.edge != null && row.edge > best.edge)) best = { edge: row.edge, label: row.label, name: r.name };
      });
      if (r && r.ok && r.arb && r.arb.isArb) arbs++;
    }
    paint({ msg: `<div class="arbe-arb ${arbs ? "arbe-good" : "arbe-muted"}"><b>${groups.size} game${groups.size === 1 ? "" : "s"}</b> on this page, ${Object.keys(res.results).length} analysed${res.truncated ? " (first " + CATEGORY_MAX + ")" : ""}, <b>${val("cat:arbs", String(arbs))}</b> with a fee-adjusted arb</div>` +
      (best && best.edge != null ? `<div class="arbe-small">best edge: ${esc(best.label)} in ${esc(best.name || "")} ${val("cat:best", fmtPct(best.edge, true))} vs consensus</div>` : "") +
      `<div class="arbe-muted arbe-small">Badges under each price: consensus fair value · max price to pay here that still locks the target margin after hedging elsewhere. Open a game for venues, depth and the in-play view.</div>` });
  }
  function runCategory(force) {
    if (categoryBusy) return;
    if (!force && Date.now() - lastCategoryAt < Math.max(refreshSeconds, 20) * 1000) return;
    const groups = categoryLinks();
    if (!groups.size) return;
    const urls = Array.from(groups.keys()).slice(0, CATEGORY_MAX).map((h) => location.origin + h);
    categoryBusy = true;
    setStatus("scanning " + urls.length + " games…", "");
    chrome.runtime.sendMessage({ type: "analyzeMany", urls, max: CATEGORY_MAX }, (res) => {
      categoryBusy = false; lastCategoryAt = Date.now();
      if (chrome.runtime.lastError || !res || !res.ok) { setStatus("error", "arbe-bad"); render({ ok: false, error: (chrome.runtime.lastError && chrome.runtime.lastError.message) || (res && res.error) || "no response" }); return; }
      lastCategory = res;
      lastFetchedAt = Date.now();
      const srcs = new Set(Object.values(res.results).map((r) => r && r.source).filter(Boolean));
      lastMode = srcs.has("bridge") ? "engine" : "direct";
      setStatus(new Date().toLocaleTimeString() + " · category · " + lastMode, "arbe-good");
      decorateCategory(groups, res);
    });
  }

  function run(force) {
    const url = eventUrl();
    if (!url && CATEGORY_RE.test(location.pathname)) { runCategory(force); return; }
    if (!url) { if (document.getElementById(PANEL_ID)) paint({ msg: `<div class="arbe-muted">Open a game / match page to see fair value and max-buy prices.</div>` }); return; }
    if (!force && url === lastUrl && lastAnalysis && Date.now() - lastAnalysis.fetchedAt < refreshSeconds * 1000) return;
    if (inflight) return;  // never queue a second request behind a slow one
    if (url !== lastUrl) resetRender();
    lastUrl = url;
    inflight = true;
    if (!lastAnalysis) setStatus("loading…", "");
    chrome.runtime.sendMessage({ type: "analyze", url }, (res) => {
      inflight = false;
      if (chrome.runtime.lastError) { setStatus("error · worker", "arbe-bad"); render({ ok: false, error: chrome.runtime.lastError.message }); return; }
      if (!res) { setStatus("no response · worker", "arbe-bad"); return; }
      // The header always says which mode is live (engine = bridge, direct = the worker's own
      // venue calls) and whether the bridge is currently down and being retried.
      const mode = modeLabel(res), b = res.bridge;
      const down = b && b.up === false ? ` (bridge down${b.retryInMs ? ", retry " + Math.ceil(b.retryInMs / 1000) + " s" : ""})` : "";
      setStatus((res.ok ? new Date().toLocaleTimeString() : "error") + " · " + mode + down, res.ok ? (down ? "arbe-amber" : "arbe-good") : "arbe-bad");
      render(res);
    });
  }

  function start() {
    ensurePanel();
    chrome.runtime.sendMessage({ type: "settings" }, (s) => { if (s && s.refreshSeconds) refreshSeconds = Math.max(1, Number(s.refreshSeconds)); });
    run(true);
    if (timer) clearInterval(timer);
    timer = setInterval(() => { run(false); tickFresh(); }, 500);  // cheap tick; actual refresh gated by refreshSeconds (1 s = live) and by inflight
    // SPA navigations: watch the URL and re-run.
    let href = location.href;
    setInterval(() => { if (location.href !== href) { href = location.href; lastAnalysis = null; resetRender(); run(true); } }, 500);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
