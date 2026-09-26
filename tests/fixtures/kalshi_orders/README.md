# Kalshi order fixtures

Shapes of the signed order endpoints as the offline tests assume them.
**Status: mixed** — `balance`, `create_order`, `cancel_*` were recorded on demo 2026-09-22 and
the fill files on 2026-09-26 (each file's `_fixture` says which); the rest were derived from the OpenAPI schemas on docs.kalshi.com/api-reference
(fetched 2026-09-19: `orders/create-order-v2`, `orders/cancel-order-v2`,
`orders/batch-cancel-orders-v2`, `orders/get-orders`, `orders/get-order`,
`portfolio/get-fills`), not from a live response. `python scripts/kalshi_demo_check.py
--record` with a demo key overwrites every file here with trimmed real responses; when a
field name changes, update `tests/test_kalshi_client.py` and `strategy/broker.py` together.

Key facts the shapes encode: writes live under `/portfolio/events/...` (V2: `side` bid/ask,
fixed-point strings, `expiration_time` as **Unix seconds int64**); reads stay on
`/portfolio/orders`, `/portfolio/orders/{id}` and `/portfolio/fills`, whose rows carry the
canonical `outcome_side` yes/no + `book_side` bid/ask and `yes_price_dollars` /
`no_price_dollars` (legacy `side`/`action` are still present but deprecated). Cancels return
`{order_id, client_order_id, reduced_by, ts_ms}` — no order object; `reduced_by` is `"0.00"`
when a batched cancel errored.

| file | endpoint | shape |
| --- | --- | --- |
| `balance.json` | `GET /portfolio/balance` | `{balance, balance_dollars, ...}` |
| `create_order.json` | `POST /portfolio/events/orders` (post-only bid at $0.01) | flat `{order_id, client_order_id, fill_count, remaining_count, ts_ms}` |
| `order.json` | `GET /portfolio/orders/{id}` | `{"order": Order}` |
| `orders_v2.json` | `GET /portfolio/orders?status=resting` | `{"orders": [Order], "cursor"}` |
| `fills_v2.json` | `GET /portfolio/fills` | `{"fills": [Fill], "cursor"}` |
| `cancel_order.json` | `DELETE /portfolio/events/orders/{id}` | flat `{order_id, client_order_id, reduced_by, ts_ms}` |
| `cancel_batched.json` | `DELETE /portfolio/events/orders/batched` body `{"orders": [{"order_id", "exchange_index"?, "market_ticker"?}]}` | `{"orders": [{order_id, client_order_id, reduced_by, ts_ms}]}` |
| `create_order_ioc.json` | `POST /portfolio/events/orders` (IOC buy at $0.01, nothing to fill) | flat, `fill_count` / `remaining_count` `"0.00"` |
| `create_order_fill.json` | `POST /portfolio/events/orders` (IOC buy 1 at the ask; **recorded 2026-09-26**) | flat, plus `average_fill_price` and `average_fee_paid` (per contract) when it filled |
| `order_filled.json` | `GET /portfolio/orders/{id}` of that fill | `status: executed`, `fill_count_fp`, `taker_fill_cost_dollars` `"0.560000"`, `taker_fees_dollars` `"0.017300"` |
| `fills_v2.json` | `GET /portfolio/fills?order_id=` of that fill (**recorded 2026-09-26**, replaces the assumed row) | `{fill_id, trade_id, order_id, count_fp, yes/no_price_dollars, fee_cost, is_taker, ts, ...}` |

The fill fixtures are what the order ledger (`arb_engine/execution/ledger.py`) reconciles
from: actual fill cost and fees replace the reserved worst case. The fee charged
($0.0173 for one contract at $0.56) is the *centicent* rounding - see docs/VENUES.md.
`balance.json` is deliberately not re-recorded (it would commit the demo account's state).
