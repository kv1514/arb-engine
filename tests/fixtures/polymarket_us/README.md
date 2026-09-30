# Polymarket US public API fixtures

`events_nfl.json` and `book_moneyline.json` are **schema-derived offline fixtures**, not
captured live responses or empirical arbitrage results. They follow the official NFL
league-events and market-book schemas, read 2026-09-30:

- https://docs.polymarket.us/api-reference/sports/get-events-by-league-slug
- https://docs.polymarket.us/api-reference/markets/get-market-book
- https://docs.polymarket.us/api-reference/orders/overview
- https://docs.polymarket.us/fees

The teams/kickoff exercise Eastern-date matching; fabricated prices, fractional depth,
reversed side order, stale metadata prices and an undocumented zero feeCoefficient are
intentional adversarial inputs. Settlement text is explicitly unverified. Successful
offline tests do not verify live availability, account eligibility, settlement compatibility
or profitability. No account identifiers or keys are present.

`events_nfl_live_trimmed.json` and `book_live_trimmed.json` are trimmed public GET responses
captured **2026-09-30**, keeping one NFL event/moneyline and only the first two displayed
levels on each book side. Source endpoints:

- https://gateway.polymarket.us/v2/leagues/nfl/events
- https://gateway.polymarket.us/v1/markets/aec-nfl-pit-cle-2026-10-01/book

Reads used `curl --max-time 20 --compressed` without keys or account calls. Public IDs
identify markets, not accounts. The actual legacy type is
`football_team_full_game_winner`, distinct from the generic schema example. The event
description is retained as evidence to inspect, not silently promoted to a verified
cross-exchange settlement guarantee. These captures establish schema compatibility, not
full-slate coverage, atomic fills or P&L.
