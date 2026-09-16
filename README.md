# arb-engine

Fee-aware arbitrage and fair-value engine for **sports prediction markets**, plus a Chrome
overlay for Robinhood's event contracts. Focus: **NFL** (in season) and **tennis**.

Venues today: **Kalshi** (direct API, public + signed), **Polymarket** (Gamma/CLOB public
data), **Robinhood** event contracts (public web/API data; contracts route to **Rothera**,
KalshiEX, ForecastEX or Nadex). Every price is compared *after* each venue's real fee
schedule (`docs/VENUES.md`), because a 1¢ gross gap on a 50¢ contract is a loss once
Kalshi's 1.75¢ taker fee or Robinhood's 2¢ is paid.

```
$ python -m arb_engine scan --sport nfl
# NFL scan  venues=kalshi,polymarket,robinhood  events=91 (live/in-play: 0)  arbs(margin>0.00%, pre-game only)=0

Buffalo vs Detroit  [nfl:BUF|DET:2026-09-17]  start=2026-09-18T00:15:00+00:00  venues=kalshi,polymarket,robinhood
  sum-of-asks=1.000  fee-adjusted margin= -2.7% per $1 payout
  Buffalo                fair=0.670  best=polymarket all-in=0.681  edge= -1.1%
      polymarket           ask=0.670 bid=0.660 fee/ct=0.011 all-in=0.681 max-buy(taker)=0.640 max-buy(maker)=0.650
      kalshi               ask=0.680 bid=0.670 fee/ct=0.015 all-in=0.695 max-buy(taker)=0.630 max-buy(maker)=0.650
      robinhood/rothera    ask=0.690 bid=0.670 fee/ct=0.020 all-in=0.710 max-buy(taker)=0.630 max-buy(maker)=0.630
  Detroit                fair=0.330  best=kalshi     all-in=0.346  edge= -1.6%
      ...
```

`max-buy` is the number the overlay is built around: the highest price you can pay for this
outcome **on this venue** such that buying the other outcome at its cheapest current ask
elsewhere still locks in your target margin after all fees — as an order that crosses the
book (taker) or as a resting order (maker, lower fees on Kalshi, none on Polymarket).

## What is in the box

| Piece | What it does |
|---|---|
| `arb_engine/fees` | Exact (`Decimal`) fee models: Kalshi taker/maker with series multipliers, Robinhood commission (Gold/no Gold) + exchange fee, Polymarket sports schedule, Polymarket US theta. Tested against the venues' own tables. |
| `arb_engine/quant` | Odds conversion & de-vig (multiplicative / power / Shin), fee-aware arb evaluation, max-buy price on the tick grid, depth-limited sizing, Kelly, consensus fair value. |
| `arb_engine/venues` | Adapters for Kalshi, Polymarket, Robinhood; Kalshi RSA-PSS signing for portfolio/orders. |
| `arb_engine/matching` | Canonical NFL team codes (every venue's spelling), tennis surname keys, Eastern-date event keys, cross-venue merge. |
| `arb_engine/scanner.py` | Sport-wide scan: merge → fees → arbs/edges/max-buy; flags live matches, stale snapshots and same-book quotes. |
| `arb_engine/eventlookup.py`, `bridge.py` | One Robinhood event across venues; local HTTP server for the overlay. |
| `arb_engine/execution` | Kalshi order plans + executor with three safety gates (dry-run → demo → prod needs `ARB_LIVE_TRADING=1`). |
| `extension/` | Chrome MV3 overlay for `robinhood.com/us/en/prediction-markets/…/events/…`: panel + badges with fair value, all-in cost, edge, max-buy; direct mode or via the bridge. |
| `.mcp.json` | Wires the [mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi) MCP server so Claude Code / Codex can browse and (with your keys) trade Kalshi. |

## Install

Python ≥ 3.10, no third-party packages needed for scanning.

```bash
git clone https://github.com/kv1514/arb-engine && cd arb-engine
pip install cryptography          # only for authenticated Kalshi calls
cp .env.example .env              # optional: Kalshi keys, Gold flag
python -m unittest discover -s tests -t .
```

## Use

```bash
python -m arb_engine scan --sport nfl                 # cross-venue scan, pre-game arbs only
python -m arb_engine scan --sport tennis --cross-only --all --limit 10
python -m arb_engine scan --sport nfl --books --json out/nfl.json   # order-book depth sizing
python -m arb_engine quote --sport nfl PHI            # one game, every venue
python -m arb_engine rh-event "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/"
python -m arb_engine fees --venue robinhood --price 0.52 --contracts 100 --gold
python -m arb_engine kelly --bankroll 2000 --fair 0.58 --cost 0.53
python -m arb_engine bridge                           # serves the overlay on 127.0.0.1:8765
```

Kalshi account (needs `KALSHI_API_KEY` + `KALSHI_PRIVATE_KEY_PATH`; demo environment by default):

```bash
python -m arb_engine kalshi balance
python -m arb_engine kalshi order --ticker KXNFLGAME-26SEP20PHITEN-PHI --side-action buy --side yes --count 10 --price 0.72 --post-only            # dry-run
python -m arb_engine kalshi order --ticker ... --confirm                                                                                        # submits (demo)
```

Real-money orders additionally require `KALSHI_ENV=prod` **and** `ARB_LIVE_TRADING=1`.

### Robinhood overlay

1. `chrome://extensions` → Developer mode → **Load unpacked** → select `extension/`.
2. Open any game/match page under `robinhood.com/us/en/prediction-markets/…/events/…`.
3. The panel (bottom-right) shows per outcome: consensus fair value, each venue's ask/bid,
   fee per contract, all-in cost, and max-buy prices; contract tabs get a badge.
4. Optional but recommended: run `python -m arb_engine bridge` — the extension detects it and
   lets the Python engine do the modelling (popup → "Local engine bridge").

Kalshi's API blocks browser origins; in direct mode the extension removes the `Origin`
header on its own requests with a `declarativeNetRequest` rule. If Kalshi rows show an
error, start the bridge.

The overlay is read-only. It never places orders and reads nothing from your account.

### Kalshi MCP server (Claude Code / Codex)

`.mcp.json` registers the [mcp-server-kalshi](https://github.com/9crusher/mcp-server-kalshi)
server (`pip install mcp-server-kalshi`, then it runs as
`python3 -c "from mcp_server_kalshi.server import main; main()"`) with `KALSHI_ENV`,
`KALSHI_API_KEY` and `KALSHI_PRIVATE_KEY_PATH` taken from your environment (defaults to the
demo exchange). Set the variables in your shell; Claude Code picks the server up from the
project and its tools cover markets, order books, rules PDFs, balance, positions and
(with `confirm=true`) orders. For Codex add the same command to `~/.codex/config.toml` under
`[mcp_servers.kalshi]`; if you use `uv`, `uvx mcp-server-kalshi` works too.

## What the numbers mean

* **all-in** = ask + fee per contract at the reference size (fees round up per order, so
  size matters at the extremes).
* **fair** = spread-weighted consensus of every venue's de-vigged mid; **edge** = fair −
  all-in at the cheapest venue. Positive edge is +EV, not risk-free.
* **margin** = 1 − Σ all-in of the cheapest leg per outcome. Positive = arbitrage at that
  size; `--books` shows the largest size that still clears it.
* **max-buy (taker / maker)** = the highest price to pay here so the round trip still locks
  `--target-margin` after fees, given the other outcome's cheapest ask elsewhere.
* Flags: `live` (in play — gaps are staleness, excluded by default), `same book as …`
  (Robinhood re-selling Kalshi; never arbed against Kalshi), `tie-rule-unverified`
  (Rothera NFL), `stale-quote` (snapshot older than `--max-quote-age`).

## Status (2026-09-15)

Live data verified for all three venues; 59 Python tests + 2 JS suites (2,160 fee parity
vectors, background-worker integration) pass. No pre-game NFL arb existed at build time
(sum-of-asks ≈ 1.00, fees 1–3%), which is the honest state of these markets; the edge is in
resting maker orders at the computed max-buy, fee routing (never buy tennis on Robinhood
when Kalshi has the same book cheaper), and transient dislocations the scanner is built to
catch. See `docs/ROADMAP.md`.

Not investment advice. Prediction-market contracts can lose their full cost; rule
differences (ties, retirements, postponements) can break a "hedge". Verify every fee and
rule on the venue before trading size.
