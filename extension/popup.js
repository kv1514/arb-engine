const DEFAULTS = { gold: false, contracts: 100, targetMargin: 0, kalshiRounding: "cent", refreshSeconds: 1, venues: { kalshi: true, polymarket: true }, executable: { polymarket: false }, bridge: "auto", positions: "", bankroll: 0, kelly: 0.25 };
const $ = (id) => document.getElementById(id);
const POSITION_RE = /^[a-z]+:[^:\s]+:(0?\.\d+|1(\.0+)?):\d+(\.\d+)?(:[A-Za-z0-9.]+)?$/;  // Lot.parse: venue:outcome:price:count[:exchange|fee_multiplier]
chrome.storage.sync.get(DEFAULTS, (s) => {
  s = Object.assign({}, DEFAULTS, s);
  $("gold").checked = !!s.gold;
  $("contracts").value = s.contracts;
  $("targetMargin").value = s.targetMargin;
  $("refreshSeconds").value = s.refreshSeconds;
  $("bankroll").value = s.bankroll || 0;
  $("kelly").value = String(s.kelly || 0.25);
  $("kalshiRounding").value = s.kalshiRounding;
  $("bridge").value = s.bridge || "auto";
  $("positions").value = s.positions || "";
  $("venue_kalshi").checked = s.venues.kalshi !== false;
  $("venue_polymarket").checked = s.venues.polymarket !== false;
  $("exec_polymarket").checked = !!(s.executable && s.executable.polymarket);
});

// Validate before saving: a bad number used to be silently replaced by the default, so a typo
// in "target margin" (0.5 for 0.005) or a malformed lot line went live unnoticed.
function validate() {
  const errs = [];
  const num = (id, lo, hi, integer) => {
    const raw = String($(id).value).trim(), x = Number(raw);
    const bad = raw === "" || !Number.isFinite(x) || x < lo || (hi != null && x > hi) || (integer && !Number.isInteger(x));
    $(id).parentElement.classList.toggle("invalid", bad);
    if (bad) errs.push(`${id}: ${raw === "" ? "required" : integer ? `whole number ≥ ${lo}` : hi != null ? `number in [${lo}, ${hi}]` : `number ≥ ${lo}`}`);
    return x;
  };
  const contracts = num("contracts", 1, null, true), targetMargin = num("targetMargin", 0, 0.5), refreshSeconds = num("refreshSeconds", 1, 3600, true), bankroll = num("bankroll", 0);
  const lines = String($("positions").value).split(/\n+/).map((x) => x.trim()).filter(Boolean);
  const badLots = lines.filter((x) => !POSITION_RE.test(x));
  $("positions").parentElement.classList.toggle("invalid", badLots.length > 0);
  if (badLots.length) errs.push("positions: expected venue:outcome:price:count, got " + badLots.map((x) => JSON.stringify(x)).join(", "));
  return { errs, contracts, targetMargin, refreshSeconds, bankroll, positions: lines.join("\n") };
}
$("save").addEventListener("click", () => {
  const v = validate();
  $("errors").textContent = v.errs.join("\n");
  if (v.errs.length) { $("status").textContent = "not saved"; return; }
  const s = { gold: $("gold").checked, contracts: v.contracts, targetMargin: v.targetMargin, refreshSeconds: v.refreshSeconds, bankroll: v.bankroll, kelly: Number($("kelly").value) || 0.25, kalshiRounding: $("kalshiRounding").value, bridge: $("bridge").value, positions: v.positions, venues: { kalshi: $("venue_kalshi").checked, polymarket: $("venue_polymarket").checked }, executable: { polymarket: $("exec_polymarket").checked } };
  chrome.storage.sync.set(s, () => { $("status").textContent = "saved"; setTimeout(() => ($("status").textContent = ""), 1500); renderHealth(null, s.bridge); });
});

// Bridge health card: the worker probes /health now and reports the live mode plus the venue
// set the bridge says is executable (from /health when it carries one, else the last /inplay
// view; "unknown" until either has been seen — never guessed from the popup's own checkbox).
function renderHealth(h, bridgeSetting) {
  const el = $("health");
  if (h === null) { el.className = "health muted"; el.textContent = "bridge: checking…"; return probeHealth(); }
  const setting = bridgeSetting || (h && h.setting) || $("bridge").value;
  if (!h || !h.ok) { el.className = "health down"; el.textContent = "bridge: could not ask the worker" + (h && h.error ? " (" + h.error + ")" : ""); return; }
  const venues = Array.isArray(h.executableVenues) && h.executableVenues.length ? h.executableVenues.join(", ") : "unknown until a game page is open";
  if (setting === "off") { el.className = "health off"; el.textContent = "bridge: never (direct mode) · executable venues per the checkboxes below"; return; }
  if (h.up) { el.className = "health up"; el.textContent = `bridge: up (${h.service || "arb-engine bridge"}) · mode: engine · executable venues: ${venues}`; return; }
  el.className = "health down";
  el.textContent = `bridge: down${h.lastError ? " — " + h.lastError : ""} · mode: ${setting === "on" ? "none (required, waiting)" : "direct"}${h.retryInMs ? ` · retry in ${Math.ceil(h.retryInMs / 1000)} s` : ""} · run: python -m arb_engine bridge`;
}
function probeHealth() {
  try {
    chrome.runtime.sendMessage({ type: "bridgeHealth" }, (h) => {
      if (chrome.runtime.lastError) { renderHealth({ ok: false, error: chrome.runtime.lastError.message }); return; }
      renderHealth(h || { ok: false, error: "no response" });
    });
  } catch (e) { renderHealth({ ok: false, error: e.message || String(e) }); }
}
renderHealth(null);
