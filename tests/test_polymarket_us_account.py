"""Credential helper regressions using fake local secrets, never an account."""
import base64
import contextlib
import importlib.util
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("polymarket_us_account_setup", Path(__file__).parents[1] / "scripts" / "polymarket_us_account.py")
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)
KEY_ID = "00000000-0000-0000-0000-000000000001"
FAKE_SECRET = base64.b64encode(bytes(range(32))).decode()


class AccountSetupTests(unittest.TestCase):
    def test_secret_format_32_or_64_bytes(self):
        self.assertEqual(HELPER.secret_bytes(FAKE_SECRET), bytes(range(32)))
        self.assertEqual(HELPER.secret_bytes(base64.b64encode(bytes(range(64))).decode()), bytes(range(32)))
        for bad in ("not a key", "", base64.b64encode(bytes(31)).decode()):
            with self.assertRaises(ValueError):
                HELPER.secret_bytes(bad)

    def test_atomic_local_save_mode_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "keys.env"
            HELPER.save_credentials(path, KEY_ID, FAKE_SECRET)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(HELPER.load_credentials(path), {"POLYMARKET_KEY_ID": KEY_ID, "POLYMARKET_SECRET_KEY": FAKE_SECRET})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_symlinks_and_world_readable_files_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys.env"
            HELPER.save_credentials(path, KEY_ID, FAKE_SECRET)
            link = Path(directory) / "link.env"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                HELPER.load_credentials(link)
            with self.assertRaises(ValueError):
                HELPER.save_credentials(link, KEY_ID, FAKE_SECRET)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                HELPER.load_credentials(path)

    def test_exact_signing_message_and_secret_not_transmitted(self):
        with mock.patch.object(HELPER, "_sign", return_value=b"signature") as signer:
            headers = HELPER.auth_headers({"POLYMARKET_KEY_ID": KEY_ID, "POLYMARKET_SECRET_KEY": FAKE_SECRET}, 1234)
        signer.assert_called_once_with(bytes(range(32)), b"1234GET/v1/account/balances")
        self.assertEqual(headers["X-PM-Signature"], base64.b64encode(b"signature").decode())
        self.assertNotIn(FAKE_SECRET, str(headers))
        self.assertEqual(headers["X-PM-Timestamp"], "1234")

    def test_redirect_refused(self):
        with self.assertRaises(ValueError):
            HELPER.NoRedirect().redirect_request(None, None, None, None, None, "https://other.example")

    def test_setup_requires_own_terminal_no_echo_or_network(self):
        with mock.patch.object(HELPER, "DEFAULT_FILE", Path("/not-a-real-credential-file")), \
             mock.patch.object(HELPER.sys.stdin, "isatty", return_value=False), \
             mock.patch.object(HELPER.getpass, "getpass", side_effect=AssertionError("must not prompt")), \
             mock.patch.object(HELPER, "check_account", side_effect=AssertionError("must not contact account")), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(HELPER.main(["setup", "--key-id", KEY_ID]), 2)
        self.assertNotIn(FAKE_SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
