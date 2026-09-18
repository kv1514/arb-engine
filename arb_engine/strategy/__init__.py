"""Strategies that place or simulate orders. Everything here goes through the same gates as
``arb_engine.execution``: paper by default, demo with keys, prod only with
``KALSHI_ENV=prod`` + ``ARB_LIVE_TRADING=1`` + an explicit confirm."""
