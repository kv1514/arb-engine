"""CLI plugin registry: built-in ``--help`` is pinned byte-for-byte (tests/fixtures/cli_help)
and a scratch plugin package can add flags, subcommands and override dispatch."""

import argparse
import contextlib
import io
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from arb_engine import cli, cli_plugins

GOLDEN = Path(__file__).parent / "fixtures" / "cli_help"
SUBCOMMANDS = ["scan", "quote", "fees", "kelly", "rh-event", "bridge", "maker", "inplay", "games", "live", "record", "stats", "backtest", "kalshi"]


def _help(argv: list[str]) -> str:
    buf = io.StringIO()
    with mock.patch.dict(os.environ, {"COLUMNS": "100"}), contextlib.redirect_stdout(buf):
        p, _ = cli.build_parser()
        with self_exit():
            p.parse_args(argv + ["--help"])
    return buf.getvalue()


@contextlib.contextmanager
def self_exit():
    try:
        yield
    except SystemExit as e:
        assert e.code == 0, e.code


class _PluginDir(contextlib.AbstractContextManager):
    """Point ``arb_engine.cli_plugins.__path__`` at a scratch directory holding ``modules``."""

    def __init__(self, modules: dict[str, str]):
        self.modules = modules

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name, src in self.modules.items():
            Path(self.tmp.name, f"{name}.py").write_text(textwrap.dedent(src), encoding="utf-8")
        self._saved_path = list(cli_plugins.__path__)
        cli_plugins.__path__ = [self.tmp.name]
        for name in self.modules:
            sys.modules.pop(f"arb_engine.cli_plugins.{name}", None)
        return self

    def __exit__(self, *exc):
        cli_plugins.__path__ = self._saved_path
        for name in self.modules:
            sys.modules.pop(f"arb_engine.cli_plugins.{name}", None)
        self.tmp.cleanup()
        return False


PLUGIN = '''
    CALLS = []

    def register(subparsers, existing_parsers):
        existing_parsers["scan"].add_argument("--zz-extra", type=int, default=0, help="added by test plugin")
        sp = subparsers.add_parser("zz-hello", help="test plugin subcommand")
        sp.add_argument("--n", type=int, default=1)
        sp.set_defaults(func=cmd_hello)
        return {"stats": cmd_stats_override}

    def cmd_hello(args, settings):
        CALLS.append(("hello", args.n, settings))
        return 5

    def cmd_stats_override(args, settings):
        CALLS.append(("stats", args.db, settings))
        return 7
'''


class GoldenHelpTest(unittest.TestCase):
    def test_every_builtin_help_is_unchanged_without_plugins(self):
        with _PluginDir({}):
            for cmd in [None] + SUBCOMMANDS:
                got = _help([cmd] if cmd else [])
                want = (GOLDEN / f"{cmd or 'root'}.txt").read_text(encoding="utf-8")
                self.assertEqual(got, want, f"--help of {cmd or 'root'} changed")

    def test_builtin_subcommand_list_is_complete(self):
        with _PluginDir({}):
            p, overrides = cli.build_parser()
        sub = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)][0]
        self.assertEqual(sorted(sub.choices), sorted(SUBCOMMANDS))
        self.assertEqual(overrides, {})
        for name, sp in sub.choices.items():
            self.assertTrue(callable(sp.get_default("func")), name)


class PluginRegistryTest(unittest.TestCase):
    def test_plugin_adds_flag_subcommand_and_override(self):
        with _PluginDir({"zz_test_flags": PLUGIN, "_private_ignored": "raise RuntimeError('must not import')"}):
            p, overrides = cli.build_parser()
            args = p.parse_args(["scan", "--zz-extra", "3"])
            self.assertEqual(args.zz_extra, 3)
            self.assertIs(args.func, cli.cmd_scan)
            self.assertEqual(p.parse_args(["zz-hello", "--n", "4"]).n, 4)
            self.assertEqual(list(overrides), ["stats"])
            mod = sys.modules["arb_engine.cli_plugins.zz_test_flags"]
            self.assertEqual(cli.main(["zz-hello", "--n", "9"]), 5)
            self.assertEqual(cli.main(["stats", "--db", "nope.sqlite"]), 7)
        kinds = [c[0] for c in mod.CALLS]
        self.assertEqual(kinds, ["hello", "stats"])
        self.assertEqual(mod.CALLS[0][1], 9)
        self.assertEqual(mod.CALLS[1][1], "nope.sqlite")
        settings = mod.CALLS[0][2]
        self.assertIn("robinhood_gold", settings)  # handlers receive config.load_settings()
        self.assertIn("kalshi_rounding", settings)

    def test_broken_plugin_is_skipped_not_fatal(self):
        err = io.StringIO()
        with _PluginDir({"aa_broken_flags": "raise ImportError('missing sibling item')", "bb_bad_register": "def register(s, p):\n    p['scan'].add_argument('--sport')\n"}), contextlib.redirect_stderr(err):
            p, overrides = cli.build_parser()
        self.assertEqual(overrides, {})
        self.assertIn("aa_broken_flags skipped: ImportError", err.getvalue())
        self.assertIn("bb_bad_register skipped: ArgumentError", err.getvalue())
        self.assertEqual(p.parse_args(["scan"]).sport, "nfl")

    def test_later_plugin_sees_earlier_plugin_subcommand(self):
        second = '''
            def register(subparsers, existing_parsers):
                existing_parsers["zz-hello"].add_argument("--from-second", action="store_true")
        '''
        with _PluginDir({"zz_test_flags": PLUGIN, "zzz_second_flags": second}):
            p, _ = cli.build_parser()
            self.assertTrue(p.parse_args(["zz-hello", "--from-second"]).from_second)

    def test_build_parser_can_skip_plugins(self):
        with _PluginDir({"zz_test_flags": PLUGIN}):
            p, overrides = cli.build_parser(plugins=False)
        self.assertEqual(overrides, {})
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            p.parse_args(["zz-hello"])


class DispatchTest(unittest.TestCase):
    def test_handler_shapes(self):
        seen = []
        ns = argparse.Namespace(cmd="x")

        def two(args, settings):
            seen.append(("two", settings))
            return 3

        def one(args):
            seen.append(("one",))
            return None

        self.assertEqual(cli._call_handler(two, ns, {"k": 1}), 3)
        self.assertEqual(cli._call_handler(one, ns, {"k": 1}), 0)
        self.assertEqual(cli._call_handler(lambda a, s=None: 2, ns, {}), 2)
        self.assertEqual(seen, [("two", {"k": 1}), ("one",)])

    def test_builtin_handlers_accept_settings(self):
        with _PluginDir({}):
            p, _ = cli.build_parser()
        sub = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)][0]
        import inspect

        for name, sp in sub.choices.items():
            params = inspect.signature(sp.get_default("func")).parameters
            self.assertIn("settings", params, name)

    def test_fees_handler_end_to_end(self):
        buf = io.StringIO()
        with _PluginDir({}), contextlib.redirect_stdout(buf):
            rc = cli.main(["fees", "--venue", "kalshi", "--price", "0.5", "--contracts", "10"])
        self.assertEqual(rc, 0)
        self.assertIn("kalshi: 10.0 contracts @ 0.5000 (taker) -> fee $0.18", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
