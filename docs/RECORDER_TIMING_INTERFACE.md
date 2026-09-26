# Recorder timing interface

The microstructure loader may treat an L1 row as exactly timed only when
`approx_time == 0`. Full live-adapter fetches and fast-lane fetches attach `req_ts` at the
request boundary and `obs_ts` after the response is in hand. Fixture calls with a pinned
time use that time for both. Rows without measured boundaries use the tick time and set
`approx_time == 1`.

A row carried through a failed or partial fast refresh retains its prior `req_ts` and
`obs_ts`, and has `refreshed == 0`. Every stored tick timestamp is at least the largest
exact `obs_ts` among its rows.

Kalshi public prints store both exchange `ts` and local receipt `obs_ts`. Causal features
must require `obs_ts <= decision_ts`; exchange time alone is insufficient. Restart cursors
use the maximum exchange second inclusively, while `trade_id` provides deduplication.

`FastLane.trade_poll_status()` exposes `last_request_ts`, `last_poll_ts` (response
completion), `last_gap_s` (request-start gap), `receipt_gap_s`, `overdue`, and `backlog` per
live ticker. Background polls page independent tickers concurrently, and
`wait_for_trade_polls()` must complete before closing their shared `Store`.
