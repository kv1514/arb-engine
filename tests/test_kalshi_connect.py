"""scripts/kalshi_connect.py: the read-only "is my Kalshi account connected?" walk.

Offline: a throwaway RSA key and a FakeHttp stand in for Kalshi. The point of each test is
that the first broken step is the one reported, with the fix, and that the key's contents
never reach the output.
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from arb_engine.venues.http import HttpError

from .helpers import FakeHttp

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("kalshi_connect", ROOT / "scripts" / "kalshi_connect.py")
kc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kc)


def _raise(status):
    def f():
        raise HttpError(status, "https://x/portfolio/balance", '{"error":"unauthorized"}')
    return f


class ConnectCheckTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.key = os.path.join(self.dir, "kalshi.key")
        Path(self.key).write_bytes(self.pem)
        os.chmod(self.key, 0o600)

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def _http(self, balance=None):
        return FakeHttp({"/exchange/status": {"trading_active": True},
                         "/portfolio/balance": balance if balance is not None else {"balance": 123456, "portfolio_value": 7800}})

    def test_all_four_steps_pass_and_report_the_balance_in_dollars(self):
        http = self._http()
        rows = kc.check("demo", "key-id-1", self.key, http=http)
        self.assertEqual([r[0] for r in rows], ["PASS"] * 4, rows)
        self.assertIn("cash $1,234.56, positions $78.00", rows[-1][2])
        # Read-only: the only calls are the status and the balance, both GETs.
        self.assertTrue(all("/exchange/status" in u or "/portfolio/balance" in u for u in http.calls), http.calls)
        # The key's contents never appear in what is printed.
        body = self.pem.decode().splitlines()[1]
        self.assertFalse(any(body in r[2] for r in rows))

    def test_missing_variables_stop_at_step_one(self):
        rows = kc.check("demo", None, self.key)
        self.assertEqual(rows, [("FAIL", "variables", rows[0][2])])
        self.assertIn("KALSHI_API_KEY not set", rows[0][2])

    def test_a_wrong_file_is_named(self):
        bad = os.path.join(self.dir, "notes.txt")
        Path(bad).write_text("this is not a key\n")
        rows = kc.check("demo", "k", bad, http=self._http())
        self.assertEqual(rows[-1][:2], ("FAIL", "key file"))
        self.assertIn("not an unencrypted PEM private key", rows[-1][2])
        rows = kc.check("demo", "k", os.path.join(self.dir, "nope.key"), http=self._http())
        self.assertIn("does not exist", rows[-1][2])

    def test_a_world_readable_key_warns_but_still_connects(self):
        os.chmod(self.key, 0o644)
        rows = kc.check("demo", "k", self.key, http=self._http())
        self.assertEqual(rows[1][0], "WARN")
        self.assertIn(f"chmod 600 {self.key}", rows[1][2])
        self.assertEqual(rows[-1][0], "PASS")

    def test_401_names_the_demo_vs_production_mismatch(self):
        rows = kc.check("prod", "k", self.key, http=self._http(balance=_raise(401)))
        self.assertEqual(rows[-1][:2], ("FAIL", "signed read"))
        self.assertIn("a demo key on the prod host", rows[-1][2])
        rows = kc.check("demo", "k", self.key, http=self._http(balance=_raise(401)))
        self.assertIn("a production key on the demo host", rows[-1][2])

    def test_env_file_fills_only_unset_kalshi_variables(self):
        f = Path(self.dir, "env")
        f.write_text('# my key\nexport KALSHI_API_KEY="abc"\nKALSHI_PRIVATE_KEY_PATH=~/.kalshi/prod.key\nexport OTHER=1\n')
        env = {"KALSHI_API_KEY": "already-exported"}
        names = kc.load_env_file(f, env)
        self.assertEqual(names, ["KALSHI_PRIVATE_KEY_PATH"])
        self.assertEqual(env["KALSHI_API_KEY"], "already-exported")          # exported wins
        self.assertEqual(env["KALSHI_PRIVATE_KEY_PATH"], os.path.expanduser("~/.kalshi/prod.key"))
        self.assertNotIn("OTHER", env)                                     # only KALSHI_* is read
        self.assertEqual(kc.load_env_file(Path(self.dir, "missing"), {}), [])


if __name__ == "__main__":
    unittest.main()


class InstallKeyScriptTests(unittest.TestCase):
    """scripts/kalshi_install_key.sh: file moved into ~/.kalshi with tight modes, env written."""

    def setUp(self):
        self.home = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        self.dl = os.path.join(self.home, "ARB.txt")
        Path(self.dl).write_bytes(self.pem)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.home, ignore_errors=True)

    def _run(self, *args):
        import subprocess

        env = {**os.environ, "HOME": self.home, "KALSHI_INSTALL_NO_CHECK": "1"}
        return subprocess.run(["bash", str(ROOT / "scripts" / "kalshi_install_key.sh"), *args], env=env, capture_output=True, text=True, timeout=30)

    def test_installs_the_file_and_settings_with_owner_only_modes(self):
        r = self._run(self.dl, "c2d58352-0000-4067-b38f-000000000000", "demo")
        self.assertEqual(r.returncode, 0, r.stderr)
        dest = Path(self.home, ".kalshi", "demo.key")
        self.assertFalse(os.path.exists(self.dl))                        # moved, not copied
        self.assertEqual(dest.read_bytes(), self.pem)
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(Path(self.home, ".kalshi")).st_mode & 0o777, 0o700)
        envf = Path(self.home, ".kalshi", "env")
        self.assertEqual(os.stat(envf).st_mode & 0o777, 0o600)
        self.assertEqual(envf.read_text().splitlines(), ["export KALSHI_ENV=demo", "export KALSHI_API_KEY=c2d58352-0000-4067-b38f-000000000000", f"export KALSHI_PRIVATE_KEY_PATH={dest}"])
        # Nothing the script prints contains the key.
        self.assertNotIn(self.pem.decode().splitlines()[1], r.stdout + r.stderr)
        # And the checker's reader loads exactly those settings.
        env = {}
        kc.load_env_file(envf, env)
        self.assertEqual(env["KALSHI_PRIVATE_KEY_PATH"], str(dest))

    def test_refuses_a_file_that_is_not_a_private_key_and_a_malformed_id(self):
        notes = os.path.join(self.home, "notes.txt")
        Path(notes).write_text("hello\n")
        r = self._run(notes, "abc-123")
        self.assertEqual(r.returncode, 2)
        self.assertIn("PRIVATE KEY header", r.stderr)
        self.assertTrue(os.path.exists(notes))                          # left where it was
        r = self._run(self.dl, "abc 123; rm -rf ~")
        self.assertEqual(r.returncode, 2)
        self.assertIn("letters, digits and dashes", r.stderr)
        self.assertTrue(os.path.exists(self.dl))
        self.assertEqual(self._run(self.dl, "abc", "staging").returncode, 2)
