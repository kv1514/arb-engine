"""Settings from environment / .env (no third-party dependency)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


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


def settings_from_env() -> dict[str, Any]:
    def flag(name: str, default: bool = False) -> bool:
        v = os.environ.get(name)
        return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

    return {
        "robinhood_gold": flag("ROBINHOOD_GOLD", False),
        "kalshi_rounding": os.environ.get("KALSHI_FEE_ROUNDING", "cent"),
        "polymarket_us_volume_rebate": float(os.environ.get("POLYMARKET_US_VOLUME_REBATE", "0") or 0),
        "venue_weights": None,
        "odds_api_key": os.environ.get("ODDS_API_KEY"),
    }
