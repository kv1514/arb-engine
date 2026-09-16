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

## Other sports already supported by the adapters

NCAAF (`KXNCAAFGAME`, Robinhood `college-football`), NBA, NHL, MLB (`KXMLBGAME` has a 0.5 fee
multiplier) can be scanned with `--sport`. Team alias tables exist only for the NFL so far;
the other leagues match on the venue's own short codes, which works when both venues use the
same codes.
