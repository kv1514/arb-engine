#!/usr/bin/env python3
"""Kept for the README: same as `python3 scripts/build_teams.py --sport ncaaf`."""

import os
import subprocess
import sys

raise SystemExit(subprocess.call([sys.executable, os.path.join(os.path.dirname(__file__), "build_teams.py"), "--sport", "ncaaf"] + sys.argv[1:]))
