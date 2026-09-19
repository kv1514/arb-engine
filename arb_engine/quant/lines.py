"""Spread / total fair values from a margin-of-victory distribution.

Exchange mids are the only fair the engine had for lines, so a tail spread at 0.04 could not
be told from a stale ladder. This module prices every spread and total outcome from a
distribution of the *home margin* ``M = home - away`` (and of the total ``T``) on the
integer lattice:

* ``NormalMargin`` — N(mu, sigma) with the integer continuity correction, the tie cell
  replaced by an explicit tie mass (overtime makes NFL ties ~0.4%, far below the normal's
  ~3% lattice mass at 0; the tie mass never exceeds the raw cell, so a decided game keeps
  its ~0 tie chance), ``mu = -spread_home`` pre-game. In play
  ``mu = margin + (-spread_home) * frac_remaining`` and ``sd = sigma * sqrt(frac_remaining)``.
  A tie at 0:00 is *not* decided: ``game_phase`` maps it to an overtime state (one OT
  period's worth of remaining game, the tie mass conditional on overtime).
* ``EmpiricalMargin`` — P(M = k | closing spread) from ``data/nfl_margin_dist.json``,
  shrunk to the normal with weight ``n / (n + n0)``. Margins pile up on key numbers (3 is
  ~15% of all NFL games, 7 ~9%), which a normal alone misprices around half-point moves.
* ``NormalTotal`` — N(total_line, sigma_total); in play the remaining points are a blend of
  the pre-game pace and the observed pace, and the lattice is clipped at the points so far.

``line_fair_for_event`` turns an ``EventInfo`` plus the moneyline / line consensus (and an
optional live game state) into a per-outcome fair with push handling per ``tie_rule`` and a
``ml_spread_gap`` flag when the moneyline-implied spread disagrees with the line consensus
by more than 2 points (one of the two is stale). A live game whose clock is unknown, a
final and a postponed game get no fair at all (flags ``clock_unknown`` / ``final`` /
``other``): pricing those from the closing line would silently ignore the score. ``middle_candidates`` prices middles
(both legs can win) as EV, explicitly *never* an arbitrage: a middle is a bet on the lattice.

Everything is stdlib and deterministic; the tables are built by ``scripts/build_margin_table.py``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Optional, Protocol

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
GAME_SECONDS = {"nfl": 3600, "ncaaf": 3600}
# One overtime period: NFL regular season 600 s. College OT is untimed alternating possessions;
# 600 s is a nominal "one more period" of scoring for the same scaling.
OVERTIME_SECONDS = {"nfl": 600, "ncaaf": 600}
# Unfitted fallbacks (no committed table for the sport). College margins are wider; the value
# is a literature prior, not a fit — replace by building a table for the sport.
FALLBACK_SIGMA = {"nfl": (12.7, 13.2, 0.0036), "ncaaf": (16.0, 16.5, 0.0)}
# P(tie | game reached overtime): NFL 2016-2025 10 of 155 OT games; college cannot tie.
FALLBACK_OT_TIE = {"nfl": 0.065, "ncaaf": 0.0}
DEFAULT_SHRINK_N0 = 50
ML_SPREAD_GAP_PTS = 2.0
# In play the remaining noise is sqrt(sigma^2 * frac + floor^2): pure Brownian scaling
# (floor 0) is far too confident late, because the last possessions add 3 or 7 points, not a
# smooth drift. Floors are one possession-ish. On the paired in-play rows of 2026 week 1
# (scripts/eval_lines.py, scored on the Kalshi market line) the spread log-loss went 0.632
# (floor 0) -> 0.616 (floor 3) and the total 0.718 -> 0.640 (floor 6); larger floors (6/10)
# do better still on that sample, but it is the same 16 games the fair is evaluated on, so
# the floors stay at the possession-sized prior until an out-of-sample week is scored. The
# pace blend (observed points-per-minute vs the pre-game rate) hurt at every floor on that
# sample, so its default weight is 0; pass ``pace_weight`` to use it.
SPREAD_INPLAY_SD_FLOOR = 3.0
TOTAL_INPLAY_SD_FLOOR = 6.0
PACE_WEIGHT_MAX = 0.0
MIN_SD = 0.05           # below this the distribution is a point mass (game decided)


@lru_cache(maxsize=8)
def load_margin_table(sport: str = "nfl") -> Optional[dict[str, Any]]:
    p = DATA_DIR / f"{sport}_margin_dist.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_sigma_table() -> Optional[dict[str, Any]]:
    p = DATA_DIR / "margin_sigma.json"
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def default_params(sport: str = "nfl") -> tuple[float, float, float]:
    """``(sigma_margin, sigma_total, tie_mass)`` for the sport: fitted table when it exists."""
    sig = load_sigma_table()
    if sig and sig.get("sport") == sport and (sig.get("margin") or {}).get("sigma"):
        return float(sig["margin"]["sigma"]), float((sig.get("total") or {}).get("sigma") or sig["margin"]["sigma"]), float(sig.get("tie_rate") or 0.0)
    return FALLBACK_SIGMA.get(sport, FALLBACK_SIGMA["nfl"])


def overtime_params(sport: str = "nfl") -> tuple[int, float]:
    """``(overtime_seconds, tie_mass_given_overtime)``: the fitted OT tie rate when the sigma
    table carries an ``overtime`` block, else the fallback."""
    secs = OVERTIME_SECONDS.get(sport, OVERTIME_SECONDS["nfl"])
    sig = load_sigma_table()
    ot = (sig or {}).get("overtime") if sig and sig.get("sport") == sport else None
    if ot and ot.get("tie_rate_given_ot") is not None:
        return secs, float(ot["tie_rate_given_ot"])
    return secs, FALLBACK_OT_TIE.get(sport, FALLBACK_OT_TIE["nfl"])


def _is_int(x: float) -> bool:
    return abs(float(x) - round(float(x))) < 1e-9


class MarginDistribution(Protocol):
    """A distribution on the integer lattice (home margin, or total points)."""

    def pmf(self, k: int) -> float: ...
    def p_gt(self, x: float) -> float: ...
    def p_lt(self, x: float) -> float: ...
    def p_eq(self, x: float) -> float: ...


class LatticeDist:
    """Concrete base: a normalised pmf over integers with the line queries every market needs."""

    source = "lattice"

    def __init__(self, pmf: Mapping[int, float], source: Optional[str] = None, meta: Optional[dict[str, Any]] = None):
        total = sum(v for v in pmf.values() if v > 0)
        if total <= 0:
            raise ValueError("pmf has no mass")
        self._pmf = {int(k): v / total for k, v in pmf.items() if v > 0}
        self.source = source or self.source
        self.meta = dict(meta or {})

    def pmf(self, k: int) -> float:
        return self._pmf.get(int(k), 0.0)

    def support(self) -> list[int]:
        return sorted(self._pmf)

    def items(self) -> list[tuple[int, float]]:
        return sorted(self._pmf.items())

    def p_gt(self, x: float) -> float:
        return sum(v for k, v in self._pmf.items() if k > x)

    def p_lt(self, x: float) -> float:
        return sum(v for k, v in self._pmf.items() if k < x)

    def p_eq(self, x: float) -> float:
        return self._pmf.get(int(round(x)), 0.0) if _is_int(x) else 0.0

    def mean(self) -> float:
        return sum(k * v for k, v in self._pmf.items())

    def sd(self) -> float:
        m = self.mean()
        return math.sqrt(max(0.0, sum((k - m) ** 2 * v for k, v in self._pmf.items())))

    # -- moneyline / spread queries (home orientation) ---------------------------------------
    def p_win(self) -> float:
        """P(home wins outright) = P(M > 0); ties excluded (they are ``p_tie``)."""
        return self.p_gt(0)

    def p_tie(self) -> float:
        return self.p_eq(0)

    def p_win_decisive(self) -> float:
        """P(home wins | no tie): the moneyline probability under a tie-void rule."""
        t = self.p_tie()
        return self.p_win() / (1.0 - t) if t < 1.0 else 0.5

    def p_cover(self, spread_home: float) -> tuple[float, float, float]:
        """``(cover, push, lose)`` for the home side at home spread ``spread_home``
        (negative = home favoured): home covers when ``M + spread_home > 0``."""
        line = -float(spread_home)
        return self.p_gt(line), self.p_eq(line), self.p_lt(line)

    def p_over(self, line: float) -> tuple[float, float, float]:
        """``(over, push, under)`` at ``line`` when the lattice is a total."""
        return self.p_gt(line), self.p_eq(line), self.p_lt(line)

    def p_push(self, line: float) -> float:
        return self.p_eq(line)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(source={self.source}, mean={self.mean():.2f}, sd={self.sd():.2f})"


def normal_lattice(mu: float, sd: float, tie_mass: Optional[float] = None, span: float = 8.0, floor: Optional[int] = None) -> dict[int, float]:
    """Continuity-corrected N(mu, sd) on the integers: P(k) = Phi(k+0.5) - Phi(k-0.5).

    ``tie_mass`` replaces the cell at 0 (rest rescaled) but never *raises* it: the fitted tie
    rate is an unconditional average, and a game with almost no lattice mass at 0 (a 21-point
    lead with six minutes left) cannot tie at the average rate. ``floor`` drops cells below it
    (totals cannot fall below the points already scored)."""
    mu, sd = float(mu), float(sd)
    if sd < MIN_SD:
        k0 = int(round(mu))
        if floor is not None:
            k0 = max(k0, floor)
        return {k0: 1.0}
    nd = NormalDist(mu, sd)
    lo, hi = int(math.floor(mu - span * sd)), int(math.ceil(mu + span * sd))
    if floor is not None:
        lo = max(lo, floor)
    out: dict[int, float] = {}
    prev = nd.cdf(lo - 0.5)
    for k in range(lo, hi + 1):
        cur = nd.cdf(k + 0.5)
        out[k] = max(0.0, cur - prev)
        prev = cur
    if floor is not None and lo in out:
        out[lo] += nd.cdf(lo - 0.5)  # mass below the floor collapses onto it
    if tie_mass is not None and 0 in out:
        tie = min(float(tie_mass), out[0])
        rest = sum(v for k, v in out.items() if k != 0)
        if rest > 0:
            scale = (1.0 - tie) / rest
            out = {k: (tie if k == 0 else v * scale) for k, v in out.items()}
    return out


class NormalMargin(LatticeDist):
    """Home margin ~ N(mu, sigma) on the lattice with an explicit tie mass."""

    source = "normal"

    def __init__(self, mu: float, sigma: float, tie_mass: Optional[float] = None, meta: Optional[dict[str, Any]] = None):
        self.mu, self.sigma, self.tie_mass = float(mu), float(sigma), tie_mass
        super().__init__(normal_lattice(mu, sigma, tie_mass), meta={"mu": float(mu), "sd": float(sigma), **(meta or {})})

    @classmethod
    def from_spread(cls, spread_home: float, sigma: Optional[float] = None, tie_mass: Optional[float] = None, sport: str = "nfl") -> "NormalMargin":
        s, _, t = default_params(sport)
        return cls(-float(spread_home), sigma if sigma is not None else s, t if tie_mass is None else tie_mass)

    @classmethod
    def in_play(cls, margin_home: float, spread_home: float, frac_remaining: float, sigma: Optional[float] = None, tie_mass: Optional[float] = None, sport: str = "nfl", sd_floor: Optional[float] = None) -> "NormalMargin":
        """Remaining game is a fresh draw scaled by the fraction left: the pre-game edge
        ``-spread_home`` accrues linearly and the noise as ``sqrt(sigma^2 frac + floor^2)``
        (the floor is a possession's worth of lumpiness; 0 at the final whistle)."""
        s, _, t = default_params(sport)
        frac = min(1.0, max(0.0, float(frac_remaining)))
        floor = SPREAD_INPLAY_SD_FLOOR if sd_floor is None else float(sd_floor)
        sd = math.sqrt((sigma if sigma is not None else s) ** 2 * frac + (floor ** 2 if frac > 0 else 0.0))
        mu = float(margin_home) + (-float(spread_home)) * frac
        return cls(mu, sd, (t if tie_mass is None else tie_mass) if sd >= MIN_SD else None, meta={"frac_remaining": frac, "margin": float(margin_home)})


class EmpiricalMargin(LatticeDist):
    """P(M = k | closing spread) from the margin table, shrunk to ``NormalMargin``.

    The table is in favourite orientation; it is flipped to the home orientation here so
    every query on the result is home-relative like ``NormalMargin``."""

    source = "empirical"

    def __init__(self, pmf: Mapping[int, float], n: int, weight: float, bucket: Optional[str], normal: NormalMargin):
        self.n, self.weight, self.bucket, self.normal = n, weight, bucket, normal
        super().__init__(pmf, meta={"mu": normal.mu, "sd": normal.sigma, "n": n, "weight": weight, "bucket": bucket})

    @classmethod
    def from_table(cls, table: Optional[Mapping[str, Any]], spread_home: float, sigma: Optional[float] = None, tie_mass: Optional[float] = None, n0: Optional[int] = None, sport: str = "nfl") -> "EmpiricalMargin":
        from .margintable import bucket_key

        normal = NormalMargin.from_spread(spread_home, sigma, tie_mass, sport)
        table = table if table is not None else load_margin_table(sport)
        bucket = bucket_key(spread_home)
        block = ((table or {}).get("buckets") or {}).get(bucket)
        n = int(block["n"]) if block else 0
        k0 = n0 if n0 is not None else int((table or {}).get("shrink_n0") or DEFAULT_SHRINK_N0)
        w = n / (n + k0) if n else 0.0
        flip = float(spread_home) > 0  # away favoured: favourite margin = -home margin
        pmf: dict[int, float] = {k: (1.0 - w) * v for k, v in normal.items()}
        if block and n:
            for ks, cnt in block["pmf"].items():
                k = -int(ks) if flip else int(ks)
                pmf[k] = pmf.get(k, 0.0) + w * cnt / n
        return cls(pmf, n, w, bucket if block else None, normal)


class NormalTotal(LatticeDist):
    """Total points ~ N(mu, sigma) on the non-negative lattice (clipped at points so far)."""

    source = "normal_total"

    def __init__(self, mu: float, sigma: float, floor: int = 0, meta: Optional[dict[str, Any]] = None):
        self.mu, self.sigma = float(mu), float(sigma)
        super().__init__(normal_lattice(mu, sigma, None, floor=floor), meta={"mu": float(mu), "sd": float(sigma), **(meta or {})})

    @classmethod
    def from_line(cls, total_line: float, sigma: Optional[float] = None, sport: str = "nfl") -> "NormalTotal":
        _, st, _ = default_params(sport)
        return cls(float(total_line), sigma if sigma is not None else st)

    @classmethod
    def in_play(cls, points_so_far: float, total_line: float, frac_remaining: float, sigma: Optional[float] = None, sport: str = "nfl", sd_floor: Optional[float] = None, pace_weight: Optional[float] = None) -> "NormalTotal":
        """Remaining points = blend of the pre-game rate (``total_line * frac``) and the observed
        pace, the pace weight growing with the share of the game played up to ``pace_weight``
        (default ``PACE_WEIGHT_MAX``, 0 = pre-game rate only); noise ``sqrt(sigma^2 frac + floor^2)``."""
        _, st, _ = default_params(sport)
        frac = min(1.0, max(0.0, float(frac_remaining)))
        played = 1.0 - frac
        pregame = float(total_line) * frac
        pace = (float(points_so_far) / played) * frac if played > 1e-9 else pregame
        w = (PACE_WEIGHT_MAX if pace_weight is None else float(pace_weight)) * played
        mu = float(points_so_far) + (1.0 - w) * pregame + w * pace
        floor = TOTAL_INPLAY_SD_FLOOR if sd_floor is None else float(sd_floor)
        sd = math.sqrt((sigma if sigma is not None else st) ** 2 * frac + (floor ** 2 if frac > 0 else 0.0))
        return cls(mu, sd, floor=int(round(points_so_far)), meta={"frac_remaining": frac, "points": float(points_so_far), "pace_weight": w})


def spread_from_p(p_home: float, sigma: Optional[float] = None, tie_mass: Optional[float] = None, sport: str = "nfl", tol: float = 0.005) -> float:
    """Home spread (negative = home favoured) whose ``NormalMargin.p_win()`` equals ``p_home``.

    ``p_home`` is clamped to what the bisection bounds can reach (``p_win`` tops out below
    ``1 - tie_mass``), so a 0.997 consensus inverts to a real heavy-favourite spread instead
    of running to the bound and flagging a spurious gap."""
    lo, hi = -60.0, 60.0  # spread; p_win decreases as the home spread rises
    p_hi, p_lo = NormalMargin.from_spread(lo, sigma, tie_mass, sport).p_win(), NormalMargin.from_spread(hi, sigma, tie_mass, sport).p_win()
    p = min(min(0.999, p_hi - 1e-9), max(max(0.001, p_lo + 1e-9), float(p_home)))
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if NormalMargin.from_spread(mid, sigma, tie_mass, sport).p_win() > p:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 2)


def game_phase(state: Any, sport: str = "nfl") -> tuple[str, Optional[float]]:
    """``(phase, frac_remaining)`` from a ``GameState``-like object.

    ``pre`` (None state or status 'pre'), ``live`` (clock known; 0 when decided), ``overtime``
    (clock at 0 with the score tied: regulation — or a playoff OT period — ended level, so one
    more period is coming and the game is *not* decided; frac is that period's share of
    regulation), ``clock_unknown`` (live but no parsable clock), ``final`` and ``other``
    (postponed / cancelled). Only ``live`` and ``overtime`` carry a frac."""
    status = getattr(state, "status", None) if state is not None else "pre"
    if state is None or status == "pre":
        return "pre", None
    if status == "final":
        return "final", None
    if status != "live":
        return "other", None
    gsr = getattr(state, "game_seconds_remaining", None)
    if gsr is None:
        return "clock_unknown", None
    reg = GAME_SECONDS.get(sport, 3600)
    if float(gsr) <= 0 and getattr(state, "home_score", 0) == getattr(state, "away_score", 0):
        return "overtime", OVERTIME_SECONDS.get(sport, OVERTIME_SECONDS["nfl"]) / reg
    return "live", min(1.0, max(0.0, float(gsr) / reg))


def frac_remaining_from_state(state: Any, sport: str = "nfl") -> Optional[float]:
    """Fraction of regulation left while the game is live (an overtime-pending tie counts as one
    OT period), None pre-game, when the clock is unknown, final or postponed."""
    phase, frac = game_phase(state, sport)
    return frac if phase in ("live", "overtime") else None


# ---------------------------------------------------------------------------------------------
# Per-event fair
# ---------------------------------------------------------------------------------------------

@dataclass
class LineFair:
    event_key: str
    market_type: str
    fair: dict[str, Optional[float]] = field(default_factory=dict)
    push: float = 0.0
    mu: Optional[float] = None
    sd: Optional[float] = None
    source: str = ""
    flags: list[str] = field(default_factory=list)
    line: Optional[float] = None
    spread_home: Optional[float] = None
    ml_spread: Optional[float] = None
    ml_spread_gap: Optional[float] = None
    tie_rule: str = "unknown"
    in_play: bool = False
    frac_remaining: Optional[float] = None

    def as_dict(self) -> dict[str, Any]:
        return {"event_key": self.event_key, "market_type": self.market_type, "fair": dict(self.fair), "push": round(self.push, 6), "mu": self.mu, "sd": self.sd, "source": self.source, "flags": list(self.flags), "line": self.line, "spread_home": self.spread_home, "ml_spread": self.ml_spread, "ml_spread_gap": self.ml_spread_gap, "tie_rule": self.tie_rule, "in_play": self.in_play, "frac_remaining": self.frac_remaining}


def _apply_tie_rule(p_yes: float, push: float, tie_rule: str) -> tuple[float, float]:
    """YES / NO fair for a binary line under the venue's push rule.

    ``half``: each side pays $0.50 on a push. ``void``: stakes back, so the fair is
    conditional on no push. Anything else (``push_possible``, ``no_push``, ``both_no``,
    ``unknown``): a push loses for YES — Kalshi's "wins by over X" and Rothera's floor lines
    resolve NO on exactly X — so the push mass sits on NO."""
    if tie_rule == "half":
        y = p_yes + 0.5 * push
        return y, 1.0 - y
    if tie_rule == "void":
        y = p_yes / (1.0 - push) if push < 1.0 else 0.5
        return y, 1.0 - y
    return p_yes, 1.0 - p_yes


def parse_spread_key(event_key: str) -> Optional[tuple[str, float]]:
    """``nfl:BUF|DET:2026-09-17:spread:BUF-1.5`` -> ('BUF', 1.5)."""
    if ":spread:" not in event_key:
        return None
    tail = event_key.rsplit(":spread:", 1)[1]
    if "-" not in tail:
        return None
    fav, line = tail.rsplit("-", 1)
    try:
        return fav, float(line)
    except ValueError:
        return None


def line_fair_for_event(
    event: Any,
    moneyline_p: Optional[float],
    spread_home: Optional[float],
    total: Optional[float],
    state: Any = None,
    *,
    home: Optional[str] = None,
    sigma: Optional[float] = None,
    table: Optional[Mapping[str, Any]] = None,
    empirical: bool = True,
    gap_pts: float = ML_SPREAD_GAP_PTS,
) -> LineFair:
    """Fair per outcome of a spread / total / moneyline ``EventInfo``.

    ``moneyline_p`` is P(home) from the moneyline consensus, ``spread_home`` the line
    consensus (negative = home favoured), ``total`` the total consensus. ``state`` is a live
    ``GameState`` (score + seconds remaining) or None pre-game. ``home`` orients the spread
    outcomes (falls back to ``state.home``); without it a spread event cannot be priced."""
    sport = getattr(event, "sport", "nfl") or "nfl"
    mtype = getattr(event, "market_type", "moneyline")
    tie_rule = getattr(event, "tie_rule", "unknown") or "unknown"
    res = LineFair(event_key=getattr(event, "event_key", ""), market_type=mtype, line=getattr(event, "line", None), tie_rule=tie_rule)
    home = home or getattr(state, "home", None)
    phase, frac = game_phase(state, sport)
    res.in_play, res.frac_remaining = frac is not None, frac
    if phase in ("clock_unknown", "final", "other"):
        # No fair rather than a wrong one: the closing line without the score is not a price.
        res.flags.append(phase)
        res.fair = {o: None for o in getattr(event, "outcomes", [])}
        return res
    overtime = phase == "overtime"
    if overtime:
        res.flags.append("overtime")
    ot_tie = overtime_params(sport)[1] if overtime else None

    ml_spread = spread_from_p(moneyline_p, sigma, sport=sport) if (moneyline_p is not None and frac is None) else None
    res.ml_spread = ml_spread
    if spread_home is None and ml_spread is not None:
        spread_home = ml_spread
        res.flags.append("spread_from_moneyline")
    elif spread_home is not None and ml_spread is not None:
        res.ml_spread_gap = round(ml_spread - spread_home, 2)
        if abs(res.ml_spread_gap) > gap_pts:
            res.flags.append("ml_spread_gap")
    res.spread_home = spread_home

    if mtype in ("spread", "moneyline"):
        if spread_home is None:
            res.flags.append("no_spread")
            res.fair = {o: None for o in getattr(event, "outcomes", [])}
            return res
        if frac is not None:
            dist: LatticeDist = NormalMargin.in_play(getattr(state, "home_score", 0) - getattr(state, "away_score", 0), spread_home, frac, sigma, tie_mass=ot_tie, sport=sport)
        elif empirical:
            dist = EmpiricalMargin.from_table(table, spread_home, sigma, sport=sport)
        else:
            dist = NormalMargin.from_spread(spread_home, sigma, sport=sport)
        res.source, res.mu, res.sd = dist.source, round(dist.mean(), 4), round(dist.sd(), 4)
        outcomes = list(getattr(event, "outcomes", []))
        if mtype == "moneyline":
            if not home or home not in outcomes or len(outcomes) != 2:
                res.flags.append("home_unknown")
                res.fair = {o: None for o in outcomes}
                return res
            away = [o for o in outcomes if o != home][0]
            p_home, tie = dist.p_win(), dist.p_tie()
            yes, no = _apply_tie_rule(p_home, tie, tie_rule)
            res.push = tie
            res.fair = {home: yes, away: (no if tie_rule in ("half", "void") else dist.p_lt(0))}
            return res
        parsed = parse_spread_key(res.event_key)
        if not parsed or not home:
            res.flags.append("home_unknown" if parsed else "bad_spread_key")
            res.fair = {o: None for o in outcomes}
            return res
        fav, line = parsed
        res.line = line
        if fav == home:
            cover, push, lose = dist.p_gt(line), dist.p_eq(line), dist.p_lt(line)
        else:
            cover, push, lose = dist.p_lt(-line), dist.p_eq(-line), dist.p_gt(-line)
        yes, no = _apply_tie_rule(cover, push, tie_rule)
        res.push = push
        yes_key = next((o for o in outcomes if o.startswith(f"{fav}-")), outcomes[0] if outcomes else f"{fav}-{line:g}")
        no_key = next((o for o in outcomes if o != yes_key), None)
        res.fair = {yes_key: yes}
        if no_key:
            res.fair[no_key] = no
        return res

    if mtype == "total":
        line = res.line
        if line is None:
            res.flags.append("no_line")
            return res
        if total is None:
            res.flags.append("no_total")
            res.fair = {o: None for o in getattr(event, "outcomes", ["over", "under"])}
            return res
        if frac is not None:
            tdist = NormalTotal.in_play(getattr(state, "home_score", 0) + getattr(state, "away_score", 0), total, frac, sigma, sport=sport)
        else:
            tdist = NormalTotal.from_line(total, sigma, sport=sport)
        res.source, res.mu, res.sd = tdist.source, round(tdist.mean(), 4), round(tdist.sd(), 4)
        over, push, under = tdist.p_over(line)
        yes, no = _apply_tie_rule(over, push, tie_rule)
        res.push = push
        res.fair = {"over": yes, "under": no}
        return res

    res.flags.append("unsupported_market_type")
    return res


# ---------------------------------------------------------------------------------------------
# Middles (never an arb)
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LineLeg:
    """One side of a line: ``side`` in fav/dog (spread, favourite orientation) or over/under."""

    side: str
    line: float
    price: float
    tie_rule: str = "push_possible"
    venue: str = ""
    outcome: str = ""


@dataclass
class Middle:
    legs: tuple[LineLeg, LineLeg]
    cost: float
    ev: float
    p_middle: float      # both legs win
    p_one: float         # exactly one wins (lose the vig)
    p_push_any: float
    never_an_arb: bool = True
    note: str = "middle: EV depends on the lattice, both legs can lose value on a push; never an arbitrage"


def _leg_payoff(leg: LineLeg, v: int) -> float:
    if leg.side in ("fav", "over"):
        win, push = v > leg.line, _is_int(leg.line) and v == round(leg.line)
    elif leg.side in ("dog", "under"):
        win, push = v < leg.line, _is_int(leg.line) and v == round(leg.line)
    else:
        raise ValueError(f"unknown leg side {leg.side!r}")
    if win:
        return 1.0
    if push:
        return 0.5 if leg.tie_rule == "half" else (leg.price if leg.tie_rule == "void" else 0.0)
    return 0.0


def middle_ev(dist: LatticeDist, a: LineLeg, b: LineLeg) -> Middle:
    """Expected payout minus cost of holding both legs, over the lattice ``dist`` (favourite
    orientation for spreads, points for totals)."""
    cost = float(a.price) + float(b.price)
    ev = -cost
    p_mid = p_one = p_push = 0.0
    for k, p in dist.items():
        pa, pb = _leg_payoff(a, k), _leg_payoff(b, k)
        ev += p * (pa + pb)
        wa, wb = pa >= 1.0, pb >= 1.0
        if wa and wb:
            p_mid += p
        elif wa or wb:
            p_one += p
        if (_is_int(a.line) and k == round(a.line)) or (_is_int(b.line) and k == round(b.line)):
            p_push += p
    return Middle(legs=(a, b), cost=cost, ev=ev, p_middle=p_mid, p_one=p_one, p_push_any=p_push)


def middle_candidates(pairs: Iterable[tuple[LineLeg, LineLeg]], dist: LatticeDist, min_ev: float = 0.0) -> list[Middle]:
    """Price each ``(leg, leg)`` pair and return the positive-EV ones, best first."""
    out = [middle_ev(dist, a, b) for a, b in pairs]
    return sorted([m for m in out if m.ev > min_ev], key=lambda m: -m.ev)


# Settings (declared through P01's registry when present; the library works without it).
try:  # pragma: no cover - depends on P01
    from ..config import declare_setting as _declare_setting
except Exception:  # noqa: BLE001
    _declare_setting = None
if _declare_setting is not None:  # pragma: no cover
    try:
        _declare_setting("line_fair", env="ARB_LINE_FAIR", default=False, cast=bool, doc="opt-in: feed the margin-model line fair (quant.lines) into the consensus as line_prior")
        _declare_setting("line_sigma", env="ARB_LINE_SIGMA", default=None, cast=float, doc="override the margin sigma used by quant.lines (default: data/margin_sigma.json)")
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "MarginDistribution", "LatticeDist", "NormalMargin", "EmpiricalMargin", "NormalTotal", "normal_lattice",
    "spread_from_p", "frac_remaining_from_state", "game_phase", "overtime_params", "LineFair", "line_fair_for_event", "parse_spread_key",
    "LineLeg", "Middle", "middle_ev", "middle_candidates", "load_margin_table", "load_sigma_table", "default_params",
    "ML_SPREAD_GAP_PTS", "GAME_SECONDS", "OVERTIME_SECONDS", "SPREAD_INPLAY_SD_FLOOR", "TOTAL_INPLAY_SD_FLOOR", "PACE_WEIGHT_MAX",
]
