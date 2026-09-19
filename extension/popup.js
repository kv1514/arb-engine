const DEFAULTS = { gold: false, contracts: 100, targetMargin: 0, kalshiRounding: "cent", refreshSeconds: 1, venues: { kalshi: true, polymarket: true }, bridge: "auto", positions: "", bankroll: 0, kelly: 0.25 };
const $ = (id) => document.getElementById(id);
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
});
$("save").addEventListener("click", () => {
  const s = { gold: $("gold").checked, contracts: Math.max(1, Number($("contracts").value) || 100), targetMargin: Math.max(0, Number($("targetMargin").value) || 0), refreshSeconds: Math.max(1, Number($("refreshSeconds").value) || 1), bankroll: Math.max(0, Number($("bankroll").value) || 0), kelly: Number($("kelly").value) || 0.25, kalshiRounding: $("kalshiRounding").value, bridge: $("bridge").value, positions: $("positions").value, venues: { kalshi: $("venue_kalshi").checked, polymarket: $("venue_polymarket").checked } };
  chrome.storage.sync.set(s, () => { $("status").textContent = "saved"; setTimeout(() => ($("status").textContent = ""), 1500); });
});
