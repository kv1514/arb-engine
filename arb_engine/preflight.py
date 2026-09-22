"""Readiness report before a live slate: ``python -m arb_engine preflight``.

Every check is a pure function of the clients it is handed and returns one :class:`Check`
(PASS / WARN / FAIL + a one-line detail + latency + structured ``data``), so the tests run
the whole report on ``FakeHttp`` fixtures and a fake extension checker, and the CLI handler
(``cli_plugins/preflight_flags.py``) is the only place that builds real clients. The
Sunday launcher (``scripts/sunday.sh``) runs the report first and refuses to start on a FAIL.

Why the checks are what they are (each one is a failure mode seen on a live evening):

* **imports** — a module that only imports under a third-party package passes the suite on
  the dev box and takes the bridge down at kickoff; ``sys.stdlib_module_names`` catches it.
  The walk runs in a fresh interpreter (``python -I -c``): by the time the handler runs,
  ``cli.build_parser`` has already imported most of the package, so an in-process
  "what is new in ``sys.modules``" diff would miss anything those modules pulled in.
* **wp-model** — the model is a JSON export read lazily; a corrupt file surfaces on the first
  in-play tick, not at start-up. Pricing a known state at start-up moves that forward.
* **settings / executable venues** — Polymarket is signal-only for a US account; an
  ``EXECUTABLE_VENUES`` left over from an experiment silently turns it into a leg.
* **venues + latency** — a truncating proxy (``ARB_HTTP_TRANSPORT=curl``), a Kalshi 403 or a
  30 MB Robinhood page that takes a minute show up here instead of as an empty slate.
* **espn / matches** — the slate scanner keys games by ESPN's event key; a game the venues
  spell differently is silently ``missing`` in ``live``. Counting matches per venue *before*
  kickoff is the only time there is room to fix the team table.
* **bridge** — the overlay talks to a bridge process that loaded the engine (and its
  executable venue set) once; a stale one prices with yesterday's code.
* **out/ + disk, extension** — the recorder and journals write under ``out/``; the overlay
  is loaded unpacked, so a broken manifest is a red card in Chrome, not a Python error.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_RANK = {PASS: 0, WARN: 1, FAIL: 2}
MIN_PYTHON = (3, 10)        # CI runs the suite on 3.10-3.13; 3.13 is what the author uses
RECOMMENDED_PYTHON = (3, 13)
OPTIONAL_THIRD_PARTY = {"cryptography"}  # Kalshi request signing; the engine imports without it
DISK_WARN_BYTES = 1 << 30      # 1 GiB: a Sunday of ticks + journals is ~100 MB, leave headroom
DISK_FAIL_BYTES = 100 << 20    # 100 MiB
DEFAULT_BRIDGE = "http://127.0.0.1:8765"
DEFAULT_LIMIT = 16             # games of the date to match (an NFL Sunday is <= 16)
DEFAULT_VENUE_TIMEOUT_S = 75.0 # per-venue budget for the merge fetch (the whole report stays < 2 min)
KNOWN_WP_STATE = dict(home_score=14, away_score=10, game_seconds_remaining=1500, possession="home", down=2, distance=7, yardline_100=45, home_timeouts=3, away_timeouts=2, vegas_spread_home=-3.0)


@dataclass
class Check:
    name: str
    status: str
    detail: str
    latency_ms: Optional[float] = None
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "latency_ms": self.latency_ms, "data": self.data}


@dataclass
class Report:
    sport: str
    date: str
    checks: list[Check] = field(default_factory=list)
    generated_at: float = 0.0

    @property
    def verdict(self) -> str:
        worst = max((_RANK[c.status] for c in self.checks), default=0)
        return {0: PASS, 1: WARN, 2: FAIL}[worst]

    @property
    def exit_code(self) -> int:
        """0 = go (PASS or WARN), 2 = FAIL; the launcher keys off this."""
        return 2 if self.verdict == FAIL else 0

    def as_dict(self) -> dict[str, Any]:
        return {"sport": self.sport, "date": self.date, "generated_at": self.generated_at, "verdict": self.verdict, "exit_code": self.exit_code, "checks": [c.as_dict() for c in self.checks]}


@dataclass
class Clients:
    """Everything a check may talk to. ``None`` skips the check with a WARN (``--offline``).

    ``kalshi`` is a ``KalshiClient`` (``.get(path, params)``); ``polymarket_http`` /
    ``robinhood`` are the adapters' transports (the Robinhood check goes through the adapter
    so its on-disk catalogue cache is warm for the merge); ``espn`` is an ``ESPNClient``;
    ``adapters`` are the scan adapters the merge uses; ``bridge_http`` has ``.get(url)``;
    ``ext_checker(ext_dir) -> (returncode, output)`` runs ``scripts/check_extension.py``.
    """

    kalshi: Any = None
    polymarket_http: Any = None
    robinhood: Any = None
    espn: Any = None
    adapters: list[Any] = field(default_factory=list)
    bridge_http: Any = None
    ext_checker: Optional[Callable[[str], tuple[int, str]]] = None
    now: Optional[float] = None


def _ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000.0, 1)


def _err(e: BaseException) -> str:
    s = " ".join((str(e) or type(e).__name__).split())
    return s if len(s) <= 160 else s[:157] + "..."


# ---- local checks ---------------------------------------------------------------------------

def check_python(version: tuple[int, ...] = tuple(sys.version_info[:3])) -> Check:
    ok = tuple(version[:2]) >= MIN_PYTHON
    note = "" if tuple(version[:2]) >= RECOMMENDED_PYTHON else f" (3.13 recommended; {'.'.join(map(str, MIN_PYTHON))}+ supported)"
    return Check("python", PASS if ok else FAIL, f"Python {'.'.join(map(str, version))}" + (note if ok else f" < {'.'.join(map(str, MIN_PYTHON))} (unsupported)"), data={"version": list(version)})


# Runs in a fresh interpreter: import every module under the package, then report every
# top-level name in sys.modules (the parent subtracts the stdlib). argv: <root dir> <package>.
_IMPORT_WALK = r"""
import importlib, json, pkgutil, sys
root, pkg_name = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
failed, names = {}, []
try:
    pkg = importlib.import_module(pkg_name)
except BaseException as e:
    failed[pkg_name] = f"{type(e).__name__}: {e}"
    pkg = None
if pkg is not None:
    for info in pkgutil.walk_packages(pkg.__path__, pkg_name + "."):
        names.append(info.name)
        try:
            importlib.import_module(info.name)
        except BaseException as e:
            failed[info.name] = f"{type(e).__name__}: {e}"
print(json.dumps({"modules": len(names), "failed": failed, "loaded": sorted({m.split(".")[0] for m in sys.modules})}))
"""
_SITE_HOOKS = {"sitecustomize", "usercustomize"}  # imported by site.py before any of ours


def run_import_walk(root: str, package: str, timeout_s: float = 90.0) -> dict[str, Any]:
    """``python -I -c <walk> <root> <package>`` and its JSON: a subprocess so the answer does
    not depend on what this process imported first. ``-I`` drops ``PYTHONPATH`` and the user
    site, so a module that only imports thanks to one of those fails here - which is the
    point of a stdlib-only check."""
    out = subprocess.run([sys.executable, "-I", "-c", _IMPORT_WALK, root, package], capture_output=True, text=True, timeout=timeout_s)
    lines = [ln for ln in out.stdout.splitlines() if ln.startswith("{")]
    if out.returncode != 0 or not lines:
        raise RuntimeError(f"import walk exited {out.returncode}: {(out.stderr or out.stdout).strip()[-200:] or 'no output'}")
    return json.loads(lines[-1])


def check_imports(package: Any = None, walk: Optional[Callable[[str, str], Mapping[str, Any]]] = None) -> Check:
    """Import every module under ``arb_engine`` in a fresh interpreter and confirm nothing
    outside the standard library came along (``cryptography`` is the one optional dependency
    and is only a note because Kalshi signing needs it while scanning does not). ``walk``
    is the subprocess runner; tests inject one."""
    if package is None:
        import arb_engine as package
    root = os.path.dirname(os.path.abspath(list(package.__path__)[0]))
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    try:
        res = (walk or run_import_walk)(root, package.__name__)
    except Exception as e:  # noqa: BLE001 - a walk that cannot run is a FAIL, not a crash
        return Check("imports", FAIL, f"import walk failed: {type(e).__name__}: {_err(e)}", data={"modules": 0, "failed": {}, "third_party": []})
    names_n = int(res.get("modules") or 0)
    failed = {str(k): " ".join(str(v).split())[:160] for k, v in (res.get("failed") or {}).items()}
    loaded = {str(m) for m in (res.get("loaded") or [])}
    third_party = sorted(m for m in loaded if m and m not in stdlib and m != package.__name__ and not m.startswith("_") and m not in _SITE_HOOKS)
    optional = [m for m in third_party if m in OPTIONAL_THIRD_PARTY]
    hard = [m for m in third_party if m not in OPTIONAL_THIRD_PARTY]
    data = {"modules": names_n, "failed": failed, "third_party": third_party}
    if failed:
        first = next(iter(failed.items()))
        return Check("imports", FAIL, f"{len(failed)} of {names_n} modules failed to import: {first[0]} ({first[1]})", data=data)
    if names_n == 0:
        return Check("imports", FAIL, f"import walk found no module under {package.__name__} in {root}", data=data)
    if hard:
        return Check("imports", FAIL, f"{names_n} modules import but pulled in non-stdlib {', '.join(hard)}", data=data)
    note = f" (optional: {', '.join(optional)})" if optional else ""
    return Check("imports", PASS, f"{names_n} modules, stdlib only{note}", data=data)


def check_wp_model(state: Optional[Mapping[str, Any]] = None) -> Check:
    """Load the packaged model and price a known Q3 state plus a pre-game spread; the
    numbers only need to be sane (leader favoured, monotone with the spread), the exact
    values are pinned by ``tests/test_wp_model.py``."""
    t0 = time.monotonic()
    try:
        from .models.wp import default_model, home_win_probability, pregame_home_probability

        model = default_model()
        st = dict(KNOWN_WP_STATE if state is None else state)
        p = home_win_probability(model=model, **st)
        pre_fav = pregame_home_probability(-3.0, model=model)
        pre_dog = pregame_home_probability(3.0, model=model)
        final = home_win_probability(home_score=21, away_score=20, game_seconds_remaining=0, final=True, model=model)
    except Exception as e:  # noqa: BLE001
        return Check("wp-model", FAIL, f"model failed to load/price: {type(e).__name__}: {_err(e)}", latency_ms=_ms(t0))
    data = {"p_home_known_state": round(p, 4), "p_home_pregame_minus3": round(pre_fav, 4), "p_home_pregame_plus3": round(pre_dog, 4), "trees": getattr(model, "n_trees", None)}
    sane = 0.5 < p < 0.98 and pre_fav > 0.5 > pre_dog and final == 1.0
    if not sane:
        return Check("wp-model", FAIL, f"model prices are not sane: known state {p:.3f}, pre-game -3 {pre_fav:.3f} / +3 {pre_dog:.3f}, final {final}", latency_ms=_ms(t0), data=data)
    return Check("wp-model", PASS, f"{data['trees'] or '?'} trees; home +4 Q3 ball {p:.3f}, pre-game -3 {pre_fav:.3f}", latency_ms=_ms(t0), data=data)


def check_settings(settings: Optional[Mapping[str, Any]], bankroll: Optional[float] = None, kelly: Optional[float] = None, today: Optional[Any] = None) -> Check:
    """Registry size, the resolved executable venue set (WARN when Polymarket is in it: that
    is an operator override, not the table), table structure / verification age, and the
    sizing knobs when given."""
    from . import compliance
    from .config import KNOWN_SETTINGS
    from .scanner import resolve_executable_venues

    problems: list[str] = []
    warns: list[str] = []
    exec_venues = resolve_executable_venues(dict(settings or {}))
    override = (settings or {}).get("executable_venues") or os.environ.get(compliance.ENV_KEY)
    if exec_venues is None:
        warns.append("EXECUTABLE_VENUES=all: every venue is a leg (Polymarket included)")
    elif "polymarket" in exec_venues:
        warns.append("polymarket is executable (operator override) - it is NOT for a US account")
    elif not exec_venues:
        problems.append("no executable venue resolved")
    table = compliance.check_table()
    if table:
        problems.append("venue_rules.json: " + "; ".join(table))
    stale = compliance.stale_verification(today=today)
    if stale:
        warns.append("venue_rules.json verification older than 30 d: " + ", ".join(f"{v} ({d} d)" for v, d in sorted(stale.items())))
    if bankroll is not None and not bankroll > 0:
        problems.append(f"bankroll {bankroll} must be > 0")
    if kelly is not None and not 0 < kelly <= 1:
        problems.append(f"kelly fraction {kelly} must be in (0, 1]")
    if bankroll is not None and kelly is not None and bankroll * kelly > 500:
        warns.append(f"bankroll x kelly = ${bankroll * kelly:.0f} per STEAL is large for a first Sunday")
    ev_txt = "all" if exec_venues is None else ",".join(sorted(exec_venues))
    data = {"known_settings": len(KNOWN_SETTINGS), "executable_venues": None if exec_venues is None else sorted(exec_venues), "override": override, "bankroll": bankroll, "kelly": kelly, "stale_verification": stale}
    head = f"{len(KNOWN_SETTINGS)} settings; executable venues = {ev_txt}" + (" (override)" if override else " (table)") + (f"; bankroll ${bankroll:g} kelly {kelly:g}" if bankroll is not None and kelly is not None else "")
    if problems:
        return Check("settings", FAIL, head + " | " + "; ".join(problems), data=data)
    if warns:
        return Check("settings", WARN, head + " | " + "; ".join(warns), data=data)
    return Check("settings", PASS, head, data=data)


# ---- venues ---------------------------------------------------------------------------------

def check_kalshi(client: Any, sport: str = "nfl") -> Check:
    """``GET /markets?series_ticker=<game series>&limit=1`` on the data host; a 403 here is
    the Origin / user-agent problem, an HTTP 0 is the truncating proxy."""
    if client is None:
        return Check("venue:kalshi", WARN, "skipped (offline)")
    from .venues.kalshi import SPORT_SERIES

    series = (SPORT_SERIES.get(sport) or [{"series": "KXNFLGAME"}])[0]["series"]
    t0 = time.monotonic()
    try:
        data = client.get("/markets", {"series_ticker": series, "limit": 1})
    except Exception as e:  # noqa: BLE001
        return Check("venue:kalshi", FAIL, f"{series}: {type(e).__name__}: {_err(e)}", latency_ms=_ms(t0), data={"series": series, "base_url": getattr(client, "base_url", None)})
    n = len((data or {}).get("markets") or [])
    base = getattr(client, "base_url", "")
    fell = " (fell back to the legacy host)" if getattr(client, "fell_back", False) else ""
    if n == 0:
        return Check("venue:kalshi", WARN, f"{series} reachable but returned no open market{fell}", latency_ms=_ms(t0), data={"series": series, "markets": 0, "base_url": base})
    return Check("venue:kalshi", PASS, f"{series} ok via {base}{fell}", latency_ms=_ms(t0), data={"series": series, "markets": n, "base_url": base})


def check_polymarket(http: Any, sport: str = "nfl") -> Check:
    """Gamma ``/events?tag_slug=<sport tag>&limit=1`` — the same call the adapter pages
    through, so a reachable-but-empty tag (off-season, renamed tag) is a WARN, not a FAIL."""
    if http is None:
        return Check("venue:polymarket", WARN, "skipped (offline)")
    from .venues.polymarket import GAMMA, SPORT_TAGS

    tag = (SPORT_TAGS.get(sport) or [sport])[0]
    t0 = time.monotonic()
    try:
        data = http.get(f"{GAMMA}/events", {"tag_slug": tag, "active": "true", "closed": "false", "limit": 1})
    except Exception as e:  # noqa: BLE001
        return Check("venue:polymarket", FAIL, f"gamma tag {tag}: {type(e).__name__}: {_err(e)}", latency_ms=_ms(t0), data={"tag": tag})
    n = len(data or [])
    if n == 0:
        return Check("venue:polymarket", WARN, f"gamma reachable, tag {tag} has no active event", latency_ms=_ms(t0), data={"tag": tag, "events": 0})
    slug = (data[0] or {}).get("slug") if isinstance(data, list) else None
    return Check("venue:polymarket", PASS, f"gamma ok, tag {tag} (e.g. {slug})", latency_ms=_ms(t0), data={"tag": tag, "events": n, "slug": slug})


def check_robinhood(adapter: Any, sport: str = "nfl") -> Check:
    """The category page through the adapter (``__NEXT_DATA__`` parsed, catalogue cache
    rewritten for the merge), always from the network. Slow but not failing is a WARN: the
    same page is what ``live`` re-pulls every 30 min."""
    if adapter is None:
        return Check("venue:robinhood", WARN, "skipped (offline)")
    from .venues.robinhood import SPORT_CATEGORY

    category = SPORT_CATEGORY.get(sport, sport)
    t0 = time.monotonic()
    try:
        # use_cache=False: this row claims reachability + latency, so it must hit the host
        # even when out/cache/ holds a page younger than the adapter's 30 min TTL (the
        # adapter rewrites the cache on the way out, so the merge below stays warm).
        pp = adapter.category_page(category, use_cache=False)
    except Exception as e:  # noqa: BLE001
        return Check("venue:robinhood", FAIL, f"category page {category}: {type(e).__name__}: {_err(e)}", latency_ms=_ms(t0), data={"category": category})
    events = pp.get("events") or []
    games = adapter.select_game_events(sport, events) if hasattr(adapter, "select_game_events") else events
    ms = _ms(t0)
    data = {"category": category, "events": len(events), "game_events": len(games), "transport": getattr(getattr(adapter, "http", None), "transport", None)}
    if not games:
        return Check("venue:robinhood", WARN, f"category {category} parsed but lists no game-winner event", latency_ms=ms, data=data)
    if ms > 45_000:
        return Check("venue:robinhood", WARN, f"category {category}: {len(games)} game events but the page took {ms / 1000:.0f} s (set ARB_HTTP_TRANSPORT=curl)", latency_ms=ms, data=data)
    return Check("venue:robinhood", PASS, f"category {category}: {len(events)} events, {len(games)} game winners", latency_ms=ms, data=data)


# ---- ESPN + matching ------------------------------------------------------------------------

def _fmt_kick(dt: Optional[datetime]) -> str:
    """Kickoff as an Eastern clock time (what the schedule and the operator use); UTC when
    the zone database is missing."""
    if dt is None:
        return "?"
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M ET")
    except Exception:  # noqa: BLE001 - no tzdata on this box
        return dt.astimezone(timezone.utc).strftime("%H:%MZ")


def _game_et_date(g: Any) -> Optional[str]:
    """The ET calendar date a game is keyed on: the event key's date when the team table
    knew both sides, else the kickoff converted to ET."""
    from .matching.normalize import et_date

    if g.event_key:
        return g.event_key.rsplit(":", 1)[-1]
    return et_date(g.start_time)


def check_espn(client: Any, sport: str, date: str) -> Check:
    """The scoreboard for the date: every game *on that ET date* with kickoff, status and
    whether the scoreboard odds block carried a spread (the pre-game anchor of the WP model).
    ESPN's NFL ``?dates=`` answers with the whole week, so the other days are counted, not
    listed: matching Thursday's final against Sunday's venues is noise."""
    if client is None:
        return Check("espn", WARN, "skipped (offline)", data={"games": []})
    from .venues.espn import ESPNFeed

    t0 = time.monotonic()
    try:
        games = ESPNFeed(client, guard=False).games(date)
    except Exception as e:  # noqa: BLE001
        return Check("espn", FAIL, f"scoreboard {date}: {type(e).__name__}: {_err(e)}", latency_ms=_ms(t0), data={"games": []})
    on_date = [g for g in games if _game_et_date(g) == date]
    other = len(games) - len(on_date)
    on_date.sort(key=lambda g: g.start_time.timestamp() if g.start_time else float("inf"))
    rows = [{"event_id": g.event_id, "event_key": g.event_key, "away": g.away, "home": g.home, "kickoff": g.start_time.isoformat() if g.start_time else None, "status": g.status, "spread_home": g.vegas_spread_home, "provider": g.odds_provider} for g in on_date]
    with_spread = sum(1 for r in rows if r["spread_home"] is not None)
    no_spread = [f"{r['away']}@{r['home']}" for r in rows if r["spread_home"] is None and r["status"] != "final"]
    unkeyed = [f"{r['away']}@{r['home']}" for r in rows if not r["event_key"]]
    ms = _ms(t0)
    data = {"date": date, "games": rows, "with_spread": with_spread, "no_spread": no_spread, "unkeyed": unkeyed, "other_dates": other}
    if not rows:
        if other:   # games exist, just not on this date: the --date is mis-set (it is the ET date)
            return Check("espn", FAIL, f"no {sport} game on {date}; the scoreboard lists {other} on other dates: wrong --date (it is the ET date)?", latency_ms=ms, data=data)
        # An empty scoreboard is an off day (the all-week launcher hits this Tue-Wed): the
        # recorder idles until the next kickoff, so it is a WARN, not a reason to refuse to start.
        return Check("espn", WARN, f"no {sport} game on {date}: off day, the recorder idles until the next kickoff (wrong --date if you expected games)", latency_ms=ms, data=data)
    kicks = [g.start_time for g in on_date if g.start_time]
    head = f"{len(rows)} games on {date}, kickoffs {_fmt_kick(min(kicks) if kicks else None)}..{_fmt_kick(max(kicks) if kicks else None)}, {with_spread} with a spread" + (f" (+{other} on other dates)" if other else "")
    if unkeyed:
        return Check("espn", WARN, head + f"; no event key for {', '.join(unkeyed)} (team table)", latency_ms=ms, data=data)
    if no_spread:
        return Check("espn", WARN, head + f"; no spread yet for {', '.join(no_spread)} (the model prices them neutral pre-game)", latency_ms=ms, data=data)
    return Check("espn", PASS, head, latency_ms=ms, data=data)


def _fetch_all(adapters: Iterable[Any], sport: str, timeout_s: float) -> tuple[list[Any], dict[str, str]]:
    """Fetch every adapter in its own daemon thread with one budget: a venue that hangs
    reports as a timeout instead of holding the launcher past kickoff. Each thread owns one
    pre-allocated slot (no shared dict): a fetch that lands a moment after its deadline, while
    this thread is collecting, can neither be counted nor trip "dict changed size"."""
    ads = list(adapters)
    slots: list[Optional[tuple[str, Any]]] = [None] * len(ads)
    threads: list[tuple[str, threading.Thread]] = []
    for i, ad in enumerate(ads):
        name = getattr(ad, "venue", ad.__class__.__name__)

        def run(i=i, ad=ad):
            try:
                slots[i] = ("ok", ad.fetch(sport))
            except Exception as e:  # noqa: BLE001
                slots[i] = ("err", f"{type(e).__name__}: {_err(e)}")

        t = threading.Thread(target=run, name=f"preflight-{name}", daemon=True)
        t.start()
        threads.append((name, t))
    deadline = time.monotonic() + timeout_s
    snaps: list[Any] = []
    errors: dict[str, str] = {}
    for i, (name, t) in enumerate(threads):
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            errors[name] = f"timeout after {timeout_s:.0f} s"
            continue
        slot = slots[i]  # the join makes the finished thread's write visible
        if slot is None:
            errors[name] = "fetch returned nothing"
        elif slot[0] == "ok":
            snaps.append(slot[1])
        else:
            errors[name] = slot[1]
    return snaps, errors


def match_games(games: list[dict[str, Any]], merged: Mapping[str, Any], executable: Optional[set[str]]) -> dict[str, Any]:
    """Per ESPN game, the venues quoting its moneyline (exact event key, else the same teams
    within a day - the merge's own tolerance), and the slate-wide counts."""
    from .matching.matcher import _days_apart, _split_key

    by_teams: dict[tuple[str, str], list[tuple[str, Any]]] = {}
    for key, me in merged.items():
        if me.info.market_type != "moneyline":
            continue
        sport, parts, date, _ = _split_key(key)
        by_teams.setdefault((sport, parts), []).append((date, me))
    rows = []
    for g in games:
        key = g.get("event_key")
        venues: list[str] = []
        if key:
            me = merged.get(key)
            if me is None:
                sport, parts, date, _ = _split_key(key)
                cands = [m for d, m in by_teams.get((sport, parts), []) if _days_apart(d, date) <= 1]
                me = cands[0] if cands else None
            if me is not None:
                venues = sorted(me.quotes_by_venue)
        rows.append({"event_key": key, "away": g.get("away"), "home": g.get("home"), "venues": venues})
    all_venues = sorted({v for r in rows for v in r["venues"]})
    per_venue = {v: sum(1 for r in rows if v in r["venues"]) for v in all_venues}
    exec_set = set(all_venues) if executable is None else set(executable)
    on_all_exec = sum(1 for r in rows if exec_set and exec_set <= set(r["venues"]))
    two_exec = sum(1 for r in rows if len(exec_set & set(r["venues"])) >= 2)
    unmatched = [f"{r['away']}@{r['home']}" for r in rows if not r["venues"]]
    return {"games": rows, "per_venue": per_venue, "executable": sorted(exec_set), "on_all_executable": on_all_exec, "on_two_executable": two_exec, "unmatched": unmatched}


def check_matches(adapters: list[Any], sport: str, games: list[dict[str, Any]], executable: Optional[set[str]], limit: int = DEFAULT_LIMIT, timeout_s: float = DEFAULT_VENUE_TIMEOUT_S, fetch_all: Optional[Callable[..., tuple[list[Any], dict[str, str]]]] = None) -> Check:
    """Run the scanner's merge over one fetch per adapter and count, for the first ``limit``
    games of the date, how many are quoted on each venue and on every executable venue.
    A FAIL means the slate would print every game as ``missing``."""
    if not adapters:
        return Check("matches", WARN, "skipped (offline)")
    if not games:
        return Check("matches", WARN, "no ESPN games to match")
    from .matching.matcher import merge_snapshots

    t0 = time.monotonic()
    snaps, errors = (fetch_all or _fetch_all)(adapters, sport, timeout_s)
    merged = merge_snapshots(snaps) if snaps else {}
    for s in snaps:
        if getattr(s, "errors", None):
            errors[s.venue] = "; ".join(str(x) for x in s.errors[:2])
    res = match_games(games[:limit], merged, executable)
    res["fetch_errors"] = errors
    res["merged_events"] = len(merged)
    ms = _ms(t0)
    n = len(res["games"])
    pv = ", ".join(f"{v} {c}/{n}" for v, c in res["per_venue"].items()) or "no venue quotes"
    head = f"{n} games: {pv}; on all executable ({'+'.join(res['executable']) or '-'}): {res['on_all_executable']}/{n}"
    tail = (" | " + "; ".join(f"{v}: {e}" for v, e in errors.items())) if errors else ""
    if res["on_two_executable"] == 0:
        return Check("matches", FAIL, head + " - nothing is cross-venue on executable venues" + tail, latency_ms=ms, data=res)
    if errors or res["unmatched"] or res["on_all_executable"] < n:
        miss = f"; unmatched: {', '.join(res['unmatched'])}" if res["unmatched"] else ""
        return Check("matches", WARN, head + miss + tail, latency_ms=ms, data=res)
    return Check("matches", PASS, head, latency_ms=ms, data=res)


# ---- bridge, disk, extension ----------------------------------------------------------------

def check_bridge(http: Any, url: str, executable: Optional[set[str]]) -> Check:
    """``GET /health``; when the bridge reports its executable venue set it must equal the
    one this process resolved (the overlay's ``signal only`` tags come from the bridge)."""
    if http is None:
        return Check("bridge", WARN, "skipped (offline)", data={"url": url})
    url = url.rstrip("/")
    t0 = time.monotonic()
    try:
        data = http.get(url + "/health")
    except Exception as e:  # noqa: BLE001
        return Check("bridge", WARN, f"not running at {url} ({type(e).__name__}: {_err(e)}); sunday.sh starts it", latency_ms=_ms(t0), data={"url": url, "running": False})
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    ms = _ms(t0)
    ok = bool((data or {}).get("ok"))
    if not ok:
        return Check("bridge", FAIL, f"{url}/health answered but not ok: {str(data)[:120]}", latency_ms=ms, data={"url": url, "running": True, "health": data})
    theirs = (data or {}).get("executable_venues")
    mine = None if executable is None else sorted(executable)
    info = {"url": url, "running": True, "bridge_executable_venues": theirs, "executable_venues": mine, "version": (data or {}).get("version")}
    if theirs is not None:
        theirs_set = None if theirs in ("all", "*") else sorted(str(v).lower() for v in (theirs if isinstance(theirs, list) else str(theirs).split(",")) if str(v).strip())
        if theirs_set != mine:
            return Check("bridge", FAIL, f"bridge executable venues {theirs_set or 'all'} != this process {mine or 'all'}: restart the bridge in the same environment", latency_ms=ms, data=info)
        return Check("bridge", PASS, f"up at {url}, executable venues match ({','.join(mine) if mine else 'all'})", latency_ms=ms, data=info)
    return Check("bridge", PASS, f"up at {url} (health does not report its venue set; restart it after changing EXECUTABLE_VENUES)", latency_ms=ms, data=info)


def check_out_dir(path: str = "out", disk_usage: Callable[[str], Any] = shutil.disk_usage) -> Check:
    """``out/`` (and ``out/logs``, ``out/run``) exist and take a write; free space on that
    volume above the thresholds."""
    try:
        for sub in ("", "logs", "run"):
            os.makedirs(os.path.join(path, sub), exist_ok=True)
        probe = os.path.join(path, f".preflight-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok\n")
        os.remove(probe)
    except OSError as e:
        return Check("out-dir", FAIL, f"{path}/ is not writable: {_err(e)}", data={"path": path})
    try:
        free = int(disk_usage(path).free)
    except OSError as e:
        return Check("out-dir", WARN, f"{path}/ writable; free space unknown ({_err(e)})", data={"path": path})
    gb = free / float(1 << 30)
    data = {"path": path, "free_bytes": free}
    if free < DISK_FAIL_BYTES:
        return Check("out-dir", FAIL, f"{path}/ writable but only {gb:.2f} GiB free", data=data)
    if free < DISK_WARN_BYTES:
        return Check("out-dir", WARN, f"{path}/ writable, {gb:.2f} GiB free (recorder + journals need headroom)", data=data)
    return Check("out-dir", PASS, f"{path}/ writable, {gb:.1f} GiB free", data=data)


def run_extension_checker(ext_dir: str) -> tuple[int, str]:
    """``python3 scripts/check_extension.py <dir>`` as a subprocess: the script keeps its
    findings in module globals, so importing it twice would double-count."""
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "check_extension.py")
    if not os.path.isfile(script):
        return 127, f"{script} not found"
    out = subprocess.run([sys.executable, script, ext_dir], capture_output=True, text=True, timeout=60)
    return out.returncode, (out.stdout + out.stderr).strip()


def check_extension(checker: Optional[Callable[[str], tuple[int, str]]], ext_dir: str) -> Check:
    if checker is None:
        return Check("extension", WARN, "skipped", data={"dir": ext_dir})
    if not os.path.isfile(os.path.join(ext_dir, "manifest.json")):
        return Check("extension", FAIL, f"{ext_dir}/manifest.json not found", data={"dir": ext_dir})
    t0 = time.monotonic()
    try:
        rc, output = checker(ext_dir)
    except Exception as e:  # noqa: BLE001
        return Check("extension", FAIL, f"checker crashed: {type(e).__name__}: {_err(e)}", latency_ms=_ms(t0), data={"dir": ext_dir})
    last = (output.strip().splitlines() or ["(no output)"])[-1]
    data = {"dir": ext_dir, "returncode": rc, "output": output[-2000:]}
    if rc != 0:
        return Check("extension", FAIL, f"check_extension.py: {last}", latency_ms=_ms(t0), data=data)
    return Check("extension", PASS, f"check_extension.py: {last}", latency_ms=_ms(t0), data=data)


# ---- the report -----------------------------------------------------------------------------

def run_report(sport: str, date: str, settings: Optional[Mapping[str, Any]], clients: Clients, *, bridge_url: str = DEFAULT_BRIDGE, out_dir: str = "out", ext_dir: str = "extension", limit: int = DEFAULT_LIMIT, venue_timeout_s: float = DEFAULT_VENUE_TIMEOUT_S, bankroll: Optional[float] = None, kelly: Optional[float] = None, fetch_all: Optional[Callable[..., Any]] = None, progress: Optional[Callable[[Check], None]] = None) -> Report:
    """Every check in order; ``progress`` sees each row as it lands so a slow venue is
    visible while the report is still running."""
    from .scanner import resolve_executable_venues

    rep = Report(sport=sport, date=date, generated_at=clients.now if clients.now is not None else time.time())

    def add(c: Check) -> Check:
        rep.checks.append(c)
        if progress:
            progress(c)
        return c

    add(check_python())
    add(check_imports())
    add(check_wp_model())
    add(check_settings(settings, bankroll=bankroll, kelly=kelly))
    executable = resolve_executable_venues(dict(settings or {}))
    add(check_kalshi(clients.kalshi, sport))
    add(check_polymarket(clients.polymarket_http, sport))
    add(check_robinhood(clients.robinhood, sport))
    espn = add(check_espn(clients.espn, sport, date))
    add(check_matches(clients.adapters, sport, list(espn.data.get("games") or []), executable, limit=limit, timeout_s=venue_timeout_s, fetch_all=fetch_all))
    add(check_bridge(clients.bridge_http, bridge_url, executable))
    add(check_out_dir(out_dir))
    add(check_extension(clients.ext_checker, ext_dir))
    return rep


def format_report(rep: Report) -> str:
    lines = [f"# preflight {rep.sport} {rep.date}"]
    for c in rep.checks:
        lat = f"{c.latency_ms / 1000:6.2f}s" if c.latency_ms is not None else "      -"
        lines.append(f"{c.status:<4} {c.name:<17} {lat}  {c.detail}")
    n = {s: sum(1 for c in rep.checks if c.status == s) for s in (PASS, WARN, FAIL)}
    verdict = {PASS: "GO", WARN: "GO WITH WARNINGS", FAIL: "NO-GO"}[rep.verdict]
    lines.append(f"VERDICT {rep.verdict}: {verdict}  ({n[PASS]} pass, {n[WARN]} warn, {n[FAIL]} fail)")
    return "\n".join(lines)


def today_et() -> str:
    """The Eastern calendar date (sports schedules are quoted in ET; ESPN's ``dates=`` is too)."""
    from .matching.normalize import et_date

    return et_date(datetime.now(timezone.utc)) or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def build_clients(sport: str, bridge: bool = True, extension: bool = True) -> Clients:
    """Real clients: the scan adapters ``cli.build_adapters`` makes, so latency and behaviour
    (rate limits, host fallbacks, catalogue cache) are exactly what ``live`` will see."""
    from .cli import ALL_VENUES, build_adapters
    from .venues.espn import ESPNClient
    from .venues.http import HttpClient

    adapters = build_adapters(list(ALL_VENUES), False)
    by = {getattr(a, "venue", ""): a for a in adapters}
    return Clients(
        kalshi=getattr(by.get("kalshi"), "client", None),
        polymarket_http=getattr(by.get("polymarket"), "http", None),
        robinhood=by.get("robinhood"),
        espn=ESPNClient(sport=sport),
        adapters=adapters,
        bridge_http=HttpClient(timeout=5.0, retries=0) if bridge else None,
        ext_checker=run_extension_checker if extension else None,
    )
