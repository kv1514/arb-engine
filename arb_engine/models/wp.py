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

Dead-ball and boundary corrections (``arb_engine/data/nfl_wp_rules.json``)
--------------------------------------------------------------------------
The trees are fit on uncertain-outcome scrimmage plays, so states the training set
never contains are composed from states it does, without retraining:

* ``play_class="try"``: the untimed down after a touchdown is a mixture of the two
  kickoff-pending states it can lead to (PAT 0.94 / two-point 0.48 from the rules
  table, the two-point choice from the conventional margin chart).
* ``play_class="kickoff"``: nobody has the ball yet; the receiver starts 1st & 10 at
  the era's neutral yardline (own 25 through 2023, ~30 in 2024, ~31 from 2025) and an
  onside recovery puts the kicker at its own 45 with probability ``p_onside``.
* ``season`` picks that neutral yardline for every dead-ball state; ``None`` keeps 75.
* ``overtime=True`` disables the ``gsr <= 0`` decided shortcut (an OT clock at 0:00
  with a lead is only final when ESPN says so — pass ``final=True``) and clamps the
  output away from 0 and 1: the model is regulation-only and scores OT as a fourth
  quarter with that much clock, see docs/MODEL.md.
* Timeouts are not plays: the caller scores the post-timeout state (timeouts reduced
  by one, everything else unchanged); there is no ``play_class="timeout"`` special case.
* A provisional kneel-out floor (``kneel_floor.enabled``, default off) lifts the
  leader to 0.995 once the trailing defence cannot stop the clock.
"""

from __future__ import annotations

import json
import math
import os
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
RULES_RESOURCE = "nfl_wp_rules.json"
REGULATION_SECONDS = 3600.0
HALF_SECONDS = 1800.0
OVERTIME_SECONDS = 600.0
NEUTRAL_YARDLINE = 75  # pre-2024 neutral state: 1st & 10 from the own 25 (the model's training era default)

# Built-in copy of the rules table so the module works without the JSON (and so tests can
# override single keys through ``rules=``); the packaged file wins where both exist.
_BUILTIN_RULES: dict[str, Any] = {
    "neutral_yardline": {"default": NEUTRAL_YARDLINE, "by_season": {"2024": 70, "2025": 69}},
    "kickoff": {"p_onside_default": 0.0, "p_onside_late_trailing": 0.06, "onside_max_deficit": 16, "onside_late_gsr": 300, "onside_recovery_yardline_100": 55},
    "try": {"p_pat": 0.94, "p_two_point": 0.48, "two_point_margins": [-21, -18, -16, -13, -10, -5, -2, 1, 4, 5, 12, 19, 20], "two_point_from_gsr": 900},
    "kneel_floor": {"enabled": False, "floor": 0.995, "seconds_per_kneel": 40, "two_minute_warning": 120},
    "spread_inversion": {"lo": -30.0, "hi": 30.0, "step": 1.0, "tolerance": 1e-4},
    "overtime": {"clamp": 1e-4},
}

try:  # P01's settings registry; optional so this module imports on any checkout.
    from ..config import declare_setting as _declare_setting  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - depends on the checkout
    _declare_setting = None
if _declare_setting is not None:
    try:
        _declare_setting("wp_kneel_floor_enabled", env="ARB_WP_KNEEL_FLOOR", default=None, cast=str, doc="Override nfl_wp_rules.json kneel_floor.enabled (1/0); unset keeps the table value.")
    except Exception:  # pragma: no cover
        pass

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


# -- rules table -------------------------------------------------------------------
_RULES: Optional[dict[str, Any]] = None


def load_rules(path: Union[str, Path, None] = None) -> dict[str, Any]:
    """The dead-ball rules table: built-in defaults updated (section by section) by the
    packaged ``nfl_wp_rules.json`` or by ``path``. Unknown sections pass through."""
    merged: dict[str, Any] = {k: dict(v) for k, v in _BUILTIN_RULES.items()}
    try:
        if path is not None:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        else:
            with resources.files("arb_engine.data").joinpath(RULES_RESOURCE).open("r", encoding="utf-8") as f:
                payload = json.load(f)
    except (OSError, ValueError):
        payload = {}
    for key, section in payload.items():
        if isinstance(section, dict) and isinstance(merged.get(key), dict):
            merged[key].update(section)
        else:
            merged[key] = section
    return merged


def default_rules() -> dict[str, Any]:
    global _RULES
    if _RULES is None:
        _RULES = load_rules()
    return _RULES


def _rules(rules: Optional[dict[str, Any]]) -> dict[str, Any]:
    """``rules`` may override single sections/keys; missing ones come from the table."""
    base = default_rules()
    if not rules:
        return base
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, section in rules.items():
        if isinstance(section, dict) and isinstance(out.get(key), dict):
            out[key].update(section)
        else:
            out[key] = section
    return out


def neutral_yardline(season: Optional[int] = None, rules: Optional[dict[str, Any]] = None) -> float:
    """``yardline_100`` of the neutral / kickoff-receiver state for a season (``None`` -> 75).

    Later seasons than the table knows keep the newest entry (the 2025 touchback spot is
    the current rule); earlier ones the default.
    """
    tbl = _rules(rules)["neutral_yardline"]
    if season is None:
        return float(tbl.get("default", NEUTRAL_YARDLINE))
    by = {int(k): float(v) for k, v in (tbl.get("by_season") or {}).items()}
    if not by:
        return float(tbl.get("default", NEUTRAL_YARDLINE))
    keys = sorted(by)
    if int(season) < keys[0]:
        return float(tbl.get("default", NEUTRAL_YARDLINE))
    return by[max(k for k in keys if k <= int(season))]


def kneel_floor_enabled(rules: Optional[dict[str, Any]] = None) -> bool:
    """Table flag, overridable by ``ARB_WP_KNEEL_FLOOR=1/0`` (declared through P01's settings when present)."""
    env = os.environ.get("ARB_WP_KNEEL_FLOOR")
    if env is not None and env.strip() != "":
        return env.strip().lower() in ("1", "true", "yes", "on")
    return bool(_rules(rules)["kneel_floor"].get("enabled", False))


def _other(side: str) -> str:
    return "away" if side == "home" else "home"


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
    monotone: bool = True,
    play_class: Optional[str] = None,
    overtime: bool = False,
    final: bool = False,
    season: Optional[int] = None,
    p_onside: Optional[float] = None,
    rules: Optional[dict[str, Any]] = None,
) -> float:
    """P(home team wins) from an ESPN-style game state.

    ``possession`` is ``"home"``, ``"away"`` or ``None`` (dead ball / pre-game: neutral
    state averaged over both perspectives, see module docstring). ``yardline_100`` is
    yards to the *opponent's* goal line for the possession team. ``vegas_spread_home``
    is the home team's sportsbook line (negative = home favoured). ``receive_2h_ko_home``
    says whether the home team receives the second-half kickoff (i.e. kicked off to open
    the game); ``None`` averages both possibilities.

    ``monotone`` (default on) guards against the unconstrained residual trees: for each
    perspective the model is also scored at every smaller margin between a tie and the
    actual one, and the leader gets the running max (the trailer the running min), so a
    bigger lead can never *lower* the leading team's probability at a fixed clock, spread
    and field position. The raw export dips by up to ~10 points at thinly-trained splits
    (e.g. a 17-point Q2 lead below a 16-point one); see docs/MODEL.md.

    Dead-ball routing (all optional, defaults reproduce the pre-rules output exactly):

    * ``play_class="try"`` with ``possession`` = the team that just scored -> ``try_state_wp``.
      ``None`` on a real 1st-and-goal from the 2 is untouched.
    * ``play_class="kickoff"`` (or ``"kickoff_pending"``) with ``possession`` = the team
      that will *receive* (nflverse ``posteam`` and ESPN's drive owner on a kickoff row);
      the kicker is the other team -> ``kickoff_state_wp``. ``p_onside`` overrides the
      table's onside mixture.
    * ``season`` selects the era neutral yardline for neutral and kickoff states.
    * ``final=True`` returns 1.0 / 0.0 (0.5 for a tied final); otherwise the decided
      shortcut at ``gsr <= 0`` only fires in regulation (``overtime=False``). In overtime
      a tie at 0:00 is the tied-Q4 fallback (still modelled) and nothing returns exactly
      0 or 1. Unknown possession in OT uses the neutral state like anywhere else.
    """
    tbl = _rules(rules)
    margin = float(home_score) - float(away_score)
    if final:
        return 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
    # Final whistle in regulation with a margin: decided (a tie at 0:00 goes to overtime, model it).
    if float(game_seconds_remaining) <= 0 and margin != 0 and not overtime:
        return 1.0 if margin > 0 else 0.0
    mdl = model or default_model()
    gsr = min(max(float(game_seconds_remaining), 0.0), REGULATION_SECONDS)
    if overtime:
        gsr = min(gsr, OVERTIME_SECONDS)  # OT scored as a fourth quarter with that much clock (docs/MODEL.md)
    second_half = gsr <= HALF_SECONDS or overtime
    spread_home = -float(vegas_spread_home or 0.0)  # nflverse sign: positive = home favoured
    common = dict(
        home_score=home_score, away_score=away_score, game_seconds_remaining=gsr, home_timeouts=home_timeouts,
        away_timeouts=away_timeouts, vegas_spread_home=vegas_spread_home, receive_2h_ko_home=receive_2h_ko_home,
        half_seconds_remaining=half_seconds_remaining, model=mdl, monotone=monotone, overtime=overtime, season=season, rules=rules,
    )
    cls = (play_class or "").lower()
    if cls == "try" and possession in ("home", "away"):
        return try_state_wp(scorer=possession, **common)
    if cls in ("kickoff", "kickoff_pending") and possession in ("home", "away"):
        return kickoff_state_wp(kicker=_other(possession), p_onside=p_onside, **common)

    def posteam_wp(is_home: bool, receive: int, sd: float, dn: float, dist: float, yl: float) -> float:
        feats = posteam_features(
            score_differential=sd,
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
        return mdl.predict_posteam_wp(feats)

    def perspective(is_home: bool, ko_home: Optional[bool], dn: float, dist: float, yl: float) -> float:
        """P(home wins) with the given team as posteam and 2H-kickoff assignment."""
        if second_half or ko_home is None:
            receive = 0
        else:
            receive = 1 if (ko_home == is_home) else 0
        sd = (home_score - away_score) if is_home else (away_score - home_score)
        wp = posteam_wp(is_home, receive, sd, dn, dist, yl)
        if monotone and sd != 0:
            # Running extremum from the tie towards the actual margin (see docstring).
            for smaller in range(0, int(sd), 1 if sd > 0 else -1):  # integer margins between tie and |sd|
                v = posteam_wp(is_home, receive, smaller, dn, dist, yl)
                wp = max(wp, v) if sd > 0 else min(wp, v)
        return wp if is_home else 1.0 - wp

    ko_options: list[Optional[bool]] = [True, False] if (receive_2h_ko_home is None and not second_half) else [receive_2h_ko_home]
    neutral_yl = neutral_yardline(season, tbl)

    if possession in ("home", "away"):
        is_home = possession == "home"
        dn = 1 if down is None or down <= 0 else down
        dist = 10 if distance is None else distance
        yl = neutral_yl if yardline_100 is None else yardline_100
        vals = [perspective(is_home, ko, dn, dist, yl) for ko in ko_options]
        p = sum(vals) / len(vals)
        if kneel_floor_enabled(tbl):
            # Pass the raw ``down`` (not the defaulted ``dn``): an unknown down must never earn the floor.
            leader_sd = margin if is_home else -margin
            p_pos = p if is_home else 1.0 - p
            p_pos = kneel_out_wp(p_pos, leader_margin=leader_sd, down=down, game_seconds_remaining=gsr, defteam_timeouts=away_timeouts if is_home else home_timeouts, second_half=second_half, rules=tbl)
            p = p_pos if is_home else 1.0 - p_pos
    else:
        # Neutral / dead-ball state: 1st & 10 from the era start for each team, averaged.
        vals = []
        for ko in ko_options:
            vals.append(perspective(True, ko, 1, 10, neutral_yl))
            vals.append(perspective(False, ko, 1, 10, neutral_yl))
        p = sum(vals) / len(vals)
    if overtime:
        clamp = float(tbl["overtime"].get("clamp", 1e-4))
        p = min(max(p, clamp), 1.0 - clamp)
    return p


def kneel_out_wp(model_wp: float, *, leader_margin: float, down: float, game_seconds_remaining: float, defteam_timeouts: float, second_half: bool = True, rules: Optional[dict[str, Any]] = None) -> float:
    """Provisional kneel-out floor for the possession team (``model_wp`` is *its* win probability).

    Applies when the posteam leads, it is 1st down, the two-minute warning has passed and
    ``gsr <= seconds_per_kneel * (3 - defteam_timeouts)`` — i.e. three kneels (about 40 s
    each with the clock running) drain the clock even after every remaining defensive
    timeout. Returns ``max(model_wp, floor)``; anything else returns ``model_wp``. The
    flag lives in the rules table (``kneel_floor.enabled``) — see ``kneel_floor_enabled``.
    """
    tbl = _rules(rules)["kneel_floor"]
    if leader_margin <= 0 or down is None or int(down) != 1 or not second_half:
        return model_wp
    gsr = float(game_seconds_remaining)
    if gsr > float(tbl.get("two_minute_warning", 120)):
        return model_wp
    to = max(0.0, min(3.0, float(defteam_timeouts if defteam_timeouts is not None else 3)))
    if gsr > float(tbl.get("seconds_per_kneel", 40)) * (3.0 - to):
        return model_wp
    return max(model_wp, float(tbl.get("floor", 0.995)))


def kickoff_state_wp(
    *,
    home_score: float,
    away_score: float,
    game_seconds_remaining: float,
    kicker: str,
    home_timeouts: float = 3,
    away_timeouts: float = 3,
    vegas_spread_home: float = 0.0,
    receive_2h_ko_home: Optional[bool] = None,
    half_seconds_remaining: Optional[float] = None,
    model: Optional[WinProbModel] = None,
    monotone: bool = True,
    overtime: bool = False,
    season: Optional[int] = None,
    p_onside: Optional[float] = None,
    rules: Optional[dict[str, Any]] = None,
) -> float:
    """P(home wins) with a kickoff pending: ``kicker`` (``"home"``/``"away"``) is about to kick.

    ``p_onside * WP(kicker 1st & 10 at its own 45) + (1 - p_onside) * WP(receiver 1st & 10
    at the era neutral yardline)``. ``p_onside=None`` takes the table policy: 0 unless the
    kicker trails by at most ``onside_max_deficit`` with fewer than ``onside_late_gsr``
    seconds left, then ``p_onside_late_trailing`` (provisional 0.06). The receiver is the
    other team; the score is the score *after* any try, so callers pass the post-try score.
    """
    tbl = _rules(rules)
    ko = tbl["kickoff"]
    if kicker not in ("home", "away"):
        raise ValueError(f"kicker must be 'home' or 'away', got {kicker!r}")
    receiver = _other(kicker)
    kicker_margin = (home_score - away_score) if kicker == "home" else (away_score - home_score)
    if p_onside is None:
        trailing = -kicker_margin
        late = float(game_seconds_remaining) < float(ko.get("onside_late_gsr", 300))
        p_onside = float(ko.get("p_onside_late_trailing", 0.06)) if (late and 0 < trailing <= float(ko.get("onside_max_deficit", 16))) else float(ko.get("p_onside_default", 0.0))
    p_onside = min(max(float(p_onside), 0.0), 1.0)
    common = dict(
        home_score=home_score, away_score=away_score, game_seconds_remaining=game_seconds_remaining, down=1, distance=10,
        home_timeouts=home_timeouts, away_timeouts=away_timeouts, vegas_spread_home=vegas_spread_home, receive_2h_ko_home=receive_2h_ko_home,
        half_seconds_remaining=half_seconds_remaining, model=model, monotone=monotone, overtime=overtime, season=season, rules=rules,
    )
    p_recv = home_win_probability(possession=receiver, yardline_100=neutral_yardline(season, tbl), **common)
    if p_onside <= 0.0:
        return p_recv
    p_kick = home_win_probability(possession=kicker, yardline_100=float(ko.get("onside_recovery_yardline_100", 55)), **common)
    return p_onside * p_kick + (1.0 - p_onside) * p_recv


def two_point_attempt(scorer_margin: float, game_seconds_remaining: float, rules: Optional[dict[str, Any]] = None) -> bool:
    """Whether the conventional chart says go for two: ``scorer_margin`` is the scoring
    team's lead after the touchdown (before the try); the chart applies only with
    ``two_point_from_gsr`` seconds or fewer left (earlier tries are PATs)."""
    tbl = _rules(rules)["try"]
    if float(game_seconds_remaining) > float(tbl.get("two_point_from_gsr", 900)):
        return False
    return int(scorer_margin) in {int(m) for m in tbl.get("two_point_margins", [])}


def try_state_wp(
    *,
    home_score: float,
    away_score: float,
    game_seconds_remaining: float,
    scorer: str,
    home_timeouts: float = 3,
    away_timeouts: float = 3,
    vegas_spread_home: float = 0.0,
    receive_2h_ko_home: Optional[bool] = None,
    half_seconds_remaining: Optional[float] = None,
    model: Optional[WinProbModel] = None,
    monotone: bool = True,
    overtime: bool = False,
    season: Optional[int] = None,
    rules: Optional[dict[str, Any]] = None,
) -> float:
    """P(home wins) on a try (the untimed down after ``scorer``'s touchdown, TD already in
    the score): ``p_conv * WP(score + points, kickoff pending) + (1 - p_conv) * WP(score,
    kickoff pending)`` with the scorer kicking off next. PAT unless ``two_point_attempt``.
    """
    tbl = _rules(rules)
    tr = tbl["try"]
    if scorer not in ("home", "away"):
        raise ValueError(f"scorer must be 'home' or 'away', got {scorer!r}")
    scorer_margin = (home_score - away_score) if scorer == "home" else (away_score - home_score)
    two = two_point_attempt(scorer_margin, game_seconds_remaining, tbl)
    p_conv = float(tr.get("p_two_point", 0.48)) if two else float(tr.get("p_pat", 0.94))
    points = 2 if two else 1
    common = dict(
        game_seconds_remaining=game_seconds_remaining, kicker=scorer, home_timeouts=home_timeouts, away_timeouts=away_timeouts,
        vegas_spread_home=vegas_spread_home, receive_2h_ko_home=receive_2h_ko_home, half_seconds_remaining=half_seconds_remaining,
        model=model, monotone=monotone, overtime=overtime, season=season, rules=rules,
    )
    hs_good = home_score + points if scorer == "home" else home_score
    as_good = away_score + points if scorer == "away" else away_score
    p_good = kickoff_state_wp(home_score=hs_good, away_score=as_good, **common)
    p_miss = kickoff_state_wp(home_score=home_score, away_score=away_score, **common)
    return p_conv * p_good + (1.0 - p_conv) * p_miss


# -- pre-game spread inversion -----------------------------------------------------
_SPREAD_TABLES: dict[tuple[int, Optional[int]], dict[str, Any]] = {}


def pregame_home_probability(vegas_spread_home: float, model: Optional[WinProbModel] = None, season: Optional[int] = None, rules: Optional[dict[str, Any]] = None) -> float:
    """P(home) for the neutral pre-game state (0-0, 3600 s, coin toss unknown) at a spread."""
    return home_win_probability(home_score=0, away_score=0, game_seconds_remaining=REGULATION_SECONDS, vegas_spread_home=vegas_spread_home, model=model, season=season, rules=rules)


def pregame_spread_table(model: Optional[WinProbModel] = None, season: Optional[int] = None, rules: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Cached P(home) at every ``step`` spread in ``[lo, hi]`` plus a monotonicity check.

    The residual trees are unconstrained in ``spread_time``, so the pre-game curve is
    checked for being non-increasing in the ESPN-sign spread; ``monotone`` reports the
    result and ``envelope`` is the running-min curve the inversion actually uses, so a
    local wiggle can never make the bisection land on the wrong branch.
    """
    mdl = model or default_model()
    key = (id(mdl), season)
    cached = _SPREAD_TABLES.get(key) if rules is None else None  # overrides bypass the cache
    if cached is not None and cached.get("_model") is mdl:
        return cached
    tbl = _rules(rules)["spread_inversion"]
    lo, hi, step = float(tbl.get("lo", -30)), float(tbl.get("hi", 30)), float(tbl.get("step", 0.5))
    n = int(round((hi - lo) / step))
    spreads = [round(lo + i * step, 6) for i in range(n + 1)]
    probs = [pregame_home_probability(s, mdl, season, rules) for s in spreads]
    monotone = all(b <= a + 1e-12 for a, b in zip(probs, probs[1:]))
    env: list[float] = []
    for p in probs:
        env.append(p if not env else min(env[-1], p))
    out = {"spreads": spreads, "probs": probs, "envelope": env, "monotone": monotone, "lo": lo, "hi": hi, "_model": mdl}
    if rules is None:
        _SPREAD_TABLES[key] = out
    return out


def spread_from_pregame_probability(p_home: float, model: Optional[WinProbModel] = None, season: Optional[int] = None, rules: Optional[dict[str, Any]] = None) -> float:
    """Invert the pre-game curve: the ESPN-sign home spread (negative = home favoured)
    whose neutral pre-game P(home) equals ``p_home``, clamped to the table's ``[lo, hi]``.

    Bisection runs on the piecewise-linear interpolant of the cached (envelope) table,
    not on the raw model: at half-point spreads around pick'em the trees wiggle by about
    1.5 points (P(-0.5) < P(0) < P(+0.5) in the shipped export), which would make a raw
    root ambiguous, while the integer grid is monotone and reproduces the docs/MODEL.md
    table exactly. Consumed by the backtest / live spread fallback chains when no
    sportsbook line is available: a market price stands in for the line.
    """
    mdl = model or default_model()
    tab = pregame_spread_table(mdl, season, rules)
    spreads, env = tab["spreads"], tab["envelope"]
    p = float(p_home)
    if p != p:  # NaN compares false everywhere and would bisect to the low bound (max home favourite)
        raise ValueError("p_home is NaN")
    p = min(max(p, 0.0), 1.0)
    if p >= env[0]:
        return tab["lo"]
    if p <= env[-1]:
        return tab["hi"]

    def interp(x: float) -> float:
        if x <= spreads[0]:
            return env[0]
        if x >= spreads[-1]:
            return env[-1]
        j = next(i for i in range(1, len(spreads)) if spreads[i] >= x)
        x0, x1, y0, y1 = spreads[j - 1], spreads[j], env[j - 1], env[j]
        return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)

    tol = float(_rules(rules)["spread_inversion"].get("tolerance", 1e-4))
    a, b = tab["lo"], tab["hi"]  # interp is non-increasing: interp(a) >= p >= interp(b)
    for _ in range(60):
        m = (a + b) / 2.0
        fm = interp(m)
        if abs(fm - p) <= tol or (b - a) < 1e-4:
            a = b = m
            break
        if fm > p:
            a = m
        else:
            b = m
    return round((a + b) / 2.0, 3)
