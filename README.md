# arb-engine

Fee-aware arbitrage and fair-value engine for **sports prediction markets**, plus a Chrome
overlay for Robinhood's event contracts. Focus: **NFL** (moneylines, spreads and totals —
every line) and **tennis** (match winners).

Venues today: **Kalshi** (direct API, public + signed), **Polymarket** (Gamma/CLOB public
data), **Robinhood** event contracts (public web/API data; contracts route to **Rothera**,
KalshiEX, ForecastEX or Nadex). Every price is compared *after* each venue's real fee
schedule (`docs/VENUES.md`), because a 1¢ gross gap on a 50¢ contract is a loss once
Kalshi's 1.75¢ taker fee or Robinhood's 2¢ is paid.

```
$ python -m arb_engine scan --sport nfl --cross-only --books
# NFL scan  venues=kalshi,polymarket,robinhood  events=1038 {'total': 518, 'spread': 488, 'moneyline': 32} (live/in-play: 0)  arbs(margin>0.00%, pre-game only)=16

[total] SEA @ ARI total 63.5 (over / under)  [nfl:ARI|SEA:2026-09-20:total:63.5]  venues=kalshi,robinhood
  sum-of-asks=0.960  fee-adjusted margin=  2.1% per $1 payout  | fillable: 450 contracts -> profit $2.85 (0.64% on $447.15)  flags=depth-checked
  Over 63.5              fair=0.064  best=robinhood  all-in=0.054  edge=  1.1%
      robinhood/rothera    ask=0.040 bid=0.030 fee/ct=0.014 all-in=0.054 max-buy(taker)=0.050 max-buy(maker)=0.050 size=5000.0
      kalshi               ask=0.090 bid=0.080 fee/ct=0.006 all-in=0.096 max-buy(taker)=0.070 max-buy(maker)=0.070 size=5020.0
  Under 63.5             fair=0.936  best=kalshi     all-in=0.925  edge=  1.0%
      kalshi               ask=0.920 bid=0.910 fee/ct=0.005 all-in=0.925 max-buy(taker)=0.940 max-buy(maker)=0.940 size=200.0
      robinhood/rothera    ask=0.970 bid=0.960 fee/ct=0.013 all-in=0.983 max-buy(taker)=0.920 max-buy(maker)=0.920 size=500.0
  legs@100: robinhood:Over 63.5@0.04(fee 1.39) + kalshi:Under 63.5@0.92(fee 0.52)  => cost $97.91 for $100 payout, profit $2.09
```

Where the arbs live today: the far tails of totals and spreads, where the quadratic fee is
smallest and Rothera's market maker quotes differently from Kalshi's. They are small
(≈1% on capital, a few dollars each at the available depth) but real and repeatable; the
moneylines are efficient to within fees.

`max-buy` is the number the overlay is built around: the highest price you can pay for this
outcome **on this venue** such that buying the other outcome at its cheapest current ask
elsewhere still locks in your target margin after all fees — as an order that crosses the
book (taker) or as a resting order (maker, lower fees on Kalshi, none on Polymarket).

## What is in the box

| Piece | What it does |
|---|---|
| `arb_engine/fees` | Exact (`Decimal`) fee models: Kalshi taker/maker with series multipliers, Robinhood commission (Gold/no Gold) + exchange fee, Polymarket sports schedule, Polymarket US theta. Tested against the venues' own tables. |
| `arb_engine/quant` | Odds conversion & de-vig (multiplicative / power / Shin), fee-aware arb evaluation, max-buy price on the tick grid, depth-limited sizing, Kelly, consensus fair value. |
| `arb_engine/venues` | Adapters for Kalshi, Polymarket, Robinhood — moneylines, and every NFL spread/total line (one binary market per line on all three venues); Kalshi RSA-PSS signing for portfolio/orders. |
| `arb_engine/matching` | Canonical NFL team codes (every venue's spelling), tennis surname keys, Eastern-date event keys with market type + line (`nfl:BUF|DET:2026-09-17:spread:BUF-1.5`), cross-venue merge. |
| `arb_engine/scanner.py` | Sport-wide scan: merge → fees → arbs/edges/max-buy; an arb must be **fillable** (≥ 1 contract at quoted depth); `--books` runs a second pass with real order books for candidate events and reports the profit-maximising size. Flags live matches, stale snapshots, thin quotes and same-book quotes. |
| `arb_engine/eventlookup.py`, `bridge.py` | One Robinhood event across venues; local HTTP server for the overlay. |
| `arb_engine/execution` | Kalshi order plans + executor with three safety gates (dry-run → demo → prod needs `ARB_LIVE_TRADING=1`). |
| `arb_engine/strategy` | **Maker runner**: rests post-only Kalshi orders at the price where a fill *creates* an arb against the cheapest hedge elsewhere, re-prices/cancels as the hedge moves, and fires a HEDGE-NOW alert (bell, macOS notification, webhook, JSONL journal) with the exact hedge instruction when a fill lands. Paper broker (simulated fills from live prices), demo and live Kalshi brokers. |
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
python -m arb_engine scan --sport nfl --cross-only    # every moneyline/spread/total line, pre-game fillable arbs
python -m arb_engine scan --sport nfl --cross-only --books --json out/nfl.json   # + real depth for candidates
python -m arb_engine scan --sport nfl --markets moneyline --all --limit 20
python -m arb_engine scan --sport tennis --cross-only --all --limit 10
python -m arb_engine quote --sport nfl "BUF|DET" --markets moneyline   # one game, every venue
python -m arb_engine rh-event "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-philadelphia-vs-tennessee-sep-20-2026/"
python -m arb_engine rh-event "https://robinhood.com/us/en/prediction-markets/nfl/events/september-20-carolina-vs-atlanta-totals-sep-20-2026/" --all   # every line of a spread/total page
python -m arb_engine fees --venue robinhood --price 0.52 --contracts 100 --gold
python -m arb_engine kelly --bankroll 2000 --fair 0.58 --cost 0.53
python -m arb_engine bridge                           # serves the overlay on 127.0.0.1:8765
```

### Maker runner

```bash
python -m arb_engine maker --sport nfl --markets total,spread --mode paper --min-margin 0.005 --duration 3600
python -m arb_engine maker --sport nfl --mode demo --confirm --size 50 --max-orders 5 --max-notional 300   # real orders on Kalshi's demo exchange
KALSHI_ENV=prod ARB_LIVE_TRADING=1 python -m arb_engine maker --sport nfl --mode live --confirm --size 20 --max-notional 200
```

What it does every `--interval` seconds: refresh Kalshi (batched) and hedge quotes for the
watchlist, cancel/re-price resting orders whose margin-if-filled decayed, poll fills, and
alert `HEDGE NOW: buy N x <other side> on <venue> at ≤ <price>`; every `--rescan` seconds it
re-runs the cross-venue scan. The price it rests at is
`min(max_buy_maker(hedge ask), Kalshi ask − 1 tick)`, only when the margin-if-filled clears
`--min-margin` and (by default) the price is at or above Kalshi's best bid — resting behind
the bid rarely fills; `--deep-queue` allows it. Limits: `--max-orders`, `--max-notional`,
`--max-per-event`. Ctrl-C cancels everything resting. The hedge leg is manual (Robinhood
has no API; Polymarket needs a wallet), so keep sizes at what you can hedge by hand within
a minute.

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
   fee per contract, all-in cost, and max-buy prices; contract tabs get a badge. On a
   game's **Spread** or **Totals** page it lists every line (arbs first) with your all-in
   cost, the max price to pay here, the cheapest hedge for the other side and the margin.
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
  size; "fillable" means every leg has ≥ 1 contract at the quoted size, and `--books` shows
  the profit-maximising size against real order books.
* **max-buy (taker / maker)** = the highest price to pay here so the round trip still locks
  `--target-margin` after fees, given the other outcome's cheapest ask elsewhere.
* Flags: `live` (in play — gaps are staleness, excluded by default), `same book as …`
  (Robinhood re-selling Kalshi; never arbed against Kalshi), `thin` (positive margin but
  not fillable), `depth-checked` (real books used), `tie-rule-unverified` (Rothera NFL
  moneyline), `stale-quote` (snapshot older than `--max-quote-age`). Spread/total lines are
  half-points on all three venues, so they cannot push.

## Status (2026-09-18)

Live data verified for all three venues; 80 Python tests + 2 JS suites (2,160 fee parity
vectors, background-worker integration incl. a totals page) pass. NFL moneylines are
efficient to within fees; on Tuesday night the ~1,000 spread/total lines held 16 fillable,
depth-checked arbs (Rothera far-tail overs vs Kalshi unders, ≈1% on capital) that were gone
by Friday — the maker runner exists to sit at the prices where they reappear. Tennis on
Robinhood is Kalshi's book re-sold, so the useful answer there is fee routing. See
`docs/ROADMAP.md`.

Not investment advice. Prediction-market contracts can lose their full cost; rule
differences (ties, retirements, postponements) can break a "hedge". Verify every fee and
rule on the venue before trading size.
