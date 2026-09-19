"""Blend the in-play fair value from three sources: market consensus, our WP model, ESPN.

Sources (all expressed as P(home wins)):

* **market** — ``consensus_fair_value`` across Kalshi / Polymarket / Rothera de-vigged mids.
* **model** — ``arb_engine.models.wp.home_win_probability`` scored on the ESPN game state
  (score, clock, possession, down/distance, field position, timeouts, pre-game spread).
* **espn** — ESPN's own win-probability number for the last play (``espn_home_wp``).

Why the market gets the most weight in play (default 0.50 / 0.35 / 0.15)
-------------------------------------------------------------------------
Exchange prices embed information the state model cannot see: injuries in the game,
weather, the quarterback who just limped off, a team's tempo, and the *next* play that the
ESPN feed has not published yet (its clock lags the broadcast by 5–20 s). Held out on the
2025 season the model matches nflfastR's ``vegas_wp`` (log-loss 0.475 vs 0.477), which is
good, but a liquid two-sided book during a game is still a better estimator of the true
probability than any public state model, so it anchors the blend. ESPN's model is a third
opinion with a smaller weight because it is unaudited and depends on the same lagging feed.

When to trust the model more
----------------------------
* **Thin or wide books** — a Rothera quote 8¢ wide with 20 contracts a side carries little
  information; ``consensus_fair_value`` already down-weights wide spreads, and
  ``market_confidence`` (0–1) lets the caller scale the market weight further (e.g. 0.5
  when the best venue is stale or the consensus comes from a single venue).
* **Stale Robinhood quotes** — Robinhood's quotes API can lag 10–30 s during a scoring
  drive; if the game state is fresher than the quote, the model is the better guide and a
  gap between them is *staleness*, which is exactly what STEAL wants to catch. The
  agreement filter in ``strategy/inplay.py`` (blended AND model both above all-in) is what
  keeps a stale market from generating a false STEAL on its own.
* **Pre-game** — the state model is just a spread-to-probability curve; the market already
  knows the spread, so the blend is market-only unless the market is missing (then the
  model's spread curve, then ESPN, are used as the only source).

``disagreement`` is the largest absolute gap between any two available sources; the
watcher prints it when it exceeds 0.05 so a human can decide who is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

DEFAULT_WEIGHTS: dict[str, float] = {"market": 0.5, "model": 0.35, "espn": 0.15}
PREGAME_ORDER = ("market", "model", "espn")  # fallback order when not live


@dataclass
class BlendedFair:
    fair: dict[str, float]                       # outcome -> blended probability
    home_p: Optional[float]                      # blended P(home)
    disagreement: Optional[float]                # max |p_i - p_j| across available sources
    sources: dict[str, Optional[float]] = field(default_factory=dict)  # source -> P(home) (None = unavailable)
    weights: dict[str, float] = field(default_factory=dict)            # normalised weights actually used
    live: bool = True

    def get(self, outcome: str) -> Optional[float]:
        return self.fair.get(outcome)

    def as_dict(self) -> dict[str, Any]:
        return {"fair": dict(self.fair), "home_p": self.home_p, "disagreement": self.disagreement, "sources": dict(self.sources), "weights": dict(self.weights), "live": self.live}


def _clamp(p: float) -> float:
    return min(max(float(p), 0.0), 1.0)


def market_home_probability(outcome_probs_market: Optional[Mapping[str, Optional[float]]], home_outcome: str, away_outcome: str) -> Optional[float]:
    """P(home) from a consensus dict; one-sided dicts are completed, two-sided ones renormalised."""
    if not outcome_probs_market:
        return None
    h = outcome_probs_market.get(home_outcome)
    a = outcome_probs_market.get(away_outcome)
    if h is None and a is None:
        return None
    if h is None:
        return _clamp(1.0 - float(a))  # type: ignore[arg-type]
    if a is None:
        return _clamp(float(h))
    tot = float(h) + float(a)
    return _clamp(float(h) / tot) if tot > 0 else None


def blended_fair(
    outcome_probs_market: Optional[Mapping[str, Optional[float]]],
    model_home_wp: Optional[float],
    espn_home_wp: Optional[float],
    home_outcome: str,
    away_outcome: str,
    weights: Optional[Mapping[str, float]] = None,
    live: bool = True,
    market_confidence: float = 1.0,
) -> BlendedFair:
    """Weighted blend of the available sources (weights renormalise over what is present).

    ``live=False`` (pre-game / final) uses the market alone; when the market is absent the
    model, then ESPN, act as the single source. ``market_confidence`` in (0, 1] scales the
    market weight (thin book, stale quote) before normalisation.
    """
    w_in = dict(DEFAULT_WEIGHTS)
    if weights:
        w_in.update({k: float(v) for k, v in weights.items()})
    conf = min(max(float(market_confidence), 0.0), 1.0)
    sources: dict[str, Optional[float]] = {
        "market": market_home_probability(outcome_probs_market, home_outcome, away_outcome),
        "model": _clamp(model_home_wp) if model_home_wp is not None else None,
        "espn": _clamp(espn_home_wp) if espn_home_wp is not None else None,
    }
    avail = {k: v for k, v in sources.items() if v is not None}
    vals = list(avail.values())
    disagreement = (max(vals) - min(vals)) if len(vals) >= 2 else (0.0 if vals else None)

    used: dict[str, float] = {}
    if not avail:
        return BlendedFair(fair={}, home_p=None, disagreement=None, sources=sources, weights={}, live=live)
    if not live:
        pick = next(k for k in PREGAME_ORDER if k in avail)
        used = {pick: 1.0}
    else:
        raw = {k: max(w_in.get(k, 0.0), 0.0) * (conf if k == "market" else 1.0) for k in avail}
        tot = sum(raw.values())
        if tot <= 0:  # all requested weights zero -> equal weights over what exists
            raw = {k: 1.0 for k in avail}
            tot = float(len(avail))
        used = {k: v / tot for k, v in raw.items()}
    home_p = _clamp(sum(avail[k] * used[k] for k in used))
    fair = {home_outcome: home_p, away_outcome: 1.0 - home_p}
    return BlendedFair(fair=fair, home_p=home_p, disagreement=disagreement, sources=sources, weights=used, live=live)
