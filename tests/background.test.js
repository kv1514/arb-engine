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
  stored.bridge = "auto"; globalThis.__bridgeOnline = true; bridgeState = newBridgeState(); cache.clear();
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

  // --- sizing settings reach the bridge's /inplay ---------------------------------------------
  stored.bankroll = 500; stored.kelly = 0.5; cache.clear();
  await analyze(url);
  eq(calls.some((u) => u.includes("/inplay?") && u.includes("bankroll=500") && u.includes("kelly=0.5")), true, "bankroll + kelly passed to /inplay");
  stored.bankroll = 0; cache.clear();
  const nBefore = calls.length;
  await analyze(url);
  eq(calls.slice(nBefore).some((u) => u.includes("bankroll=")), false, "no bankroll -> no sizing params");

  // --- bridge status rides along with every answer --------------------------------------
  eq(b.bridge && b.bridge.mode, "bridge", "bridge answer carries mode=bridge");
  eq(b.bridge.up, true, "bridge answer says up");

  // --- bridge required but down: never degrades to direct, says so and when it retries ----
  stored.bridge = "on"; globalThis.__bridgeOnline = false; bridgeState = newBridgeState(); cache.clear();
  let reqErr = null;
  try { await analyze(url); } catch (e) { reqErr = e; }
  eq(!!reqErr, true, "bridge=on + bridge down -> analyze throws instead of direct");
  eq(/bridge required/.test(reqErr && reqErr.message), true, "required error names the bridge: " + (reqErr && reqErr.message));
  eq(reqErr.bridge && reqErr.bridge.up, false, "required error carries bridge state (down)");
  eq(reqErr.bridge.retryInMs > 0 && reqErr.bridge.retryInMs <= BRIDGE_BACKOFF_MIN_MS, true, "first retry due within the minimum backoff");

  // --- pure state machine: N /analyze failures -> down; /health backoff 2 s -> 30 s; recovery -
  {
    const st = newBridgeState();
    eq(bridgeShouldProbe(st, 1000), true, "fresh state probes");
    bridgeNoteSuccess(st, 1000, { ok: true, service: "arb-engine bridge", executable_venues: ["kalshi", "robinhood"] });
    eq(st.up === true && st.mode === "bridge", true, "health ok -> up/bridge");
    eq(st.service, "arb-engine bridge", "service recorded from /health");
    eq(st.executableVenues.join(","), "kalshi,robinhood", "executable venues recorded from /health");
    eq(bridgeShouldProbe(st, 1000 + BRIDGE_HEALTH_TTL_MS - 1), false, "up: no re-probe inside the health TTL");
    eq(bridgeShouldProbe(st, 1000 + BRIDGE_HEALTH_TTL_MS), true, "up: re-probe once the TTL lapses");
    for (let i = 1; i < BRIDGE_FAIL_LIMIT; i++) { bridgeNoteFailure(st, 2000, new Error("Failed to fetch"), false); eq(st.up, true, `soft failure ${i} keeps the bridge up`); }
    bridgeNoteFailure(st, 2000, new Error("Failed to fetch"), false);
    eq(st.up === false && st.mode === "direct", true, `failure ${BRIDGE_FAIL_LIMIT} -> down/direct`);
    eq(st.backoffMs, BRIDGE_BACKOFF_MIN_MS, "first backoff is the minimum (2 s)");
    eq(st.nextProbeAt, 2000 + BRIDGE_BACKOFF_MIN_MS, "next probe due after the backoff");
    eq(bridgeShouldProbe(st, 2000 + BRIDGE_BACKOFF_MIN_MS - 1), false, "down: no probe before the backoff");
    eq(bridgeShouldProbe(st, 2000 + BRIDGE_BACKOFF_MIN_MS), true, "down: probe when due");
    const seq = [];
    let t = st.nextProbeAt;
    for (let i = 0; i < 6; i++) { bridgeNoteFailure(st, t, new Error("x"), true); seq.push(st.backoffMs); t = st.nextProbeAt; }
    eq(seq.join(","), "4000,8000,16000,30000,30000,30000", "backoff doubles and caps at 30 s");
    eq(bridgeStatus(st, t - 500).retryInMs, 500, "status reports the time to the next probe");
    bridgeNoteSuccess(st, t);
    eq(st.up === true && st.mode === "bridge" && st.failures === 0 && st.backoffMs === 0, true, "health answers -> back to bridge, counters reset");
    eq(bridgeStatus(st, t).retryInMs, 0, "up: no retry pending");
    eq(st.executableVenues.join(","), "kalshi,robinhood", "recovery keeps the last known executable set");
  }

  // --- integration: /analyze failing N times in auto mode flips to direct, then recovers ----
  {
    const realFetch = globalThis.fetch, realNow = Date.now;
    let clock = 1_000_000, analyzeDown = false;
    Date.now = () => clock;
    globalThis.fetch = async (u, init) => { if (analyzeDown && u.startsWith("http://127.0.0.1:8765/analyze")) throw new TypeError("Failed to fetch"); return realFetch(u, init); };
    try {
      stored.bridge = "auto"; globalThis.__bridgeOnline = true; bridgeState = newBridgeState(); cache.clear();
      const ok1 = await analyze(url);
      eq(ok1.analysis.source, "bridge", "auto: bridge answers -> bridge mode");
      analyzeDown = true; globalThis.__bridgeOnline = false;
      let healthCalls = () => calls.filter((u) => u.startsWith("http://127.0.0.1:8765/health")).length;
      const h0 = healthCalls();
      for (let i = 1; i <= BRIDGE_FAIL_LIMIT; i++) {
        clock += 1000; cache.clear();
        const r = await analyze(url);
        eq(r.analysis.source, "direct", `auto: failed /analyze ${i} falls through to direct for this tick`);
        eq(r.bridge.failures, i, `failure count ${i}`);
        eq(r.bridge.up, i < BRIDGE_FAIL_LIMIT, `bridge marked down only on failure ${BRIDGE_FAIL_LIMIT}`);
      }
      eq(healthCalls(), h0, "no /health probe while counting failures (the health TTL is still valid)");
      const nAnalyze = () => calls.filter((u) => u.startsWith("http://127.0.0.1:8765/analyze")).length;
      const a0 = nAnalyze();
      clock += 500; cache.clear();
      const r2 = await analyze(url);
      eq(r2.analysis.source, "direct", "down: direct without touching the bridge");
      eq(nAnalyze(), a0, "down: /analyze not attempted before the backoff");
      eq(healthCalls(), h0, "down: /health not probed before the backoff");
      eq(r2.bridge.retryInMs > 0, true, "down: retry countdown reported");
      clock += BRIDGE_BACKOFF_MIN_MS; cache.clear();
      const r3 = await analyze(url);
      eq(healthCalls(), h0 + 1, "backoff elapsed: one /health probe");
      eq(r3.analysis.source, "direct", "probe failed: still direct");
      eq(r3.bridge.backoffMs, 2 * BRIDGE_BACKOFF_MIN_MS, "probe failed: backoff doubled");
      globalThis.__bridgeOnline = true; analyzeDown = false;
      clock += 2 * BRIDGE_BACKOFF_MIN_MS; cache.clear();
      const r4 = await analyze(url);
      eq(healthCalls(), h0 + 2, "second probe after the doubled backoff");
      eq(r4.analysis.source, "bridge", "health answers -> back to bridge mode");
      eq(r4.bridge.up === true && r4.bridge.failures === 0, true, "recovered state reported");
      eq(Array.isArray(r4.bridge.executableVenues) || r4.bridge.executableVenues === null, true, "executable venues degrade to null when the bridge does not say");

      // --- popup health probe: forced, ignores the backoff, reports setting + mode ----------
      globalThis.__bridgeOnline = false; bridgeState = newBridgeState(); bridgeNoteFailure(bridgeState, clock, new Error("x"), true);
      const hc = healthCalls();
      const ph = await bridgeHealth(clock);
      eq(healthCalls(), hc + 1, "bridgeHealth probes even inside the backoff");
      eq(ph.ok === true && ph.up === false && ph.mode === "direct" && ph.setting === "auto", true, "health card: down/direct/auto: " + JSON.stringify(ph));
      globalThis.__bridgeOnline = true;
      const ph2 = await bridgeHealth(clock + 60_000);
      eq(ph2.up === true && ph2.mode === "bridge" && ph2.service === null, true, "health card: up; the stub /health carries no service name so it stays null: " + JSON.stringify(ph2));
      stored.bridge = "off";
      const ph3 = await bridgeHealth(clock + 60_000);
      eq(ph3.up === null && ph3.mode === "direct" && ph3.setting === "off", true, "health card: bridge=off never probes");
      const offRes = await analyze(url);
      eq(offRes.bridge.mode === "direct" && offRes.bridge.up === null, true, "bridge=off: status says direct, no up/down claim");
    } finally { globalThis.fetch = realFetch; Date.now = realNow; }
  }

  // --- an HTTP error answer is the engine's opinion, not an outage: no fall-back, no backoff -
  {
    const realFetch = globalThis.fetch, realNow = Date.now;
    let clock = 2_000_000, analyzeStatus = 0, analyzeBody = '{"ok":false,"error":"Robinhood page fetch: HTTP 429"}';
    Date.now = () => clock;
    globalThis.fetch = async (u, init) => {
      if (analyzeStatus && u.startsWith("http://127.0.0.1:8765/analyze")) { __calls.push(u); return { ok: false, status: analyzeStatus, text: async () => analyzeBody, json: async () => JSON.parse(analyzeBody) }; }
      return realFetch(u, init);
    };
    try {
      stored.bridge = "auto"; globalThis.__bridgeOnline = true; bridgeState = newBridgeState(); cache.clear();
      eq((await analyze(url)).analysis.source, "bridge", "http-error: bridge answers first");
      const healthCalls = () => calls.filter((u) => u.startsWith("http://127.0.0.1:8765/health")).length;
      const nAnalyze = () => calls.filter((u) => u.startsWith("http://127.0.0.1:8765/analyze")).length;
      const h0 = healthCalls();
      analyzeStatus = 500;
      for (let i = 1; i <= BRIDGE_FAIL_LIMIT + 1; i++) {
        clock += 1000; cache.clear();
        const a0 = nAnalyze();
        const r = await analyze(url);
        eq(nAnalyze(), a0 + 1, `500 #${i}: /analyze still attempted (the bridge is alive)`);
        eq(r.ok, false, `500 #${i}: surfaced as an error, not silently swapped for direct`);
        eq(/engine: Robinhood page fetch: HTTP 429/.test(r.error), true, `500 #${i}: engine error text kept: ${r.error}`);
        eq(r.analysis === undefined, true, `500 #${i}: no direct-mode analysis substituted`);
        eq(r.bridge.up === true && r.bridge.mode === "bridge" && r.bridge.failures === 0, true, `500 #${i}: bridge stays up/bridge with no failures counted: ${JSON.stringify(r.bridge)}`);
      }
      eq(healthCalls(), h0, "500s: no /health backoff probe started");
      // A 500 with a non-JSON body still names the route; a transport failure after it starts counting from zero.
      analyzeBody = "Internal Server Error"; clock += 1000; cache.clear();
      const rt = await analyze(url);
      eq(rt.ok === false && /engine: HTTP 500 \/analyze/.test(rt.error), true, "500 without JSON: route named: " + rt.error);
      analyzeStatus = 0; clock += 1000; cache.clear();
      eq((await analyze(url)).analysis.source, "bridge", "engine recovers: bridge mode without any probe or backoff");
      eq(healthCalls(), h0, "still no /health probe (the health TTL is valid throughout)");
      // bridge=on: an engine error is reported as such, never as 'bridge required but not answering'.
      stored.bridge = "on"; analyzeStatus = 500; analyzeBody = '{"ok":false,"error":"boom"}'; clock += 1000; cache.clear();
      const ro = await analyze(url);
      eq(ro.ok === false && ro.error === "engine: boom", true, "bridge=on + 500: engine error surfaced: " + ro.error);
      eq(ro.bridge.up === true && ro.bridge.mode === "bridge", true, "bridge=on + 500: still bridge mode");
      // The in-play strip's 500 is optional and never poisons the /analyze answer.
      analyzeStatus = 0; stored.bridge = "auto";
      const realFetch2 = globalThis.fetch;
      globalThis.fetch = async (u, init) => (u.startsWith("http://127.0.0.1:8765/inplay") ? (__calls.push(u), { ok: false, status: 500, text: async () => "{}", json: async () => ({ ok: false, error: "espn" }) }) : realFetch2(u, init));
      clock += 1000; cache.clear();
      const ri = await analyze(url);
      eq(ri.ok === true && ri.analysis.source === "bridge" && ri.analysis.inplay === undefined, true, "inplay 500: tables still from the bridge, strip absent");
      eq(ri.bridge.up === true && ri.bridge.failures === 0, true, "inplay 500: no failure counted");
      // The transport classifier itself.
      let te = null;
      globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
      try { await bridgeJson("http://127.0.0.1:8765/analyze?x"); } catch (e) { te = e; }
      eq(!!te && te.transport === true && /Failed to fetch/.test(te.message), true, "rejected fetch -> transport error");
      globalThis.fetch = async () => ({ ok: true, status: 200, text: async () => "nope", json: async () => { throw new SyntaxError("bad json"); } });
      const nj = await bridgeJson("http://127.0.0.1:8765/analyze?x");
      eq(nj.ok === false && /engine: no JSON from \/analyze/.test(nj.error), true, "2xx without JSON -> engine error, not transport: " + nj.error);
    } finally { globalThis.fetch = realFetch; Date.now = realNow; stored.bridge = "auto"; }
  }

  // --- quote ages: degrade to null when the engine does not send them ----------------------
  eq(quoteAgeOf({ quote_age: 0.4 }), 0.4, "quote_age read");
  eq(quoteAgeOf({ age: 2 }), 2, "age alias read");
  eq(quoteAgeOf({}), null, "missing quote age -> null");
  eq(quoteAgeOf({ quote_age: "nan" }), null, "non-numeric quote age -> null");
  eq(JSON.stringify(venueAges(["robinhood", { venue: "kalshi", quote_age: 1.5 }, { quote_age: 1 }])), JSON.stringify([{ venue: "robinhood", quoteAge: null }, { venue: "kalshi", quoteAge: 1.5 }]), "venueAges normalises names and objects");
  eq(b.analysis.rows[0].venues.every((v) => "quoteAge" in v && v.quoteAge === null), true, "bridge rows carry quoteAge=null when absent");

  // --- category page: analyzeMany --------------------------------------------------------
  stored.bridge = "off"; cache.clear();
  const before = calls.length;
  const many = await analyzeMany([url, url, "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/"], 16);
  eq(many.ok, true, "many ok");
  eq(Object.keys(many.results).length, 2, "many dedupes urls");
  eq(many.truncated, false, "many not truncated");
  const mr = many.results[url];
  eq(mr.ok, true, "many game ok");
  eq(mr.rows.length, 2, "many rows");
  eq(mr.rows.find((x) => x.outcome === "PHI").here.ask, 0.77, "many here ask");
  eq(typeof mr.rows.find((x) => x.outcome === "PHI").here.maxBuyTaker, "number", "many max buy");
  eq(mr.arb.isArb, false, "many arb flag");
  eq(many.results["https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/"].ok, false, "many skips line pages");
  const again = await analyzeMany([url], 16);
  eq(calls.length > before, true, "many fetched");
  const afterFirst = calls.length;
  await analyzeMany([url], 16);
  eq(calls.length, afterFirst, "many result cached per url");
  eq((await analyzeMany([url, "https://robinhood.com/x/", "https://robinhood.com/y/"], 1)).truncated, true, "many truncates at max");

  // --- college football (CDNA symbols) in direct mode: needs the bridge ------------------
  eq(rhExchange({ symbol: "NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228", exchange: "EXCHANGE_SOURCE_CDNA" }), "cdna", "cdna exchange from enum");
  eq(rhExchange({ symbol: "NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228" }), "cdna", "cdna exchange from symbol");
  eq(Number(ArbCore.feeRobinhood(0.15, 100, { exchange: "cdna" })), 2.0, "cdna fee: $1 commission cap + $1 exchange for 100 contracts");

  print((failures ? "FAILED " + failures + "/" : "ok ") + checks + " checks");
  if (failures) throw new Error("background tests failed");
})();
