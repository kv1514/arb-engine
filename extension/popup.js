const DEFAULTS = { gold: false, contracts: 100, targetMargin: 0, kalshiRounding: "cent", refreshSeconds: 15, venues: { kalshi: true, polymarket: true }, bridge: "auto" };
const $ = (id) => document.getElementById(id);
chrome.storage.sync.get(DEFAULTS, (s) => {
  s = Object.assign({}, DEFAULTS, s);
  $("gold").checked = !!s.gold;
  $("contracts").value = s.contracts;
  $("targetMargin").value = s.targetMargin;
  $("refreshSeconds").value = s.refreshSeconds;
  $("kalshiRounding").value = s.kalshiRounding;
  $("bridge").value = s.bridge || "auto";
  $("venue_kalshi").checked = s.venues.kalshi !== false;
  $("venue_polymarket").checked = s.venues.polymarket !== false;
});
$("save").addEventListener("click", () => {
  const s = { gold: $("gold").checked, contracts: Math.max(1, Number($("contracts").value) || 100), targetMargin: Math.max(0, Number($("targetMargin").value) || 0), refreshSeconds: Math.max(5, Number($("refreshSeconds").value) || 15), kalshiRounding: $("kalshiRounding").value, bridge: $("bridge").value, venues: { kalshi: $("venue_kalshi").checked, polymarket: $("venue_polymarket").checked } };
  chrome.storage.sync.set(s, () => { $("status").textContent = "saved"; setTimeout(() => ($("status").textContent = ""), 1500); });
});
