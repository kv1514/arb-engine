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
| `popup.html`, `popup.js`, `popup.css` | Settings (Gold, size, target margin, refresh, bridge, venues). |
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

