"""Pure math: odds conversion & de-vigging, fee-aware arbitrage, sizing, fair value."""

from .odds import (
    american_to_decimal,
    decimal_to_american,
    implied_from_decimal,
    decimal_from_prob,
    overround,
    devig_multiplicative,
    devig_power,
    devig_shin,
)
from .arbitrage import (
    Leg,
    LegResult,
    ArbResult,
    evaluate,
    leg_all_in_cost,
    best_leg_per_outcome,
    max_price_for_leg,
    size_from_books,
    walk_book,
)
from .sizing import kelly_fraction, kelly_stake, equal_payout_stakes
from .fairvalue import consensus_fair_value, FairValue

__all__ = [
    "american_to_decimal", "decimal_to_american", "implied_from_decimal", "decimal_from_prob",
    "overround", "devig_multiplicative", "devig_power", "devig_shin",
    "Leg", "LegResult", "ArbResult", "evaluate", "leg_all_in_cost", "best_leg_per_outcome",
    "max_price_for_leg", "size_from_books", "walk_book",
    "kelly_fraction", "kelly_stake", "equal_payout_stakes",
    "consensus_fair_value", "FairValue",
]
