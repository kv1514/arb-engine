/* Parity + logic tests for extension/arb-core.js.
 * Run: scripts/test_js.sh  (uses node if present, else macOS's bundled JavaScriptCore `jsc`).
 * The runner prepends `const FEE_VECTORS = <tests/fixtures/fee_vectors.json>;` and arb-core.js.
 *
 * Arb vectors (tie payouts, 0.001 tick, size step) come from tests/fixtures/arb_vectors.json
 * when it exists (P09 writes it; loaded through globalThis.ARB_VECTORS if the runner prepends
 * it, else read from the working directory) and otherwise from INLINE_ARB_VECTORS below, which
 * were computed with arb_engine.quant.arbitrage on the Python fee models. Both use the shape
 *   { evaluate: [{legs: [LEG], contracts, expect: {profit, margin, tie_margin, tie_payout_total, is_arb}}],
 *     max_price: [{other_legs: [LEG], fee: FEE, contracts, target_margin, tick, role, price_floor, expect}],
 *     step: [{size, step, expect}] }
 *   LEG = {outcome, venue, price, role?, tie_payout?, fee: FEE}; FEE = {venue, params, settings?}
 */
(function () {
  const A = globalThis.ArbCore;
  let failures = 0, checks = 0;
  function eq(actual, expected, msg) {
    checks++;
    const ok = typeof expected === "number" ? Math.abs(actual - expected) < 1e-9 : actual === expected;
    if (!ok) { failures++; print("FAIL " + msg + ": expected " + JSON.stringify(expected) + " got " + JSON.stringify(actual)); }
  }
  function print(s) { (typeof console !== "undefined" && console.log) ? console.log(s) : globalThis.print(s); }

  // 1. Fee parity with the Python models (which are checked against the venues' tables).
  let bad = 0;
  const seen = { flat: 0, quadratic: 0, cdna: 0 };
  for (const v of FEE_VECTORS) {
    const fn = A.feeFn(v.venue, v.params, v.settings || {});
    const fee = fn(v.price, v.contracts, v.role);
    checks++;
    if (Math.abs(fee - v.fee) > 1e-9) { bad++; if (bad < 10) print("FAIL fee " + JSON.stringify(v) + " -> " + fee); }
    if (v.venue === "robinhood") {
      const s = v.settings || {};
      if (s.cdnaFeeModel) seen.cdna++; else if (s.rotheraFeeModel === "quadratic") seen.quadratic++; else seen.flat++;
    }
  }
  failures += bad;
  print("fee vectors: " + FEE_VECTORS.length + " checked, " + bad + " mismatches");
  eq(seen.flat > 0 && seen.quadratic > 0 && seen.cdna > 0, true, "fee vectors cover flat, quadratic and cdna models");

  // 1b. Venue worked numbers, straight from the schedules (fees/robinhood.py, fees/polymarket.py).
  const RQ = A.feeFn("robinhood", { exchange: "rothera" }, { gold: false, rotheraFeeModel: "quadratic" });
  eq(A.rotheraOrderFee(0.35, 100, 0.06), 1.37, "rothera schedule example k=0.06 (half-up)");
  eq(A.rotheraOrderFee(0.99, 100), 0.02, "rothera k=0.02 @0.99 x100");
  eq(A.rotheraOrderFee(0.99, 10), 0.01, "rothera floor x10");
  eq(A.rotheraOrderFee(0.50, 1), 0.01, "rothera floor x1");
  eq(RQ(0.99, 100, "taker"), 0.12, "rothera quadratic all-in fee 100 @0.99 = $0.10 commission + $0.02");
  eq(RQ(0.99, 1, "taker"), 0.02, "rothera quadratic 1 @0.99 = 1c commission + 1c floor");
  eq(A.feeFn("robinhood", { exchange: "rothera" }, {})(0.99, 100, "taker"), 1.10, "rothera default stays flat_001");
  eq(A.feeFn("robinhood", { exchange: "cdna" }, { cdnaFeeModel: "flat_002" })(0.50, 100, "taker"), 3.00, "cdna flat_002");
  eq(A.feeFn("robinhood", { exchange: "cdna" }, { cdnaFeeModel: "weighted_007" })(0.50, 100, "taker"), 2.75, "cdna weighted_007");
  eq(A.feeFn("robinhood", { exchange: "nadex" }, { cdnaFeeModel: "weighted_007" })(0.50, 100, "taker"), 2.75, "nadex is the same entity as cdna");
  eq(A.feeFn("robinhood", { exchange: "rothera", rothera_fee_model: "quadratic" }, { rotheraFeeModel: "flat_001" })(0.99, 100, "taker"), 0.12, "quote fee_params pin the model over settings");
  eq(A.feeFn("polymarket_us", { takerTheta: 0.0695 }, {})(0.50, 1000, "taker"), 17.38, "polymarket us worked example taker");
  eq(A.feeFn("polymarket_us", { takerTheta: 0.0695 }, {})(0.50, 1000, "maker"), -3.12, "polymarket us worked example maker");
  eq(A.feeFn("kalshi", { fee_type: "quadratic_with_maker_fees", fee_multiplier: 0.5 }, {})(0.50, 100, "taker"), 0.88, "kalshi fee_multiplier 0.5");

  // 2. Arbitrage math.
  const K = A.feeFn("kalshi", { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 }, {});
  const P = A.feeFn("polymarket", { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true } }, {});
  const R = A.feeFn("robinhood", { exchange: "rothera" }, { gold: false });
  const r = A.evaluate([{ outcome: "A", venue: "polymarket", price: 0.40, fee: P }, { outcome: "B", venue: "kalshi", price: 0.55, fee: K }], 100);
  eq(r.profit, 100 - (40 + 1.2 + 55 + 1.74), "evaluate profit");
  eq(r.isArb, true, "evaluate isArb");
  eq(r.tiePayoutTotal, 1, "default tie payouts sum to 1");
  eq(r.tieMargin, r.margin, "default tie margin equals margin");
  eq(A.maxPrice([{ outcome: "TEN", venue: "polymarket", price: 0.25, fee: P }], R, 100, 0, "taker"), 0.72, "maxPrice robinhood");
  eq(A.maxPrice([{ outcome: "TEN", venue: "polymarket", price: 0.25, fee: P }], K, 100, 0.01, "taker"), 0.71, "maxPrice kalshi target 1%");
  eq(A.maxPrice([{ outcome: "TEN", venue: "polymarket", price: 0.25, fee: P }], K, 100, 0, "maker"), 0.73, "maxPrice kalshi maker");
  eq(A.maxPrice([{ outcome: "A", venue: "polymarket", price: 0.99, fee: P }], K, 100, 0.05, "taker"), null, "maxPrice impossible");
  // Tail arb that only exists under Rothera's per-order fee (100 @ 0.05 Kalshi x 0.93 Rothera).
  const tailFlat = A.evaluate([{ outcome: "A", venue: "kalshi", price: 0.05, fee: K }, { outcome: "B", venue: "robinhood", price: 0.93, fee: R }], 100);
  const tailQuad = A.evaluate([{ outcome: "A", venue: "kalshi", price: 0.05, fee: K }, { outcome: "B", venue: "robinhood", price: 0.93, fee: RQ }], 100);
  eq(tailFlat.isArb, false, "tail no arb under flat_001");
  eq(tailQuad.profit, 0.87, "tail profit under quadratic");
  eq(A.evaluate([{ outcome: "A", venue: "kalshi", price: 0.05, fee: K }, { outcome: "B", venue: "robinhood", price: 0.93, fee: RQ }], 1).isArb, false, "1-lot pays the per-order floor: no arb");
  // Tie payouts: Kalshi YES-A + Rothera YES-B loses the tie; Kalshi YES-A + Rothera NO-A keeps it.
  const yesYes = A.evaluate([{ outcome: "A", venue: "kalshi", price: 0.52, fee: K, tiePayout: 0.5 }, { outcome: "B", venue: "robinhood", price: 0.47, fee: R, tiePayout: 0 }], 100);
  const yesNo = A.evaluate([{ outcome: "A", venue: "kalshi", price: 0.52, fee: K, tiePayout: 0.5 }, { outcome: "B", venue: "robinhood", price: 0.47, fee: R, tiePayout: 1 }], 100);
  eq(yesYes.tieMargin, -0.5275, "tie margin YES/YES");
  eq(yesNo.tieMargin, 0.4725, "tie margin YES/NO");
  eq(yesNo.margin, yesYes.margin, "tie payout does not change margin");
  // 0.001 tick, price floor, size step.
  eq(A.maxPrice([{ outcome: "A", venue: "polymarket", price: 0.253, fee: P }], P, 100, 0, "taker", 0.001), 0.727, "maxPrice 0.001 grid");
  eq(A.maxPrice([{ outcome: "A", venue: "polymarket", price: 0.253, fee: P }], P, 100, 0, "taker", 0.01), 0.72, "maxPrice 0.01 grid");
  eq(A.maxPrice([{ outcome: "A", venue: "polymarket", price: 0.253, fee: P }], P, 100, 0, "taker", 0.001, 0.75), null, "maxPrice below price floor");
  eq(A.stepSize(23, 5), 20, "step 5 rounds down");
  eq(A.stepSize(4, 5), 0, "below one step");
  eq(A.stepSize(100), 100, "default step 1");
  // On an all-in tie the leg that pays more in a tie wins (Rothera NO over YES).
  const tieLegs = A.bestLegPerOutcome({ A: [{ venue: "robinhood", ask: 0.47, fee: R, tiePayout: 0, label: "yes" }, { venue: "robinhood", ask: 0.47, fee: R, tiePayout: 1, label: "no" }] }, 100);
  eq(tieLegs[0].label, "no", "bestLegPerOutcome prefers the tie-paying leg");

  // 2b. Arb vectors shared with the Python suite.
  function loadArbVectors() {
    if (globalThis.ARB_VECTORS) return { src: "ARB_VECTORS", vec: globalThis.ARB_VECTORS };
    const path = "tests/fixtures/arb_vectors.json";
    try {  // node only: jsc's readFile prints on a missing file, so the runner passes ARB_VECTORS there
      if (typeof require === "function") { const fs = require("fs"); if (fs.existsSync(path)) return { src: path, vec: JSON.parse(fs.readFileSync(path, "utf8")) }; }
    } catch (e) { /* not present or unreadable: fall back to the inline vectors */ }
    return { src: "inline", vec: INLINE_ARB_VECTORS };
  }
  const FEE = (f) => A.feeFn(f.venue, f.params || {}, f.settings || {});
  const LEG = (l) => ({ outcome: l.outcome, venue: l.venue, price: l.price, role: l.role || "taker", tiePayout: l.tie_payout, fee: FEE(l.fee) });
  const INLINE_ARB_VECTORS = {
    evaluate: [
      { legs: [{ outcome: "A", venue: "kalshi", price: 0.52, tie_payout: 0.5, fee: { venue: "kalshi", params: { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 } } }, { outcome: "B", venue: "robinhood", price: 0.47, tie_payout: 0.0, fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false } } }], contracts: 100, expect: { profit: -2.75, margin: -0.0275, tie_margin: -0.5275, tie_payout_total: 0.5, is_arb: false } },
      { legs: [{ outcome: "A", venue: "kalshi", price: 0.52, tie_payout: 0.5, fee: { venue: "kalshi", params: { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 } } }, { outcome: "B", venue: "robinhood", price: 0.47, tie_payout: 1.0, fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false } } }], contracts: 100, expect: { profit: -2.75, margin: -0.0275, tie_margin: 0.4725, tie_payout_total: 1.5, is_arb: false } },
      { legs: [{ outcome: "A", venue: "kalshi", price: 0.05, fee: { venue: "kalshi", params: { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 } } }, { outcome: "B", venue: "robinhood", price: 0.93, fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false } } }], contracts: 100, expect: { profit: 0.0, margin: 0.0, tie_margin: 0.0, tie_payout_total: 1.0, is_arb: false } },
      { legs: [{ outcome: "A", venue: "kalshi", price: 0.05, fee: { venue: "kalshi", params: { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 } } }, { outcome: "B", venue: "robinhood", price: 0.93, fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false, rotheraFeeModel: "quadratic" } } }], contracts: 100, expect: { profit: 0.87, margin: 0.0087, tie_margin: 0.0087, tie_payout_total: 1.0, is_arb: true } },
      { legs: [{ outcome: "A", venue: "kalshi", price: 0.05, fee: { venue: "kalshi", params: { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 } } }, { outcome: "B", venue: "robinhood", price: 0.93, fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false, rotheraFeeModel: "quadratic" } } }], contracts: 10, expect: { profit: 0.08, margin: 0.008, tie_margin: 0.008, tie_payout_total: 1.0, is_arb: true } },
      { legs: [{ outcome: "A", venue: "kalshi", price: 0.05, fee: { venue: "kalshi", params: { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 } } }, { outcome: "B", venue: "robinhood", price: 0.93, fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false, rotheraFeeModel: "quadratic" } } }], contracts: 1, expect: { profit: -0.01, margin: -0.01, tie_margin: -0.01, tie_payout_total: 1.0, is_arb: false } },
    ],
    max_price: [
      { other_legs: [{ outcome: "A", venue: "polymarket", price: 0.253, fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } } }], fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } }, contracts: 100, target_margin: 0, tick: 0.001, role: "taker", expect: 0.727 },
      { other_legs: [{ outcome: "A", venue: "polymarket", price: 0.253, fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } } }], fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } }, contracts: 100, target_margin: 0, tick: 0.01, role: "taker", expect: 0.72 },
      { other_legs: [{ outcome: "A", venue: "polymarket", price: 0.253, fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } } }], fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } }, contracts: 100, target_margin: 0, tick: 0.001, role: "taker", price_floor: 0.75, expect: null },
      { other_legs: [{ outcome: "A", venue: "polymarket", price: 0.05, fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } } }], fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false, rotheraFeeModel: "quadratic" } }, contracts: 100, target_margin: 0, tick: 0.01, role: "taker", expect: 0.94 },
      { other_legs: [{ outcome: "A", venue: "polymarket", price: 0.05, fee: { venue: "polymarket", params: { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true }, feesEnabled: true } } }], fee: { venue: "robinhood", params: { exchange: "rothera" }, settings: { gold: false } }, contracts: 100, target_margin: 0, tick: 0.01, role: "taker", expect: 0.93 },
    ],
    step: [{ size: 23, step: 5, expect: 20 }, { size: 4, step: 5, expect: 0 }, { size: 100, step: 1, expect: 100 }],
  };
  const arb = loadArbVectors();
  let arbChecked = 0, arbBad = 0;
  for (const v of arb.vec.evaluate || []) {
    const res = A.evaluate(v.legs.map(LEG), v.contracts || 100);
    const got = { profit: res.profit, margin: res.margin, tie_margin: res.tieMargin, tie_payout_total: res.tiePayoutTotal, is_arb: res.isArb };
    for (const k of Object.keys(v.expect || {})) {
      arbChecked++; checks++;
      const exp = v.expect[k], act = got[k];
      const ok = typeof exp === "number" ? Math.abs(act - exp) < 1e-9 : act === exp;
      if (!ok) { arbBad++; failures++; if (arbBad < 10) print("FAIL arb evaluate " + k + " " + JSON.stringify(v) + " -> " + JSON.stringify(got)); }
    }
  }
  for (const v of arb.vec.max_price || []) {
    arbChecked++; checks++;
    const act = A.maxPrice((v.other_legs || []).map(LEG), FEE(v.fee), v.contracts || 100, v.target_margin || 0, v.role || "taker", v.tick || 0.01, v.price_floor == null ? 0.01 : v.price_floor, v.price_cap == null ? 0.99 : v.price_cap);
    const ok = v.expect == null ? act == null : (act != null && Math.abs(act - v.expect) < 1e-9);
    if (!ok) { arbBad++; failures++; if (arbBad < 10) print("FAIL arb max_price " + JSON.stringify(v) + " -> " + JSON.stringify(act)); }
  }
  for (const v of arb.vec.step || []) {
    arbChecked++; checks++;
    const act = A.stepSize(v.size, v.step);
    if (Math.abs(act - v.expect) > 1e-9) { arbBad++; failures++; if (arbBad < 10) print("FAIL arb step " + JSON.stringify(v) + " -> " + act); }
  }
  print("arb vectors (" + arb.src + "): " + arbChecked + " checked, " + arbBad + " mismatches");

  // 3. Identity helpers.
  A.loadTeams({ teams: { LAR: { city: "Los Angeles R", nick: "Rams", aliases: ["LA", "Los Angeles Rams"] }, BUF: { city: "Buffalo", nick: "Bills", aliases: [] }, DET: { city: "Detroit", nick: "Lions", aliases: [] } } });
  eq(A.nflTeamCode("Los Angeles R"), "LAR", "team city");
  eq(A.nflTeamCode("Spread: Bills (-1.5)"), "BUF", "team substring");
  eq(A.nflTeamCode("nobody"), null, "team unknown");
  eq(A.personKey("K. Miyoshi (b. 2004)"), "miyoshi", "person key initial");
  eq(A.personKey("Zeynep Sönmez"), "sonmez", "person key accent");
  eq(A.personKey("R. Pacheco Mendez"), "pacheco mendez", "person key two-word surname");
  const s = A.parseSymbol("NFLGAME-26SEP20PHITEN-PHI");
  eq(s.date, "2026-09-20", "symbol date");
  eq(s.teams.join(","), "PHI,TEN", "symbol teams");
  eq(s.kalshiTicker, "KXNFLGAME-26SEP20PHITEN-PHI", "symbol kalshi ticker");
  eq(A.parseSymbol("KXWTAMATCH-26SEP14YOUCHA-YOU").routed, "kalshi", "symbol routed kalshi");
  eq(A.parseSymbol("KXWTAMATCH-26SEP14YOUCHA-YOU").teams.join(","), "YOU,CHA", "symbol tennis codes");
  eq(A.polymarketNflSlugs(["DET", "BUF"], "2026-09-17")[0], "nfl-det-buf-2026-09-17", "slug 1");
  eq(A.polymarketNflSlugs(["DET", "BUF"], "2026-09-17")[3], "nfl-buf-det-2026-09-18", "slug 4");

  // 4. Consensus fair value renormalises to 1.
  const fair = A.consensusFair({ kalshi: [{ outcome: "A", ask: 0.33, bid: 0.32 }, { outcome: "B", ask: 0.68, bid: 0.67 }], polymarket: [{ outcome: "A", ask: 0.34, bid: 0.33 }, { outcome: "B", ask: 0.67, bid: 0.66 }] }, ["A", "B"]);
  eq(Math.round((fair.A + fair.B) * 1e9) / 1e9, 1, "fair sums to 1");
  eq(fair.A > 0.32 && fair.A < 0.34, true, "fair A range");

  // 5. Category-page helpers.
  eq(A.categoryPath("/us/en/prediction-markets/nfl/"), "nfl", "category public path");
  eq(A.categoryPath("/prediction-markets/tennis"), "tennis", "category app path");
  eq(A.categoryPath("/us/en/prediction-markets/nfl/events/x-vs-y-sep-20-2026/"), null, "event page is not a category");
  eq(A.categoryPath("/"), null, "root is not a category");
  eq(A.categoryGameHref("/us/en/prediction-markets/pro-football/events/september-20-philadelphia-vs-tennessee-sep-20-2026/").kind, "game", "game href");
  eq(A.categoryGameHref("/us/en/prediction-markets/pro-football/events/september-20-philadelphia-vs-tennessee-spread-sep-20-2026/").kind, "spread", "spread href");
  eq(A.categoryGameHref("/us/en/prediction-markets/pro-football/events/cincinnati-vs-houston-1st-half-total-sep-20-2026/").kind, "total", "1st-half total href");
  eq(A.categoryGameHref("/us/en/prediction-markets/pro-football/events/september-20-minnesota-vs-chicago-totals-sep-20-2026/").kind, "total", "totals href");
  eq(A.categoryGameHref("/us/en/prediction-markets/pro-football/events/maxx-crosbys-next-team-feb-26-2026/").kind, "other", "prop href");
  eq(A.categoryGameHref("/us/en/prediction-markets/nfl/"), null, "category href is not an event");
  eq(A.contractCode("PHI - 77¢").code, "PHI", "contract code");
  eq(A.contractCode("PHI - 77¢").cents, 77, "contract cents");
  eq(A.contractCode("Sep 20 @ 10:00 AM294k"), null, "card title is not a contract");
  eq(A.contractCode("PHI - 77¢fair 75.0¢ · max 72.0¢"), null, "already badged text does not re-match");

  print((failures ? "FAILED " + failures + "/" : "ok ") + checks + " checks");
  if (failures) throw new Error("arb-core tests failed");
})();
