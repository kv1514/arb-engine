"""arb-engine: fee-aware arbitrage and fair-value engine for sports prediction markets.

Venues: Kalshi (direct), Polymarket (Gamma/CLOB public data), Robinhood event contracts
(Rothera + KalshiEX routed, public quotes API). The engine is standard-library only;
authenticated Kalshi trading additionally needs the `cryptography` package.
"""

__version__ = "0.1.0"
