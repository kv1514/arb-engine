/* background.js — fetches venue data (CORS-free from the service worker) and runs the math.
 *
 * Message: {type: "analyze", url, contractsHint?} -> {ok, event, analysis}
 *   1. Load the Robinhood event page for `url` and read __NEXT_DATA__ (event + contracts +
 *      SSR quotes), then refresh quotes from api.robinhood.com.
 *   2. For each contract: Kalshi ticker = symbol with "KX" prefix (Robinhood re-uses Kalshi's
 *      ticker scheme on Rothera); fetch /markets/{ticker} (+ series fee params).
 *   3. Polymarket: NFL -> /markets?slug=nfl-{away}-{home}-{date}; tennis -> public-search.
 *   4. Fees, consensus fair value, best legs, margin and max-buy prices via ArbCore.
 */
importScripts("arb-core.js");

const RH_API = "https://api.robinhood.com";
const KALSHI = "https://api.elections.kalshi.com/trade-api/v2";
const GAMMA = "https://gamma-api.polymarket.com";
const BRIDGE = "http://127.0.0.1:8765";
// executable.polymarket: Polymarket (non-US) is not executable for US accounts (arb_engine/data/venue_rules.json),
// so it is signal-only — priced into the fair value, never an arb leg — unless the user opts in here.
const DEFAULTS = { gold: false, contracts: 100, targetMargin: 0, kalshiRounding: "cent", refreshSeconds: 1, venues: { kalshi: true, polymarket: true }, executable: { polymarket: false }, showBadges: true, bridge: "auto", positions: "", bankroll: 0, kelly: 0.25 };

// Kalshi's production API answers 403 to any browser Origin other than kalshi.com. Strip the
// Origin header on our own requests to it (allowed by declarativeNetRequestWithHostAccess).
async function installKalshiOriginRule() {
  try {
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: [1],
      addRules: [{ id: 1, priority: 1, action: { type: "modifyHeaders", requestHeaders: [{ header: "origin", operation: "remove" }] }, condition: { urlFilter: "||api.elections.kalshi.com/", initiatorDomains: [chrome.runtime.id], resourceTypes: ["xmlhttprequest"] } }],
    });
  } catch (e) { console.warn("arb-engine: could not install Kalshi origin rule", e); }
}
chrome.runtime.onInstalled.addListener(installKalshiOriginRule);
chrome.runtime.onStartup.addListener(installKalshiOriginRule);

// ---- bridge availability: a small state machine (pure transitions, no DOM, jsc-testable) -----
// The overlay polls every second, so a dead bridge must not cost a failed connect per tick and a
// bridge that comes back must be noticed without a reload. Rules:
//   * a /health answer marks the bridge UP and is trusted for BRIDGE_HEALTH_TTL_MS;
//   * BRIDGE_FAIL_LIMIT consecutive /analyze transport failures (fetch rejecting; an HTTP
//     status such as bridge.py's 500 {ok:false,error} is an answer) or one failed /health mark it DOWN;
//   * while DOWN, /health is re-probed with exponential backoff 2 s -> 30 s and nothing else
//     touches the bridge; the first answering /health switches back (auto mode) immediately.
// bridgeState.mode is what the content script and the popup show ("bridge" / "direct").
const BRIDGE_FAIL_LIMIT = 3, BRIDGE_BACKOFF_MIN_MS = 2_000, BRIDGE_BACKOFF_MAX_MS = 30_000, BRIDGE_HEALTH_TTL_MS = 30_000;
function newBridgeState() { return { up: null, failures: 0, backoffMs: 0, nextProbeAt: 0, lastOkAt: 0, lastError: null, mode: "direct", service: null, executableVenues: null }; }
let bridgeState = newBridgeState();
function bridgeNoteSuccess(st, now, health) {
  st.up = true; st.failures = 0; st.backoffMs = 0; st.lastOkAt = now; st.lastError = null; st.nextProbeAt = now + BRIDGE_HEALTH_TTL_MS; st.mode = "bridge";
  if (health && typeof health === "object") { if (health.service) st.service = String(health.service); if (Array.isArray(health.executable_venues)) st.executableVenues = health.executable_venues.map(String); }
  return st;
}
// `hard` = the probe itself failed (or the caller decided): go DOWN at once; otherwise count
// towards BRIDGE_FAIL_LIMIT so one slow /analyze does not flip the mode.
function bridgeNoteFailure(st, now, err, hard) {
  st.failures += 1; st.lastError = err ? String(err.message || err) : "bridge error";
  if (hard || st.failures >= BRIDGE_FAIL_LIMIT) {
    st.up = false; st.mode = "direct";
    st.backoffMs = st.backoffMs ? Math.min(BRIDGE_BACKOFF_MAX_MS, st.backoffMs * 2) : BRIDGE_BACKOFF_MIN_MS;
    st.nextProbeAt = now + st.backoffMs;
  }
  return st;
}
function bridgeShouldProbe(st, now) { return st.up === null || now >= st.nextProbeAt; }
// What the UI needs to say which mode is live and when the next retry is due.
function bridgeStatus(st, now) {
  return { up: st.up, mode: st.mode, failures: st.failures, backoffMs: st.backoffMs, retryInMs: st.up === false ? Math.max(0, st.nextProbeAt - now) : 0, lastOkAt: st.lastOkAt || null, lastError: st.lastError, service: st.service, executableVenues: st.executableVenues };
}
let bridgeProbe = null;
async function bridgeAvailable(now, force) {
  now = now == null ? Date.now() : now;
  if (!force && !bridgeShouldProbe(bridgeState, now)) return !!bridgeState.up;
  if (bridgeProbe) return bridgeProbe;  // one in-flight probe, however many ticks ask
  bridgeProbe = (async () => {
    try {
      const r = await fetch(`${BRIDGE}/health`, { credentials: "omit" });
      if (!r.ok) throw new Error(`HTTP ${r.status} /health`);
      let body = null;
      try { body = await r.json(); } catch (e) { /* an older bridge answers plain text */ }
      bridgeNoteSuccess(bridgeState, now, body);
    } catch (e) { bridgeNoteFailure(bridgeState, now, e, true); }
    finally { bridgeProbe = null; }
    return !!bridgeState.up;
  })();
  return bridgeProbe;
}

// Convert the Python engine's EventReport into the shape content.js renders.
function rowsFromReport(a) {
  return (a.outcomes || []).map((o) => ({ outcome: o.outcome, label: o.label, fair: o.fair, best: o.best_buy_venue, bestAllIn: o.best_buy_all_in, edge: o.edge_at_best, venues: (o.venues || []).map((v) => ({ venue: v.venue, exchange: v.exchange, mirror: v.mirror_of, ineligible: v.ineligible || null, side: v.side || null, tiePayout: v.tie_payout == null ? null : v.tie_payout, ask: v.ask, bid: v.bid, askSize: v.ask_size, feePerContract: v.fee_per_contract, allIn: v.all_in, maxBuyTaker: v.max_buy_price, maxBuyMaker: v.max_buy_maker, url: v.url, feeNote: "", quoteAge: quoteAgeOf(v) })) }));
}
// Seconds since the venue's quote was taken, when the engine says (``quote_age`` today, ``age``
// as OutcomeQuote spells it); null when absent so the overlay can leave the column out.
function quoteAgeOf(v) {
  const x = v && (v.quote_age != null ? v.quote_age : v.age);
  return x == null || !Number.isFinite(Number(x)) ? null : Number(x);
}
// analysis.venues is a list of venue names today; a future bridge may send {venue, quote_age}
// objects. Normalise to [{venue, quoteAge}] so content.js never branches on the shape.
function venueAges(list) {
  return (list || []).map((v) => (typeof v === "string" ? { venue: v, quoteAge: null } : { venue: String(v && v.venue || ""), quoteAge: quoteAgeOf(v) })).filter((v) => v.venue);
}
function arbFromReport(a) {
  return a.arb ? { grossSum: a.arb.gross_sum, margin: a.arb.margin, profit: a.arb.profit, contracts: a.arb.contracts, isArb: a.arb.is_arb, legs: (a.arb.legs || []).map((l) => ({ venue: l.venue, label: l.label || l.outcome, price: l.price, fee: l.fee })) } : null;
}
function fromBridge(res, cfg) {
  if (!res || !res.ok || !res.analysis) return res;
  const a = res.analysis;
  if (a.lines) {
    const lines = a.lines.map((d) => ({ key: d.event_key, title: d.title, line: d.line, marketType: d.market_type, fillable: !!d.fillable, flags: d.flags || [], contractId: d.contract_id, rows: rowsFromReport(d), arb: arbFromReport(d), sizedContracts: d.sized_arb ? d.sized_arb.contracts : null, sizedProfit: d.sized_arb ? d.sized_arb.profit : null }));
    return { ok: true, event: res.event, analysis: { contracts: cfg.contracts, targetMargin: cfg.targetMargin, gold: cfg.gold, marketType: a.market_type, lines, errors: a.errors || [], fetchedAt: Date.now(), venues: venueAges(a.venues), source: "bridge" } };
  }
  const rows = rowsFromReport(a);
  const arb = arbFromReport(a);
  if (arb && a.sized_arb) { arb.sizedContracts = a.sized_arb.contracts; arb.sizedProfit = a.sized_arb.profit; arb.sizedLegs = (a.sized_arb.legs || []).map((l) => ({ venue: l.venue, label: l.label || l.outcome, price: l.price, contracts: l.contracts })); }
  return { ok: true, event: res.event, analysis: { contracts: cfg.contracts, targetMargin: cfg.targetMargin, gold: cfg.gold, rows, arb, errors: (a.errors || []).concat(a.flags && a.flags.length ? ["flags: " + a.flags.join(", ")] : []), fetchedAt: Date.now(), venues: venueAges(a.venues), source: "bridge" } };
}

const cache = new Map();
async function cached(key, ttlMs, fn) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.t < ttlMs) return hit.v;
  const v = await fn();
  cache.set(key, { t: Date.now(), v });
  return v;
}
async function getJson(url, init) {
  const r = await fetch(url, Object.assign({ credentials: "omit" }, init || {}));
  if (!r.ok) throw new Error(`HTTP ${r.status} ${url}`);
  return r.json();
}
async function getText(url) {
  const r = await fetch(url, { credentials: "omit" });
  if (!r.ok) throw new Error(`HTTP ${r.status} ${url}`);
  return r.text();
}
// The bridge's own transport. Only a rejected fetch (connection refused, reset, DNS) is a
// transport failure — it throws with `transport: true` and counts towards BRIDGE_FAIL_LIMIT.
// An HTTP status is an answer from a live bridge: bridge.py sends 500 {ok:false,error} for any
// engine exception (a Robinhood 429 on its page fetch, say), so it comes back as an ordinary
// {ok:false, error} report for the overlay to show beside the last good tables, never as an
// outage. A 2xx without JSON is reported the same way.
async function bridgeJson(url) {
  let r;
  try { r = await fetch(url, { credentials: "omit" }); }
  catch (e) { const err = new Error(String((e && e.message) || e)); err.transport = true; throw err; }
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (!r.ok) return { ok: false, error: `engine: ${(body && body.error) ? String(body.error) : `HTTP ${r.status} ${url.split("?")[0].slice(BRIDGE.length)}`}`, httpStatus: r.status };
  if (!body || typeof body !== "object") return { ok: false, error: `engine: no JSON from ${url.split("?")[0].slice(BRIDGE.length)}`, httpStatus: r.status };
  return body;
}
let teamsLoaded = false;
async function ensureTeams() {
  if (teamsLoaded) return;
  ArbCore.loadTeams(await getJson(chrome.runtime.getURL("nfl_teams.json")));
  teamsLoaded = true;
}
async function settings() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  return Object.assign({}, DEFAULTS, s);
}

// ---- Robinhood ---------------------------------------------------------------------------
function extractNextData(html) {
  const m = /<script id="__NEXT_DATA__" type="application\/json">([\s\S]*?)<\/script>/.exec(html);
  if (!m) throw new Error("__NEXT_DATA__ not found on the Robinhood page");
  return JSON.parse(m[1]);
}
const PUBLIC_CATEGORIES = ["nfl", "tennis", "college-football", "nba", "nhl", "baseball", "soccer", "mma", "golf", "pro-football", "esports", "cricket"];
const slugCategory = new Map();
async function robinhoodPageProps(url) {
  // Public marketing page: has __NEXT_DATA__. Logged-in trading route (/events/<slug>) is
  // client-rendered, so fetch the public page for the same slug, trying categories.
  const app = /robinhood\.com\/events\/([^/?#]+)/.exec(url);
  if (!app) return extractNextData(await getText(url)).props.pageProps;
  const slug = app[1];
  const cats = (slugCategory.has(slug) ? [slugCategory.get(slug)] : []).concat(PUBLIC_CATEGORIES.filter((c) => c !== slugCategory.get(slug)));
  for (const cat of cats) {
    try {
      const pp = extractNextData(await getText(`https://robinhood.com/us/en/prediction-markets/${cat}/events/${slug}/`)).props.pageProps;
      if (pp && pp.event) { slugCategory.set(slug, cat); return pp; }
    } catch (e) { /* try the next category */ }
  }
  throw new Error("no public event page found for " + slug);
}
async function robinhoodEvent(url) {
  return cached("rh:" + url, 60_000, async () => {
    const pp = await robinhoodPageProps(url);
    const ev = pp.event;
    if (!ev) throw new Error("not an event page");
    const contracts = Object.values(ev.eventContracts || {});
    return { id: ev.id, name: ev.name, category: ev.category, mutuallyExclusive: ev.mutuallyExclusive, eventType: ev.eventType, contracts: contracts.map((c) => ({ id: c.id, symbol: c.symbol, exchange: c.exchange, short: c.displayShortName, long: c.displayLongName, floor: c.floorStrikeValue })), ssrQuotes: pp.quotes || {} };
  });
}
async function robinhoodQuotes(ids) {
  const out = {};
  for (let i = 0; i < ids.length; i += 20) {
    const d = await getJson(`${RH_API}/marketdata/event/contract/quotes/v1/?ids=${encodeURIComponent(ids.slice(i, i + 20).join(","))}`);
    for (const item of d.data || []) if (item.data && item.data.instrument_id) out[item.data.instrument_id] = item.data;
  }
  return out;
}
function rhExchange(c) {
  const e = (c.exchange || "").toUpperCase();
  if (e.includes("ROTHERA")) return "rothera";
  if (e.includes("KALSHI")) return "kalshi";
  if (e.includes("FORECAST")) return "forecastex";
  if (e.includes("NADEX") || e.includes("NORTH_AMERICAN")) return "nadex";
  if (e.includes("CDNA")) return "cdna";
  const s = c.symbol || "";
  return s.startsWith("KX") ? "kalshi" : s.startsWith("NX.") ? "cdna" : "rothera";
}

// ---- Kalshi ------------------------------------------------------------------------------
async function kalshiMarket(ticker) {
  return cached("km:" + ticker, 10_000, async () => {
    try { return (await getJson(`${KALSHI}/markets/${ticker}`)).market; }
    catch (e) {
      // 403 = Origin blocked (rule not active). Fall back to the local bridge if it is running.
      if (await bridgeAvailable()) return (await getJson(`${BRIDGE}/kalshi/market/${ticker}`)).market;
      throw e;
    }
  });
}
async function kalshiEventMarkets(eventTicker) {
  return cached("ke:" + eventTicker, 10_000, async () => {
    try { return (await getJson(`${KALSHI}/markets?event_ticker=${eventTicker}&limit=200`)).markets || []; }
    catch (e) {
      if (await bridgeAvailable()) return (await getJson(`${BRIDGE}/kalshi/markets?event_ticker=${eventTicker}&limit=200`)).markets || [];
      throw e;
    }
  });
}
async function polymarketGame(teams, date) {
  return cached("pm:game:" + teams.join("") + date, 60_000, async () => {
    for (const slug of ArbCore.polymarketNflSlugs(teams, date)) {
      const evs = await getJson(`${GAMMA}/events?slug=${slug}`);
      if (evs && evs.length) return evs[0];
    }
    return null;
  });
}
const LINE_FAMILIES = { NFLSPREAD: "spread", NFLTOTAL: "total" };
function fmtLine(x) { return String(Number(x)); }
function executableVenue(venue, cfg) {
  if (venue === "polymarket") return !!(cfg.executable && cfg.executable.polymarket);
  return true;
}
function analyzeTwoOutcome(byVenue, outcomes, labels, cfg) {
  const fair = ArbCore.consensusFair(byVenue, outcomes);
  const quotesByOutcome = {}, legQuotes = {};
  outcomes.forEach((o) => {
    quotesByOutcome[o] = Object.values(byVenue).flat().filter((q) => q.outcome === o);
    legQuotes[o] = quotesByOutcome[o].filter((q) => executableVenue(q.venue, cfg));  // signal-only venues never become legs
  });
  const legs = ArbCore.bestLegPerOutcome(legQuotes, cfg.contracts);
  const complete = legs.length === outcomes.length;
  const arb = complete ? ArbCore.evaluate(legs, cfg.contracts) : null;
  const rows = outcomes.map((o) => {
    const others = legs.filter((l) => l.outcome !== o);
    const hedgeable = complete && others.length === outcomes.length - 1;
    const venues = quotesByOutcome[o].map((q) => {
      const feePc = q.ask != null ? q.fee(q.ask, cfg.contracts, "taker") / cfg.contracts : null;
      const eligible = executableVenue(q.venue, cfg);
      return { venue: q.venue, exchange: q.exchange || null, mirror: q.mirror || null, ineligible: eligible ? null : "not executable", ask: q.ask, bid: q.bid, askSize: q.askSize == null ? null : q.askSize, feePerContract: feePc, allIn: q.ask != null ? q.ask + feePc : null, maxBuyTaker: hedgeable && eligible ? ArbCore.maxPrice(others, q.fee, cfg.contracts, cfg.targetMargin, "taker") : null, maxBuyMaker: hedgeable && eligible ? ArbCore.maxPrice(others, q.fee, cfg.contracts, cfg.targetMargin, "maker") : null, url: q.url || null, feeNote: q.feeNote || "", contractId: q.contractId || null };
    }).sort((a, b) => (a.allIn == null) - (b.allIn == null) || a.allIn - b.allIn);
    const best = venues.find((v) => v.allIn != null && !v.mirror && !v.ineligible) || null;
    return { outcome: o, label: labels[o], fair: fair[o], venues, best: best ? best.venue : null, bestAllIn: best ? best.allIn : null, edge: best && fair[o] != null ? fair[o] - best.allIn : null };
  });
  // Fillable = every arb leg has at least 1 contract at the quoted size (unknown size = unlimited).
  let fillable = false, sizedContracts = null;
  if (arb && arb.isArb) {
    const caps = legs.map((l) => (l.quote && l.quote.askSize != null ? l.quote.askSize : Infinity));
    const cap = Math.min(...caps);
    fillable = cap >= 1;
    sizedContracts = Number.isFinite(cap) ? Math.floor(cap) : cfg.contracts;
  }
  return { rows, arb, fillable, sizedContracts, legs };
}
async function analyzeLines(url, ev, cfg, mtype) {
  const contracts = ev.contracts;
  const p0 = ArbCore.parseSymbol(contracts[0].symbol);
  const pair = p0.pair;
  let codes = null;
  for (let cut = 2; cut < pair.length - 1; cut++) {
    const a = ArbCore.nflTeamCode(pair.slice(0, cut)), b = ArbCore.nflTeamCode(pair.slice(cut));
    if (a && b) { codes = [a, b]; break; }
  }
  if (!codes) return { ok: true, event: ev, analysis: null, note: "could not split team pair " + pair };
  const [away, home] = codes;
  const errors = [];
  let live = {};
  try { live = await robinhoodQuotes(contracts.map((c) => c.id)); } catch (e) { errors.push("robinhood quotes: " + e.message); }
  const kalshiEvent = "KX" + p0.family + "-" + contracts[0].symbol.split("-")[1];
  const kIndex = new Map();
  let kFee = () => 0;
  if (cfg.venues.kalshi !== false) {
    try {
      const series = await kalshiSeries(kalshiEvent.split("-")[0]).catch(() => ({}));
      kFee = ArbCore.feeFn("kalshi", { fee_type: series.fee_type, fee_multiplier: series.fee_multiplier }, cfg);
      for (const m of await kalshiEventMarkets(kalshiEvent)) {
        if (m.status && m.status !== "active" && m.status !== "open") continue;
        if (m.floor_strike == null) continue;
        const team = mtype === "spread" ? m.ticker.split("-").pop().replace(/\d+$/, "") : "";
        kIndex.set(team + "|" + fmtLine(m.floor_strike), m);
      }
    } catch (e) { errors.push("kalshi: " + e.message); }
  }
  const pmIndex = new Map();
  let pmEvent = null;
  if (cfg.venues.polymarket !== false) {
    try {
      pmEvent = await polymarketGame([away, home], p0.date);
      if (!pmEvent) errors.push("polymarket: game not found");
      for (const m of (pmEvent && pmEvent.markets) || []) {
        const ln = m.line == null ? null : Number(m.line);
        if (ln == null) continue;
        if (m.sportsMarketType === "spreads") {
          const oc = parseJsonList(m.outcomes).map((o) => ArbCore.nflTeamCode(o));
          if (oc.length === 2 && oc[0] && oc[1]) pmIndex.set("spread|" + (ln < 0 ? oc[0] : oc[1]) + "-" + fmtLine(Math.abs(ln)), m);
        } else if (m.sportsMarketType === "totals") pmIndex.set("total|" + fmtLine(ln), m);
      }
    } catch (e) { errors.push("polymarket: " + e.message); }
  }
  const lines = [];
  const missingK = new Set();
  for (const c of contracts) {
    const line = c.floor == null ? null : Number(c.floor);
    if (line == null || !Number.isFinite(line)) continue;
    const exch = rhExchange(c);
    let yesKey, noKey, labels, title, km, pm, teamRaw = "";
    if (mtype === "spread") {
      teamRaw = c.symbol.split("-").pop().replace(/\d+$/, "");
      const fav = ArbCore.nflTeamCode(teamRaw);
      const otherRaw = pair.endsWith(teamRaw) ? pair.slice(0, pair.length - teamRaw.length) : pair.slice(teamRaw.length);
      const dog = ArbCore.nflTeamCode(otherRaw);
      if (!fav || !dog) continue;
      yesKey = fav + "-" + fmtLine(line); noKey = dog + "+" + fmtLine(line);
      labels = { [yesKey]: fav + " -" + fmtLine(line), [noKey]: dog + " +" + fmtLine(line) };
      title = labels[yesKey] + " / " + labels[noKey];
      km = kIndex.get(teamRaw + "|" + fmtLine(line));
      pm = pmIndex.get("spread|" + fav + "-" + fmtLine(line));
    } else {
      yesKey = "over"; noKey = "under";
      labels = { over: "Over " + fmtLine(line), under: "Under " + fmtLine(line) };
      title = away + " @ " + home + " total " + fmtLine(line);
      km = kIndex.get("|" + fmtLine(line));
      pm = pmIndex.get("total|" + fmtLine(line));
    }
    const outcomes = [yesKey, noKey];
    const q = live[c.id] || ev.ssrQuotes[c.id] || {};
    const rhFee = ArbCore.feeFn("robinhood", { exchange: exch }, cfg);
    const bookId = exch === "kalshi" ? "kalshi" : exch;
    const byVenue = { robinhood: [
      { venue: "robinhood", outcome: yesKey, label: labels[yesKey], ask: q.yes_ask_price != null ? Number(q.yes_ask_price) : null, bid: q.yes_bid_price != null ? Number(q.yes_bid_price) : null, askSize: q.ask_size != null ? Number(q.ask_size) : null, fee: rhFee, exchange: exch, bookId, contractId: c.id },
      { venue: "robinhood", outcome: noKey, label: labels[noKey], ask: q.no_ask_price != null ? Number(q.no_ask_price) : null, bid: q.no_bid_price != null ? Number(q.no_bid_price) : null, askSize: q.bid_size != null ? Number(q.bid_size) : null, fee: rhFee, exchange: exch, bookId, contractId: c.id },
    ] };
    if (km) {
      const n = (x) => (x == null ? null : Number(x));
      const url = `https://kalshi.com/markets/${kalshiEvent.split("-")[0].toLowerCase()}/${kalshiEvent.toLowerCase()}`;
      byVenue.kalshi = [
        { venue: "kalshi", outcome: yesKey, label: labels[yesKey], ask: n(km.yes_ask_dollars) && n(km.yes_ask_dollars) < 1 ? n(km.yes_ask_dollars) : null, bid: n(km.yes_bid_dollars) > 0 ? n(km.yes_bid_dollars) : null, askSize: n(km.yes_ask_size_fp), fee: kFee, bookId: "kalshi", url, ticker: km.ticker },
        { venue: "kalshi", outcome: noKey, label: labels[noKey], ask: n(km.no_ask_dollars) && n(km.no_ask_dollars) < 1 ? n(km.no_ask_dollars) : null, bid: n(km.no_bid_dollars) > 0 ? n(km.no_bid_dollars) : null, askSize: n(km.yes_bid_size_fp), fee: kFee, bookId: "kalshi", url, ticker: km.ticker },
      ];
    } else if (cfg.venues.kalshi !== false) missingK.add(fmtLine(line));
    if (pm) {
      const ln = Number(pm.line);
      const keys = mtype === "spread" ? (ln < 0 ? [yesKey, noKey] : [noKey, yesKey]) : (String(parseJsonList(pm.outcomes)[0]).toLowerCase().startsWith("over") ? ["over", "under"] : ["under", "over"]);
      const qs = polymarketQuotes(pm, (label, i) => keys[i]);
      qs.forEach((x) => { x.url = `https://polymarket.com/event/${pmEvent.slug}`; x.label = labels[x.outcome]; });
      byVenue.polymarket = qs;
    }
    if (byVenue.kalshi) byVenue.robinhood.forEach((x) => { if (x.bookId === "kalshi") x.mirror = "kalshi"; });
    const r = analyzeTwoOutcome(byVenue, outcomes, labels, cfg);
    lines.push({ key: `nfl:${[away, home].sort().join("|")}:${p0.date}:${mtype}:${mtype === "spread" ? yesKey : fmtLine(line)}`, title, line, marketType: mtype, fillable: r.fillable, flags: [], contractId: c.id, rows: r.rows, arb: r.arb, sizedContracts: r.sizedContracts, sizedProfit: r.arb && r.sizedContracts ? r.arb.margin * r.sizedContracts : null });
  }
  if (missingK.size) errors.push("kalshi lists no market for lines: " + [...missingK].join(", "));
  lines.sort((a, b) => (b.fillable - a.fillable) || ((b.arb ? b.arb.margin : -9) - (a.arb ? a.arb.margin : -9)));
  return { ok: true, event: { id: ev.id, name: ev.name, sport: "nfl", url, marketType: mtype, game: `${away} @ ${home}` }, analysis: { contracts: cfg.contracts, targetMargin: cfg.targetMargin, gold: cfg.gold, marketType: mtype, lines, errors, fetchedAt: Date.now(), venues: ["robinhood", "kalshi", "polymarket"], source: "direct" } };
}
async function kalshiSeries(series) {
  return cached("ks:" + series, 3_600_000, async () => (await getJson(`${KALSHI}/series/${series}`)).series);
}

// ---- Polymarket --------------------------------------------------------------------------
function parseJsonList(x) { try { return Array.isArray(x) ? x : JSON.parse(x || "[]"); } catch (e) { return []; } }
async function polymarketNfl(teams, date) {
  return cached("pm:nfl:" + teams.join("") + date, 20_000, async () => {
    for (const slug of ArbCore.polymarketNflSlugs(teams, date)) {
      const ms = await getJson(`${GAMMA}/markets?slug=${slug}`);
      const m = (ms || []).find((x) => x.sportsMarketType === "moneyline");
      if (m) return m;
    }
    return null;
  });
}
async function polymarketTennis(names) {
  const q = names.map((n) => ArbCore.personKey(n)).join(" ");
  return cached("pm:tennis:" + q, 60_000, async () => {
    const d = await getJson(`${GAMMA}/public-search?q=${encodeURIComponent(q)}&limit_per_type=3`);
    const keys = names.map((n) => ArbCore.personKey(n)).sort().join("|");
    for (const ev of d.events || []) {
      for (const m of ev.markets || []) {
        if (m.sportsMarketType !== "moneyline" || m.closed) continue;
        const outs = parseJsonList(m.outcomes);
        if (outs.length !== 2) continue;
        if (outs.map((o) => ArbCore.personKey(o)).sort().join("|") === keys) return m;
      }
    }
    return null;
  });
}
function polymarketQuotes(m, outcomeKeys) {
  // bestBid/bestAsk describe outcome[0]'s token; outcome[1] is the complement.
  const outs = parseJsonList(m.outcomes);
  const bb = m.bestBid == null ? null : Number(m.bestBid), ba = m.bestAsk == null ? null : Number(m.bestAsk);
  const sides = [[bb, ba], [ba == null ? null : 1 - ba, bb == null ? null : 1 - bb]];
  const fee = ArbCore.feeFn("polymarket", { feeSchedule: m.feeSchedule, feesEnabled: m.feesEnabled });
  return outs.map((label, i) => ({ venue: "polymarket", outcome: outcomeKeys(label, i), label, bid: sides[i][0] != null && sides[i][0] > 0 && sides[i][0] < 1 ? Math.round(sides[i][0] * 1e4) / 1e4 : null, ask: sides[i][1] != null && sides[i][1] > 0 && sides[i][1] < 1 ? Math.round(sides[i][1] * 1e4) / 1e4 : null, fee, url: `https://polymarket.com/event/${(m.events && m.events[0] && m.events[0].slug) || m.slug}`, feeNote: m.feeSchedule ? `taker ${Number(m.feeSchedule.rate) * 100}% x p(1-p)` : "" }));
}

// ---- analysis ----------------------------------------------------------------------------
async function analyze(url, opts) {
  opts = opts || {};
  await ensureTeams();
  const cfg = await settings();
  const now = Date.now();
  if (cfg.bridge !== "off" && (await bridgeAvailable(now))) {
    try {
      const res = await bridgeJson(`${BRIDGE}/analyze?url=${encodeURIComponent(url)}&contracts=${cfg.contracts}&target_margin=${cfg.targetMargin}&gold=${cfg.gold ? 1 : 0}`);
      const out = fromBridge(res, cfg);
      // In-play view (game state, model vs market, LOCK/STEAL) for game-winner pages.
      if (opts.inplay !== false && out && out.ok && out.analysis && !out.analysis.lines) {
        try {
          const pos = String(cfg.positions || "").split(/\n+/).map((x) => x.trim()).filter(Boolean).map((x) => "&position=" + encodeURIComponent(x)).join("");
          const sizing = cfg.bankroll > 0 ? `&bankroll=${Number(cfg.bankroll)}&kelly=${Number(cfg.kelly) || 0.25}` : "";
          const ip = await bridgeJson(`${BRIDGE}/inplay?url=${encodeURIComponent(url)}&contracts=${cfg.contracts}&target_margin=${cfg.targetMargin}&gold=${cfg.gold ? 1 : 0}${pos}${sizing}`);
          if (ip && ip.ok) { out.analysis.inplay = ip.view; if (ip.view && Array.isArray(ip.view.executable_venues)) bridgeState.executableVenues = ip.view.executable_venues.map(String); }
        } catch (e) { /* optional */ }
      }
      // The bridge answered: it is up, whatever /health said 29 s ago (an HTTP error body is
      // still an answer — a {ok:false} report is the engine's opinion, not an outage).
      if (bridgeState.failures) bridgeNoteSuccess(bridgeState, now);
      if (out && typeof out === "object") out.bridge = bridgeStatus(bridgeState, Date.now());
      return out;
    } catch (e) {
      if (!e || !e.transport) {
        // The bridge answered but its report could not be mapped: an engine-side error, not an
        // outage. Surface it (the overlay keeps the last good tables) and leave the counters alone.
        if (bridgeState.failures) bridgeNoteSuccess(bridgeState, now);
        const err = new Error(`engine: ${(e && e.message) || e}`); err.bridge = bridgeStatus(bridgeState, Date.now()); throw err;
      }
      // A transport failure counts towards the fall-back; the N-th one marks the bridge down
      // and starts the /health backoff. `required` never degrades to direct: it reports.
      bridgeNoteFailure(bridgeState, now, e, false);
      if (cfg.bridge === "on") { const err = new Error(`bridge required but not answering (${e.message || e})`); err.bridge = bridgeStatus(bridgeState, Date.now()); throw err; }
      /* else fall through to direct mode for this tick */
    }
  } else if (cfg.bridge === "on") {
    const st = bridgeStatus(bridgeState, Date.now());
    const err = new Error(`bridge required but down${st.retryInMs ? ` (retry in ${Math.ceil(st.retryInMs / 1000)} s)` : ""} — run: python -m arb_engine bridge`);
    err.bridge = st; throw err;
  }
  const out = await analyzeDirect(url, cfg);
  if (out && typeof out === "object") out.bridge = cfg.bridge === "off" ? Object.assign(bridgeStatus(bridgeState, Date.now()), { mode: "direct", up: null, retryInMs: 0 }) : bridgeStatus(bridgeState, Date.now());
  return out;
}
async function analyzeDirect(url, cfg) {
  const ev = await robinhoodEvent(url);
  const contracts = ev.contracts;
  const lineTypes = new Set(contracts.map((c) => { const p = ArbCore.parseSymbol(c.symbol); return p && LINE_FAMILIES[p.family]; }).filter(Boolean));
  if (contracts.length && lineTypes.size === 1 && contracts.every((c) => { const p = ArbCore.parseSymbol(c.symbol); return p && LINE_FAMILIES[p.family]; })) return analyzeLines(url, ev, cfg, [...lineTypes][0]);
  if (contracts.length !== 2) return { ok: true, event: ev, analysis: null, note: `${contracts.length} contracts — the overlay handles game winners, spreads, totals and matches` };
  const parsed = contracts.map((c) => ArbCore.parseSymbol(c.symbol));
  // College football on Robinhood is CDNA-routed (NX.F.OPT.CFB-…): no team codes in the symbol, so
  // the Kalshi ticker and Polymarket slug can only be derived by the Python engine (761-team table).
  if (contracts.every((c) => (c.symbol || "").startsWith("NX.F.OPT.CFB")) || parsed.every((p) => p && /^NCAAF/.test(p.family))) {
    return { ok: true, event: ev, analysis: null, note: "College football needs the local engine: run `python -m arb_engine bridge` and set the popup's bridge to auto/on (direct mode cannot map college teams across venues)." };
  }
  if (parsed.some((p) => !p)) return { ok: true, event: ev, analysis: null, note: "unrecognised contract symbols: " + contracts.map((c) => c.symbol).join(", ") };
  const family = parsed[0].family;
  const isNfl = /^NFLGAME$/.test(family);
  const isTennis = /(ATP|WTA|ITF)/.test(family) && /MATCH/.test(family);
  const sport = isNfl ? "nfl" : isTennis ? "tennis" : "other";
  const outcomes = contracts.map((c, i) => (isNfl ? (ArbCore.nflTeamCode(c.short) || ArbCore.nflTeamCode(c.long) || parsed[i].side) : ArbCore.personKey(c.long || c.short) || parsed[i].side));
  const labels = {}; contracts.forEach((c, i) => { labels[outcomes[i]] = c.long || c.short; });

  // Robinhood quotes (live API, SSR fallback).
  let live = {};
  try { live = await robinhoodQuotes(contracts.map((c) => c.id)); } catch (e) { /* fall back to SSR */ }
  const byVenue = { robinhood: [] };
  contracts.forEach((c, i) => {
    const q = live[c.id] || ev.ssrQuotes[c.id] || {};
    const exch = rhExchange(c);
    byVenue.robinhood.push({ venue: "robinhood", outcome: outcomes[i], label: labels[outcomes[i]], ask: q.yes_ask_price != null ? Number(q.yes_ask_price) : null, bid: q.yes_bid_price != null ? Number(q.yes_bid_price) : null, askSize: q.ask_size != null ? Number(q.ask_size) : null, fee: ArbCore.feeFn("robinhood", { exchange: exch }, cfg), exchange: exch, bookId: exch === "kalshi" ? "kalshi" : exch, contractId: c.id, symbol: c.symbol, updatedAt: q.updated_at || null });
  });

  const errors = [];
  // Kalshi: same ticker scheme.
  if (cfg.venues.kalshi !== false) {
    const qs = [];
    for (let i = 0; i < contracts.length; i++) {
      const ticker = parsed[i].kalshiTicker;
      try {
        const m = await kalshiMarket(ticker);
        const series = await kalshiSeries(ticker.split("-")[0]).catch(() => ({}));
        const ask = m.yes_ask_dollars != null ? Number(m.yes_ask_dollars) : null, bid = m.yes_bid_dollars != null ? Number(m.yes_bid_dollars) : null;
        qs.push({ venue: "kalshi", outcome: outcomes[i], label: labels[outcomes[i]], ask: ask && ask < 1 ? ask : null, bid: bid && bid > 0 ? bid : null, askSize: m.yes_ask_size_fp != null ? Number(m.yes_ask_size_fp) : null, fee: ArbCore.feeFn("kalshi", { fee_type: series.fee_type, fee_multiplier: series.fee_multiplier }, cfg), bookId: "kalshi", ticker, status: m.status, url: `https://kalshi.com/markets/${ticker.split("-")[0].toLowerCase()}/${m.event_ticker ? m.event_ticker.toLowerCase() : ""}`, feeNote: series.fee_type === "quadratic_with_maker_fees" ? "taker 7% / maker 1.75% x p(1-p)" : "taker 7% x p(1-p)" });
      } catch (e) { errors.push(`kalshi ${ticker}: ${e.message}`); }
    }
    if (qs.length) byVenue.kalshi = qs;
  }
  // Polymarket.
  if (cfg.venues.polymarket !== false) {
    try {
      let m = null;
      if (isNfl && parsed[0].teams) m = await polymarketNfl(parsed[0].teams, parsed[0].date);
      else if (isTennis) m = await polymarketTennis(contracts.map((c) => c.long || c.short));
      if (m) {
        const keyFor = (label, i) => (isNfl ? ArbCore.nflTeamCode(label) : ArbCore.personKey(label)) || outcomes[i];
        const qs = polymarketQuotes(m, keyFor).filter((q) => outcomes.includes(q.outcome));
        if (qs.length === 2) byVenue.polymarket = qs; else errors.push("polymarket: outcome names did not match");
      } else errors.push("polymarket: no matching market found");
    } catch (e) { errors.push(`polymarket: ${e.message}`); }
  }

  // Same-book de-duplication: Robinhood's Kalshi-routed quotes ARE Kalshi's book.
  if (byVenue.kalshi) byVenue.robinhood.forEach((q) => { if (q.bookId === "kalshi") q.mirror = "kalshi"; });

  const fair = ArbCore.consensusFair(byVenue, outcomes);
  const quotesByOutcome = {};
  outcomes.forEach((o) => { quotesByOutcome[o] = Object.values(byVenue).flat().filter((q) => q.outcome === o); });
  const legs = ArbCore.bestLegPerOutcome(quotesByOutcome, cfg.contracts);
  const complete = legs.length === outcomes.length;
  const arb = complete ? ArbCore.evaluate(legs, cfg.contracts) : null;

  const rows = outcomes.map((o) => {
    const others = legs.filter((l) => l.outcome !== o);
    const venues = quotesByOutcome[o].map((q) => {
      const feePc = q.ask != null ? q.fee(q.ask, cfg.contracts, "taker") / cfg.contracts : null;
      const allIn = q.ask != null ? q.ask + feePc : null;
      const hedgeable = complete && others.length === outcomes.length - 1;
      return { venue: q.venue, exchange: q.exchange || null, mirror: q.mirror || null, ask: q.ask, bid: q.bid, askSize: q.askSize == null ? null : q.askSize, feePerContract: feePc, allIn, maxBuyTaker: hedgeable ? ArbCore.maxPrice(others, q.fee, cfg.contracts, cfg.targetMargin, "taker") : null, maxBuyMaker: hedgeable ? ArbCore.maxPrice(others, q.fee, cfg.contracts, cfg.targetMargin, "maker") : null, url: q.url || null, feeNote: q.feeNote || "", contractId: q.contractId || null, updatedAt: q.updatedAt || null };
    }).sort((a, b) => (a.allIn == null) - (b.allIn == null) || a.allIn - b.allIn);
    const best = venues.find((v) => v.allIn != null && !v.mirror) || null;
    return { outcome: o, label: labels[o], fair: fair[o], venues, best: best ? best.venue : null, bestAllIn: best ? best.allIn : null, edge: best && fair[o] != null ? fair[o] - best.allIn : null };
  });
  return { ok: true, event: { id: ev.id, name: ev.name, sport, url }, analysis: { contracts: cfg.contracts, targetMargin: cfg.targetMargin, gold: cfg.gold, rows, arb, errors, fetchedAt: Date.now(), venues: Object.keys(byVenue), source: "direct" } };
}

// Category pages: analyse many event URLs (moneylines) with a small concurrency and return a
// trimmed per-URL summary for the card badges. Results are cached briefly per URL.
const MANY_CONCURRENCY = 3;
async function analyzeMany(urls, max) {
  const unique = Array.from(new Set(urls || []));
  const list = unique.slice(0, max || 16);
  const results = {};
  let i = 0;
  async function worker() {
    while (i < list.length) {
      const u = list[i++];
      try {
        const r = await cached("many:" + u, 20_000, () => analyze(u, { inplay: false }));
        if (!r || !r.ok || !r.analysis || r.analysis.lines) { results[u] = { ok: false, error: (r && r.error) || "no analysis" }; continue; }
        const a = r.analysis;
        results[u] = {
          ok: true, name: r.event && r.event.name, source: a.source, fetchedAt: a.fetchedAt,
          arb: a.arb ? { isArb: !!a.arb.isArb, margin: a.arb.margin } : null,
          rows: (a.rows || []).map((row) => { const here = (row.venues || []).find((v) => v.venue === "robinhood" && v.side !== "no") || null; /* the YES contract on this page */ return { outcome: row.outcome, label: row.label, fair: row.fair, edge: row.edge, best: row.best, here: here ? { ask: here.ask, allIn: here.allIn, maxBuyTaker: here.maxBuyTaker, maxBuyMaker: here.maxBuyMaker } : null }; }),
        };
      } catch (e) { results[u] = { ok: false, error: e.message || String(e) }; }
    }
  }
  await Promise.all(Array.from({ length: Math.min(MANY_CONCURRENCY, list.length) }, worker));
  return { ok: true, results, fetchedAt: Date.now(), truncated: unique.length > list.length };
}

// Popup health card: a forced /health probe plus the executable venue set the bridge reported
// (its /health field when it has one, else the last /inplay view's executable_venues, else null
// so the popup can say "unknown" instead of guessing).
async function bridgeHealth(now) {
  now = now == null ? Date.now() : now;
  const cfg = await settings();
  const up = cfg.bridge === "off" ? null : await bridgeAvailable(now, true);
  const st = bridgeStatus(bridgeState, Date.now());
  return { ok: true, up, setting: cfg.bridge, mode: cfg.bridge === "off" ? "direct" : st.mode, failures: st.failures, retryInMs: st.retryInMs, lastError: st.lastError, service: st.service, executableVenues: st.executableVenues };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "analyze") {
    analyze(msg.url).then(sendResponse).catch((e) => sendResponse({ ok: false, error: e.message || String(e), bridge: e.bridge || bridgeStatus(bridgeState, Date.now()) }));
    return true;
  }
  if (msg && msg.type === "bridgeHealth") {  // popup: probe now (ignores the backoff) and report the state
    bridgeHealth().then(sendResponse).catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
    return true;
  }
  if (msg && msg.type === "analyzeMany") {
    analyzeMany(msg.urls, msg.max).then(sendResponse).catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
    return true;
  }
  if (msg && msg.type === "settings") { settings().then(sendResponse); return true; }
  return false;
});
