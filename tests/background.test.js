/* Offline integration test for extension/background.js: stubs `fetch` with recorded venue
 * responses (tests/fixtures/ext/*) and the chrome.* APIs, then runs analyze() in direct
 * mode and in bridge mode. The runner (scripts/test_js.sh) prepends:
 *   const FIXTURES = {...};        // path -> file contents (strings)
 *   <extension/arb-core.js>
 *   <extension/background.js with importScripts removed>
 */
(async function () {
  const print = (s) => ((typeof console !== "undefined" && console.log) ? console.log(s) : globalThis.print(s));
  let failures = 0, checks = 0;
  const eq = (a, b, msg) => { checks++; const ok = typeof b === "number" ? Math.abs(a - b) < 1e-9 : a === b; if (!ok) { failures++; print(`FAIL ${msg}: expected ${JSON.stringify(b)} got ${JSON.stringify(a)}`); } };

  const calls = __calls, stored = __stored;

  const url = "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/";
  // --- direct mode -------------------------------------------------------------------
  const r = await analyze(url);
  eq(r.ok, true, "direct ok");
  eq(r.analysis.source, "direct", "direct source");
  eq(r.analysis.venues.slice().sort().join(","), "kalshi,polymarket,robinhood", "direct venues");
  eq(r.analysis.errors.length, 0, "direct no errors: " + JSON.stringify(r.analysis.errors));
  const phi = r.analysis.rows.find((x) => x.outcome === "PHI"), ten = r.analysis.rows.find((x) => x.outcome === "TEN");
  eq(!!phi && !!ten, true, "rows PHI/TEN");
  eq(phi.label, "Philadelphia", "label");
  const rh = phi.venues.find((v) => v.venue === "robinhood");
  eq(rh.exchange, "rothera", "rh exchange");
  eq(rh.mirror, null, "rothera is not a mirror");
  eq(rh.ask, 0.77, "rh ask from live quotes");
  eq(rh.feePerContract, 0.02, "rh fee/ct (commission cap + exchange)");
  eq(Math.round(rh.allIn * 1e4) / 1e4, 0.79, "rh all-in");
  eq(typeof rh.maxBuyTaker, "number", "max buy present");
  eq(rh.maxBuyMaker >= rh.maxBuyTaker, true, "maker >= taker max buy");
  eq(Math.abs(phi.fair + ten.fair - 1) < 1e-9, true, "fair sums to 1");
  eq(r.analysis.arb.legs.length, 2, "arb legs");
  eq(r.analysis.arb.isArb, false, "no arb in this snapshot");
  const kal = phi.venues.find((v) => v.venue === "kalshi");
  eq(kal.url.indexOf("kalshi.com/markets/kxnflgame/") >= 0, true, "kalshi url");
  eq(calls.some((u) => u.includes("markets/KXNFLGAME-26SEP20PHITEN-PHI")), true, "kalshi ticker derived from RH symbol");
  eq(calls.some((u) => u.includes("slug=nfl-phi-ten-2026-09-20")), true, "polymarket slug derived from RH symbol");

  // --- bridge mode ---------------------------------------------------------------------
  stored.bridge = "auto"; globalThis.__bridgeOnline = true; bridgeUp = null; bridgeChecked = 0; cache.clear();
  const b = await analyze(url);
  eq(b.ok, true, "bridge ok");
  eq(b.analysis.source, "bridge", "bridge source");
  eq(b.analysis.rows.length, 2, "bridge rows");
  const brh = b.analysis.rows[0].venues.find((v) => v.venue === "robinhood");
  eq(typeof brh.allIn, "number", "bridge all-in mapped");
  eq(typeof brh.maxBuyMaker, "number", "bridge maker max mapped");
  eq(b.analysis.arb && typeof b.analysis.arb.margin, "number", "bridge arb mapped");

  // --- bridge required but down --------------------------------------------------------
  stored.bridge = "on"; globalThis.__bridgeOnline = false; bridgeUp = null; bridgeChecked = 0; cache.clear();
  const d = await analyze(url);
  eq(d.analysis.source, "direct", "bridge unavailable -> direct still works when not reachable");

  print((failures ? "FAILED " + failures + "/" : "ok ") + checks + " checks");
  if (failures) throw new Error("background tests failed");
})();
