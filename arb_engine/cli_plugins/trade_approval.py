"""One-time authenticated standing permission; no order method is called here."""
import json
import shlex
import stat
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from ..execution.ledger import client_identity, default_path
from ..execution.standing_approval import ApprovalStore, PROFILE, read_status
from ..execution.polymarket_us_ioc import decimal
from ..venues.kalshi import KalshiClient, ENV_REST_BASE
from ..venues.polymarket_us_trading import API, NoRedirect, PolymarketUSTradingClient, load_credentials


def register(subparsers, existing_parsers):
    p = subparsers.add_parser('trade-approval', help='arm/revoke standing account-bound pair permission; never sends orders')
    p.add_argument('action', choices=('arm', 'status', 'revoke'))
    p.add_argument('--confirm', action='store_true', help='arm/revoke permission once; does not enable order transports')
    p.add_argument('--hours', default='6', help='permission lifetime, >0 and <=24h (default 6)')
    p.add_argument('--kalshi-env-file', type=Path, default=Path.home()/'.kalshi/prod.env')
    p.add_argument('--us-env-file', type=Path, default=Path(__file__).resolve().parents[2]/'secrets/polymarket_us.env')
    p.set_defaults(func=run)


def _path(settings=None):
    try:
        from ..config import setting
        directory = setting(settings, 'order_ledger_dir')
    except ImportError:
        directory = (settings or {}).get('order_ledger_dir')
    return Path(default_path('prod', directory=directory)).parent/'standing_approval.sqlite3'


class ReadOnlyAccounts:
    def get(self, url, params=None, headers=None):
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'external-api.kalshi.com' or
                parsed.query or parsed.fragment or
                parsed.path not in ('/trade-api/v2/portfolio/balance', '/trade-api/v2/communications/id') or params):
            raise ValueError('account diagnostic scope refused')
        req = urllib.request.Request(url, headers=headers or {}, method='GET')
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=12) as response:
            data = response.read(1000001)
        if len(data) > 1000000:
            raise ValueError('account response too large')
        return json.loads(data)


def _binding(args):
    path = args.kalshi_env_file.expanduser()
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('private Kalshi production env file required')
    values = {}
    for line in path.read_text().splitlines():
        parts = shlex.split(line, comments=True)
        if parts and parts[0] == 'export':
            parts = parts[1:]
        if len(parts) != 1:
            continue
        name, sep, val = parts[0].partition('=')
        if sep and name in {'KALSHI_ENV', 'KALSHI_API_KEY', 'KALSHI_PRIVATE_KEY_PATH'}:
            if name in values:
                raise ValueError('duplicate Kalshi credential field')
            values[name] = val
    if values.get('KALSHI_ENV') != 'prod' or not values.get('KALSHI_API_KEY'):
        raise ValueError('production credentials required')
    key = Path(values.get('KALSHI_PRIVATE_KEY_PATH', '/nonexistent')).expanduser()
    if key.is_symlink() or not key.is_file() or stat.S_IMODE(key.stat().st_mode) & 0o077:
        raise ValueError('private Kalshi signing file required')
    kal = KalshiClient(env='prod', base_url=ENV_REST_BASE['prod'], api_key=values['KALSHI_API_KEY'],
                       private_key_path=str(key), http=ReadOnlyAccounts())
    ident = client_identity(kal, 'prod')
    if ident.error or not ident.key_fp or not ident.account_fp:
        raise ValueError('Kalshi account identity unavailable')
    us = PolymarketUSTradingClient(load_credentials(args.us_env_file))
    if us.base_url != API:
        raise ValueError('US production host required')
    balance = us.balances()
    if not isinstance(balance.get('balances'), list) or not any(r.get('currency') == 'USD' for r in balance['balances'] if isinstance(r, dict)):
        raise ValueError('US authenticated USD account unavailable')
    return {'kalshi_key': ident.key_fp, 'kalshi_account': ident.account_fp, 'polymarket_us_key': us.fingerprint}


def run(args, settings=None):
    ledger = None
    try:
        path = _path(settings)
        if args.action == 'status':
            result = read_status(path)
        elif not args.confirm:
            result = {'status': 'DRY_RUN', 'profile': PROFILE, 'hours': args.hours,
                      'execution_enabled': False, 'note': 'one --confirm arms/revokes permission; no accounts or order endpoints used'}
        elif args.action == 'arm':
            hours = decimal(args.hours)
            if not 0 < hours <= 24:
                raise ValueError('invalid approval lifetime')
            binding = _binding(args)  # authenticated reads only; no ledger created on failed reads
            ledger = ApprovalStore(path)
            result = ledger.arm(binding, hours=args.hours, settings=settings)
        elif not path.exists():
            result = {'status': 'UNARMED', 'approval_active': False, 'execution_enabled': False}
        else:
            ledger = ApprovalStore(path)
            result = ledger.revoke()
        print(json.dumps(result, default=str))
        return 0
    except Exception:
        print(json.dumps({'status': 'BLOCKED', 'execution_enabled': False,
                          'reason': 'invalid input, unverified accounts or unavailable private permission store; nothing sent'}))
        return 3
    finally:
        if ledger is not None:
            ledger.close()
