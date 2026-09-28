"""Customer IA: the signed-in customer experience (Overview / Campaigns / Settings)."""
import os
import tempfile
import unittest
from pathlib import Path
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


    def test_audience_count_and_discovery_status_are_scoped(self):
        self.owner.insert("clients", {"id": new_id(), "business_name": "Acme Roofing", "short_name": "Acme", "email": "a@acme.test",
                                      "status": "new", "source": "manual", "category": "roofing", "state": "TX",
                                      "created_at": STAMP, "updated_at": STAMP})
        self.owner.insert("clients", {"id": new_id(), "business_name": "Other Co", "short_name": "Other", "email": "b@other.test",
                                      "status": "new", "source": "manual", "category": "dental", "state": "CA",
                                      "created_at": STAMP, "updated_at": STAMP})
        self.assertEqual(self.client.get("/campaigns/audience-count").json()["count"], 2)
        self.assertEqual(self.client.get("/campaigns/audience-count?category=roofing").json()["count"], 1)
        self.assertEqual(self.client.get("/campaigns/audience-count?category=roofing&state=CA").json()["count"], 0)
        status = self.client.get("/campaigns/discovery-status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("active", status.json())

    def test_discovery_status_explains_scoped_discard_reasons(self):
        job = self.owner.insert("scrape_jobs", {
            "id": new_id(), "category": "insurance adjuster", "city": "Austin", "state": "TX",
            "result_limit": 30, "status": "done", "found_count": 2, "saved_count": 0,
            "discarded_count": 2, "serp_calls_used": 3, "created_at": STAMP, "updated_at": STAMP,
        })
        for name, reason in (("One Adjusting", "no_email_found"), ("Two Adjusting", "email_domain_has_no_mx")):
            self.owner.insert("scrape_discards", {
                "id": new_id(), "scrape_job_id": job["id"], "business_name": name,
                "reason": reason, "created_at": STAMP,
            })
        other = OwnerStore(self.raw, ADMIN_OWNER_ID)
        other_job = other.insert("scrape_jobs", {
            "id": new_id(), "category": "private", "city": "Dallas", "state": "TX",
            "result_limit": 1, "status": "done", "found_count": 1, "saved_count": 0,
            "discarded_count": 1, "serp_calls_used": 1, "created_at": STAMP, "updated_at": STAMP,
        })
        other.insert("scrape_discards", {
            "id": new_id(), "scrape_job_id": other_job["id"], "business_name": "Private",
            "reason": "duplicate_email", "created_at": STAMP,
        })

        body = self.client.get("/campaigns/discovery-status").json()
        self.assertEqual(len(body["jobs"]), 1)
        self.assertEqual(body["jobs"][0]["discarded_count"], 2)
        self.assertEqual(
            {item["reason"] for item in body["jobs"][0]["discard_reasons"]},
            {"no_email_found", "email_domain_has_no_mx"},
        )
        page = self.client.get("/campaigns").text
        self.assertIn("1 no email found", page)
        self.assertIn("1 email domain has no mail server", page)
        self.assertNotIn("duplicate email", page)

    def test_discovery_status_names_serpapi_auth_failure_without_worker_instruction(self):
        self.owner.insert("scrape_jobs", {
            "id": new_id(), "category": "roofing contractors", "city": "Austin", "state": "TX",
            "result_limit": 30, "status": "failed", "found_count": 0, "saved_count": 0,
            "discarded_count": 0, "serp_calls_used": 0,
            "error": "SerpApi rejected SERP_API_KEY; verify the key and account access.",
            "created_at": STAMP, "updated_at": STAMP,
        })
        job = self.client.get("/campaigns/discovery-status").json()["jobs"][0]
        self.assertEqual(job["user_error"], "SerpApi rejected the API key. Verify the key and account access.")
        self.assertNotIn("worker", job["user_error"].lower())


    def test_empty_send_days_blocks_queueing(self):
        from app.core.sender import _schedule_config
        self.assertEqual(_schedule_config({})["send_days"], [0, 1, 2, 3, 4])
        self.assertEqual(_schedule_config({"send_days": []})["send_days"], [])
        self.client.post("/settings/gmail-senders", data={
            "email": "dee@gmail.com", "app_password": "abcd efgh ijkl mnop", "provider": "gmail"}, follow_redirects=False)
        sender = self.owner.list("gmail_senders")[0]
        template = self.owner.list("email_templates")[0]
        self.owner.insert("clients", {"id": new_id(), "business_name": "Acme Roofing", "short_name": "Acme",
                                      "email": "owner@acme.test", "status": "new", "source": "manual",
                                      "created_at": STAMP, "updated_at": STAMP})
        self.client.post("/campaigns/save", data={
            "name": "Roofing", "template_ids": [str(template["id"])],
            "gmail_accounts": [str(sender["id"])], "provider": "gmail"}, follow_redirects=False)
        campaign = self.owner.list("campaigns")[0]
        self.owner.update("settings", 1, {"send_days": []})
        started = self.client.post(f"/campaigns/{campaign['id']}/start", follow_redirects=False)
        self.assertEqual(started.status_code, 303)
        self.assertIn("no%20send%20days", started.headers["location"])
        self.assertEqual(self.owner.list("email_log"), [])
        self.assertEqual(self.owner.get("campaigns", campaign["id"])["state"], "draft")


    def test_fail_closed_when_nobody_is_eligible(self):
        from app.core.sender import queue_campaign
        self.client.post("/settings/gmail-senders", data={
            "email": "dee@gmail.com", "app_password": "abcd efgh ijkl mnop", "provider": "gmail"}, follow_redirects=False)
        template = self.owner.list("email_templates")[0]
        sender_id = str(self.owner.list("gmail_senders")[0]["id"])
        # Saving with no leads at all is refused and creates nothing.
        blocked = self.client.post("/campaigns/save", data={
            "name": "Empty", "template_ids": [str(template["id"])],
            "gmail_accounts": [sender_id], "provider": "gmail"}, follow_redirects=False)
        self.assertEqual(blocked.status_code, 303)
        self.assertIn("No+leads+match", blocked.headers["location"])
        self.assertEqual(self.owner.list("campaigns"), [])
        # A lead that was just contacted is matching but not eligible.
        lead = self.owner.insert("clients", {"id": new_id(), "business_name": "Acme Roofing", "short_name": "Acme",
                                             "email": "owner@acme.test", "status": "new", "source": "manual",
                                             "created_at": STAMP, "updated_at": STAMP})
        campaign = self.owner.insert("campaigns", {
            "id": new_id(), "name": "Manual", "provider": "gmail", "gmail_accounts": [sender_id],
            "template_ids": [template["id"]], "target_filter": {}, "resend_block_days": 90,
            "state": "draft", "created_at": STAMP, "updated_at": STAMP})
        self.owner.insert("email_log", {"id": new_id(), "client_id": lead["id"], "template_id": template["id"],
                                        "campaign_id": campaign["id"], "provider": "gmail", "status": "sent",
                                        "subject_sent": "Hi", "body_sent": "Hello",
                                        "sent_at": STAMP, "created_at": STAMP})
        still_blocked = self.client.post("/campaigns/save", data={
            "name": "Empty", "template_ids": [str(template["id"])],
            "gmail_accounts": [sender_id], "provider": "gmail"}, follow_redirects=False)
        self.assertIn("No+leads+are+eligible", still_blocked.headers["location"])
        self.assertEqual([c["name"] for c in self.owner.list("campaigns")], ["Manual"])
        # The count endpoint agrees: 1 matching record, 0 eligible.
        count = self.client.get("/campaigns/audience-count").json()
        self.assertEqual(count["count"], 1)
        self.assertEqual(count["eligible"], 0)
        # queue_campaign itself raises the same fail-closed error.
        with self.assertRaises(ValueError):
            queue_campaign(campaign["id"], storage=self.owner)


    def test_count_gate_latest_filter_wins_race(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        script = Path(__file__).with_name("frontend_race.js")
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

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
