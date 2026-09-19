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

  // --- totals page (direct) ----------------------------------------------------------------
  globalThis.__totalsMode = true; cache.clear();
  const t = await analyze("https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/");
  globalThis.__totalsMode = false;
  eq(t.ok, true, "totals ok");
  eq(t.analysis.marketType, "total", "totals market type");
  eq(t.event.game, "CAR @ ATL", "totals game");
  eq(t.analysis.lines.length, 3, "totals lines");
  const l65 = t.analysis.lines.find((l) => l.line === 65.5), l44 = t.analysis.lines.find((l) => l.line === 44.5), l17 = t.analysis.lines.find((l) => l.line === 17.5);
  eq(!!l65 && !!l44 && !!l17, true, "lines present");
  eq(l65.arb.isArb, true, "65.5 is an arb");
  eq(l65.fillable, true, "65.5 fillable");
  eq(l65.sizedContracts, 200, "65.5 sized by Kalshi under depth");
  eq(l65.arb.legs.map((x) => x.venue + ":" + (x.outcome || x.label)).sort().join(","), "kalshi:under,robinhood:over", "65.5 legs");
  eq(Math.round(l65.arb.margin * 1e4) / 1e4, 0.0115, "65.5 margin");
  eq(l44.rows.length, 2, "44.5 rows");
  eq(l44.rows.find((r) => r.outcome === "over").venues.map((v) => v.venue).sort().join(","), "kalshi,polymarket,robinhood", "44.5 venues incl polymarket");
  eq(l44.rows.find((r) => r.outcome === "over").venues.find((v) => v.venue === "polymarket").ask, 0.47, "44.5 polymarket ask");
  eq(l44.arb.isArb, false, "44.5 no arb");
  eq(l17.arb, null, "17.5 no yes ask -> no arb");
  eq(t.analysis.lines[0].line, 65.5, "arbs sort first");
  eq(t.analysis.errors.some((e) => e.indexOf("17.5") >= 0), true, "missing kalshi line reported");

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
  // The in-play request must carry the popup's target margin (the bridge defaults it to 0 otherwise).
  stored.targetMargin = 0.02; stored.positions = "robinhood:PHI:0.50:100\nkalshi:TEN:0.40:50"; cache.clear();
  const b2 = await analyze(url);
  const ipCall = calls.filter((u) => u.startsWith("http://127.0.0.1:8765/inplay")).pop();
  eq(!!ipCall, true, "bridge mode requests /inplay");
  eq(ipCall.indexOf("&target_margin=0.02&") >= 0, true, "inplay carries target_margin: " + ipCall);
  eq(ipCall.indexOf("&contracts=100&") >= 0, true, "inplay carries contracts");
  eq((ipCall.match(/&position=/g) || []).length, 2, "inplay carries both positions");
  eq(ipCall.indexOf("position=robinhood%3APHI%3A0.50%3A100") >= 0, true, "position encoded");
  eq(b2.analysis.inplay && b2.analysis.inplay.actions.length, 1, "inplay view attached");
  delete stored.targetMargin; delete stored.positions;

  // --- bridge mapping for line pages (pure function on the Python engine's output) --------
  const bl = fromBridge(JSON.parse(FIXTURES["bridge_lines.json"]), { contracts: 100, targetMargin: 0, gold: false });
  eq(bl.analysis.source, "bridge", "bridge lines source");
  eq(bl.analysis.lines.length, 3, "bridge lines count");
  const b65 = bl.analysis.lines.find((l) => l.line === 65.5);
  eq(b65.fillable, true, "bridge 65.5 fillable");
  eq(b65.sizedContracts, 200, "bridge 65.5 sized");
  eq(b65.rows[0].venues[0].allIn != null, true, "bridge line rows mapped");
  eq(b65.arb.isArb, true, "bridge line arb mapped");

  // --- bridge required but down --------------------------------------------------------
  stored.bridge = "on"; globalThis.__bridgeOnline = false; bridgeUp = null; bridgeChecked = 0; cache.clear();
  const d = await analyze(url);
  eq(d.analysis.source, "direct", "bridge unavailable -> direct still works when not reachable");

  print((failures ? "FAILED " + failures + "/" : "ok ") + checks + " checks");
  if (failures) throw new Error("background tests failed");
})();
