/* arb-core.js — shared pure math for the Robinhood overlay (no DOM, no fetch).
 *
 * Mirrors arb_engine/fees/*.py and arb_engine/quant/arbitrage.py. Fees use BigInt integer
 * arithmetic so cent rounding is exact (0.07*100*0.1*0.9 is 0.6300000000000001 in floats and
 * would round the wrong way). tests/arb-core.test.js checks parity against
 * tests/fixtures/fee_vectors.json, which the Python models generate.
 */
(function (root) {
  "use strict";

  const BI = (n) => BigInt(n);
  const TEN = 10n;
  const pow10 = (n) => TEN ** BI(n);

  // ---- exact helpers -------------------------------------------------------------------
  function toCentiCents(price) { return BI(Math.round(Number(price) * 10000)); }      // $0.5234 -> 5234
  function contractsU(c) { return BI(Math.round(Number(c) * 100)); }                    // 2 dp of a contract
  function rateU(r) { return BI(Math.round(Number(r) * 1e6)); }                         // rate in 1e-6
  function ceilDiv(n, d) { return n <= 0n ? 0n : (n + d - 1n) / d; }
  function roundHalfUpDiv(n, d) { return (n + d / 2n) / d; }
  function roundHalfEvenDiv(n, d) {
    const q = n / d, r = n % d, twice = 2n * (r < 0n ? -r : r);
    if (twice < d) return q;
    if (twice > d) return n < 0n ? q - 1n : q + 1n;
    return q % 2n === 0n ? q : (n < 0n ? q - 1n : q + 1n);
  }
  const money = (cents) => Number(cents) / 100;

  // ---- Kalshi ---------------------------------------------------------------------------
  // fee = round_up(M x 0.07 x C x P x (1-P)); maker 0.0175 on quadratic_with_maker_fees series.
  function feeKalshi(price, contracts, role, opts) {
    opts = opts || {};
    const mult = opts.multiplier == null ? 1 : Number(opts.multiplier);
    const makerFees = !!opts.makerFees;
    let rate = 0.07;
    if (role === "maker") rate = makerFees ? 0.0175 : 0;
    if (rate === 0 || mult === 0) return 0;
    const p = toCentiCents(price), q = 10000n - p;
    // raw * 10^(6+2+2+4+4) = rate_u * mult_u * C_u * p * q
    const N = rateU(rate) * BI(Math.round(mult * 100)) * contractsU(contracts) * p * q;
    if (opts.rounding === "centicent") return Number(ceilDiv(N, pow10(14))) / 10000;
    return money(ceilDiv(N, pow10(16)));
  }

  // ---- Robinhood ------------------------------------------------------------------------
  // commission = min(round_up_cent(k P (1-P) C), $0.01 C) with k = 0.10 (0.05 Gold);
  // plus the routing exchange's fee (fees/robinhood.py). Exchange-fee models, all opt-in and
  // defaulting to the historical flat $0.01/contract until an order ticket confirms them:
  //   rotheraFeeModel: "flat_001" | "quadratic"  — Rothera Fee Schedule 20260520:
  //       max(round_half_up(0.02 P (1-P) C, cent), $0.01) PER ORDER (floor is per order, so
  //       a 1-lot pays a full cent while 100 @ $0.99 pays $0.02)
  //   cdnaFeeModel: "flat_001" | "flat_002" | "weighted_007" — CDNA (ex-Nadex; also the
  //       "nadex" key): $0.01/ct, $0.02/ct, or round_up_cent(0.07 P (1-P) C). Unverified.
  // ForecastEX embeds its fee in the spread.
  const RH_EXCHANGE_FEE = { kalshi: 0.01, rothera: 0.01, nadex: 0.01, forecastex: 0, cdna: 0.01 };  // cdna = college games (NX.F.OPT.*), assumed at the cap
  const ROTHERA_RETAIL_K = 0.02;
  function rotheraOrderFee(price, contracts, k) {
    const C = contractsU(contracts);
    if (C <= 0n) return 0;
    const p = toCentiCents(price), q = 10000n - p;
    // k scaled at 1e-6 (not whole cents) so a non-schedule rate such as 0.025 matches Python's exact Decimal.
    const N = BI(Math.round((k == null ? ROTHERA_RETAIL_K : Number(k)) * 1e6)) * p * q * C;   // scale 10^16
    let cents = roundHalfUpDiv(N, pow10(14));
    if (cents < 1n) cents = 1n;
    return money(cents);
  }
  function rhExchangeFeeModel(exch, opts) {
    if (exch === "rothera") return opts.rotheraFeeModel || "flat_001";
    if (exch === "cdna" || exch === "nadex") return opts.cdnaFeeModel || "flat_001";
    return "flat_001";
  }
  function feeRobinhood(price, contracts, role, opts) {
    opts = opts || {};
    const k = opts.gold ? 0.05 : 0.10;
    const p = toCentiCents(price), q = 10000n - p, C = contractsU(contracts);
    if (C <= 0n) return 0;
    const N = BI(Math.round(k * 100)) * p * q * C;           // scale 10^(2+4+4+2) = 10^12
    let cents = ceilDiv(N, pow10(10));
    const capCents = C / 100n;                                 // $0.01 per contract
    if (cents > capCents) cents = capCents;
    const exch = opts.exchange || "rothera";
    const model = rhExchangeFeeModel(exch, opts);
    let exchangeCents;
    if (model === "quadratic") exchangeCents = BI(Math.round(rotheraOrderFee(price, contracts, opts.rotheraK) * 100));
    else if (model === "weighted_007") exchangeCents = ceilDiv(rateU(0.07) * C * p * q, pow10(14));   // scale 10^(6+2+4+4) -> cents
    else if (model === "flat_002") exchangeCents = 2n * C / 100n;
    else {
      const per = RH_EXCHANGE_FEE[exch] == null ? 0.01 : RH_EXCHANGE_FEE[exch];
      exchangeCents = BI(Math.round(per * 100)) * C / 100n;
    }
    return money(cents + exchangeCents);
  }

  // ---- Polymarket -----------------------------------------------------------------------
  // fee (USDC) = C x rate x (P (1-P))^exponent, takers only, 5 dp.
  function feePolymarket(price, contracts, role, opts) {
    opts = opts || {};
    if (opts.feesEnabled === false) return 0;
    if (role === "maker" && opts.takerOnly !== false) return 0;
    const rate = opts.rate == null ? 0.05 : Number(opts.rate);
    const exponent = opts.exponent == null ? 1 : Number(opts.exponent);
    if (exponent !== 1) {
      const pf = Number(price);
      return Math.round(Number(contracts) * rate * Math.pow(pf * (1 - pf), exponent) * 1e5) / 1e5;
    }
    const p = toCentiCents(price), q = 10000n - p;
    const N = rateU(rate) * contractsU(contracts) * p * q;   // scale 10^(6+2+4+4) = 10^16
    let units = roundHalfUpDiv(N, pow10(11));                  // 1e-5 USDC
    if (N > 0n && units < 1n) units = 1n;
    return Number(units) / 1e5;
  }

  // Polymarket US: fee = theta C P (1-P), banker's rounding to the cent.
  function feePolymarketUS(price, contracts, role, opts) {
    opts = opts || {};
    const theta = role === "maker" ? (opts.makerTheta == null ? -0.0125 : opts.makerTheta) : (opts.takerTheta == null ? 0.0695 : opts.takerTheta);
    const p = toCentiCents(price), q = 10000n - p;
    const N = BI(Math.round(theta * 1e6)) * contractsU(contracts) * p * q;   // scale 10^16
    return money(roundHalfEvenDiv(N, pow10(14)));
  }

  function feeFn(venue, params, settings) {
    params = params || {}; settings = settings || {};
    if (venue === "kalshi") {
      const o = { multiplier: params.fee_multiplier == null ? 1 : params.fee_multiplier, makerFees: params.fee_type === "quadratic_with_maker_fees", rounding: settings.kalshiRounding || "cent" };
      return (price, c, role) => feeKalshi(price, c, role, o);
    }
    if (venue === "robinhood") {
      // A quote's own fee_params pin the model (params.rothera_fee_model) over the user setting.
      const o = { gold: !!settings.gold, exchange: params.exchange || "rothera", rotheraFeeModel: params.rothera_fee_model || settings.rotheraFeeModel || "flat_001", cdnaFeeModel: params.cdna_fee_model || settings.cdnaFeeModel || "flat_001", rotheraK: params.rothera_k };
      return (price, c, role) => feeRobinhood(price, c, role, o);
    }
    if (venue === "polymarket") {
      const s = params.feeSchedule || {};
      const o = { rate: s.rate, exponent: s.exponent, takerOnly: s.takerOnly, feesEnabled: params.feesEnabled };
      return (price, c, role) => feePolymarket(price, c, role, o);
    }
    if (venue === "polymarket_us") return (price, c, role) => feePolymarketUS(price, c, role, params);
    return () => 0;
  }

  // ---- arbitrage ------------------------------------------------------------------------
  // legs: [{outcome, venue, price, fee: fn(price, contracts, role), role, tiePayout}]
  // tiePayout is what one contract of the leg pays if the game ties (quant/arbitrage.py):
  // 0.5 on venues that settle a tie half/half (Kalshi, Polymarket, Kalshi-routed Robinhood),
  // 0 for a Rothera YES and 1 for a Rothera NO. tieMargin is the locked margin *if* the game
  // ties; margin/isArb are unchanged (a tie is a separate risk the caller flags).
  const DEFAULT_TIE_PAYOUT = 0.5;
  function tiePayoutOf(l) { return l.tiePayout == null ? DEFAULT_TIE_PAYOUT : Number(l.tiePayout); }
  function evaluate(legs, contracts) {
    contracts = contracts || 100;
    let total = 0, gross = 0, tieTotal = 0;
    const out = legs.map((l) => {
      const fee = l.fee(l.price, contracts, l.role || "taker");
      const cost = l.price * contracts + fee;
      total += cost; gross += l.price; tieTotal += tiePayoutOf(l);
      return { outcome: l.outcome, venue: l.venue, price: l.price, fee, cost, allIn: cost / contracts, role: l.role || "taker", label: l.label || "", tiePayout: tiePayoutOf(l) };
    });
    const profit = contracts - total;
    const tieProfit = tieTotal * contracts - total;
    return { contracts, totalCost: total, payout: contracts, profit, margin: profit / contracts, roi: total ? profit / total : 0, legs: out, isArb: profit > 0, grossSum: gross, tiePayoutTotal: tieTotal, tieMargin: tieProfit / contracts };
  }

  function legCost(l, contracts) { return l.price * contracts + l.fee(l.price, contracts, l.role || "taker"); }

  // Highest price on the tick grid (0.01 Kalshi/Robinhood, 0.001 Polymarket tails) between
  // priceFloor and priceCap at which this leg still locks target margin against the others.
  // Prices are built as integer ticks so a 0.001 grid yields exact 3-dp values.
  function maxPrice(otherLegs, fee, contracts, targetMargin, role, tick, priceFloor, priceCap) {
    contracts = contracts || 100; targetMargin = targetMargin || 0; role = role || "taker"; tick = tick || 0.01;
    priceFloor = priceFloor == null ? 0.01 : priceFloor; priceCap = priceCap == null ? 0.99 : priceCap;
    const others = otherLegs.reduce((s, l) => s + legCost(l, contracts), 0);
    const budget = contracts * (1 - targetMargin) - others;
    if (budget <= 0) return null;
    const ticks = Math.round(1 / tick), dp = Math.max(0, Math.ceil(Math.log10(ticks) - 1e-9));
    const floorT = Math.max(1, Math.ceil(priceFloor * ticks - 1e-9));
    let t = Math.min(Math.floor(priceCap * ticks + 1e-9), Math.floor((budget / contracts) * ticks + 1e-9));
    for (; t >= floorT; t--) {
      const p = Number((t / ticks).toFixed(dp));
      if (p * contracts + fee(p, contracts, role) <= budget + 1e-9) return p;
    }
    return null;
  }

  // Largest multiple of `step` (a venue's minimum order size, e.g. 5 Polymarket shares) not
  // above `size`; 0 when even one step does not fit (mirrors size_from_books' cap rounding).
  function stepSize(size, step) {
    step = step || 1;
    if (!(size > 0)) return 0;
    return Math.floor(size / step + 1e-9) * step;
  }

  // Cheapest all-in leg per outcome; on an all-in tie prefer the leg that pays more if the
  // game ties (a Rothera NO over a Rothera YES at the same ask).
  function bestLegPerOutcome(quotesByOutcome, contracts) {
    const legs = [];
    for (const outcome of Object.keys(quotesByOutcome)) {
      let best = null, bestCost = Infinity;
      for (const q of quotesByOutcome[outcome]) {
        if (q.ask == null || q.mirror) continue;
        const leg = { outcome, venue: q.venue, price: q.ask, fee: q.fee, role: "taker", label: q.label, quote: q, tiePayout: q.tiePayout };
        const cost = legCost(leg, contracts);
        if (cost < bestCost - 1e-12 || (best && Math.abs(cost - bestCost) <= 1e-12 && tiePayoutOf(leg) > tiePayoutOf(best))) { best = leg; bestCost = cost; }
      }
      if (best) legs.push(best);
    }
    return legs;
  }

  // ---- fair value -----------------------------------------------------------------------
  const VENUE_WEIGHTS = { polymarket: 1.0, kalshi: 1.0, robinhood: 0.7, polymarket_us: 0.8 };
  function consensusFair(quotesByVenue, outcomes, weights) {
    weights = Object.assign({}, VENUE_WEIGHTS, weights || {});
    const per = {}, w = {};
    outcomes.forEach((o) => { per[o] = {}; w[o] = {}; });
    for (const venue of Object.keys(quotesByVenue)) {
      const qs = quotesByVenue[venue].filter((q) => q.ask != null || q.bid != null);
      const mids = {};
      qs.forEach((q) => { mids[q.outcome] = q.ask != null && q.bid != null ? (q.ask + q.bid) / 2 : (q.ask != null ? q.ask : q.bid); });
      if (Object.keys(mids).length === 1 && outcomes.length === 2) {
        const known = Object.keys(mids)[0];
        const other = outcomes.find((o) => o !== known);
        if (other) mids[other] = 1 - mids[known];
      }
      if (Object.keys(mids).length < outcomes.length) continue;
      const s = outcomes.reduce((a, o) => a + mids[o], 0);
      if (s <= 0) continue;
      qs.forEach((q) => {
        const spread = Math.max(q.ask != null && q.bid != null ? q.ask - q.bid : 0.05, 0.01);
        per[q.outcome][venue] = mids[q.outcome] / s;
        w[q.outcome][venue] = (weights[venue] == null ? 0.5 : weights[venue]) / spread;
      });
    }
    const raw = {};
    let total = 0, n = 0;
    outcomes.forEach((o) => {
      const vs = Object.keys(per[o]);
      if (!vs.length) { raw[o] = null; return; }
      const tw = vs.reduce((a, v) => a + w[o][v], 0);
      raw[o] = vs.reduce((a, v) => a + per[o][v] * w[o][v], 0) / tw;
      total += raw[o]; n++;
    });
    const out = {};
    outcomes.forEach((o) => { out[o] = raw[o] == null ? null : (n === outcomes.length && total > 0 ? raw[o] / total : raw[o]); });
    return out;
  }

  // ---- identity -------------------------------------------------------------------------
  let TEAM_INDEX = null;
  function loadTeams(json) {
    TEAM_INDEX = {};
    const teams = json.teams || json;
    for (const code of Object.keys(teams)) {
      const t = teams[code];
      [code, t.city, t.nick, t.city + " " + t.nick].concat(t.aliases || []).forEach((k) => { TEAM_INDEX[k.toLowerCase().replace(/[^a-z0-9]/g, "")] = code; });
    }
  }
  function nflTeamCode(name) {
    if (!name || !TEAM_INDEX) return null;
    const key = String(name).toLowerCase().replace(/[^a-z0-9]/g, "");
    if (TEAM_INDEX[key]) return TEAM_INDEX[key];
    const keys = Object.keys(TEAM_INDEX).sort((a, b) => b.length - a.length);
    for (const k of keys) if (k.length >= 4 && key.indexOf(k) >= 0) return TEAM_INDEX[k];
    return null;
  }
  function normalizePerson(name) {
    return String(name || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "").replace(/\(.*?\)/g, " ").replace(/[^A-Za-z\s\-']/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
  }
  function personKey(name) {
    const s = normalizePerson(name);
    if (!s) return "";
    const parts = s.replace(/-/g, " ").split(" ");
    return parts.length === 1 ? parts[0] : parts.slice(1).join(" ");
  }
  const MONTHS = { JAN: 1, FEB: 2, MAR: 3, APR: 4, MAY: 5, JUN: 6, JUL: 7, AUG: 8, SEP: 9, OCT: 10, NOV: 11, DEC: 12 };
  // "NFLGAME-26SEP20PHITEN-PHI" -> {family:"NFLGAME", date:"2026-09-20", teams:["PHI","TEN"], side:"PHI", kalshiTicker:"KXNFLGAME-26SEP20PHITEN-PHI"}
  function parseSymbol(symbol) {
    const m = /^(KX)?([A-Z0-9]+)-(\d{2})([A-Z]{3})(\d{2})([A-Z0-9]+)-([A-Z0-9]+)$/.exec(symbol || "");
    if (!m || !MONTHS[m[4]]) return null;
    const family = m[2], date = "20" + m[3] + "-" + String(MONTHS[m[4]]).padStart(2, "0") + "-" + m[5];
    const pair = m[6], side = m[7];
    let teams = null;
    if (pair.endsWith(side)) teams = [pair.slice(0, pair.length - side.length), side];
    else if (pair.startsWith(side)) teams = [side, pair.slice(side.length)];
    return { family, date, pair, side, teams, kalshiTicker: (m[1] ? "" : "KX") + symbol, routed: m[1] ? "kalshi" : "other" };
  }
  function addDays(iso, d) {
    const t = new Date(iso + "T12:00:00Z"); t.setUTCDate(t.getUTCDate() + d);
    return t.toISOString().slice(0, 10);
  }
  // Candidate Polymarket event slugs for an NFL game: nfl-{away}-{home}-{utc date}.
  function polymarketNflSlugs(teams, date) {
    const a = teams[0].toLowerCase(), b = teams[1].toLowerCase();
    const dates = [date, addDays(date, 1)];
    const out = [];
    dates.forEach((d) => { out.push(`nfl-${a}-${b}-${d}`); out.push(`nfl-${b}-${a}-${d}`); });
    return out;
  }

  // --- category pages (one card per event) -------------------------------------------------
  // "/us/en/prediction-markets/nfl/" or "/prediction-markets/nfl" -> "nfl"; event pages -> null.
  function categoryPath(pathname) {
    const m = /^(?:\/us\/en)?\/prediction-markets\/([^/?#]+)\/?$/.exec(pathname || "");
    return m ? m[1] : null;
  }
  // Classify an event href by its slug: game (moneyline), spread, total (incl. 1st-half), other.
  function categoryGameHref(href) {
    const m = /\/events\/([^/?#]+)/.exec(href || "");
    if (!m) return null;
    const slug = m[1];
    const kind = /spread/.test(slug) ? "spread" : /total|points/.test(slug) ? "total" : /-vs-/.test(slug) ? "game" : "other";
    return { slug, kind };
  }
  // Contract button text on a card: "PHI - 77¢" -> { code: "PHI", cents: 77 }.
  function contractCode(text) {
    const m = /^\s*([A-Z]{2,4})\s*-\s*(\d{1,2})¢\s*$/.exec(text || "");
    return m ? { code: m[1], cents: Number(m[2]) } : null;
  }

  root.ArbCore = { feeKalshi, feeRobinhood, rotheraOrderFee, feePolymarket, feePolymarketUS, feeFn, evaluate, maxPrice, stepSize, bestLegPerOutcome, consensusFair, loadTeams, nflTeamCode, personKey, normalizePerson, parseSymbol, polymarketNflSlugs, addDays, categoryPath, categoryGameHref, contractCode };
})(typeof globalThis !== "undefined" ? globalThis : this);
