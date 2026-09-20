# Results: what is measured, where it lives, how the docs stay honest

Every number quoted in `README.md` and `docs/*.md` that came out of a replay or a
measurement script is committed twice: as a **metrics-only JSON fixture** under
`tests/fixtures/results/` (the source of truth, canonical `json.dumps(sort_keys=True,
indent=1)`) and, for the two week-long replays, as the **text report** in this folder
(`replay_nfl_2026_w1.txt`, `replay_ncaaf_2026_w2.txt`, the exact stdout of the run). The
docs never carry a hand-typed copy of a table: each table sits between
`<!-- results:<name> -->` … `<!-- /results:<name> -->` markers and is rendered from its
fixture by `scripts/render_results.py`.

```bash
python scripts/render_results.py --list     # every block, its fixture and the docs that carry it
python scripts/render_results.py --check    # CI: every doc block equals its rendered table
python scripts/render_results.py --write    # after a fixture changes: rewrite the blocks in place
python -m unittest tests.test_docs_results  # the same check, imported as a module
```

## The fixtures

| fixture | produced by | what it holds |
|---|---|---|
| `replay_nfl_2026_w1.json` (+ `.txt` here) | `python -m arb_engine backtest --week 1 --season 2026 --bar-mode both --slices --placebo --results-json …` | NFL 2026 week 1, 16 games, 2,904 rows: pooled tables (all rows / in play / in-play scrimmage), Kalshi under both candle alignments, per-slice and per-play-class tables, class gaps and STEAL-qualifying counts, linear and logit blend fits, game-cluster bootstrap intervals with games-needed, STEAL/LOCK simulations incl. the shift and shuffle placebos |
| `replay_ncaaf_2026_w2.json` (+ `.txt`) | `… backtest --sport ncaaf --week 2 --season 2026 --bar-mode both --slices --placebo --results-json …` | college week 2, 86 games, 15,060 rows, same shape |
| `week1_p03.json` | `backtest --week 1 --season 2026 --offline --cache-dir tests/fixtures/history/replay_trim --no-polymarket --slices --placebo --min-cell 10` (what `tests/test_backtest.py` re-runs) | the trimmed two-game replay cache's report: pins the harness's structure and numbers with a float tolerance; not a result to quote |
| `week1_p05.json` | `tests/test_wp_model.py` (offline) | WP rules-table checks: spread inversion, era neutral yardline, kickoff-pending and try states, OT clamps, kneel floor |
| `college_experiment_p04.json` | `python scripts/college_experiment.py --week 1 2 --season 2026` | college spread rescale / clamp / OT-clock variants of the NFL model, 185 games, with game-cluster intervals |
| `espn_wp_alignment_p04.json` | `python scripts/check_espn_wp_alignment.py --week 1 --season 2026` | is ESPN's `winprobability` the state before or after the play (scoring plays, per game) |
| `feed_parity_w1_p04.json` | `python scripts/backtest_live_feed.py --week 1 --season 2026 --record …` | ESPN-derived replay state vs nflverse play-by-play, field by field |
| `fee_flip_p10.json` | `python scripts/fee_flip_p10.py` (offline, fixture scans; rewrites this fixture — `--print` only prints) | what the opt-in Rothera / CDNA fee models do to the fixture scans' margins |
| `eligibility_p11.json` | `python scripts/eligibility_impact.py --fixtures` | how many fixture-scan arbs and maker hedges relied on a non-executable (Polymarket) leg |
| `arb_fixture_p09.json` | `tests/test_scanner.py::FixtureMetricsTests` (re-computed on every run) | tie-aware cross-venue pair metrics on the fixture scans |
| `lines_eval_p13.json` | `python scripts/eval_lines.py --week 1 --season 2026` (= `arb-engine lines-eval`) | spread / total fair values (normal, empirical) vs Kalshi mids under both alignments, pre-game and in play |

The replay reports are the runs of 2026-09-19 with `ARB_HTTP_TRANSPORT=curl` against the
public histories (Kalshi 1-minute candles, Polymarket price history, ESPN summaries), read
through the on-disk cache (`--cache-dir`, `--offline` for a reproducible rerun). Nothing in
the test suite re-runs them; `tests/test_backtest.py` compares the two-game synthetic
fixture structurally with a float tolerance and `tests/test_docs_results.py` compares the
docs' tables with the fixtures byte for byte.

## Reading the tables

* **log-loss / Brier** are on P(home wins) per row, lower is better. The headline pool is
  *in-play scrimmage plays* (`play_class == "scrimmage"`, score can still change): kickoffs,
  tries, timeouts and end-of-period rows are reported separately because the market and the
  model see different states there.
* **Kalshi before / after** is the bracket: `kalshi_before` is the last 1-minute candle that
  ends at or before the play's wall-clock stamp (what a taker could have hit), `kalshi_after`
  the first candle ending after it (the market has seen the play). The truth for
  "model vs market" lies inside that range; the docs quote the range, never one end.
* **Intervals** are 90% paired game-cluster bootstrap intervals (`quant/calibration.py`, B =
  1000, games resampled with replacement) on the per-game mean difference; "games needed" is
  the number of games at which an interval of the observed per-game spread would exclude
  zero at the observed effect size.
* **STEAL/LOCK simulations** enter 10 contracts once per game at Kalshi's candle ask with
  taker fees. `pre_before` pairs the pre-play fair with the candle before the play,
  `post_after` the post-play fair with the candle after (the executable pairing),
  `pre_after` is the leaky pairing kept for comparison, `shift` (one candle later still)
  and `shuffle` (the games' outcomes permuted) are placebos. ROI is P&L over dollars staked;
  the interval is on P&L per game.
