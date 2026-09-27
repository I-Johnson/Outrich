import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-auth-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.core.auth import hash_password, verify_password
from app.core.tenancy import OwnerStore
from app.db import SQLiteStore


class PasswordTests(unittest.TestCase):
    def test_hash_round_trip(self):
        stored = hash_password("correct horse")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn("correct horse", stored)
        self.assertTrue(verify_password("correct horse", stored))
        self.assertFalse(verify_password("wrong horse", stored))
        self.assertFalse(verify_password("anything", ""))
        self.assertFalse(verify_password("anything", "plain$text"))
        self.assertNotEqual(stored, hash_password("correct horse"))


class SignupLoginTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.core import freight as freight_core
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "auth.db"))
        self.raw.init()
        from app import db as app_db
        for target in (patch.object(main, "store", self.raw), patch.object(freight_core, "store", self.raw), patch.object(app_db, "store", self.raw)):
            target.start(); self.addCleanup(target.stop)
        main.LOGIN_ATTEMPTS.clear(); self.addCleanup(main.LOGIN_ATTEMPTS.clear)
        self.client = TestClient(main.app)

    def signup(self, client=None, email="Driver@Example.com", password="longenough1", name="Dee"):
        return (client or self.client).post("/signup", data={"name": name, "email": email, "password": password}, follow_redirects=False)

    def test_signup_creates_user_session_and_lands_on_overview(self):
        response = self.signup()
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/?notice="))
        users = self.raw.list("app_users", {"email": "driver@example.com"}, order="", limit=5)
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["role"], "user")
        self.assertTrue(verify_password("longenough1", users[0]["password_hash"]))
        owner = OwnerStore(self.raw, users[0]["id"])
        self.assertTrue(owner.get("freight_settings", 1))
        self.assertTrue(owner.list("email_templates", {"vertical": "freight"}))
        page = self.client.get("/freight")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('href="/leads"', page.text)
        # Every signed-in user gets the Freight/Outreach workspace toggle.
        self.assertIn("workspace-switch", page.text)

    def test_signup_validation_and_duplicates(self):
        self.assertIn("valid+email", self.signup(email="not-an-email").headers["location"])
        self.assertIn("at+least", self.signup(password="short").headers["location"])
        self.assertEqual(self.raw.list("app_users", {"role": "user"}, order="", limit=5), [])
        self.signup()
        from fastapi.testclient import TestClient
        other = TestClient(self.main.app)
        response = self.signup(client=other, email="driver@example.com ")
        self.assertIn("already+has+an+account", response.headers["location"])
        self.assertEqual(len(self.raw.list("app_users", {"email": "driver@example.com"}, order="", limit=5)), 1)
        self.assertIn("already+has+an+account", self.signup(client=other, email=self.main.env.ADMIN_EMAIL).headers["location"])

    def test_login_logout_and_wrong_password(self):
        self.signup(); self.client.post("/logout", follow_redirects=False)
        self.assertEqual(self.client.get("/freight", follow_redirects=False).status_code, 303)
        bad = self.client.post("/login", data={"email": "driver@example.com", "password": "nope-nope"}, follow_redirects=False)
        self.assertIn("Invalid", bad.headers["location"])
        good = self.client.post("/login", data={"email": "DRIVER@example.com", "password": "longenough1"}, follow_redirects=False)
        self.assertEqual(good.headers["location"], "/")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/freight").status_code, 200)

    def test_customer_reaches_only_the_customer_ia(self):
        self.signup()
        for path in ("/", "/campaigns", "/settings"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 200, path)
        # Admin tools stay admin-only.
        for path in ("/leads", "/templates", "/scraper", "/pingram-inbox", "/imports"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303, path)
            self.assertEqual(response.headers["location"], "/freight", path)
        # Customers manage their own senders and templates.
        self.assertEqual(self.client.post("/settings/gmail-senders", data={"email": "x@gmail.com", "app_password": "abcd efgh ijkl mnop"}, follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.post("/email-log/some-id/send-now", follow_redirects=False).headers["location"], "/freight")
        for vertical in ("outreach", "freight"):
            saved = self.client.post("/templates/save", data={"name": f"Mine {vertical}", "subject": "Load", "body": "Hi", "vertical": vertical, "active": "on"}, follow_redirects=False)
            self.assertEqual(saved.status_code, 303, vertical)
        owner = OwnerStore(self.raw, str(self.raw.list("app_users", {"email": "driver@example.com"}, order="", limit=1)[0]["id"]))
        self.assertEqual([t["name"] for t in owner.list("email_templates", {"vertical": "outreach", "name": "Mine outreach"})], ["Mine outreach"])
        settings = self.client.get("/freight/settings")
        self.assertNotIn('href="/admin/login"', settings.text)
        self.assertIn("driver@example.com", settings.text)

    def test_env_admin_login_lands_on_outreach_overview(self):
        creds = {"email": self.main.env.ADMIN_EMAIL, "password": self.main.env.ADMIN_PASSWORD}
        response = self.client.post("/login", data=creds, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        for path in ("/", "/leads", "/campaigns", "/settings", "/templates", "/scraper", "/pingram-inbox"):
            page = self.client.get(path, follow_redirects=False)
            self.assertEqual(page.status_code, 200, path)
        overview = self.client.get("/")
        self.assertIn("workspace-switch", overview.text)
        freight = self.client.get("/freight/settings")
        self.assertNotIn('href="/admin/login"', freight.text)

    def test_db_admin_role_login_gets_full_workspace(self):
        from app.core.auth import hash_password
        from app.db import new_id, now_iso
        stamp = now_iso()
        self.raw.insert("app_users", {"id": new_id(), "email": "ops@example.com", "password_hash": hash_password("longenough2"), "name": "Ops", "role": "admin", "created_at": stamp, "last_login_at": stamp})
        response = self.client.post("/login", data={"email": "ops@example.com", "password": "longenough2"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/campaigns").status_code, 200)

    def test_admin_login_page_redirects_to_single_login(self):
        response = self.client.get("/admin/login", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        posted = self.client.post("/admin/login", data={"email": self.main.env.ADMIN_EMAIL, "password": self.main.env.ADMIN_PASSWORD}, follow_redirects=False)
        self.assertEqual(posted.status_code, 303)
        self.assertEqual(posted.headers["location"], "/login")
        # The retired endpoint must not create a session: visitor state persists.
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/leads", follow_redirects=False).status_code, 303)

    def test_ordinary_user_is_not_elevated(self):
        self.signup()
        for path in ("/leads", "/pingram-inbox", "/scraper", "/templates"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303, path)
            self.assertEqual(response.headers["location"], "/freight", path)
        # The customer IA is reachable but shows none of the admin nav.
        overview = self.client.get("/")
        self.assertEqual(overview.status_code, 200)
        self.assertNotIn('href="/leads"', overview.text)
        self.assertNotIn('href="/pingram-inbox"', overview.text)
        freight = self.client.get("/freight")
        self.assertIn("workspace-switch", freight.text)
        self.assertNotIn('href="/admin/login"', self.client.get("/freight/settings").text)

    def test_admin_sign_out_returns_to_visitor_state(self):
        creds = {"email": self.main.env.ADMIN_EMAIL, "password": self.main.env.ADMIN_PASSWORD}
        self.client.post("/login", data=creds, follow_redirects=False)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post("/logout", follow_redirects=False)
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Set the rules.", response.text)
        self.assertEqual(self.client.get("/leads", follow_redirects=False).status_code, 303)

    def test_rate_limit_trips_after_eight_failures(self):
        for _ in range(8):
            self.client.post("/login", data={"email": "x@example.com", "password": "wrong-pass"}, follow_redirects=False)
        response = self.client.post("/login", data={"email": self.main.env.ADMIN_EMAIL, "password": self.main.env.ADMIN_PASSWORD}, follow_redirects=False)
        self.assertIn("Too+many", response.headers["location"])

    def test_signed_out_requests_redirect_to_login(self):
        for path in ("/freight", "/freight/settings", "/leads"):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].startswith("/login"))
        for path in ("/", "/login", "/signup", "/health"):
            self.assertEqual(self.client.get(path, follow_redirects=False).status_code, 200, path)
        admin_login = self.client.get("/admin/login", follow_redirects=False)
        self.assertEqual(admin_login.status_code, 303)
        self.assertEqual(admin_login.headers["location"], "/login")

    def test_landing_page_for_visitors_only(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Set the rules.", page.text)
        self.assertIn('href="/signup"', page.text)
        self.assertIn('href="/login"', page.text)
        self.assertNotIn('href="#"', page.text)
        self.signup()
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Find people. Start conversations.", response.text)
        self.assertIn('href="/freight"', response.text)
        self.client.post("/logout")
        self.client.post("/login", data={"email": self.main.env.ADMIN_EMAIL, "password": self.main.env.ADMIN_PASSWORD})
        admin = self.client.get("/", follow_redirects=False)
        self.assertEqual(admin.status_code, 200)
        self.assertIn("workspace-switch", admin.text)


if __name__ == "__main__":
    unittest.main()
