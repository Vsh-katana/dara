from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from backend import auth
from backend.database import connect
from backend.service import dispatch


class TenantAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.previous = {key: os.environ.get(key) for key in ("DARA_DATA_DIR", "DARA_SESSION_SECRET", "FINANCE_SEED_DEFAULT", "ALLOW_SIGNUP")}
        os.environ["DARA_DATA_DIR"] = self.temp.name
        os.environ["DARA_SESSION_SECRET"] = "s" * 64
        os.environ["FINANCE_SEED_DEFAULT"] = "0"
        os.environ["ALLOW_SIGNUP"] = "1"
        auth.initialize_auth()

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_accounts_get_separate_empty_finance_databases(self):
        alice, alice_token, _ = auth.register("Alice", "a-long-password-001", "127.0.0.1")
        bob, _, _ = auth.register("Bob", "b-long-password-002", "127.0.0.1")
        self.assertNotEqual(auth.tenant_db_path(alice["id"]), auth.tenant_db_path(bob["id"]))
        with auth.tenant_scope(auth.resolve_session(alice_token)):
            created = dispatch("POST", "/api/transactions", {}, {
                "kind": "income", "title": "Alice entry", "amount": "125.50",
                "date": "2026-11-23", "calendar": "gregorian", "category": "Salary",
                "attachment": {"name": "receipt.pdf", "content_base64": "QQ=="},
            })
            self.assertEqual(created["amount"], "125.5")
            attachment = dispatch("GET", f"/api/transactions/{created['id']}/attachment", {}, {})
            self.assertEqual(attachment.body, b"A")
            self.assertTrue((auth.tenant_db_path(alice["id"]).parent / "attachments").is_dir())
        with auth.tenant_scope({"tenant_db": auth.tenant_db_path(bob["id"])}):
            rows = dispatch("GET", "/api/transactions", {"limit": 100}, {})
            self.assertEqual(rows["total"], 0)
            self.assertEqual(dispatch("GET", "/api/bootstrap", {}, {})["counts"], {"income": 0, "expense": 0})
            self.assertFalse((auth.tenant_db_path(bob["id"]).parent / "attachments").exists())

    def test_session_csrf_and_password_rotation(self):
        user, current_token, _ = auth.register("sample_user", "initial-very-long-passphrase", "192.0.2.10")
        other_user, other_token, _ = auth.login("SAMPLE_USER", "initial-very-long-passphrase", "192.0.2.10")
        self.assertEqual(other_user["id"], user["id"])
        current_session = auth.resolve_session(current_token)
        self.assertTrue(auth.verify_csrf(current_session, current_session["csrf_token"]))
        self.assertFalse(auth.verify_csrf(current_session, "wrong-token"))
        auth.change_password(user["id"], current_token, "initial-very-long-passphrase", "replacement-passphrase-2026", "192.0.2.10")
        self.assertIsNotNone(auth.resolve_session(current_token))
        self.assertIsNone(auth.resolve_session(other_token))
        with self.assertRaises(auth.AuthError):
            auth.login("sample_user", "initial-very-long-passphrase", "192.0.2.10")
        signed_in, _, _ = auth.login("sample_user", "replacement-passphrase-2026", "192.0.2.10")
        self.assertEqual(signed_in["id"], user["id"])


if __name__ == "__main__":
    unittest.main()


