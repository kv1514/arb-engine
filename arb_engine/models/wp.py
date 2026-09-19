"""In-game NFL win probability — standard-library inference.

The model is trained by ``scripts/train_wp_model.py`` on nflverse play-by-play
(2016–2024, held out on 2025) with the same feature set nflfastR uses for its
``vegas_wp`` model, and exported to ``arb_engine/data/nfl_wp_model.json``. The shipped
model is a polynomial logistic regression with XGBoost residual trees stacked on its
margin; this module evaluates both (or the logistic stage alone, the training fallback)
with nothing but the standard library, so the engine stays dependency free. See
``docs/MODEL.md`` for data, metrics and limitations.

Conventions
-----------
* Everything the model sees is from the **possession team's** perspective
  (``score_differential`` = posteam score − defteam score, ``spread_time`` positive
  when the posteam is favoured).
* ``home_win_probability`` converts an ESPN-style game state into those features.
  Its ``vegas_spread_home`` argument uses the sportsbook convention shown on ESPN /
  DraftKings: **negative = home favoured** (``-3`` means the home team gives 3).
  nflverse's ``spread_line`` has the opposite sign (positive = home favoured); the
  training script handles that, this module handles the ESPN sign.
* When nobody has the ball (pre-game, between plays, halftime) we evaluate a neutral
  state — 1st & 10 from the team's own 25 (``yardline_100=75``) — once with the home
  team as posteam and once with the away team, and average the two home-win numbers.
  Pre-game the 2nd-half kickoff recipient is usually unknown too, so both assignments
  are averaged unless ``receive_2h_ko_home`` is given.
"""

from __future__ import annotations

import json
import math
import struct
from importlib import resources
from pathlib import Path
from typing import Any, Optional, Union

# Feature order is fixed; the exported JSON repeats it and ``load_model`` checks it.
FEATURES: tuple[str, ...] = (
    "score_differential",
    "game_seconds_remaining",
    "half_seconds_remaining",
    "receive_2h_ko",
    "spread_time",
    "diff_time_ratio",
    "down",
    "ydstogo",
    "yardline_100",
    "posteam_timeouts_remaining",
    "defteam_timeouts_remaining",
    "is_home",
)

MODEL_RESOURCE = "nfl_wp_model.json"
REGULATION_SECONDS = 3600.0
HALF_SECONDS = 1800.0

_F32 = struct.Struct("f")


def _f32(x: float) -> float:
    """Round to float32 the way XGBoost stores thresholds and reads features."""
    return _F32.unpack(_F32.pack(x))[0]


def _sigmoid(margin: float) -> float:
    if margin >= 0:
        return 1.0 / (1.0 + math.exp(-margin))
    e = math.exp(margin)
    return e / (1.0 + e)


def _logit(p: float) -> float:
    p = min(max(p, 1e-12), 1.0 - 1e-12)
    return math.log(p / (1.0 - p))


def time_features(score_differential: float, game_seconds_remaining: float, posteam_spread: float) -> tuple[float, float]:
    """nflfastR's ``spread_time`` and ``diff_time_ratio`` for a posteam-perspective state.

    ``posteam_spread`` is positive when the possession team is favoured (nflverse sign).
    """
    gsr = min(max(float(game_seconds_remaining), 0.0), REGULATION_SECONDS)
    elapsed_share = (REGULATION_SECONDS - gsr) / REGULATION_SECONDS
    decay = math.exp(-4.0 * elapsed_share)
    return posteam_spread * decay, score_differential / decay


def posteam_features(
    *,
    score_differential: float,
    game_seconds_remaining: float,
    half_seconds_remaining: Optional[float] = None,
    receive_2h_ko: int = 0,
    posteam_spread: float = 0.0,
    down: float = 1,
    ydstogo: float = 10,
    yardline_100: float = 75,
    posteam_timeouts_remaining: float = 3,
    defteam_timeouts_remaining: float = 3,
    is_home: int = 1,
) -> dict[str, float]:
    """Assemble the model's feature dict for one possession-team state."""
    gsr = min(max(float(game_seconds_remaining), 0.0), REGULATION_SECONDS)
    if half_seconds_remaining is None:
        half_seconds_remaining = gsr - HALF_SECONDS if gsr > HALF_SECONDS else gsr
    spread_time, diff_time_ratio = time_features(score_differential, gsr, posteam_spread)
    return {
        "score_differential": float(score_differential),
        "game_seconds_remaining": gsr,
        "half_seconds_remaining": float(half_seconds_remaining),
        "receive_2h_ko": float(receive_2h_ko),
        "spread_time": spread_time,
        "diff_time_ratio": diff_time_ratio,
        "down": float(down),
        "ydstogo": float(ydstogo),
        "yardline_100": float(yardline_100),
        "posteam_timeouts_remaining": float(posteam_timeouts_remaining),
        "defteam_timeouts_remaining": float(defteam_timeouts_remaining),
        "is_home": float(is_home),
    }


class _Logistic:
    """Logistic regression on standardized features expanded into polynomial terms."""

    def __init__(self, lr: dict[str, Any]):
        self.mean: list[float] = lr["mean"]
        self.std: list[float] = lr["std"]
        self.intercept: float = float(lr["intercept"])
        self.coef: list[float] = lr["coef"]
        self.terms: list[list[int]] = lr["terms"]  # each term = list of feature indices multiplied

    def margin(self, x: list[float]) -> float:
        z = [(v - m) / s if s else 0.0 for v, m, s in zip(x, self.mean, self.std)]
        total = self.intercept
        for coef, term in zip(self.coef, self.terms):
            prod = 1.0
            for i in term:
                prod *= z[i]
            total += coef * prod
        return total


class WinProbModel:
    """Exported model: XGBoost trees (compact arrays), optionally stacked on a logistic
    base margin, or the logistic regression alone (the training fallback)."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.model_type: str = payload.get("model_type", "xgboost")
        feats = tuple(payload.get("features") or FEATURES)
        if feats != FEATURES:
            raise ValueError(f"model feature list {feats} does not match {FEATURES}")
        self.features = feats
        self.meta: dict[str, Any] = payload.get("meta", {})
        self.base_logistic: Optional[_Logistic] = None
        if self.model_type == "xgboost":
            self.base_margin = _logit(float(payload.get("base_score", 0.5)))
            self.trees: list[tuple[list, list, list, list, list, list]] = []
            for tree in payload["trees"]:
                self.trees.append((tree["f"], [_f32(t) for t in tree["t"]], tree["y"], tree["n"], tree["m"], tree["v"]))
            if payload.get("base_logistic"):
                self.base_logistic = _Logistic(payload["base_logistic"])
        elif self.model_type == "logistic":
            self.logistic = _Logistic(payload["logistic"])
        else:
            raise ValueError(f"unknown model_type {self.model_type!r}")

    # -- scoring -----------------------------------------------------------------
    def margin(self, x: list[float]) -> float:
        if self.model_type == "logistic":
            return self.logistic.margin(x)
        xf = [None if (v is None or v != v) else _f32(v) for v in x]
        total = self.base_margin
        if self.base_logistic is not None:
            total += self.base_logistic.margin(x)
        for feat, thr, yes, no, missing, val in self.trees:
            node = 0
            while True:
                f = feat[node]
                if f < 0:
                    total += val[node]
                    break
                v = xf[f]
                if v is None:
                    node = missing[node]
                elif v < thr[node]:
                    node = yes[node]
                else:
                    node = no[node]
        return total

    def predict_posteam_wp(self, features: dict[str, float]) -> float:
        x = [features.get(name) for name in self.features]
        missing = [n for n, v in zip(self.features, x) if v is None]
        if missing:
            raise KeyError(f"missing features: {missing}")
        return _sigmoid(self.margin([float(v) for v in x]))

    @property
    def n_trees(self) -> int:
        return len(self.trees) if self.model_type == "xgboost" else 0


def load_model(path: Union[str, Path, None] = None) -> WinProbModel:
    """Load the exported model from ``path`` or the packaged ``arb_engine/data`` copy."""
    if path is not None:
        with open(path, encoding="utf-8") as f:
            return WinProbModel(json.load(f))
    with resources.files("arb_engine.data").joinpath(MODEL_RESOURCE).open("r", encoding="utf-8") as f:
        return WinProbModel(json.load(f))


_DEFAULT: Optional[WinProbModel] = None


def default_model() -> WinProbModel:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = load_model()
    return _DEFAULT


def predict_posteam_wp(features: dict[str, float], model: Optional[WinProbModel] = None) -> float:
    """P(possession team wins) for a feature dict (see ``FEATURES``)."""
    return (model or default_model()).predict_posteam_wp(features)


def home_win_probability(
    *,
    home_score: float,
    away_score: float,
    game_seconds_remaining: float,
    possession: Optional[str] = None,
    down: Optional[float] = None,
    distance: Optional[float] = None,
    yardline_100: Optional[float] = None,
    home_timeouts: float = 3,
    away_timeouts: float = 3,
    vegas_spread_home: float = 0.0,
    receive_2h_ko_home: Optional[bool] = None,
    half_seconds_remaining: Optional[float] = None,
    model: Optional[WinProbModel] = None,
) -> float:
    """P(home team wins) from an ESPN-style game state.

    ``possession`` is ``"home"``, ``"away"`` or ``None`` (dead ball / pre-game: neutral
    state averaged over both perspectives, see module docstring). ``yardline_100`` is
    yards to the *opponent's* goal line for the possession team. ``vegas_spread_home``
    is the home team's sportsbook line (negative = home favoured). ``receive_2h_ko_home``
    says whether the home team receives the second-half kickoff (i.e. kicked off to open
    the game); ``None`` averages both possibilities.
    """
    mdl = model or default_model()
    gsr = min(max(float(game_seconds_remaining), 0.0), REGULATION_SECONDS)
    second_half = gsr <= HALF_SECONDS
    spread_home = -float(vegas_spread_home or 0.0)  # nflverse sign: positive = home favoured

    def perspective(is_home: bool, ko_home: Optional[bool], dn: float, dist: float, yl: float) -> float:
        """P(home wins) with the given team as posteam and 2H-kickoff assignment."""
        if second_half or ko_home is None:
            receive = 0
        else:
            receive = 1 if (ko_home == is_home) else 0
        feats = posteam_features(
            score_differential=(home_score - away_score) if is_home else (away_score - home_score),
            game_seconds_remaining=gsr,
            half_seconds_remaining=half_seconds_remaining,
            receive_2h_ko=receive,
            posteam_spread=spread_home if is_home else -spread_home,
            down=dn,
            ydstogo=dist,
            yardline_100=yl,
            posteam_timeouts_remaining=home_timeouts if is_home else away_timeouts,
            defteam_timeouts_remaining=away_timeouts if is_home else home_timeouts,
            is_home=1 if is_home else 0,
        )
        wp = mdl.predict_posteam_wp(feats)
        return wp if is_home else 1.0 - wp

    ko_options: list[Optional[bool]] = [True, False] if (receive_2h_ko_home is None and not second_half) else [receive_2h_ko_home]

    if possession in ("home", "away"):
        is_home = possession == "home"
        dn = 1 if down is None or down <= 0 else down
        dist = 10 if distance is None else distance
        yl = 75 if yardline_100 is None else yardline_100
        vals = [perspective(is_home, ko, dn, dist, yl) for ko in ko_options]
        return sum(vals) / len(vals)

    # Neutral / dead-ball state: 1st & 10 from own 25 for each team, averaged.
    vals = []
    for ko in ko_options:
        vals.append(perspective(True, ko, 1, 10, 75))
        vals.append(perspective(False, ko, 1, 10, 75))
    return sum(vals) / len(vals)
