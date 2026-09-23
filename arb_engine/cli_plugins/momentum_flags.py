"""Offline momentum evaluation without changing execution defaults."""
import json
from pathlib import Path
from ..strategy.momentum import replay


def run(args, settings):
    result = replay(json.loads(Path(args.fixture).read_text()))
    rendered = json.dumps(result, indent=2, sort_keys=True) + '\n'
    if args.output:
        Path(args.output).write_text(rendered)
    print(rendered, end='')
    return 0


def register(subparsers, existing_parsers):
    parser = subparsers.add_parser('momentum-replay', help='Score experimental momentum against persistence on an offline tick fixture')
    parser.add_argument('fixture')
    parser.add_argument('--output', help='Write metrics-only JSON')
    parser.set_defaults(func=run)
