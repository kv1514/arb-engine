# Installing and debugging the Chrome overlay

The overlay is a Manifest V3 extension in the folder **`extension/`** (inside this repo).
It is read-only: it never places orders and reads nothing from your account.

Before installing, run the self-check (it catches every load-time problem we know how to
detect: bad manifest, missing files, bad permission names, JS syntax errors, BOMs):

```bash
python3 scripts/check_extension.py        # prints PASS or FAIL with reasons
bash scripts/package_extension.sh         # optional: dist/arb-engine-extension.zip
```

## Install on macOS Chrome (Load unpacked)

1. Open Chrome and go to `chrome://extensions`.
2. Turn on **Developer mode** (toggle, top-right corner).
3. Click **Load unpacked** (top-left).
4. In the file picker, navigate **into** the repo and select the folder named
   **`extension`** — e.g. `~/Documents/GitHub/arb-engine/extension`. Click **Select**.
   Do **not** select the repo root (`arb-engine/`): it has no `manifest.json`, and Chrome
   will say the manifest is missing. Do not select a single file either.
5. The card "Arb Engine Robinhood Overlay" appears. If it shows a red **Errors** button,
   click it and see the table below.
6. Open a game page under `robinhood.com/us/en/prediction-markets/…/events/…`. The panel
   appears bottom-right; the toolbar popup holds the settings.

If you use the zip from `scripts/package_extension.sh`, unzip it first and load the
unzipped folder (Chrome does not load `.zip` files via Load unpacked).

After editing any file under `extension/`, click the circular **reload** arrow on the
extension card (or remove and load again).

## "not a Robinhood prediction-market event URL" (or any error the CLI does not show)

The bridge loads the engine once at start. If the panel shows an error that
`python3 -m arb_engine rh-event <same url>` does not, the bridge process is older than the code —
stop it (Ctrl-C) and start it again, then press ↻ in the panel. `curl http://127.0.0.1:8765/health`
tells you it is up; `curl 'http://127.0.0.1:8765/analyze?url=<url>'` shows exactly what the
overlay receives.

## Chrome error strings and what they mean

| Chrome says | Meaning | Fix |
|---|---|---|
| **Manifest file is missing or unreadable** | The folder you picked has no `manifest.json` at its top level, or Chrome cannot read it. | Pick `…/arb-engine/extension`, not the repo root. On macOS also check that Chrome is allowed to read the folder: **System Settings → Privacy & Security → Files and Folders → Google Chrome → Documents Folder** (or move the repo out of `Documents`/`Desktop`/`Downloads`, or iCloud-synced folders where the files may be placeholders that are not downloaded). |
| **Failed to load extension** … **Manifest is not valid JSON** | `manifest.json` has a syntax error or a BOM. | `python3 scripts/check_extension.py` names the line. |
| **Invalid value for 'permissions[N]'** / **Permission 'x' is unknown or URL pattern is malformed** | A permission name is not one Chrome knows, or a host pattern is in `permissions` instead of `host_permissions`. | Run the checker; it validates names against the MV3 list. |
| **Invalid value for 'web_accessible_resources'** | MV2 shape (plain string list) instead of MV3 objects. | The repo manifest uses `[{resources, matches}]`. |
| **Could not load javascript 'content.js' for content script** / **Could not load background script** | A file named in the manifest is missing from the folder. | Checker reports "does not exist". Make sure you copied the whole folder. |
| **Service worker registration failed. Status code: 15** (or 3) | `background.js` threw while starting (syntax error, or a `chrome.*` API missing in your Chrome version). | Click **Errors** on the card, or **Inspect views → service worker**. Run `bash scripts/test_js.sh`; it parses and runs `background.js`. |
| **This extension requires Chrome version 101 or greater** | Old Chrome. | Update Chrome (`chrome://settings/help`). |
| **Extensions cannot be loaded in developer mode** / Developer mode toggle greyed out / "managed by your organization" | An enterprise or school policy blocks unpacked extensions. | Check `chrome://policy` for `ExtensionSettings` (a `*` entry with `installation_mode: blocked` or `allowed_types`), `ExtensionInstallBlocklist` (`*`), `ExtensionInstallForcelist`, `DeveloperToolsAvailability` (=2 also blocks unpacked loads). Use a personal Chrome profile / non-managed Chrome. |
| **Unrecognized manifest key** (yellow warning, not an error) | Harmless; the extension still loads. | Ignore. |
| **Extension is not installed because it is not from the Chrome Web Store** (on a later restart) | Some Chrome builds disable unpacked extensions after a restart for non-developer profiles. | Reopen `chrome://extensions`, Developer mode on, click reload on the card. |

### Other Chromium browsers

- **Brave**: same steps at `brave://extensions`. Brave Shields may block the calls to
  `api.robinhood.com`; the panel then shows a fetch error — turn Shields off for robinhood.com
  or run the bridge (below).
- **Arc**: `arc://extensions`, Developer mode, Load unpacked. Arc hides the toolbar popup
  behind the puzzle icon in the sidebar.
- **Edge**: `edge://extensions`, "Developer mode" toggle is on the left, then **Load unpacked**.
- **Chrome Canary / Beta / Chromium** all work; the extension needs Chrome ≥ 101 (MV3
  service workers + `declarativeNetRequest` `initiatorDomains`).
- Safari and Firefox are not supported (no MV3 `declarativeNetRequestWithHostAccess`
  with the same semantics).

## Seeing console errors

- **Service worker (`background.js`)**: `chrome://extensions` → the extension card →
  **Inspect views: service worker** (if it says "inactive", open a Robinhood event page
  first; the worker wakes on the first message). Its DevTools console shows fetch errors
  such as Kalshi 403s. The red **Errors** button on the card collects uncaught errors.
- **Content script (`content.js`)**: on the Robinhood page press **⌥⌘I** (View → Developer
  → JavaScript Console). Messages from the overlay are prefixed `arb-engine`. In the
  console's context dropdown (top-left, "top") you can pick the extension's context.
- **Popup (`popup.js`)**: right-click the toolbar icon → **Inspect popup**.
- **Network**: in the service-worker DevTools, the Network tab shows every venue request
  (Robinhood quotes, Kalshi markets, Polymarket gamma, and `127.0.0.1:8765` when the bridge
  is on).

## Running with the bridge (recommended)

The extension can hand the modelling to the Python engine over a local HTTP server:

```bash
cd arb-engine
export ARB_HTTP_TRANSPORT=curl        # only needed on networks that truncate chunked HTTP
python3 -m arb_engine bridge          # listens on http://127.0.0.1:8765
```

Then in the toolbar popup set **Local engine bridge** to `auto` (default; used when the
health check succeeds) or `required`. The panel status line shows `bridge` as the source.
Stop the bridge with Ctrl-C; the extension falls back to direct mode (`auto`) within 30 s.

Direct mode (no bridge) calls the venue APIs from the service worker. Kalshi answers 403 to
browser Origins, so the worker installs a `declarativeNetRequest` rule that strips the
`Origin` header on its own Kalshi requests; if Kalshi rows still show an error, run the
bridge.

## Files

| File | Role |
|---|---|
| `manifest.json` | MV3 manifest: `storage` + `declarativeNetRequestWithHostAccess`, host permissions for the venues and `127.0.0.1:8765`. |
| `background.js` | Service worker: fetches Robinhood/Kalshi/Polymarket or the bridge, runs the math. `importScripts("arb-core.js")`. |
| `arb-core.js` | JS twin of `arb_engine/fees` + `quant` (BigInt money math; parity-tested against `tests/fixtures/fee_vectors.json`). |
| `content.js`, `content.css` | Overlay panel and badges on robinhood.com event pages. |
| `popup.html`, `popup.js`, `popup.css` | Settings (Gold, size, target margin, refresh, bridge, venues, "I can trade on Polymarket", positions, bankroll, Kelly fraction). |
| `nfl_teams.json` | Team code mapping, exposed via `web_accessible_resources`. |


## Which Robinhood pages the overlay works on

| URL | What it is | Overlay |
|---|---|---|
| `robinhood.com/us/en/prediction-markets/<category>/events/<slug>/` | public (logged-out) event page, server-rendered | panel + contract-tab badges |
| `robinhood.com/events/<slug>?contract=<id>` | logged-in trading page with the order ticket ("Trade the winner", Buy Yes/No, Review order) | panel (the page is client-rendered; the extension reads the public page for the same slug) |
| `robinhood.com/prediction-markets/` | logged-in hub | none yet |

The logged-in event page also lists the game's Spread and Totals rows and props on one page;
the overlay analyses the market the URL's slug names (the game winner) — open the Spread /
Totals event pages for line-by-line analysis.

### Category pages

`robinhood.com/us/en/prediction-markets/<category>/` (and the app route without `/us/en`)
list one card per event with a price button per contract (`PHI - 77¢`). The content script
collects the moneyline cards (slugs with `-vs-` and no `spread`/`totals`/`points`), sends up
to 16 event URLs to the worker (`analyzeMany`, concurrency 3, per-URL cache 20 s) and
appends a badge to each price button: `fair 75.0¢ · max 72.0¢` (green when the edge vs
consensus is positive) plus `ARB +x%` when the cheapest legs across venues sum below $1
after fees. Spread/total cards are skipped (open the game for the line table). The panel
shows the count analysed and the best edge. Rescans every `max(refresh, 20)` seconds.

### College football pages

Robinhood's college-football game winners are CDNA-routed (`NX.F.OPT.CFB-…` symbols): the
symbol carries no team codes, so the worker cannot derive the Kalshi ticker or the Polymarket
slug on its own. With the bridge running (`python -m arb_engine bridge`, popup bridge = auto/on)
the Python engine resolves the teams through its 761-program table and the panel, tab badges
and category-page badges work exactly as for the NFL; in direct mode the panel says so instead
of showing nothing. CDNA quotes are priced with the $0.01/contract exchange fee (assumed).

## Which venues can be a leg: the `signal only` tag

Global Polymarket is not executable for a US-resident account (`arb_engine/data/venue_rules.json`,
`docs/VENUES.md` "Eligibility"), so by default the overlay treats it as a **signal**: its ask
still enters the consensus fair value, but it is never chosen as an arb leg, never gets a
max-buy price, and never becomes the "best" venue of a side. Such rows are dimmed and carry a
`signal only` tag (hover: "priced into the fair value, never an arb leg"). This holds in both
modes — the bridge resolves eligibility exactly like `python -m arb_engine scan`, and direct
mode applies the same rule in `background.js`. If your account really can trade there, tick
**"I can trade on Polymarket"** in the popup (direct mode) or run the bridge with
`EXECUTABLE_VENUES=kalshi,robinhood,polymarket`; the tag disappears and the arb math includes it.

**LAG boxes.** Below the arb line the panel shows a **LAG** box whenever one venue has
repriced ≥ 5¢ in the last 30 s and an executable venue has not followed (the bridge runs
`strategy/leadlag.py` on every 1 s poll): "robinhood moved +8¢, kalshi has not: buy Kansas
City on kalshi at 0.60 vs robinhood mid 0.675 — edge +5.8% → 208 contracts (300 offered)".
Sunday's replay put the laggard's catch-up at ~80 % within a minute (docs/MODEL.md, "The
first live Sunday"); act within ~20 s, and treat a leader that snaps back as a bad print.
The bankroll / Kelly fraction in the popup size the suggestion; without a bankroll only the
depth is shown.

**The LIVE strip is feed-gated the same way the CLI is.** The in-play feed gates
(`docs/ARCHITECTURE.md`, "Gates": `feed-stale`, `clock-frozen`, `quote-old:<venue>`,
`score-pending`, `suspect`, `review-pending`) need poll-to-poll memory of when the ESPN state
and each venue's mid last moved; the bridge keeps one `FeedFreshness` per event across
`GET /inplay` requests (the overlay polls every second, so the gates see the same cadence as
`live`). A side whose STEAL or LOCK NOW would have fired but for the gates shows a
`GATED · wait` tag (hover for the reasons) instead of an actionable `NOW`, and the engine's
`GATED … wait: <reasons>` line is echoed under the strip; a STEAL whose best venue is not
executable for you carries a `signal only` tag and no buy count. The bridge keeps no JSONL
journal — run `python -m arb_engine live --journal` alongside if you want the gate history on
disk. The `LOCK ≤ price` hint is a
guarantee, not advice: the engine's LOCK NOW line also carries what holding is worth at fair,
because in the NFL week-1 replay the break-even lock lost at every edge and on college week 2
it came out ahead only at the 10 % edge (`docs/MODEL.md`).

## Refresh rate and sizing

The content script ticks every 500 ms and re-analyses when the last result is older than the
popup's "Refresh every" setting (default 1 s); a new request is never started while one is in
flight, so a slow venue only delays the next update. At 1 s the bridge re-pulls the Robinhood
quotes API, two Kalshi markets, one Polymarket market and the ESPN scoreboard each tick (the
ESPN summary every 30 s; the 1-3 MB Robinhood event page is cached for 60 s). That is ~5
requests/s per open tab — keep one game page open at a time to stay under Kalshi's ~10 req/s.

Sizing: the panel's ARB line shows `Buy N contracts` — the depth-limited size from
`size_from_books` (top-of-book sizes on Robinhood and Kalshi; Polymarket sizes when the book was
fetched), stepped to each venue's tick and floored at its minimum order size (a leg below the
venue minimum is flagged `below-min-size` instead of sized) — with the per-leg contract counts and
the locked profit after fees. STEAL alerts include `→ buy N contracts` when a bankroll is set in
the popup: fractional Kelly (default ¼) on the fee-inclusive edge `(fair − all-in) / (1 − all-in)`,
capped by the contracts offered at that ask; the slate-wide cap in `live` scales every STEAL's
stake on a tick so their sum stays under `--slate-cap`. Nothing is placed; the counts are what the
engine would do at those exact prices — and the replays say the STEAL edge itself is not yet
demonstrated on the NFL (`docs/MODEL.md`), so treat the count as a ceiling, not a recommendation.

