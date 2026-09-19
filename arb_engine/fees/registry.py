"""Pick the fee model for a venue quote from its ``fee_params`` and the user's settings.

Settings keys this module owns (declared through ``config.declare_setting`` when that
helper exists, resolved ``settings dict > env > default`` either way):

    rothera_fee_model   ROBINHOOD_ROTHERA_FEE_MODEL   flat_001 | quadratic
    cdna_fee_model      CDNA_FEE_MODEL                flat_001 | flat_002 | weighted_007

Both default to ``flat_001`` so every recorded constant (test_inplay $52.00, test_maker,
test_eventlookup, test_ncaaf CDNA 0.02) is untouched; flipping the default is a
one-commit follow-up once a Robinhood order ticket confirms the pass-through.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional

from ..models import (
    VENUE_KALSHI,
    VENUE_POLYMARKET,
    VENUE_POLYMARKET_US,
    VENUE_ROBINHOOD,
    OutcomeQuote,
)
from .base import FeeModel, ZeroFees
from .kalshi import KalshiFees
from .polymarket import PolymarketFees, PolymarketUSFees
from .robinhood import DEFAULT_CDNA_FEE_MODEL, DEFAULT_ROTHERA_FEE_MODEL, RobinhoodFees

FEE_SETTINGS: dict[str, tuple[str, str, str]] = {
    # key -> (env var, default, doc)
    "rothera_fee_model": ("ROBINHOOD_ROTHERA_FEE_MODEL", DEFAULT_ROTHERA_FEE_MODEL, "Rothera exchange-fee model for Robinhood NFL contracts: 'flat_001' ($0.01/contract, historical) or 'quadratic' (Rothera Fee Schedule 20260520: max(round(0.02*P*(1-P)*C, 2), $0.01) per order)."),
    "cdna_fee_model": ("CDNA_FEE_MODEL", DEFAULT_CDNA_FEE_MODEL, "CDNA (Crypto.com | Derivatives North America, ex-Nadex) exchange-fee model for Robinhood college contracts: 'flat_001', 'flat_002' or 'weighted_007' (0.07*P*(1-P)*C). Unverified until an order ticket is seen."),
}

try:  # config.declare_setting / setting arrive with plan item P01; stay importable without them.
    from ..config import declare_setting as _declare_setting, setting as _setting  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised when P01 is absent
    _declare_setting = None  # type: ignore[assignment]
    _setting = None  # type: ignore[assignment]

if _declare_setting is not None:
    for _key, (_env, _default, _doc) in FEE_SETTINGS.items():
        _declare_setting(_key, env=_env, default=_default, cast=str, doc=_doc)


def fee_setting(settings: Optional[Mapping[str, Any]], key: str) -> str:
    """Resolve one of ``FEE_SETTINGS``: explicit dict > environment > default. Uses
    ``config.setting`` when present so precedence matches every other declared key."""
    env, default, _ = FEE_SETTINGS[key]
    if _setting is not None:
        value = _setting(settings, key)
    elif settings is not None and key in settings:
        value = settings[key]
    else:
        value = os.environ.get(env) or default
    value = str(value or default).strip().lower()
    return value or default


def fee_model_for(venue: str, fee_params: Mapping[str, Any] | None = None, *, settings: Mapping[str, Any] | None = None) -> FeeModel:
    """Pick and configure the fee model for a venue quote.

    ``settings`` carries user-level knobs: ``robinhood_gold`` (bool), ``kalshi_rounding``
    ("cent"|"centicent"), ``polymarket_us_volume_rebate`` (Decimal-able),
    ``rothera_fee_model`` and ``cdna_fee_model`` (see the module docstring). A quote's own
    ``fee_params`` may pin ``rothera_fee_model`` / ``cdna_fee_model`` and wins over settings.
    """
    settings = settings or {}
    fee_params = fee_params or {}
    if venue == VENUE_KALSHI:
        return KalshiFees.from_series(fee_params, rounding=str(settings.get("kalshi_rounding", "cent")))
    if venue == VENUE_ROBINHOOD:
        params = dict(fee_params)
        params.setdefault("gold", settings.get("robinhood_gold", False))
        params.setdefault("rothera_fee_model", fee_setting(settings, "rothera_fee_model"))
        params.setdefault("cdna_fee_model", fee_setting(settings, "cdna_fee_model"))
        return RobinhoodFees.from_params(params)
    if venue == VENUE_POLYMARKET:
        return PolymarketFees.from_market(fee_params)
    if venue == VENUE_POLYMARKET_US:
        return PolymarketUSFees.for_date(volume_rebate=settings.get("polymarket_us_volume_rebate", 0) or 0)
    return ZeroFees()


def fee_model_for_quote(q: OutcomeQuote, settings: Mapping[str, Any] | None = None) -> FeeModel:
    return fee_model_for(q.venue, q.fee_params, settings=settings)


def fee_model_factory(settings: Mapping[str, Any] | None = None) -> Callable[[OutcomeQuote], FeeModel]:
    """``lambda q: fee_model_for_quote(q, settings)`` with the settings bound once."""
    return lambda q: fee_model_for_quote(q, settings)
