/* Stubs for `fetch` and the chrome.* APIs, loaded BEFORE extension/background.js in the
 * test bundle (background.js registers listeners at load time). FIXTURES is defined by
 * scripts/test_js.sh. */
globalThis.__calls = [];
globalThis.__bridgeOnline = false;
globalThis.__stored = { bridge: "off" };
globalThis.fetch = async (url) => {
  __calls.push(url);
  const res = (status, body) => ({ ok: status < 400, status, text: async () => body, json: async () => JSON.parse(body) });
  if (url.startsWith("http://127.0.0.1:8765/health")) return __bridgeOnline ? res(200, '{"ok":true}') : Promise.reject(new TypeError("Failed to fetch"));
  if (url.startsWith("http://127.0.0.1:8765/analyze")) return res(200, FIXTURES["bridge_analyze.json"]);
  if (url.startsWith("data:application/json,")) return res(200, decodeURIComponent(url.slice("data:application/json,".length)));
  if (url.includes("/prediction-markets/nfl/events/")) return res(200, FIXTURES["rh_event_page.html"]);
  if (url.includes("/marketdata/event/contract/quotes/v1/")) return res(200, FIXTURES["rh_quotes.json"]);
  let m = /trade-api\/v2\/markets\/([A-Z0-9-]+)$/.exec(url);
  if (m) return FIXTURES["kalshi_market_" + m[1] + ".json"] ? res(200, FIXTURES["kalshi_market_" + m[1] + ".json"]) : res(404, "{}");
  m = /trade-api\/v2\/series\/([A-Z0-9]+)$/.exec(url);
  if (m) return res(200, FIXTURES["kalshi_series_" + m[1] + ".json"]);
  m = /gamma-api\.polymarket\.com\/markets\?slug=([a-z0-9-]+)$/.exec(url);
  if (m) return res(200, FIXTURES["pm_market_" + m[1] + ".json"] || "[]");
  return res(404, "not stubbed: " + url);
};
globalThis.chrome = {
  runtime: { id: "testextensionid", getURL: (p) => "data:application/json," + encodeURIComponent(FIXTURES["nfl_teams.json"]), onMessage: { addListener() {} }, onInstalled: { addListener() {} }, onStartup: { addListener() {} } },
  storage: { sync: { get: async (d) => Object.assign({}, d, __stored) } },
  declarativeNetRequest: { updateDynamicRules: async () => {} },
};
