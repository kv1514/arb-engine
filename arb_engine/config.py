"""Settings from environment / .env (no third-party dependency).

Every settings key the engine reads is *declared* here or in the module that owns it via
:func:`declare_setting`, so the full set is enumerable (``KNOWN_SETTINGS``) and the docs
test can prove "undocumented settings keys = 0". Resolution order is always

    explicit ``settings`` dict  >  environment variable  >  declared default

which is what :func:`setting` implements and what :func:`load_settings` bakes into the
dict handed to every CLI handler and the bridge. Modules declare their own keys at import
time (``declare_setting("kalshi_rounding", env="KALSHI_FEE_ROUNDING", default="cent")``)
so this file never needs editing when a feature grows a knob.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

_MISSING = object()


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def as_bool(v: Any) -> bool:
    """``ROBINHOOD_GOLD=1|true|yes|on`` -> True; anything else (including "") -> False."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class SettingSpec:
    key: str
    env: Optional[str]
    default: Any
    cast: Optional[Callable[[str], Any]]
    doc: str

    def from_env(self, environ: Optional[Mapping[str, str]] = None) -> Any:
        """Resolve env -> default. An *empty* env value for a typed (``cast``) setting means
        "unset" (``POLYMARKET_US_VOLUME_REBATE=`` must not crash ``float``); for untyped
        string settings the empty string is kept as-is, matching the historical behaviour."""
        env = os.environ if environ is None else environ
        if not self.env or self.env not in env:
            return self.default
        raw = env[self.env]
        if self.cast is None:
            return raw
        if raw.strip() == "":
            return self.default
        return self.cast(raw)


#: key -> spec, in declaration order. Enumerated by the docs test and ``python -m arb_engine``
#: plugins; never cleared at runtime.
KNOWN_SETTINGS: dict[str, SettingSpec] = {}


def _same_cast(a: Optional[Callable], b: Optional[Callable]) -> bool:
    return a is b or getattr(a, "__qualname__", None) == getattr(b, "__qualname__", None)


def declare_setting(key: str, env: Optional[str] = None, default: Any = None, cast: Optional[Callable[[str], Any]] = None, doc: str = "") -> SettingSpec:
    """Register a settings key. Idempotent for an identical re-declaration (module reloads);
    a *conflicting* re-declaration raises so two plan items cannot silently fight over a key."""
    spec = SettingSpec(key=key, env=env, default=default, cast=cast, doc=doc)
    prev = KNOWN_SETTINGS.get(key)
    if prev is not None and not (prev.env == env and prev.default == default and _same_cast(prev.cast, cast)):
        raise ValueError(f"setting {key!r} already declared with env={prev.env!r} default={prev.default!r}")
    KNOWN_SETTINGS[key] = spec
    return spec


def setting(settings: Optional[Mapping[str, Any]], key: str, default: Any = _MISSING) -> Any:
    """Resolve one key: ``settings`` dict (if it holds the key) > env > declared default.

    ``default`` only applies to *undeclared* keys (ad-hoc knobs a caller threads through the
    dict); asking for a key that is neither declared nor given a default raises, which is
    how a typo in a settings key surfaces in tests instead of silently reading None."""
    if settings is not None and key in settings:
        return settings[key]
    spec = KNOWN_SETTINGS.get(key)
    if spec is None:
        if default is _MISSING:
            raise KeyError(f"undeclared setting {key!r}; call config.declare_setting first")
        return default
    return spec.from_env()


def load_settings() -> dict[str, Any]:
    """Every declared key resolved from the environment (env > default), in declaration order."""
    return {k: spec.from_env() for k, spec in KNOWN_SETTINGS.items()}


def settings_from_env() -> dict[str, Any]:
    """Historical name for :func:`load_settings` (bridge.py and older callers)."""
    return load_settings()


# ---- the keys config.py has always read, migrated verbatim ------------------------------
declare_setting("robinhood_gold", env="ROBINHOOD_GOLD", default=False, cast=as_bool, doc="Price Robinhood commission at the Gold rate ($0.005 instead of $0.01 per contract).")
declare_setting("kalshi_rounding", env="KALSHI_FEE_ROUNDING", default="cent", doc="How Kalshi's per-order fee is rounded: 'cent' (up to the cent, conservative) or 'centicent'.")
declare_setting("polymarket_us_volume_rebate", env="POLYMARKET_US_VOLUME_REBATE", default=0.0, cast=float, doc="Polymarket US taker-fee volume rebate as a fraction (0 = none).")
declare_setting("venue_weights", env=None, default=None, doc="Optional {venue: weight} override for consensus fair value; None = quant.fairvalue.DEFAULT_VENUE_WEIGHTS.")
declare_setting("odds_api_key", env="ODDS_API_KEY", default=None, doc="The Odds API key for sportsbook consensus (pre-game only); None disables it.")
