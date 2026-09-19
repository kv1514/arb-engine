"""CLI plugin (P06): execution-gate flags for ``live`` and ``inplay``.

The plugin loader in ``arb_engine.cli`` calls ``register(subparsers, existing_parsers)``;
``existing_parsers`` maps subcommand name -> ArgumentParser. This adds

* ``--stale-after S``   feed-stale threshold (setting ``inplay_stale_after_s`` / env
                        ``INPLAY_STALE_AFTER_S``, default 15)
* ``--cdna-haircut E``  extra STEAL edge on CDNA-routed contracts (``inplay_delay_haircut_cdna``)
* ``--slate-cap $``     (``live`` only) dollars per tick across every STEAL
* ``--record DB``       only when the subcommand does not already have it

and returns wrapped handlers that copy the flags into the settings dict (and the matching
environment variables, so a handler that builds its own settings sees them too) before
delegating to ``cli.cmd_live`` / ``cli.cmd_inplay``.
"""

from __future__ import annotations

import inspect
import os
from typing import Any, Callable, Optional

FLAGS: dict[str, tuple[str, str, str]] = {
    # dest -> (settings key, env var, help)
    "stale_after": ("inplay_stale_after_s", "INPLAY_STALE_AFTER_S", "seconds without an ESPN state change (while a venue mid moves >= 0.02) before STEAL/LOCK are gated as feed-stale (default 15)"),
    "cdna_haircut": ("inplay_delay_haircut_cdna", "INPLAY_DELAY_HAIRCUT_CDNA", "extra edge a STEAL on a CDNA-routed Robinhood contract needs for its 3 s order delay (default 0.02)"),
}


def _has_option(parser: Any, opt: str) -> bool:
    return any(opt in getattr(a, "option_strings", ()) for a in getattr(parser, "_actions", []))


def apply_flags(args: Any, settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Copy the parsed flags into ``settings`` (returned) and the environment."""
    settings = settings if settings is not None else {}
    for dest, (key, env, _) in FLAGS.items():
        v = getattr(args, dest, None)
        if v is not None:
            settings[key] = v
            os.environ[env] = str(v)
    cap = getattr(args, "slate_cap", None)
    if cap is not None:
        settings["inplay_slate_cap"] = cap
        os.environ["INPLAY_SLATE_CAP"] = str(cap)
    return settings


def _wrap(name: str) -> Callable[..., int]:
    def handler(args: Any, settings: Optional[dict[str, Any]] = None, *rest: Any, **kw: Any) -> int:
        from .. import cli  # late: the plugin loader runs while cli builds its parser
        orig = getattr(cli, f"cmd_{name}")
        apply_flags(args, settings)
        try:
            params = inspect.signature(orig).parameters
        except (TypeError, ValueError):
            params = {}
        if settings is not None and ("settings" in params or len(params) >= 2):
            return orig(args, settings, *rest, **kw)
        return orig(args)
    handler.__name__ = f"cmd_{name}_with_gates"
    return handler


def register(subparsers: Any, existing_parsers: dict[str, Any]) -> dict[str, Callable[..., int]]:
    handlers: dict[str, Callable[..., int]] = {}
    for name in ("live", "inplay"):
        p = (existing_parsers or {}).get(name)
        if p is None:
            continue
        for dest, (key, env, help_) in FLAGS.items():
            opt = "--" + dest.replace("_", "-")
            if not _has_option(p, opt):
                p.add_argument(opt, dest=dest, type=float, default=None, help=f"{help_} [env {env}]")
        if name == "live" and not _has_option(p, "--slate-cap"):
            p.add_argument("--slate-cap", dest="slate_cap", type=float, default=None, help="dollars to deploy per tick across every STEAL on the slate (default: --bankroll); stakes are scaled proportionally")
        if not _has_option(p, "--record"):
            p.add_argument("--record", metavar="DB", help="append every tick (ESPN state, venue L1, freshness, STEAL/GATED) to this SQLite file")
        handlers[name] = _wrap(name)
    return handlers
