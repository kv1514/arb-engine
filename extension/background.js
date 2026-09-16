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
const DEFAULTS = { gold: false, contracts: 100, targetMargin: 0, kalshiRounding: "cent", refreshSeconds: 15, venues: { kalshi: true, polymarket: true }, showBadges: true, bridge: "auto" };

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

let bridgeUp = null, bridgeChecked = 0;
async function bridgeAvailable() {
  if (Date.now() - bridgeChecked < 30_000 && bridgeUp !== null) return bridgeUp;
  bridgeChecked = Date.now();
  try { const r = await fetch(`${BRIDGE}/health`, { credentials: "omit" }); bridgeUp = r.ok; } catch (e) { bridgeUp = false; }
  return bridgeUp;
}

// Convert the Python engine's EventReport into the shape content.js renders.
function fromBridge(res, cfg) {
  if (!res || !res.ok || !res.analysis) return res;
  const a = res.analysis;
  const rows = (a.outcomes || []).map((o) => ({ outcome: o.outcome, label: o.label, fair: o.fair, best: o.best_buy_venue, bestAllIn: o.best_buy_all_in, edge: o.edge_at_best, venues: (o.venues || []).map((v) => ({ venue: v.venue, exchange: v.exchange, mirror: v.mirror_of, ask: v.ask, bid: v.bid, askSize: v.ask_size, feePerContract: v.fee_per_contract, allIn: v.all_in, maxBuyTaker: v.max_buy_price, maxBuyMaker: v.max_buy_maker, url: v.url, feeNote: "" })) }));
  const arb = a.arb ? { grossSum: a.arb.gross_sum, margin: a.arb.margin, profit: a.arb.profit, contracts: a.arb.contracts, isArb: a.arb.is_arb, legs: (a.arb.legs || []).map((l) => ({ venue: l.venue, label: l.label || l.outcome, price: l.price, fee: l.fee })) } : null;
  return { ok: true, event: res.event, analysis: { contracts: cfg.contracts, targetMargin: cfg.targetMargin, gold: cfg.gold, rows, arb, errors: (a.errors || []).concat(a.flags && a.flags.length ? ["flags: " + a.flags.join(", ")] : []), fetchedAt: Date.now(), venues: a.venues || [], source: "bridge" } };
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
async function robinhoodEvent(url) {
  return cached("rh:" + url, 60_000, async () => {
    const pp = extractNextData(await getText(url)).props.pageProps;
    const ev = pp.event;
    if (!ev) throw new Error("not an event page");
    const contracts = Object.values(ev.eventContracts || {});
    return { id: ev.id, name: ev.name, category: ev.category, mutuallyExclusive: ev.mutuallyExclusive, contracts: contracts.map((c) => ({ id: c.id, symbol: c.symbol, exchange: c.exchange, short: c.displayShortName, long: c.displayLongName })), ssrQuotes: pp.quotes || {} };
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
  return (c.symbol || "").startsWith("KX") ? "kalshi" : "rothera";
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
async function analyze(url) {
  await ensureTeams();
  const cfg = await settings();
  if (cfg.bridge !== "off" && (await bridgeAvailable())) {
    try {
      const res = await getJson(`${BRIDGE}/analyze?url=${encodeURIComponent(url)}&contracts=${cfg.contracts}&target_margin=${cfg.targetMargin}&gold=${cfg.gold ? 1 : 0}`);
      return fromBridge(res, cfg);
    } catch (e) { if (cfg.bridge === "on") throw e; /* else fall through to direct mode */ }
  }
  const ev = await robinhoodEvent(url);
  const contracts = ev.contracts;
  if (contracts.length !== 2) return { ok: true, event: ev, analysis: null, note: `${contracts.length} contracts — the overlay handles two-outcome game/match markets` };
  const parsed = contracts.map((c) => ArbCore.parseSymbol(c.symbol));
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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "analyze") {
    analyze(msg.url).then(sendResponse).catch((e) => sendResponse({ ok: false, error: e.message || String(e) }));
    return true;
  }
  if (msg && msg.type === "settings") { settings().then(sendResponse); return true; }
  return false;
});
