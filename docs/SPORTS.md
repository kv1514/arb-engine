# Sports: why NFL first, then tennis

## NFL (in season now)

* Every venue lists the same 16 games a week with two mutually exclusive outcomes, ties
  settle 50/50 on Kalshi and Polymarket (Rothera's public blurb does not say — the engine
  flags it), so a two-leg hedge is clean.
* **Robinhood's NFL game markets trade on Rothera, a separate order book** from Kalshi
  (symbols `NFLGAME-…`, `NFLSPREAD-…`, `NFLTOTAL-…`). That is the only place in the
  Robinhood catalogue where a real cross-venue price gap can exist against Kalshi.
* Kalshi books are very deep (millions of contracts resting on Thursday-night games), so
  the binding constraint is usually Rothera/Polymarket depth.
* Series used: Kalshi `KXNFLGAME` / `KXNFLSPREAD` / `KXNFLTOTAL`; Polymarket tag `nfl`, slugs
  `nfl-{away}-{home}-{utc date}` (+ `-spread-{home|away}-{L}pt5`, `-total-{L}pt5`);
  Robinhood category `nfl` (Rothera `NFLGAME-…`, `NFLSPREAD-…`, `NFLTOTAL-…`).
* **Spreads and totals are one binary market per line** on every venue ("Buffalo wins by
  over 1.5 points", "Over 49.5 points"): YES = cover/over, NO = the other side. A game has
  ~24 spread lines per team and ~45 totals lines, so the NFL week is ~1,000 events. The
  arbs found so far are all in the tails (totals 60+, spreads 14+), where the quadratic
  fee is tiny and Rothera's maker prices the tails differently from Kalshi's.
* Kalshi ticker suffix = ⌈line⌉ (`BUF2` = 1.5, `-50` = 49.5); Rothera suffix = ⌊line⌋
  (`BUF1` = 1.5, `-49` = 49.5); Polymarket encodes it in the slug. Always match on the
  numeric line (`floor_strike` / `floorStrikeValue` / `line`), never on the suffix.

## Tennis

* Highest event count of any sport (hundreds of matches a day across ATP/WTA/ITF/
  challengers), thin books on the smaller events, frequent walkovers/retirements — the
  classic environment for mispricing between venues.
* **Robinhood tennis is Kalshi's book re-sold** (all `KX*` symbols), so the overlay's
  useful output there is fee routing ("buy this on Kalshi directly") and the Polymarket
  comparison; Kalshi-vs-Polymarket is the real cross-venue pair.
* Retirement/walkover rules differ by venue and by tournament stage (Kalshi voids or
  settles per match rules; Polymarket has a separate "completed match" market). Treat
  tennis arbs as slightly riskier than NFL and prefer the pre-game window.
* Matching is by surname pair + Eastern date with one-day tolerance (order-of-play times
  drift); duplicate surnames fall back to full names.

### Settlement rules, verbatim from the venues (2026-09-18)

| Case | Kalshi (`KXATPMATCH` / `KXWTAMATCH` market `rules_primary` + `rules_secondary`; Robinhood tennis is this book) | Polymarket (market `description`) |
|---|---|---|
| Match completed | "If X wins the … match … **after a ball has been played**, then the market resolves to Yes." | "resolve to 'X' if X **advances** against Y" |
| Retirement / default / DQ after the first ball | X "wins" per the tour = the player who advances | "the player who advances" |
| Walkover / withdrawal / cancellation **before** the first ball | "the market will resolve to a **fair price** in accordance with the rules" (not a 50-50, not a refund at cost) | "resolve to **50-50**" |
| Postponed | "will remain open and close after the rescheduled match has finished (within two weeks)" | "delayed beyond 7 days … without a winner → 50-50" |

Retirements agree, so a Kalshi × Polymarket hedge survives the common case. **Walkovers and
cancellations do not**: Polymarket pays 50¢ a side while Kalshi settles at a "fair price" it
determines, so the pair is not a lock in that case. The adapters store each venue's rules in
`EventInfo.venues[venue]["settlement"]` (`TENNIS_SETTLEMENT` in `venues/kalshi.py` and
`venues/polymarket.py`) and the scanner adds `settlement-mismatch:<case>` flags whenever the
venues that hold quotes differ — `settlement-mismatch:walkover`, `:cancelled`, `:postponed` for
every Kalshi × Polymarket tennis pair. Kalshi and its Robinhood mirror never mismatch.


## Other sports already supported by the adapters

NCAAF (`KXNCAAFGAME`, Robinhood `college-football`), NBA, NHL, MLB (`KXMLBGAME` has a 0.5 fee
multiplier) can be scanned with `--sport`. Team alias tables exist only for the NFL so far;
the other leagues match on the venue's own short codes, which works when both venues use the
same codes.

### NBA and NHL (tables added 2026-09-18)

`arb_engine/data/nba_teams.json` (30) and `nhl_teams.json` (32) are built by
`scripts/build_teams.py --sport nba|nhl` from ESPN's team lists plus Kalshi ticker codes learned
from open `KXNBAGAME` / `KXNHLGAME` markets and a manual alias list (Kalshi says "Vegas",
"Utah", "Los Angeles"; ESPN says VGK, UTAH, LA/LAL/LAC). Canonical codes are ESPN's; an alias
two teams share resolves to nothing. `scan / games / live --sport nhl|nba` work through the same
adapters (Kalshi, Polymarket tags `nhl`/`nba`, Robinhood categories `nhl`/`nba`) and ESPN's
`hockey/nhl` / `basketball/nba` scoreboards; the win-probability model is football-only, so the
in-play blend for these sports is market + ESPN. First live NHL scan (preseason, 2026-09-18):
906 events (176 moneylines, 704 totals, 26 spreads) on Kalshi × Polymarket, keys matching
ESPN's (`nhl:MTL|TOR:2026-09-19`); NBA has six Kalshi markets for opening night so far.


## College football (`--sport ncaaf`, added 2026-09-18)

| Venue | Where | Codes / names | Notes |
|---|---|---|---|
| Kalshi | `KXNCAAFGAME` (moneyline), `KXNCAAFSPREAD`, `KXNCAAFTOTAL` — ~230 game markets on a Saturday | ticker codes (`SJSU`, `FRES`, `NW`…) — 84% equal ESPN's abbreviations, the rest learned from titles ("San Jose St. wins") | `occurrence_datetime` is **not** kickoff (06:00Z for a 03:00Z kick); the ET game date comes from the ticker (`26SEP19`). Maker fees apply. |
| Polymarket | Gamma `tag_slug=cfb`; event slug `cfb-{away}-{home}-{UTC date}` with Polymarket's own codes (`frest`, `sjst`, `oregst`) | outcomes are full names ("Fresno State", "San Jose State") | 150+ markets per event (props); moneyline `sportsMarketType`; `feeSchedule` rate 0 this week, 0.05 on later games |
| Robinhood | category `college-football` (1,540 events) | game winners are **CDNA-routed** (`NX.F.OPT.CFB-00027-260919-M.O.1.1.20270228`, `EXCHANGE_SOURCE_CDNA`, `mutuallyExclusive: false` but one two-contract `EVENT_TYPE_WINNER`); ~half the games are KalshiEX mirrors (`KXNCAAFGAME-…`); Rothera lists only futures (conference champions, Heisman) | CDNA is its own order book — the real cross-venue pair. Exchange fee assumed $0.01/contract (unverified). |
| ESPN | `…/football/college-football/scoreboard?groups=80&limit=300` (FBS week; 75 games) | abbreviations = our canonical codes | summaries carry the same situation / win-probability fields as the NFL |

Canonical codes are ESPN's abbreviations for 761 programs, in `arb_engine/data/ncaaf_teams.json`
(built by `scripts/build_ncaaf_teams.py`: ESPN team list + Kalshi codes learned from open
markets + Robinhood short names from the cached catalogue + a manual alias list). Matching is
exact on code or normalised alias ("St." → "State"), never substring — "Miami" vs "Miami (OH)"
and "Washington" vs "Washington State" must not collide. Event keys are
`ncaaf:<A>|<B>:<ET date>`; on 2026-09-18 a full scan merged 263 games (109 on all three
venues, 100 Kalshi + Robinhood-mirror), and `live --sport ncaaf` matched 65 ESPN games to venue
quotes with none missing.

In-play: the NFL win-probability model is used as an approximation (same clock, different OT,
pace and variance; spreads above ±17 are outside its training range), so the college blend
keeps the market as the anchor (`SPORT_WEIGHTS["ncaaf"]` = market 0.50 / model 0.35 / ESPN 0.15)
until a college replay says otherwise. College spreads/totals: Kalshi `KXNCAAFSPREAD`/`KXNCAAFTOTAL` (≈1,000 open markets
each, same ⌈line⌉ ticker suffix as the NFL) and Robinhood's CDNA line events
(`EVENT_TYPE_SPREAD` / `EVENT_TYPE_TOTALS`, ~45 contracts per game named "Oregon -93.5 points" /
"Over 44.5 points", one contract per line, favourite named on the contract) are ingested;
a scan on 2026-09-18 with all three venues produced 10,525 line events, 1,384 of them on
Kalshi, Polymarket and CDNA together, nine positive-margin spreads and two fillable ones
(≈1 % on capital, Polymarket × CDNA tails) — the same far-tail pattern as the NFL. Polymarket's college spreads/totals are ingested too (teams taken
from the event's moneyline outcomes because the slug codes are Polymarket's own).

