"""CLI plugin (P06): execution-gate flags for ``live`` and ``inplay``.

The plugin loader in ``arb_engine.cli`` calls ``register(subparsers, existing_parsers)``;
``existing_parsers`` maps subcommand name -> ArgumentParser. This adds

* ``--stale-after S``   feed-stale threshold (setting ``inplay_stale_after_s`` / env
                        ``INPLAY_STALE_AFTER_S``, default 15)
* ``--cdna-haircut E``  extra STEAL edge on CDNA-routed contracts (``inplay_delay_haircut_cdna``)
* ``--slate-cap $``     (``live`` only) dollars per tick across every STEAL
* ``--quiet``           (``live`` only) STEAL / LOCK / GATED lines + one summary line per tick
                        (setting ``inplay_quiet`` / env ``INPLAY_QUIET``)
* ``--record [DB]``     only when the subcommand does not already have it (bare flag:
                        ``out/history.db``, the same default as ``cli.py``'s own flag)

and returns wrapped handlers that copy the flags into the settings dict (and the matching
environment variables, so a handler that builds its own settings sees them too) before
delegating to ``cli.cmd_live`` / ``cli.cmd_inplay``. The wrapper is also installed *as*
``cli.cmd_live`` / ``cli.cmd_inplay`` (``_install``), because a plugin later in name order
(``maker_flags.run_live``) wins the ``live`` override and dispatches through ``cli.cmd_live``:
the flags must reach the handler whichever plugin's override the loader picks.
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
    if getattr(args, "quiet", False):
        settings["inplay_quiet"] = True
        os.environ["INPLAY_QUIET"] = "1"
    return settings


_GATED = "_with_gates"


def _wrap(name: str, base: Callable[..., int]) -> Callable[..., int]:
    """``cli.cmd_<name>`` with the gate flags copied into ``settings`` first.

    ``base`` is the handler in place when the plugin registered. At call time the wrapper
    re-reads ``cli.cmd_<name>``: if that is still itself it calls ``base``; if a test has
    swapped a fake in since, it calls the fake, so a monkeypatch after registration keeps
    working."""
    def handler(args: Any, settings: Optional[dict[str, Any]] = None, *rest: Any, **kw: Any) -> int:
        from .. import cli  # late: the plugin loader runs while cli builds its parser
        cur = getattr(cli, f"cmd_{name}", None)
        orig = base if (cur is None or cur is handler) else cur
        apply_flags(args, settings)
        try:
            params = inspect.signature(orig).parameters
        except (TypeError, ValueError):
            params = {}
        if settings is not None and ("settings" in params or len(params) >= 2):
            return orig(args, settings, *rest, **kw)
        return orig(args)
    handler.__name__ = f"cmd_{name}{_GATED}"
    handler.__wrapped__ = base  # type: ignore[attr-defined]
    return handler


def _install(name: str) -> Optional[Callable[..., int]]:
    """Wrap ``cli.cmd_<name>`` and put the wrapper back on the module. Plugins load in name
    order and the last override wins, so ``maker_flags.run_live`` (m > l) is what ``live``
    dispatches to; it calls ``cli.cmd_live`` directly, and without this the gate flags
    (``--stale-after``, ``--cdna-haircut``, ``--slate-cap``, ``--quiet``) were parsed and
    silently dropped. Idempotent: a wrapper already in place is returned as is."""
    from .. import cli
    base = getattr(cli, f"cmd_{name}", None)
    if base is None:
        return None
    if getattr(base, "__name__", "") == f"cmd_{name}{_GATED}":
        return base
    handler = _wrap(name, base)
    setattr(cli, f"cmd_{name}", handler)
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
        if name == "live" and not _has_option(p, "--quiet"):
            p.add_argument("--quiet", dest="quiet", action="store_true", default=False, help="print only STEAL / LOCK / GATED lines and a one-line summary per tick [env INPLAY_QUIET]")
        if not _has_option(p, "--record"):
            p.add_argument("--record", metavar="DB", nargs="?", const="out/history.db", help="append every tick (ESPN state, venue L1, freshness, STEAL/GATED) to this SQLite file (bare flag: out/history.db)")
        handler = _install(name)
        if handler is not None:
            handlers[name] = handler
    return handlers
