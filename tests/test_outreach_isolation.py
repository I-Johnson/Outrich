"""Outreach data isolation: one account must never see or act on another's data."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-isolation-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.adapters.base import SendResult
from app.core.sender import queue_campaign, send_due
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore, outreach_owner_ids
from app.db import SQLiteStore, new_id, now_iso

STAMP = "2026-09-26T12:00:00+00:00"


def lead(owner_store, email, **extra):
    row = {"id": new_id(), "business_name": "Acme", "short_name": "Acme", "category": "roofing",
           "email": email, "domain": email.split("@")[1], "status": "new", "source": "manual",
           "created_at": STAMP, "updated_at": STAMP}
    row.update(extra)
    return owner_store.insert("clients", row)


class OutreachScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "iso.db"))
        self.raw.init()
        self.a = OwnerStore(self.raw, "user-a")
        self.b = OwnerStore(self.raw, "user-b")
        self.admin = OwnerStore(self.raw, ADMIN_OWNER_ID)

    def test_leads_campaigns_logs_settings_are_scoped(self):
        lead_a = lead(self.a, "same@example.com")
        camp = self.a.insert("campaigns", {"id": new_id(), "name": "A campaign", "state": "draft", "created_at": STAMP, "updated_at": STAMP})
        self.a.insert("email_log", {"id": new_id(), "client_id": lead_a["id"], "campaign_id": camp["id"], "provider": "gmail",
                                    "subject_sent": "Hi", "status": "queued", "scheduled_for": STAMP, "created_at": STAMP})
        self.a.insert("settings", {"sender_name": "A", "created_at": STAMP, "updated_at": STAMP})
        # The same address may exist once per owner.
        lead(self.b, "same@example.com")

        for table in ("clients", "campaigns", "email_log"):
            with self.subTest(table=table):
                self.assertEqual(len(self.a.list(table)), 1)
                self.assertEqual(len(self.b.list(table)), 1 if table == "clients" else 0)
        self.assertIsNone(self.b.get("clients", lead_a["id"]))
        self.assertIsNone(self.b.get("campaigns", camp["id"]))
        self.assertEqual(self.b.get("settings", 1), None)
        self.assertEqual(self.a.get("settings", 1)["sender_name"], "A")
        with self.assertRaises(LookupError):
            self.b.update("clients", lead_a["id"], {"status": "replied"})
        self.assertEqual(self.b.delete("campaigns", {"id": camp["id"]}), 0)

    def test_admin_scope_owns_legacy_null_rows(self):
        # Rows written before isolation carry owner_id NULL and belong to the admin.
        self.raw.insert("clients", {"id": "legacy", "business_name": "Old", "short_name": "Old", "email": "old@x.com",
                                    "status": "new", "source": "manual", "created_at": STAMP, "updated_at": STAMP})
        self.assertEqual([c["id"] for c in self.admin.list("clients")], ["legacy"])
        self.assertEqual(self.b.list("clients"), [])
        self.assertEqual(self.admin.get("clients", "legacy")["email"], "old@x.com")
        self.assertIsNone(self.b.get("clients", "legacy"))

    def test_work_queue_and_replies_are_scoped(self):
        self.a.insert("jobs", {"id": "j-a", "kind": "scrape", "payload": {}, "status": "queued", "run_after": STAMP, "created_at": STAMP, "updated_at": STAMP})
        self.a.insert("pingram_replies", {"id": "r-a", "from_email": "x@y.com", "received_at": STAMP})
        self.assertEqual([j["id"] for j in self.a.list("jobs")], ["j-a"])
        self.assertEqual(self.b.list("jobs"), [])
        self.assertEqual(self.b.list("pingram_replies"), [])
        self.assertIn("user-a", outreach_owner_ids(self.raw))

    def test_queue_campaign_refuses_another_owners_campaign(self):
        camp = self.a.insert("campaigns", {"id": new_id(), "name": "A", "state": "draft", "template_ids": ["t1"], "created_at": STAMP, "updated_at": STAMP})
        self.a.insert("email_templates", {"id": "t1", "name": "T", "subject": "S", "body": "B", "active": True, "vertical": "outreach", "created_at": STAMP, "updated_at": STAMP})
        lead(self.a, "one@example.com")
        self.a.insert("settings", {"created_at": STAMP, "updated_at": STAMP})
        self.a.insert("gmail_senders", {"id": "1", "email": "a@gmail.com", "app_password_encrypted": "", "active": True, "provider": "gmail", "created_at": STAMP, "updated_at": STAMP})
        with self.assertRaises(ValueError):
            queue_campaign(str(camp["id"]), storage=self.b)
        result = queue_campaign(str(camp["id"]), storage=self.a)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(len(self.b.list("email_log")), 0)
        self.assertEqual(len(self.a.list("email_log")), 1)

    def test_send_due_only_touches_the_scoped_owner(self):
        camp = self.a.insert("campaigns", {"id": new_id(), "name": "A", "state": "running", "template_ids": ["t1"], "created_at": STAMP, "updated_at": STAMP})
        self.a.insert("email_templates", {"id": "t1", "name": "T", "subject": "S", "body": "B", "active": True, "vertical": "outreach", "created_at": STAMP, "updated_at": STAMP})
        client = lead(self.a, "due@example.com")
        self.a.insert("settings", {"send_days": [0, 1, 2, 3, 4, 5, 6], "send_start": "00:00", "send_end": "23:59", "timezone": "UTC", "created_at": STAMP, "updated_at": STAMP})
        self.a.insert("gmail_senders", {"id": "1", "email": "a@gmail.com", "app_password_encrypted": "", "active": True, "provider": "gmail", "created_at": STAMP, "updated_at": STAMP})
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.a.insert("email_log", {"id": new_id(), "client_id": client["id"], "campaign_id": camp["id"], "template_id": "t1",
                                    "provider": "gmail", "sender_account": "1", "subject_sent": "S", "status": "queued",
                                    "scheduled_for": past, "created_at": STAMP})
        provider = Mock(); provider.send = Mock(return_value=SendResult(True, provider_id="m-1"))
        provider.user = "a@gmail.com"
        with patch("app.core.sender.get_provider", return_value=provider), patch("app.core.sender.store", self.raw):
            other = send_due(storage=self.b)
            self.assertEqual((other["sent"], other["failed"]), (0, 0))
            provider.send.assert_not_called()
            mine = send_due(storage=self.a)
            self.assertEqual(mine["sent"], 1)
            self.assertEqual(self.a.list("email_log", {"status": "sent"})[0]["provider_message_id"], "m-1")


class LegacyMigrationTests(unittest.TestCase):
    def test_pre_isolation_database_is_backfilled_to_admin(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = os.path.join(temp.name, "legacy.db")
        con = sqlite3.connect(path)
        con.execute("""CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1), sender_name TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        con.execute("INSERT INTO settings(id, sender_name, created_at, updated_at) VALUES(1, 'Boss', 'x', 'x')")
        con.execute("""CREATE TABLE clients (
          id TEXT PRIMARY KEY, business_name TEXT NOT NULL, short_name TEXT NOT NULL,
          owner_first_name TEXT, category TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '', zip TEXT,
          website TEXT, domain TEXT, email TEXT, phone TEXT, source TEXT NOT NULL DEFAULT 'manual',
          source_detail TEXT, import_batch_id TEXT, scrape_job_id TEXT, outreach_angle TEXT, notes TEXT,
          extra TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'new', last_contacted_at TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )""")
        con.execute("CREATE UNIQUE INDEX clients_email_unique ON clients(lower(email)) WHERE email IS NOT NULL AND email <> ''")
        con.execute("INSERT INTO clients(id, business_name, short_name, email, domain, status, created_at, updated_at) VALUES('c1', 'Legacy Co', 'Legacy', 'dup@example.com', 'example.com', 'new', 'x', 'x')")
        con.commit(); con.close()

        store = SQLiteStore(path)
        store.init()
        admin = OwnerStore(store, ADMIN_OWNER_ID)
        other = OwnerStore(store, "new-user")
        self.assertEqual(admin.get("settings", 1)["sender_name"], "Boss")
        self.assertEqual([c["id"] for c in admin.list("clients")], ["c1"])
        self.assertEqual(other.list("clients"), [])
        # Per-owner uniqueness: a customer may reuse the same address.
        other.insert("clients", {"id": "c2", "business_name": "New", "short_name": "New", "email": "dup@example.com",
                                 "status": "new", "source": "manual", "created_at": STAMP, "updated_at": STAMP})
        self.assertEqual(len(other.list("clients")), 1)
        con = sqlite3.connect(path)
        cols = {row[1] for row in con.execute("PRAGMA table_info(clients)")}
        con.close()
        self.assertIn("owner_id", cols)


if __name__ == "__main__":
    unittest.main()
