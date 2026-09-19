"""Robinhood event-contract fees (Robinhood Derivatives, LLC) and the routing exchanges' fees.

Sources: Robinhood support article "Event contracts overview" (pricing model effective
June 1, 2026), the Robinhood Derivatives fee schedule (RHD Fee Schedule 2026-05-28),
Rothera Exchange and Clearing "Fee Schedule" (certified 20260520) and the Nadex fee
schedule effective 2026-08-01. Verified 2026-09-15 (Robinhood), 2026-09-19 (Rothera text).

    commission = min( round_up_to_cent( k x P x (1 - P) x C ),  $0.01 x C )
        k = 0.10 without Robinhood Gold, 0.05 with Gold
    exchange fee = set by the routing exchange (see the models below)

Both are charged when you open AND when you close a position (a contract held to
settlement pays only the opening side). Robinhood's published commission examples
(100 contracts):

    price   with Gold   without Gold
    $0.01   $0.05       $0.10
    $0.05   $0.24       $0.48
    $0.25   $0.94       $1.00   <- capped at $0.01/contract
    $0.50   $1.00       $1.00
    $0.75   $0.94       $1.00
    $0.99   $0.05       $0.10

Exchange fee models
-------------------
Robinhood's article only says "up to $0.01 per contract, varies by exchange". The engine
historically charged that ceiling per contract for every exchange (``flat_001``); the
per-exchange schedules say otherwise and this module carries them as **opt-in** models so
no downstream constant moves until an order ticket confirms which one Robinhood passes on
(see docs/ROADMAP.md "Needs you"). The default stays ``flat_001`` everywhere.

* **Rothera** (Robinhood/Susquehanna JV; NFL game/spread/total symbols without the ``KX``
  prefix). Rothera Fee Schedule 20260520 defines the *order* fee as

      fee = max( round_half_up( k x P x (1 - P) x C , $0.01 ),  $0.01 )   per order
      k = 0.02 for retail participants (the schedule's own example uses k = 0.06)

  which at $0.97-$0.99 is ~$0.0006 per contract on a 100-lot, 17-25x below the $0.01
  ceiling the engine charged, and is size-dependent: the $0.01 floor is per order, so a
  1-lot pays a full cent while a 100-lot at $0.50 pays $0.50. ``rothera_fee_model``:
  ``flat_001`` (default, historical) | ``quadratic`` (the schedule).
* **CDNA** = North American Derivatives Exchange, Inc. d/b/a "Crypto.com | Derivatives
  North America" (the exchange formerly branded Nadex; college-football symbols
  ``NX.F.OPT.CFB-…``). Its published fee is a range, not a number, so ``cdna_fee_model``
  offers ``flat_001`` (default, $0.01/contract) | ``flat_002`` ($0.02/contract, the top of
  the range) | ``weighted_007`` (0.07 x P x (1 - P) x C rounded up, the Nadex 2026-08-01
  taker-style schedule). All three are **unverified** until an order ticket is seen; the
  same model applies to the ``nadex`` exchange key since it is the same entity.
* **KalshiEX** (``KX…`` symbols) and **ForecastEX**: $0.01/contract and $0 (fee embedded in
  the spread) respectively, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from .base import CENT, D, Number, _Base, ceil_to

EXCHANGE_FEE_PER_CONTRACT: dict[str, Decimal] = {
    "kalshi": Decimal("0.01"),
    "rothera": Decimal("0.01"),
    "nadex": Decimal("0.01"),
    "forecastex": Decimal("0.00"),
    "cdna": Decimal("0.01"),  # college-football games (symbols NX.F.OPT.*); assumed at the $0.01 cap, unverified
}

#: Rothera Fee Schedule 20260520: retail k; the schedule's worked example uses 0.06.
ROTHERA_RETAIL_K = Decimal("0.02")
ROTHERA_ORDER_FEE_FLOOR = Decimal("0.01")
ROTHERA_FEE_MODELS = ("flat_001", "quadratic")
CDNA_FEE_MODELS = ("flat_001", "flat_002", "weighted_007")
CDNA_WEIGHTED_RATE = Decimal("0.07")
DEFAULT_ROTHERA_FEE_MODEL = "flat_001"
DEFAULT_CDNA_FEE_MODEL = "flat_001"


def exchange_from_symbol_or_enum(symbol: str | None, exchange_enum: str | None = None) -> str:
    """Map Robinhood's ``exchange`` enum / contract symbol to a short exchange key."""
    e = (exchange_enum or "").upper()
    if "ROTHERA" in e:
        return "rothera"
    if "KALSHI" in e:
        return "kalshi"
    if "FORECAST" in e:
        return "forecastex"
    if "NADEX" in e or "NORTH_AMERICAN" in e:
        return "nadex"
    if "CDNA" in e:
        return "cdna"
    s = (symbol or "").upper()
    if s.startswith("KX"):
        return "kalshi"
    if s.startswith("NX."):
        return "cdna"
    return "rothera" if s else "unknown"


def rothera_order_fee(price: Number, contracts: Number, k: Decimal = ROTHERA_RETAIL_K, floor: Decimal = ROTHERA_ORDER_FEE_FLOOR) -> Decimal:
    """Rothera exchange fee for ONE order of ``contracts`` at ``price``:
    ``max(round_half_up(k x P x (1-P) x C, $0.01), $0.01)``. Half-up (not banker's) because the
    schedule's own example, k=0.06 @ $0.35 x 100 = $1.365, is printed as $1.37."""
    c = D(contracts)
    if c <= 0:
        return Decimal("0.00")
    p = D(price)
    raw = k * p * (Decimal(1) - p) * c
    return max(raw.quantize(CENT, rounding=ROUND_HALF_UP), floor)


@dataclass(frozen=True)
class RobinhoodFees(_Base):
    name = "robinhood"
    gold: bool = False
    exchange: str = "rothera"
    commission_cap_per_contract: Decimal = Decimal("0.01")
    exchange_fee_per_contract: Decimal | None = None
    exchange_fee_table: Mapping[str, Decimal] = field(default_factory=lambda: dict(EXCHANGE_FEE_PER_CONTRACT))
    rothera_fee_model: str = DEFAULT_ROTHERA_FEE_MODEL
    cdna_fee_model: str = DEFAULT_CDNA_FEE_MODEL
    rothera_k: Decimal = ROTHERA_RETAIL_K

    def __post_init__(self) -> None:
        if self.rothera_fee_model not in ROTHERA_FEE_MODELS:
            raise ValueError(f"rothera_fee_model must be one of {ROTHERA_FEE_MODELS}, got {self.rothera_fee_model!r}")
        if self.cdna_fee_model not in CDNA_FEE_MODELS:
            raise ValueError(f"cdna_fee_model must be one of {CDNA_FEE_MODELS}, got {self.cdna_fee_model!r}")

    @classmethod
    def from_params(cls, params: Mapping[str, Any] | None, **overrides: Any) -> "RobinhoodFees":
        """Build from a quote's ``fee_params`` (``exchange`` / ``symbol`` / ``exchange_enum`` /
        ``gold``; optional ``rothera_fee_model`` / ``cdna_fee_model`` / ``rothera_k``)."""
        params = params or {}
        exchange = params.get("exchange") or exchange_from_symbol_or_enum(
            params.get("symbol"), params.get("exchange_enum")
        )
        kwargs: dict[str, Any] = {"exchange": exchange, "gold": bool(params.get("gold", False))}
        for key in ("rothera_fee_model", "cdna_fee_model"):
            if params.get(key):
                kwargs[key] = str(params[key])
        if params.get("rothera_k") is not None:
            kwargs["rothera_k"] = D(params["rothera_k"])
        kwargs.update(overrides)
        return cls(**kwargs)

    @property
    def k(self) -> Decimal:
        return Decimal("0.05") if self.gold else Decimal("0.10")

    @property
    def exchange_fee_model(self) -> str:
        """The exchange-fee model that applies to this quote's routing exchange."""
        if self.exchange == "rothera":
            return self.rothera_fee_model
        if self.exchange in ("cdna", "nadex"):
            return self.cdna_fee_model
        return "flat_001"

    def commission(self, price: Number, contracts: Number) -> Decimal:
        p = D(price)
        c = D(contracts)
        if c <= 0:
            return Decimal("0.00")
        raw = self.k * p * (Decimal(1) - p) * c
        rounded = ceil_to(raw, CENT)
        cap = self.commission_cap_per_contract * c
        return min(rounded, cap)

    def exchange_fee(self, price: Number, contracts: Number) -> Decimal:
        """Routing-exchange fee for one order. Price-dependent under the ``quadratic`` and
        ``weighted_007`` models, so callers must pass the order price."""
        c = D(contracts)
        if c <= 0:
            return Decimal("0.00")
        if self.exchange_fee_per_contract is not None:
            return (self.exchange_fee_per_contract * c).quantize(CENT)
        model = self.exchange_fee_model
        if model == "quadratic":
            return rothera_order_fee(price, c, k=self.rothera_k)
        if model == "weighted_007":
            p = D(price)
            return ceil_to(CDNA_WEIGHTED_RATE * p * (Decimal(1) - p) * c, CENT)
        if model == "flat_002":
            return (Decimal("0.02") * c).quantize(CENT)
        per = self.exchange_fee_table.get(self.exchange, Decimal("0.01"))
        return (per * c).quantize(CENT)

    def fee(self, price: Number, contracts: Number, role: str = "taker") -> Decimal:
        """Commission + exchange fee for ONE order of ``contracts`` at ``price``. Robinhood
        charges the same commission whether the order rests or crosses, and Rothera's floor
        is per order, so the all-in per contract depends on the order size: evaluate at the
        size you will actually send (``size_from_books`` re-prices at the sized quantity)."""
        return self.commission(price, contracts) + self.exchange_fee(price, contracts)
