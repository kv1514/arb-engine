"""CLI plugin (P13): the ``lines-eval`` sub-command.

Loaded by the plugin registry in ``arb_engine/cli.py`` (P01) through ``register(subparsers,
existing_parsers)``; the sub-command is a thin wrapper over ``scripts/eval_lines.py`` so the
evaluation stays a by-hand, cached, network step and the engine imports none of it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

ROOT = Path(__file__).resolve().parents[2]


def _load_eval_module():
    """Import ``scripts/eval_lines.py`` by path (scripts/ is not a package)."""
    import importlib.util

    path = ROOT / "scripts" / "eval_lines.py"
    spec = importlib.util.spec_from_file_location("arb_engine_scripts_eval_lines", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cmd_lines_eval(args: argparse.Namespace) -> int:
    """Pre-game and in-play cover/over log-loss of normal vs empirical vs Kalshi mid under both alignments."""
    try:
        mod = _load_eval_module()
    except Exception as e:
        print(f"lines-eval unavailable: {e}", file=sys.stderr)
        return 2
    return int(mod.run(args))


def register(subparsers: Any, existing_parsers: Optional[Mapping[str, argparse.ArgumentParser]] = None) -> dict[str, argparse.ArgumentParser]:
    """Add ``lines-eval``; returns the parsers this plugin created."""
    if existing_parsers and "lines-eval" in existing_parsers:
        return {}
    sp = subparsers.add_parser("lines-eval", help="score spread/total fair values (normal, empirical) against Kalshi mids on a finished NFL week, pre-game and in play, under both candle alignments")
    try:
        _load_eval_module().add_arguments(sp)
    except Exception:  # script missing: still register the flags so --help works
        sp.add_argument("--season", type=int, default=2026)
        sp.add_argument("--week", type=int, required=True)
        sp.add_argument("--limit", type=int)
        sp.add_argument("--offline", action="store_true")
        sp.add_argument("--sigma", type=float)
        sp.add_argument("--pre-minutes", type=int, default=30)
        sp.add_argument("--post-minutes", type=int, default=10)
        sp.add_argument("--out-dir", default=str(ROOT / "out"))
        sp.add_argument("--results", default=str(ROOT / "tests" / "fixtures" / "results" / "lines_eval_p13.json"))
    sp.set_defaults(func=cmd_lines_eval, handler=cmd_lines_eval)
    return {"lines-eval": sp}


__all__ = ["register", "cmd_lines_eval"]
