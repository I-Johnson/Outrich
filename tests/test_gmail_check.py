import imaplib
import os
import smtplib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-gmail-check-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.core import gmail_check
from app.core.gmail_check import CheckResult, check_gmail_login, clean_app_password, looks_like_app_password
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore
from app.db import SQLiteStore


def _cm(obj):
    cm = MagicMock(); cm.__enter__.return_value = obj; cm.__exit__.return_value = False
    return cm


class CheckGmailLoginTests(unittest.TestCase):
    def test_password_format(self):
        self.assertEqual(clean_app_password(" abcd efgh\tijkl mnop "), "abcdefghijklmnop")
        self.assertTrue(looks_like_app_password("abcdefghijklmnop"))
        self.assertFalse(looks_like_app_password("myGmailPassword!"))
        self.assertFalse(looks_like_app_password("short"))

    def test_success_logs_in_to_smtp_and_imap(self):
        smtp, imap = MagicMock(), MagicMock()
        with patch.object(gmail_check.smtplib, "SMTP_SSL", return_value=_cm(smtp)), patch.object(gmail_check.imaplib, "IMAP4_SSL", return_value=_cm(imap)):
            result = check_gmail_login("a@gmail.com", "abcdefghijklmnop")
        self.assertTrue(result.ok)
        smtp.login.assert_called_once_with("a@gmail.com", "abcdefghijklmnop")
        imap.login.assert_called_once_with("a@gmail.com", "abcdefghijklmnop")
        smtp.send_message.assert_not_called()

    def test_bad_password(self):
        smtp = MagicMock(); smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
        with patch.object(gmail_check.smtplib, "SMTP_SSL", return_value=_cm(smtp)):
            result = check_gmail_login("a@gmail.com", "abcdefghijklmnop")
        self.assertFalse(result.ok)
        self.assertIn("app password", result.message)

    def test_imap_refused(self):
        imap = MagicMock(); imap.login.side_effect = imaplib.IMAP4.error("nope")
        with patch.object(gmail_check.smtplib, "SMTP_SSL", return_value=_cm(MagicMock())), patch.object(gmail_check.imaplib, "IMAP4_SSL", return_value=_cm(imap)):
            result = check_gmail_login("a@gmail.com", "abcdefghijklmnop")
        self.assertFalse(result.ok)
        self.assertIn("IMAP", result.message)

    def test_network_down(self):
        with patch.object(gmail_check.smtplib, "SMTP_SSL", side_effect=OSError("down")):
            result = check_gmail_login("a@gmail.com", "abcdefghijklmnop")
        self.assertFalse(result.ok)
        self.assertIn("reach Gmail", result.message)


class SenderRouteTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app import main
        self.main = main
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "g.db")); self.raw.init()
        p = patch.object(main, "store", self.raw); p.start(); self.addCleanup(p.stop)
        main.LOGIN_ATTEMPTS.clear()
        self.client = TestClient(main.app)
        self.client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)
        main.seed_owner_defaults(ADMIN_OWNER_ID)
        self.admin = OwnerStore(self.raw, ADMIN_OWNER_ID)

    def add(self, password="abcd efgh ijkl mnop"):
        return self.client.post("/freight/senders/add", data={"email": "d@gmail.com", "app_password": password, "auto_select": "on"}, follow_redirects=False)

    def test_failed_check_saves_nothing(self):
        with patch.object(self.main, "check_gmail_login", return_value=CheckResult(False, "Google didn't accept that")) as check:
            response = self.add()
        check.assert_called_once_with("d@gmail.com", "abcdefghijklmnop")
        self.assertIn("error=", response.headers["location"])
        self.assertEqual(self.admin.list("gmail_senders", {"email": "d@gmail.com"}), [])

    def test_normal_password_rejected_before_calling_gmail(self):
        with patch.object(self.main, "check_gmail_login") as check:
            response = self.add(password="MyGmailPassword1!")
        check.assert_not_called()
        self.assertIn("error=", response.headers["location"])

    def test_test_connection_button(self):
        with patch.object(self.main, "check_gmail_login", return_value=CheckResult(True, "Connected.")):
            self.add()
        sender = self.admin.list("gmail_senders", {"email": "d@gmail.com"})[0]
        page = self.client.get("/freight/settings")
        self.assertIn('formaction="/freight/senders/test"', page.text)
        self.assertIn("myaccount.google.com/apppasswords", page.text)
        with patch.object(self.main, "check_gmail_login", return_value=CheckResult(False, "Google didn't accept that")) as check:
            response = self.client.post("/freight/senders/test", data={"default_sender_account": sender["id"]}, follow_redirects=False)
        check.assert_called_once_with("d@gmail.com", "abcdefghijklmnop")
        self.assertIn("error=", response.headers["location"])
        page = self.client.get(response.headers["location"])
        self.assertIn('class="error"', page.text)
        with patch.object(self.main, "check_gmail_login", return_value=CheckResult(True, "Connected.")):
            response = self.client.post("/freight/senders/test", data={"default_sender_account": sender["id"]}, follow_redirects=False)
        self.assertIn("notice=", response.headers["location"])

    def test_cannot_test_another_owners_sender(self):
        other = OwnerStore(self.raw, "someone-else")
        other.insert("gmail_senders", {"id": "theirs", "email": "x@gmail.com", "app_password_encrypted": "", "active": True, "provider": "gmail", "created_at": "t", "updated_at": "t"})
        with patch.object(self.main, "check_gmail_login") as check:
            response = self.client.post("/freight/senders/test", data={"default_sender_account": "theirs"}, follow_redirects=False)
        check.assert_not_called()
        self.assertIn("Choose", response.headers["location"])


if __name__ == "__main__":
    unittest.main()
