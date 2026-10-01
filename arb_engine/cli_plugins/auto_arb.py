"""Automatic public-book paper pairs; live mode is explicitly unavailable."""
import json
import os
import time
from pathlib import Path

try:
    from ..config import declare_setting, setting
except ImportError:  # keep a feature plugin importable with the pre-registry loader
    declare_setting = None
    def setting(settings, key):
        env, default = {'auto_pair_mode': ('ARB_AUTO_PAIR_MODE', 'off'),
                        'order_ledger_dir': ('ARB_ORDER_LEDGER_DIR', 'out/orders')}[key]
        return settings[key] if settings is not None and key in settings else os.environ.get(env, default)
from ..execution.pairpaper import PaperPairs, live_readiness
from .us_arbs import _contracts, _default_adapters, _interval, fetch_snapshots

if declare_setting is not None:
    declare_setting('auto_pair_mode', env='ARB_AUTO_PAIR_MODE', default='off', cast=str,
                    doc='Automatic two-venue runner: off/paper; live fails closed until production paired recovery and settlement proof exist.')


def register(subparsers, existing_parsers):
    parser = subparsers.add_parser('auto-arb', help='automatic two-venue paper pairs; production live mode blocked')
    parser.add_argument('--mode', choices=('off', 'paper', 'live'), default=None)
    parser.add_argument('--every', type=_interval, default=0, help='0 = one public scan; repeat interval >=5s')
    parser.add_argument('--contracts', type=_contracts, default=100)
    parser.add_argument('--status', action='store_true', help='read existing paper ledger without fetching books')
    parser.set_defaults(func=run)


def _path(settings):
    return Path(setting(settings, 'order_ledger_dir')) / 'auto_pair_paper.sqlite3'


def run(args, settings=None):
    mode = args.mode or setting(settings, 'auto_pair_mode')
    if mode == 'live':
        readiness = live_readiness()
        try:
            from .trade_approval import _path as approval_path
            from ..execution.standing_approval import read_status
            readiness['standing_approval'] = read_status(approval_path(settings))
        except Exception:
            readiness['standing_approval'] = {'status': 'UNAVAILABLE', 'approval_active': False,
                                              'execution_enabled': False}
        print(json.dumps(readiness))
        return 3
    if mode not in ('off', 'paper'):
        print(json.dumps({'status': 'BLOCKED', 'reason': 'invalid auto pair mode'}))
        return 3
    if mode == 'off' and not args.status:
        print(json.dumps({'status': 'OFF', 'live_enabled': False, 'orders_submitted': 0}))
        return 0
    path = _path(settings)
    if args.status and not path.is_file():
        print(json.dumps({'mode': 'paper', 'pairs': [], 'held_paper_cash': '0', 'orders_submitted': 0}))
        return 0
    ledger = PaperPairs(path)
    try:
        if args.status:
            print(json.dumps(ledger.status()))
            return 0
        adapters = [a for a in _default_adapters() if a.venue in {'kalshi', 'polymarket_us'}]
        while True:
            started = time.monotonic()
            snapshots = fetch_snapshots(adapters=adapters)
            now = time.time()
            ledger.advance(snapshots, now=now)
            admitted = ledger.admit(snapshots, now=now, contracts=args.contracts, settings=settings)
            result = {**ledger.status(), 'admitted': admitted,
                      'scan_errors': {s.venue: s.errors for s in snapshots if s.errors},
                      'live_readiness': live_readiness()}
            print(json.dumps(result), flush=True)
            if not args.every:
                return 0
            time.sleep(max(0, args.every-(time.monotonic()-started)))
    except KeyboardInterrupt:
        return 0  # paper state persists; there are no real orders to cancel
    finally:
        ledger.close()
