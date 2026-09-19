/* Parity + logic tests for extension/arb-core.js.
 * Run: scripts/test_js.sh  (uses node if present, else macOS's bundled JavaScriptCore `jsc`).
 * The runner prepends `const FEE_VECTORS = <tests/fixtures/fee_vectors.json>;` and arb-core.js.
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
  for (const v of FEE_VECTORS) {
    const fn = A.feeFn(v.venue, v.params, v.settings || {});
    const fee = fn(v.price, v.contracts, v.role);
    checks++;
    if (Math.abs(fee - v.fee) > 1e-9) { bad++; if (bad < 10) print("FAIL fee " + JSON.stringify(v) + " -> " + fee); }
  }
  failures += bad;
  print("fee vectors: " + FEE_VECTORS.length + " checked, " + bad + " mismatches");

  // 2. Arbitrage math.
  const K = A.feeFn("kalshi", { fee_type: "quadratic_with_maker_fees", fee_multiplier: 1 }, {});
  const P = A.feeFn("polymarket", { feeSchedule: { rate: 0.05, exponent: 1, takerOnly: true } }, {});
  const R = A.feeFn("robinhood", { exchange: "rothera" }, { gold: false });
  const r = A.evaluate([{ outcome: "A", venue: "polymarket", price: 0.40, fee: P }, { outcome: "B", venue: "kalshi", price: 0.55, fee: K }], 100);
  eq(r.profit, 100 - (40 + 1.2 + 55 + 1.74), "evaluate profit");
  eq(r.isArb, true, "evaluate isArb");
  eq(A.maxPrice([{ outcome: "TEN", venue: "polymarket", price: 0.25, fee: P }], R, 100, 0, "taker"), 0.72, "maxPrice robinhood");
  eq(A.maxPrice([{ outcome: "TEN", venue: "polymarket", price: 0.25, fee: P }], K, 100, 0.01, "taker"), 0.71, "maxPrice kalshi target 1%");
  eq(A.maxPrice([{ outcome: "TEN", venue: "polymarket", price: 0.25, fee: P }], K, 100, 0, "maker"), 0.73, "maxPrice kalshi maker");
  eq(A.maxPrice([{ outcome: "A", venue: "polymarket", price: 0.99, fee: P }], K, 100, 0.05, "taker"), null, "maxPrice impossible");

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
