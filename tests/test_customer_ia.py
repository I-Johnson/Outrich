"""Customer IA: the signed-in customer experience (Overview / Campaigns / Settings)."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-customer-ia-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore
from app.db import SQLiteStore, new_id, now_iso

STAMP = "2026-09-26T12:00:00+00:00"


class CustomerIATests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.core import freight as freight_core
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "ia.db"))
        self.raw.init()
        from app import db as app_db
        for target in (patch.object(main, "store", self.raw), patch.object(freight_core, "store", self.raw), patch.object(app_db, "store", self.raw)):
            target.start(); self.addCleanup(target.stop)
        main.LOGIN_ATTEMPTS.clear(); self.addCleanup(main.LOGIN_ATTEMPTS.clear)
        self.client = TestClient(main.app)
        self.client.post("/signup", data={"name": "Dee", "email": "driver@example.com", "password": "longenough1"}, follow_redirects=False)
        self.owner_id = str(self.raw.list("app_users", {"email": "driver@example.com"}, order="", limit=1)[0]["id"])
        self.owner = OwnerStore(self.raw, self.owner_id)

    def admin_client(self):
        from fastapi.testclient import TestClient
        client = TestClient(self.main.app)
        client.post("/login", data={"email": self.main.env.ADMIN_EMAIL, "password": self.main.env.ADMIN_PASSWORD}, follow_redirects=False)
        return client

    def test_customer_pages_use_the_customer_shell(self):
        self.assertIn("Find people. Start conversations.", self.client.get("/").text)
        campaigns = self.client.get("/campaigns")
        self.assertIn("One place to build outreach.", campaigns.text)
        self.assertIn("1 Find", campaigns.text)
        settings = self.client.get("/settings")
        self.assertIn("Set the rules in one place.", settings.text)
        self.assertIn("Used templates", settings.text)
        self.assertIn("Sending identity", settings.text)
        for page in (campaigns.text, settings.text):
            self.assertNotIn("Pingram Inbox", page)


    def test_customer_csv_import_stays_in_their_account(self):
        csv = "business_name,email,city,state\nPeak Roofing,peak@example.com,Austin,TX\n"
        preview = self.client.post("/imports/preview", files={"file": ("roofers.csv", csv, "text/csv")}, follow_redirects=False)
        self.assertEqual(preview.status_code, 200)
        batch = self.owner.list("import_batches")[0]
        confirmed = self.client.post(f"/imports/{batch['id']}/confirm", data={"action": "campaign"}, follow_redirects=False)
        self.assertEqual(confirmed.status_code, 303)
        self.assertIn("/campaigns?batch_id=", confirmed.headers["location"])
        leads = self.owner.list("clients", {"import_batch_id": batch["id"]})
        self.assertEqual(len(leads), 1)
        self.assertEqual(str(leads[0].get("owner_id")), self.owner_id)
        builder = self.client.get(f"/campaigns?batch_id={batch['id']}")
        self.assertIn("roofers.csv", builder.text)
        self.assertIn(f'name="import_batch_id" value="{batch["id"]}"', builder.text)

    def test_admin_keeps_the_full_tools(self):
        admin = self.admin_client()
        overview = admin.get("/")
        self.assertIn("Outreach Activity", overview.text)
        for path in ("/leads", "/scraper", "/templates", "/pingram-inbox"):
            self.assertEqual(admin.get(path, follow_redirects=False).status_code, 200, path)

    def test_customer_sender_is_scoped_and_hidden_from_others(self):
        saved = self.client.post("/settings/gmail-senders", data={
            "email": "dee@gmail.com", "app_password": "abcd efgh ijkl mnop", "display_name": "Dee"}, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        mine = self.owner.list("gmail_senders")
        self.assertEqual([s["email"] for s in mine], ["dee@gmail.com"])
        self.assertEqual(OwnerStore(self.raw, ADMIN_OWNER_ID).list("gmail_senders"), [])
        # The settings page offers it as a campaign sender.
        self.assertIn("dee@gmail.com", self.client.get("/settings").text)
        self.assertIn("dee@gmail.com", self.client.get("/campaigns").text)

    def test_discovery_from_builder_is_scoped(self):
        response = self.client.post("/campaigns/discovery", data={
            "categories": "roofing", "locations": "Austin, TX", "result_limit": 30}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        jobs = self.owner.list("scrape_jobs")
        self.assertEqual(len(jobs), 1)
        self.assertEqual((jobs[0]["category"], jobs[0]["city"], jobs[0]["state"]), ("roofing", "Austin", "TX"))
        queue = self.owner.list("jobs", {"kind": "scrape"})
        self.assertEqual(len(queue), 1)
        self.assertEqual(OwnerStore(self.raw, ADMIN_OWNER_ID).list("scrape_jobs"), [])

    def test_campaign_create_and_detail_flow(self):
        self.client.post("/settings/gmail-senders", data={"email": "dee@gmail.com", "app_password": "abcd efgh ijkl mnop", "provider": "gmail"}, follow_redirects=False)
        sender = self.owner.list("gmail_senders")[0]
        template = self.owner.list("email_templates", {"vertical": "outreach"})[0]
        self.owner.insert("clients", {"id": new_id(), "business_name": "Acme Roofing", "short_name": "Acme",
                                      "email": "owner@acme.test", "status": "new", "source": "manual",
                                      "created_at": STAMP, "updated_at": STAMP})
        created = self.client.post("/campaigns/save", data={
            "name": "Roofing in Austin", "template_ids": [str(template["id"])],
            "gmail_accounts": [str(sender["id"])], "provider": "gmail"}, follow_redirects=False)
        self.assertEqual(created.status_code, 303)
        campaign = self.owner.list("campaigns")[0]
        self.assertEqual(campaign["name"], "Roofing in Austin")
        detail = self.client.get(f"/campaigns/{campaign['id']}")
        self.assertIn("Ready when you are.", detail.text)
        self.assertIn("Start sending", detail.text)
        started = self.client.post(f"/campaigns/{campaign['id']}/start", follow_redirects=False)
        self.assertEqual(started.status_code, 303)
        logs = self.owner.list("email_log", {"campaign_id": campaign["id"]})
        self.assertEqual(len(logs), 1)
        detail = self.client.get(f"/campaigns/{campaign['id']}")
        self.assertIn("Acme Roofing", detail.text)
        self.assertIn("Queued", detail.text)

    def test_settings_save_is_scoped_to_the_customer(self):
        saved = self.client.post("/settings", data={
            "sender_name": "Dee", "sender_email": "dee@co.com", "timezone": "America/Chicago",
            "send_days": ["1", "3"], "send_start": "08:00", "send_end": "16:00", "daily_cap": "10"}, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        cfg = self.owner.get("settings", 1)
        self.assertEqual(cfg["sender_name"], "Dee")
        self.assertEqual(cfg["send_days"], [1, 3])
        admin_cfg = OwnerStore(self.raw, ADMIN_OWNER_ID).get("settings", 1)
        self.assertNotEqual(admin_cfg.get("sender_name"), "Dee")


if __name__ == "__main__":
    unittest.main()
