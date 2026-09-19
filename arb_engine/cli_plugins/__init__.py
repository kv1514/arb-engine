"""CLI plugins: each feature adds its own flags / subcommands without editing ``cli.py``.

``arb_engine.cli.build_parser`` imports every public module in this package (sorted by
name, ``_private`` modules skipped) and calls its ``register``::

    # arb_engine/cli_plugins/example_flags.py
    def register(subparsers, existing_parsers):
        # 1. add a flag to an existing subcommand
        existing_parsers["scan"].add_argument("--example-threshold", type=float, default=0.0)
        # 2. add a whole new subcommand
        ex = subparsers.add_parser("example", help="demo plugin command")
        ex.add_argument("--n", type=int, default=1)
        ex.set_defaults(func=cmd_example)
        # 3. optionally override the dispatch of an existing subcommand
        return {"stats": cmd_stats_with_extras}

    def cmd_example(args, settings) -> int:
        ...

Contract
--------
* ``register(subparsers, existing_parsers)``: ``subparsers`` is the ``argparse``
  sub-parser action of the root parser; ``existing_parsers`` maps subcommand name ->
  ``ArgumentParser`` for every subcommand registered so far (built-ins first, then earlier
  plugins in name order). Add flags with unique names (``--<feature>-...``) so two plugins
  never collide; argparse raises on a duplicate and the CLI reports which plugin did it.
* The return value is ``None`` or ``{subcommand: handler}``. Handlers take
  ``(args, settings)`` where ``settings`` is ``config.load_settings()``; a one-argument
  ``handler(args)`` is also accepted. The last plugin (name order) to override a
  subcommand wins.
* Settings keys a plugin needs are declared where they are used with
  ``config.declare_setting(...)``, never here.
* A plugin that fails to import or register is reported on stderr and skipped, so one
  broken feature cannot take down ``scan`` for everyone; its own tests exercise it directly.
* Plugins must be importable without any other plan item present: guard cross-item
  imports (``try: ... except ImportError``) and keep flags' defaults inert.

With no plugin modules present the CLI is byte-for-byte what it was before this package
existed (``tests/test_cli_registry.py`` pins the ``--help`` of every built-in subcommand).
"""

from __future__ import annotations
