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
* Series used: Kalshi `KXNFLGAME`; Polymarket tag `nfl` / slug `nfl-{away}-{home}-{date}`;
  Robinhood category `nfl`. Spreads/totals (`KXNFLSPREAD`, `KXNFLTOTAL`, Polymarket
  `spreads`/`totals`, Rothera `NFLSPREAD`/`NFLTOTAL`) are the next step — they need exact
  line matching (`line` field) which the data model already carries.

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
