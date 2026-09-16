"""Kalshi RSA-PSS request signing (skipped when `cryptography` is not installed)."""

import base64
import os
import tempfile
import unittest

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

from arb_engine.execution.kalshi import KalshiExecutor
from arb_engine.venues.kalshi import KalshiClient


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class SigningTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fd, self.path = tempfile.mkstemp(suffix=".pem", dir=os.environ.get("TMPDIR"))
        with os.fdopen(fd, "wb") as f:
            f.write(pem)

    def tearDown(self):
        os.unlink(self.path)

    def test_headers_and_signature_verify(self):
        c = KalshiClient(env="demo", api_key="key-123", private_key_path=self.path)
        self.assertTrue(c.has_credentials)
        headers = c._auth_headers("GET", "/portfolio/balance?x=1".split("?")[0])
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "key-123")
        ts = headers["KALSHI-ACCESS-TIMESTAMP"]
        self.assertTrue(ts.isdigit() and len(ts) >= 13)
        msg = (ts + "GET" + "/trade-api/v2/portfolio/balance").encode()
        sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        # verify() raises on failure
        self.key.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())

    def test_missing_credentials_raise(self):
        c = KalshiClient(env="demo", api_key=None, private_key_path=None)
        with self.assertRaises(RuntimeError):
            c._auth_headers("GET", "/portfolio/balance")


class ExecutorSafetyTests(unittest.TestCase):
    def test_dry_run_by_default(self):
        ex = KalshiExecutor(KalshiClient(env="demo"))
        plan = ex.plan("KXNFLGAME-26SEP20PHITEN-PHI", "buy", "yes", 10, 0.72, post_only=True, exchange_index=0)
        res = ex.execute(plan, confirm=False)
        self.assertTrue(res["status"].startswith("DRY_RUN"))
        self.assertEqual(res["payload"]["side"], "bid")
        self.assertEqual(res["payload"]["price"], "0.7200")
        self.assertEqual(res["payload"]["count"], "10")
        self.assertTrue(res["payload"]["post_only"])

    def test_prod_requires_live_flag(self):
        os.environ.pop("ARB_LIVE_TRADING", None)
        ex = KalshiExecutor(KalshiClient(env="prod", api_key="k", private_key_path="/nonexistent.pem"))
        plan = ex.plan("T", "buy", "no", 1, 0.30)
        res = ex.execute(plan, confirm=True)
        self.assertTrue(res["status"].startswith("BLOCKED"))

    def test_plan_validation(self):
        ex = KalshiExecutor(KalshiClient(env="demo"))
        with self.assertRaises(ValueError):
            ex.plan("T", "buy", "yes", 1, 1.5)
        with self.assertRaises(ValueError):
            ex.plan("T", "buy", "yes", 0, 0.5)


if __name__ == "__main__":
    unittest.main()
