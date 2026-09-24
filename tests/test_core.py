import os
import errno
import json
import sqlite3
import smtplib
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.core.importer import MAX_CSV_BYTES, _gemini_json, clean_row, confirm_import, decode_csv, gemini_cleanup_rows, gemini_mapping, heuristic_mapping, select_mapping, undo_import
from app.core.campaign_reporting import campaign_performance
from app.core.leads import duplicate_reason, short_name, valid_email
from app.core.lead_status import set_client_status
from app.core.schedule import calendar_events, in_send_window, next_send_day_start, next_send_time
from app.core.scraper import crawl_contact, fallback_contact_search, process_scrape_job
from app.core.template_engine import render_template
from app.adapters.gmail_adapter import GmailProvider
from app.core.sender import _choose_account_slot, _choose_pingram_conveyor_slot, _global_schedule_state, _next_global_slot, cancel_campaign_queue, delete_campaign, queue_campaign, recover_interrupted_sends, refresh_campaign_states, reschedule_queued_emails, send_due
from app.jobs.scheduler import recover_stale_jobs


class ShortNameTests(unittest.TestCase):
    def test_varied_names(self):
        cases = {
            "ACME ROOFING, LLC": "Acme Roofing",
            "Smith Brothers Heating & Air Inc.": "Smith Brothers Heating & Air",
            "Jones Electric L.L.C.": "Jones Electric",
            "Bravo Plumbing Incorporated": "Bravo Plumbing",
            "Delta Builders Corporation": "Delta Builders",
            "Echo Landscapes Corp.": "Echo Landscapes",
            "Foxtrot Services Co.": "Foxtrot Services",
            "Garden State Company": "Garden State",
            "Hotel Works Ltd.": "Hotel Works",
            "India Partners LLP": "India Partners",
            "Juliet Medical PLLC": "Juliet Medical",
            "KILO HVAC, INC.": "Kilo HVAC",
            "Lima Roofing, LLC.": "Lima Roofing",
            "Mike & Sons Co": "Mike & Sons",
            "North Star d/b/a Bright Roof": "Bright Roof",
            "  Oscar   Construction, LLC  ": "Oscar Construction",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw): self.assertEqual(short_name(raw), expected)


class TemplateTests(unittest.TestCase):
    def test_fallback_and_missing_are_explicit(self):
        text, missing = render_template("Hey {{owner_first_name}} — {{booking_link}} {{unknown}}", {"short_name": "Acme"}, {"booking_link": ""})
        self.assertEqual(text, "Hey Acme Team, ")
        self.assertEqual(missing, ["booking_link", "unknown"])
        self.assertNotIn("{{", text)
    def test_allowed_variables_check(self):
        from app.core.template_engine import unknown_variables
        valid = "Hey {{short_name}}, check {{booking_link}} for {{product_name}} with {{person_name}}"
        self.assertEqual(unknown_variables(valid), [])
        invalid = "Hey {{first_name}}, check {{company_link}}"
        self.assertEqual(unknown_variables(invalid), ["company_link", "first_name"])

    def test_person_name_inferred_from_email_and_fallback(self):
        from app.core.leads import infer_name_from_email
        # Direct helper checks
        self.assertEqual(infer_name_from_email("Roy@gconstruct.com"), "Roy")
        self.assertEqual(infer_name_from_email("roy.miller@gconstruct.com"), "Roy")
        self.assertEqual(infer_name_from_email("john_doe@gmail.com"), "John")
        self.assertIsNone(infer_name_from_email("info@formularoofing.com"))
        self.assertIsNone(infer_name_from_email("contact@acme.com"))
        self.assertIsNone(infer_name_from_email("sales@builders.com"))

        # Template rendering: inferred name
        text, missing = render_template(
            "Hey {{person_name}},",
            {"email": "roy@gconstruct.com", "short_name": "G Construct", "business_name": "G Construct LLC"},
            {}
        )
        self.assertEqual(text, "Hey Roy,")
        self.assertEqual(missing, [])

        # Template rendering: fallback to short_name + Team when generic email
        text, missing = render_template(
            "Hey {{person_name}},",
            {"email": "info@formularoofing.com", "short_name": "Formula Roofing", "business_name": "Formula Roofing LLC"},
            {}
        )
        self.assertEqual(text, "Hey Formula Roofing Team,")
        self.assertEqual(missing, [])

        # Template rendering: company name already ending with Team is not duplicated
        text, missing = render_template(
            "Hey {{person_name}},",
            {"email": "info@apexteam.com", "short_name": "Apex Remodeling Team"},
            {}
        )
        self.assertEqual(text, "Hey Apex Remodeling Team,")
        self.assertEqual(missing, [])

        # Template rendering: explicit owner_first_name takes priority
        text, missing = render_template(
            "Hey {{person_name}},",
            {"owner_first_name": "Dave", "email": "info@formularoofing.com", "short_name": "Formula Roofing"},
            {}
        )
        self.assertEqual(text, "Hey Dave,")
        self.assertEqual(missing, [])


class GmailProviderTests(unittest.TestCase):
    @patch("app.adapters.gmail_adapter.settings.DRY_RUN", False)
    @patch("app.adapters.gmail_adapter.smtplib.SMTP_SSL")
    def test_temporary_recipient_refusal_is_retryable_not_a_bounce(self, smtp_ssl):
        smtp = smtp_ssl.return_value.__enter__.return_value
        smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused(
            {"recipient@example.com": (450, b"4.2.0 mailbox temporarily unavailable")}
        )
        result = GmailProvider("sender@gmail.com", "app-password").send(
            to="recipient@example.com", subject="Test", body="Body", content_type="plain",
            from_address="sender@gmail.com", from_name="Sender", reply_to="sender@gmail.com",
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.retryable)
        self.assertFalse(result.hard_bounce)

    @patch("app.adapters.gmail_adapter.settings.DRY_RUN", False)
    @patch("app.adapters.gmail_adapter.smtplib.SMTP_SSL")
    def test_missing_recipient_is_a_hard_bounce(self, smtp_ssl):
        smtp = smtp_ssl.return_value.__enter__.return_value
        smtp.send_message.side_effect = smtplib.SMTPRecipientsRefused(
            {"recipient@example.com": (550, b"5.1.1 user unknown")}
        )
        result = GmailProvider("sender@gmail.com", "app-password").send(
            to="recipient@example.com", subject="Test", body="Body", content_type="plain",
            from_address="sender@gmail.com", from_name="Sender", reply_to="sender@gmail.com",
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertTrue(result.hard_bounce)

    @patch("app.adapters.gmail_adapter.settings.DRY_RUN", False)
    @patch("app.adapters.gmail_adapter.smtplib.SMTP_SSL")
    def test_network_unreachable_is_clear_and_not_retryable(self, smtp_ssl):
        smtp_ssl.side_effect = OSError(errno.ENETUNREACH, "Network is unreachable")
        result = GmailProvider("sender@gmail.com", "app-password").send(
            to="recipient@example.com",
            subject="Test",
            body="Test body",
            content_type="plain",
            from_address="sender@gmail.com",
            from_name="Sender",
            reply_to="sender@gmail.com",
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertIn("Railway blocks outbound SMTP", result.error)

    @patch("app.adapters.gmail_adapter.settings.DRY_RUN", False)
    @patch("app.adapters.gmail_adapter.settings.GMAIL_TRANSPORT", "supabase")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_URL", "https://project.supabase.co")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_SERVICE_ROLE_KEY", "secret-key")
    @patch("app.adapters.gmail_adapter.httpx.post")
    def test_supabase_transport_sends_rendered_message_over_https(self, post):
        response = Mock()
        response.content = b'{"ok":true}'
        response.is_success = True
        response.status_code = 200
        response.json.return_value = {"ok": True, "message_id": "gmail-message-id"}
        post.return_value = response

        result = GmailProvider("", "").send(
            to="recipient@example.com",
            subject="Rendered subject",
            body="Rendered body",
            content_type="plain",
            from_address="sender@gmail.com",
            from_name="Sender",
            reply_to="sender@gmail.com",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider_id, "gmail-message-id")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["subject"], "Rendered subject")
        self.assertEqual(payload["body"], "Rendered body")
        self.assertEqual(payload["account_id"], "1")

    @patch("app.adapters.gmail_adapter.settings.DRY_RUN", False)
    @patch("app.adapters.gmail_adapter.settings.GMAIL_TRANSPORT", "supabase")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_URL", "https://project.supabase.co")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_SERVICE_ROLE_KEY", "secret-key")
    @patch("app.adapters.gmail_adapter.httpx.post")
    def test_edge_function_failure_metadata_controls_retry_and_bounce(self, post):
        response = Mock(content=b'{"ok":false}', is_success=False, status_code=422)
        response.json.return_value = {
            "ok": False,
            "error": "550 5.1.1 user unknown",
            "retryable": False,
            "hard_bounce": True,
        }
        post.return_value = response
        result = GmailProvider("sender@gmail.com", "app-password").send(
            to="missing@example.com", subject="Test", body="Body", content_type="plain",
            from_address="sender@gmail.com", from_name="Sender", reply_to="sender@gmail.com",
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.retryable)
        self.assertTrue(result.hard_bounce)

    @patch("app.adapters.gmail_adapter.settings.DRY_RUN", False)
    @patch("app.adapters.gmail_adapter.settings.GMAIL_TRANSPORT", "supabase")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_URL", "https://project.supabase.co")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_SERVICE_ROLE_KEY", "secret-key")
    @patch("app.adapters.gmail_adapter.httpx.post")
    def test_second_gmail_account_is_sent_to_edge_function(self, post):
        response = Mock(content=b'{"ok":true}', is_success=True, status_code=200)
        response.json.return_value = {"ok": True, "message_id": "second-account-message"}
        post.return_value = response
        result = GmailProvider("second@gmail.com", "app-password", account_id="2").send(
            to="recipient@example.com", subject="Test", body="Body", content_type="plain",
            from_address="second@gmail.com", from_name="Sender", reply_to="second@gmail.com",
        )
        self.assertTrue(result.ok)
        self.assertEqual(post.call_args.kwargs["json"]["account_id"], "2")

    @patch("app.adapters.gmail_adapter.settings.DRY_RUN", False)
    @patch("app.adapters.gmail_adapter.settings.GMAIL_TRANSPORT", "supabase")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_URL", "https://project.supabase.co")
    @patch("app.adapters.gmail_adapter.settings.SUPABASE_SERVICE_ROLE_KEY", "secret-key")
    @patch("app.adapters.gmail_adapter.httpx.post")
    def test_dynamic_gmail_account_credentials_are_sent_to_edge_function(self, post):
        response = Mock(content=b'{"ok":true}', is_success=True, status_code=200)
        response.json.return_value = {"ok": True, "message_id": "dynamic-message"}
        post.return_value = response
        result = GmailProvider("new@gmail.com", "dynamic-password", account_id="sender-uuid").send(
            to="recipient@example.com", subject="Test", body="Body", content_type="plain",
            from_address="new@gmail.com", from_name="New Sender", reply_to="new@gmail.com",
        )
        payload = post.call_args.kwargs["json"]
        self.assertTrue(result.ok)
        self.assertEqual(payload["account_id"], "sender-uuid")
        self.assertEqual(payload["gmail_user"], "new@gmail.com")
        self.assertEqual(payload["gmail_app_password"], "dynamic-password")


class CampaignSenderTests(unittest.TestCase):
    def test_expired_send_lease_becomes_reviewable_failure(self):
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        rows = {
            "expired": {"id": "expired", "status": "sending", "next_attempt_at": "2026-09-22T11:59:00+00:00", "created_at": "2026-09-22T11:00:00+00:00"},
            "active": {"id": "active", "status": "sending", "next_attempt_at": "2026-09-22T12:10:00+00:00", "created_at": "2026-09-22T11:01:00+00:00"},
            "legacy": {"id": "legacy", "status": "sending", "next_attempt_at": None, "created_at": "2026-09-21T11:00:00+00:00"},
        }

        class MemoryStore:
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.values())
                for key, value in (filters or {}).items():
                    if isinstance(value, tuple) and value[0] == "lte":
                        values = [row for row in values if row.get(key) and row[key] <= value[1]]
                    else:
                        values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def update(self, table, row_id, values): rows[row_id].update(values); return rows[row_id]

        with patch("app.core.sender.store", MemoryStore()):
            recovered = recover_interrupted_sends(now)

        self.assertEqual(recovered, 2)
        self.assertEqual(rows["expired"]["status"], "failed")
        self.assertIn("avoid a duplicate", rows["expired"]["error"])
        self.assertEqual(rows["legacy"]["status"], "failed")
        self.assertEqual(rows["active"]["status"], "sending")

    def test_new_mail_backfills_an_earlier_day_with_remaining_capacity(self):
        cfg = {"timezone": "UTC", "send_days": list(range(7)), "send_start": "09:00", "send_end": "17:00", "min_delay_minutes": 1, "max_delay_minutes": 1}
        logs = [
            {"status": "queued", "sender_account": "1", "scheduled_for": "2026-09-22T09:00:00+00:00"},
            {"status": "queued", "sender_account": "1", "scheduled_for": "2026-09-22T09:01:00+00:00"},
            {"status": "queued", "sender_account": "1", "scheduled_for": "2026-09-23T09:00:00+00:00"},
            {"status": "queued", "sender_account": "1", "scheduled_for": "2026-09-24T09:00:00+00:00"},
            {"status": "queued", "sender_account": "1", "scheduled_for": "2026-09-24T09:01:00+00:00"},
        ]
        now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
        counts, cursors = _global_schedule_state(logs, cfg, now)
        slot = _next_global_slot("1", now, cfg, counts, cursors, cap=2)
        self.assertEqual(slot, datetime(2026, 9, 23, 9, 1, tzinfo=timezone.utc))

    def test_new_mail_uses_sender_with_capacity_before_spilling_to_tomorrow(self):
        cfg = {"timezone": "UTC", "send_days": list(range(7)), "send_start": "09:00", "send_end": "17:00", "min_delay_minutes": 1, "max_delay_minutes": 1}
        logs = [
            {"status": "queued", "sender_account": "a", "scheduled_for": "2026-09-22T09:00:00+00:00"},
            {"status": "queued", "sender_account": "a", "scheduled_for": "2026-09-22T09:01:00+00:00"},
            {"status": "queued", "sender_account": "b", "scheduled_for": "2026-09-22T09:00:00+00:00"},
        ]
        now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
        counts, cursors = _global_schedule_state(logs, cfg, now)
        account, slot = _choose_account_slot(["a", "b"], now, cfg, counts, cursors, 2, Counter())
        self.assertEqual(account, "b")
        self.assertEqual(slot.date().isoformat(), "2026-09-22")

    def test_reschedule_enforces_one_global_cap_per_account(self):
        rows = {
            "settings": {1: {"daily_cap": 2, "timezone": "UTC", "send_days": list(range(7)), "send_start": "09:00", "send_end": "17:00", "min_delay_minutes": 1, "max_delay_minutes": 1}},
            "email_log": {
                f"log-{i}": {"id": f"log-{i}", "status": "queued", "sender_account": "1", "scheduled_for": f"2026-09-22T09:0{i}:00+00:00", "created_at": f"2026-09-21T00:0{i}:00+00:00"}
                for i in range(5)
            },
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000): return list(rows.get(table, {}).values())[:limit]
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        now = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
        with patch("app.core.sender.store", MemoryStore()):
            result = reschedule_queued_emails(now)

        self.assertEqual(result["queued"], 5)
        days = [datetime.fromisoformat(row["scheduled_for"]).date().isoformat() for row in rows["email_log"].values()]
        self.assertEqual(days.count("2026-09-22"), 2)
        self.assertEqual(days.count("2026-09-23"), 2)
        self.assertEqual(days.count("2026-09-24"), 1)

    def test_only_unused_non_running_campaigns_can_be_deleted(self):
        class MemoryStore:
            def __init__(self, campaign, logs=None): self.campaign, self.logs, self.deleted = campaign, logs or [], False
            def get(self, table, row_id): return self.campaign if table == "campaigns" else None
            def list(self, table, filters=None, order="", limit=1): return self.logs if table == "email_log" else []
            def delete(self, table, filters): self.deleted = True; return 1

        draft = MemoryStore({"id": "draft", "state": "draft"})
        with patch("app.core.sender.store", draft): delete_campaign("draft")
        self.assertTrue(draft.deleted)

        running = MemoryStore({"id": "running", "state": "running"})
        with patch("app.core.sender.store", running), self.assertRaisesRegex(ValueError, "Stop"):
            delete_campaign("running")
        self.assertFalse(running.deleted)

        used = MemoryStore({"id": "used", "state": "draft"}, [{"id": "log"}])
        with patch("app.core.sender.store", used), self.assertRaisesRegex(ValueError, "history"):
            delete_campaign("used")
        self.assertFalse(used.deleted)

    def test_two_accounts_are_assigned_evenly(self):
        campaign = {
            "id": "campaign", "provider": "gmail", "gmail_accounts": ["1", "2"],
            "template_ids": ["template"], "target_filter": {"status": "new"},
            "daily_cap": 2, "send_window": {"timezone": "UTC", "send_days": list(range(7)), "send_start": "00:00", "send_end": "23:59"},
            "min_delay_minutes": 3, "max_delay_minutes": 3, "resend_block_days": 90, "state": "draft",
        }
        rows = {
            "campaigns": {"campaign": campaign},
            "email_templates": {"template": {"id": "template", "active": True, "subject": "Hi {{short_name}}", "body": "Hello {{short_name}}", "type": "plain"}},
            "clients": {f"client-{i}": {"id": f"client-{i}", "short_name": f"Lead {i}", "email": f"lead{i}@example.com", "domain": f"example{i}.com", "status": "new", "created_at": str(i)} for i in range(4)},
            "settings": {1: {"timezone": "UTC", "send_days": list(range(7)), "send_start": "00:00", "send_end": "23:59"}},
            "suppression": {}, "email_log": {},
            # Explicit senders so the test does not depend on GMAIL_USER in the local .env.
            "gmail_senders": {
                "1": {"id": "1", "email": "one@example.com", "active": True, "provider": "gmail", "created_at": "1"},
                "2": {"id": "2", "email": "two@example.com", "active": True, "provider": "gmail", "created_at": "2"},
            },
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for key, value in (filters or {}).items(): values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def insert(self, table, row): rows.setdefault(table, {})[row["id"]] = dict(row); return row
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        with patch("app.core.sender.store", MemoryStore()):
            result = queue_campaign("campaign")

        self.assertEqual(result["queued"], 4)
        logs = list(rows["email_log"].values())
        self.assertEqual(Counter(log["sender_account"] for log in logs), Counter({"1": 2, "2": 2}))

    def test_dynamic_accounts_render_their_own_names_and_signatures(self):
        account_ids = [f"sender-{i}" for i in range(4)]
        rows = {
            "campaigns": {"campaign": {"id": "campaign", "provider": "gmail", "gmail_accounts": account_ids, "template_ids": ["template"], "target_filter": {"status": "new"}, "resend_block_days": 90}},
            "email_templates": {"template": {"id": "template", "active": True, "subject": "From {{sender_name}}", "body": "{{signature}}", "type": "plain"}},
            "clients": {f"client-{i}": {"id": f"client-{i}", "short_name": f"Lead {i}", "email": f"lead{i}@example.com", "domain": f"example{i}.com", "status": "new", "created_at": str(i)} for i in range(8)},
            "gmail_senders": {sender_id: {"id": sender_id, "email": f"person{i}@gmail.com", "display_name": f"Person {i}", "signature": f"Signature {i}", "reply_to": f"person{i}@gmail.com", "active": True, "created_at": str(i)} for i, sender_id in enumerate(account_ids)},
            "settings": {1: {"daily_cap": 2, "timezone": "UTC", "send_days": list(range(7)), "send_start": "00:00", "send_end": "23:59", "min_delay_minutes": 1, "max_delay_minutes": 1}},
            "suppression": {}, "email_log": {},
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for key, value in (filters or {}).items(): values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def insert(self, table, row): rows.setdefault(table, {})[row["id"]] = dict(row); return row
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        with patch("app.core.sender.store", MemoryStore()):
            result = queue_campaign("campaign")

        self.assertEqual(result["queued"], 8)
        logs = list(rows["email_log"].values())
        self.assertEqual([log["sender_account"] for log in logs], account_ids * 2)
        self.assertEqual([log["subject_sent"] for log in logs[:4]], [f"From Person {i}" for i in range(4)])
        self.assertEqual([log["body_sent"] for log in logs[:4]], [f"Signature {i}" for i in range(4)])

    def test_pingram_conveyor_slot_spaces_and_rotates(self):
        accounts = ["rep-1", "rep-2", "rep-3"]
        counts = Counter()
        assigned = Counter()
        start = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
        cursor = start

        chosen_accounts = []
        slot_times = []
        for _ in range(6):
            acc, slot, cursor = _choose_pingram_conveyor_slot(
                accounts, cursor, counts, assigned,
                daily_cap=10, min_delay_minutes=5, max_delay_minutes=20
            )
            chosen_accounts.append(acc)
            slot_times.append(slot)

        self.assertEqual(chosen_accounts, ["rep-1", "rep-2", "rep-3", "rep-1", "rep-2", "rep-3"])
        for i in range(len(slot_times) - 1):
            gap_seconds = (slot_times[i + 1] - slot_times[i]).total_seconds()
            self.assertGreaterEqual(gap_seconds, 5 * 60)
            self.assertLessEqual(gap_seconds, 20 * 60)

    def test_pingram_conveyor_belt_reschedule_spaces_and_rerenders_signatures(self):
        rows = {
            "settings": {1: {
                "pingram_daily_cap": 30, "pingram_min_delay": 5, "pingram_max_delay": 15,
                "daily_cap": 20, "timezone": "UTC", "send_days": list(range(7)), "send_start": "00:00", "send_end": "23:59"
            }},
            "gmail_senders": {
                "rep-a": {"id": "rep-a", "email": "alice@contractorops.ai", "display_name": "Alice", "signature": "Alice\nContractorOps", "active": True, "provider": "pingram"},
                "rep-b": {"id": "rep-b", "email": "beth@contractorops.ai", "display_name": "Beth", "signature": "Beth\nContractorOps", "active": True, "provider": "pingram"},
            },
            "clients": {
                "c-1": {"id": "c-1", "short_name": "Client 1", "email": "c1@example.com"},
                "c-2": {"id": "c-2", "short_name": "Client 2", "email": "c2@example.com"},
                "c-3": {"id": "c-3", "short_name": "Client 3", "email": "c3@example.com"},
            },
            "email_templates": {
                "t-1": {"id": "t-1", "active": True, "subject": "Hi from {{sender_name}}", "body": "{{signature}}", "type": "plain"}
            },
            "email_log": {
                "log-1": {"id": "log-1", "status": "queued", "provider": "pingram", "sender_account": "rep-a", "client_id": "c-1", "template_id": "t-1", "scheduled_for": "2026-09-23T10:00:00+00:00", "created_at": "2026-09-23T09:00:00+00:00", "body_sent": "Old"},
                "log-2": {"id": "log-2", "status": "queued", "provider": "pingram", "sender_account": "rep-a", "client_id": "c-2", "template_id": "t-1", "scheduled_for": "2026-09-23T10:00:00+00:00", "created_at": "2026-09-23T09:01:00+00:00", "body_sent": "Old"},
                "log-3": {"id": "log-3", "status": "queued", "provider": "pingram", "sender_account": "rep-a", "client_id": "c-3", "template_id": "t-1", "scheduled_for": "2026-09-23T10:00:00+00:00", "created_at": "2026-09-23T09:02:00+00:00", "body_sent": "Old"},
            },
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000): return list(rows.get(table, {}).values())[:limit]
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        now = datetime(2026, 9, 23, 10, 0, 0, tzinfo=timezone.utc)
        with patch("app.core.sender.store", MemoryStore()):
            result = reschedule_queued_emails(now)

        self.assertEqual(result["queued"], 3)
        logs = list(rows["email_log"].values())
        logs.sort(key=lambda l: l["scheduled_for"])
        
        times = [datetime.fromisoformat(l["scheduled_for"]) for l in logs]
        self.assertGreaterEqual((times[1] - times[0]).total_seconds(), 5 * 60)
        self.assertGreaterEqual((times[2] - times[1]).total_seconds(), 5 * 60)
        
        self.assertEqual([l["sender_account"] for l in logs], ["rep-a", "rep-b", "rep-a"])
        self.assertEqual(logs[0]["body_sent"], "Alice\nContractorOps")
        self.assertEqual(logs[1]["body_sent"], "Beth\nContractorOps")
        self.assertEqual(logs[2]["body_sent"], "Alice\nContractorOps")

    def test_pingram_dispatch_guardrail_defers_concurrent_sends(self):
        rows = {
            "campaigns": {"camp-1": {"id": "camp-1", "state": "running"}},
            "clients": {
                "c-1": {"id": "c-1", "status": "queued", "email": "c1@example.com"},
                "c-2": {"id": "c-2", "status": "queued", "email": "c2@example.com"},
            },
            "email_templates": {"t-1": {"id": "t-1", "type": "plain"}},
            "settings": {1: {"daily_cap": 20, "pingram_daily_cap": 30, "timezone": "UTC", "send_days": list(range(7)), "send_start": "00:00", "send_end": "23:59"}},
            "gmail_senders": {
                "rep-1": {"id": "rep-1", "email": "rep1@contractorops.ai", "display_name": "Rep 1", "signature": "Sig", "active": True, "provider": "pingram"}
            },
            "email_log": {
                "log-1": {"id": "log-1", "status": "queued", "campaign_id": "camp-1", "client_id": "c-1", "template_id": "t-1", "provider": "pingram", "sender_account": "rep-1", "scheduled_for": "2026-09-23T11:59:00+00:00", "subject_sent": "Sub", "body_sent": "Body"},
                "log-2": {"id": "log-2", "status": "queued", "campaign_id": "camp-1", "client_id": "c-2", "template_id": "t-1", "provider": "pingram", "sender_account": "rep-1", "scheduled_for": "2026-09-23T11:59:00+00:00", "subject_sent": "Sub", "body_sent": "Body"},
            }
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for k, v in (filters or {}).items():
                    values = [row for row in values if row.get(k) == v]
                return values[:limit]
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        mock_provider = Mock()
        mock_provider.send.return_value = Mock(ok=True, provider_id="msg-1", error=None)

        with patch("app.core.sender.store", MemoryStore()), patch("app.core.sender.get_provider", return_value=mock_provider), patch("app.core.sender.now_iso", return_value="2026-09-23T12:00:00+00:00"):
            result = send_due()

        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(rows["email_log"]["log-1"]["status"], "sent")
        self.assertEqual(rows["email_log"]["log-2"]["status"], "queued")

    def test_campaign_queue_can_be_cancelled_and_completed(self):
        rows = {
            "campaigns": {"campaign": {"id": "campaign", "state": "running"}},
            "clients": {"lead": {"id": "lead", "status": "queued"}},
            "email_log": {
                "queued": {"id": "queued", "client_id": "lead", "campaign_id": "campaign", "status": "queued"},
                "sent": {"id": "sent", "client_id": "lead", "campaign_id": "campaign", "status": "sent", "sent_at": "2026-09-22T12:00:00+00:00"},
            },
        }

        class MemoryStore:
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for key, value in (filters or {}).items():
                    if isinstance(value, tuple) and value[0] == "in":
                        values = [row for row in values if row.get(key) in value[1]]
                    else:
                        values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)

        with patch("app.core.sender.store", MemoryStore()):
            self.assertEqual(cancel_campaign_queue("campaign"), 1)
            self.assertEqual(refresh_campaign_states({"campaign"}), 1)

        self.assertEqual(rows["email_log"]["queued"]["status"], "skipped")
        self.assertEqual(rows["clients"]["lead"]["status"], "new")
        self.assertEqual(rows["campaigns"]["campaign"]["state"], "done")


class ScheduleTests(unittest.TestCase):
    config = {"timezone": "America/New_York", "send_days": [0,1,2,3,4], "send_start": "09:00", "send_end": "17:00", "min_delay_minutes": 3, "max_delay_minutes": 15}
    def test_weekend_rolls_to_monday(self):
        saturday = datetime(2026, 9, 19, 15, tzinfo=timezone.utc)
        result = next_send_time(saturday, self.config, jitter=False)
        self.assertEqual(result.weekday(), 0); self.assertTrue(in_send_window(result, self.config))
    def test_after_hours_rolls_forward(self):
        late = datetime(2026, 9, 21, 23, tzinfo=timezone.utc)
        result = next_send_time(late, self.config, jitter=False)
        self.assertTrue(in_send_window(result, self.config)); self.assertGreater(result, late)
    def test_full_day_rolls_to_next_send_day_start(self):
        friday = datetime(2026, 9, 25, 15, tzinfo=timezone.utc)
        result = next_send_day_start(friday, self.config)
        local = result.astimezone(ZoneInfo("America/New_York"))
        self.assertEqual(local.weekday(), 0)
        self.assertEqual((local.hour, local.minute), (9, 0))
    def test_calendar_includes_sent_and_only_running_scheduled_email(self):
        campaigns = [{"id": "running", "state": "running"}, {"id": "paused", "state": "paused"}]
        logs = [
            {"campaign_id": "running", "status": "queued", "scheduled_for": "2026-09-22T13:00:00+00:00", "sender_account": "1"},
            {"campaign_id": "paused", "status": "queued", "scheduled_for": "2026-09-22T14:00:00+00:00", "sender_account": "2"},
            {"campaign_id": "paused", "status": "sent", "sent_at": "2026-09-21T23:00:00+00:00", "sender_account": "2"},
            {"campaign_id": "running", "status": "skipped", "scheduled_for": "2026-09-22T15:00:00+00:00", "sender_account": "1"},
            {"campaign_id": "running", "status": "replied", "sent_at": "2026-09-21T20:00:00+00:00", "sender_account": "1"},
        ]
        self.assertEqual(calendar_events(logs, campaigns), [
            {"timestamp": "2026-09-22T13:00:00+00:00", "status": "scheduled", "account": "1"},
            {"timestamp": "2026-09-21T23:00:00+00:00", "status": "sent", "account": "2"},
            {"timestamp": "2026-09-21T20:00:00+00:00", "status": "sent", "account": "1"},
        ])


class LeadStatusTests(unittest.TestCase):
    def test_do_not_contact_suppresses_once_and_cancels_pending_email(self):
        rows = {
            "clients": {"lead": {"id": "lead", "email": "owner@example.com", "domain": "example.com", "status": "queued"}},
            "email_log": {
                "pending": {"id": "pending", "client_id": "lead", "status": "queued"},
                "sent": {"id": "sent", "client_id": "lead", "status": "sent", "sent_at": "2026-09-21T12:00:00+00:00"},
            },
            "suppression": {},
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for key, value in (filters or {}).items():
                    if isinstance(value, tuple) and value[0] == "in":
                        values = [row for row in values if row.get(key) in value[1]]
                    else:
                        values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def insert(self, table, row): rows[table][row["id"]] = dict(row); return row
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        storage = MemoryStore()
        first = set_client_status("lead", "do_not_contact", storage=storage)
        second = set_client_status("lead", "do_not_contact", storage=storage)
        self.assertEqual(first["cancelled"], 1)
        self.assertTrue(first["suppressed"])
        self.assertFalse(second["suppressed"])
        self.assertEqual(len(rows["suppression"]), 1)
        self.assertEqual(rows["email_log"]["pending"]["status"], "skipped")

    def test_reply_marks_latest_delivery_and_cancels_future_email(self):
        rows = {
            "clients": {"lead": {"id": "lead", "email": "owner@example.com", "status": "contacted"}},
            "email_log": {
                "old": {"id": "old", "client_id": "lead", "status": "sent", "sent_at": "2026-09-20T12:00:00+00:00"},
                "latest": {"id": "latest", "client_id": "lead", "status": "sent", "sent_at": "2026-09-21T12:00:00+00:00"},
                "pending": {"id": "pending", "client_id": "lead", "status": "queued"},
            },
            "suppression": {},
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for key, value in (filters or {}).items():
                    if isinstance(value, tuple) and value[0] == "in":
                        values = [row for row in values if row.get(key) in value[1]]
                    else:
                        values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        result = set_client_status("lead", "replied", storage=MemoryStore())
        self.assertTrue(result["reply_recorded"])
        self.assertEqual(rows["email_log"]["latest"]["status"], "replied")
        self.assertEqual(rows["email_log"]["old"]["status"], "sent")
        self.assertEqual(rows["email_log"]["pending"]["status"], "skipped")


class CampaignReportingTests(unittest.TestCase):
    def test_delivery_and_template_metrics_include_replies_and_bounces(self):
        logs = [
            {"template_id": "a", "status": "sent", "sent_at": "2026-09-20T12:00:00+00:00"},
            {"template_id": "a", "status": "replied", "sent_at": "2026-09-20T13:00:00+00:00"},
            {"template_id": "b", "status": "bounced", "sent_at": "2026-09-20T14:00:00+00:00"},
            {"template_id": "b", "status": "failed", "sent_at": None},
            {"template_id": "a", "status": "queued", "sent_at": None},
        ]
        result = campaign_performance(logs, {"a": {"name": "Intro"}, "b": {"name": "Question"}})
        self.assertEqual(result["summary"], {"total": 5, "scheduled": 1, "sent": 3, "replies": 1, "bounced": 1, "failed": 1, "skipped": 0})
        intro = next(row for row in result["templates"] if row["id"] == "a")
        self.assertEqual(intro["sent"], 2)
        self.assertEqual(intro["reply_rate"], 50.0)


class WorkerRecoveryTests(unittest.TestCase):
    def test_stale_jobs_retry_with_backoff_and_eventually_fail(self):
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        rows = {
            "jobs": {
                "retry": {"id": "retry", "kind": "reschedule_email_queue", "payload": {}, "status": "running", "attempts": 0, "locked_at": "2026-09-22T11:30:00+00:00"},
                "terminal": {"id": "terminal", "kind": "scrape", "payload": {"scrape_job_id": "scrape-1"}, "status": "running", "attempts": 2, "locked_at": "2026-09-22T11:30:00+00:00"},
                "active": {"id": "active", "kind": "scrape", "payload": {"scrape_job_id": "scrape-2"}, "status": "running", "attempts": 0, "locked_at": "2026-09-22T11:55:00+00:00"},
            },
            "scrape_jobs": {"scrape-1": {"id": "scrape-1", "status": "running"}},
        }

        class MemoryStore:
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for key, value in (filters or {}).items():
                    if isinstance(value, tuple) and value[0] == "lte":
                        values = [row for row in values if row.get(key) and row[key] <= value[1]]
                    else:
                        values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        with patch("app.jobs.scheduler.store", MemoryStore()):
            recovered = recover_stale_jobs(now)

        self.assertEqual(recovered, 2)
        self.assertEqual(rows["jobs"]["retry"]["status"], "queued")
        self.assertEqual(rows["jobs"]["retry"]["attempts"], 1)
        self.assertIsNone(rows["jobs"]["retry"]["locked_at"])
        self.assertGreater(rows["jobs"]["retry"]["run_after"], now.isoformat())
        self.assertEqual(rows["jobs"]["terminal"]["status"], "failed")
        self.assertEqual(rows["scrape_jobs"]["scrape-1"]["status"], "failed")
        self.assertEqual(rows["jobs"]["active"]["status"], "running")


class ImportTests(unittest.TestCase):
    def test_confirm_import_reports_late_insert_conflicts(self):
        batch_id = "batch"
        rows = {
            "import_batches": {
                batch_id: {
                    "id": batch_id,
                    "status": "preview",
                    "rejected_csv": json.dumps(
                        {
                            "ready": [
                                {"business_name": "Acme", "short_name": "Acme", "email": "a@example.com", "domain": "example.com", "extra": {}},
                                {"business_name": "Bravo", "short_name": "Bravo", "email": "b@example.com", "domain": "bravo.com", "extra": {}},
                            ],
                            "rejected": [],
                        }
                    ),
                }
            },
            "clients": {},
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def insert(self, table, row):
                if row.get("email") == "b@example.com":
                    raise sqlite3.IntegrityError("unique conflict")
                rows[table][row["id"]] = dict(row)
                return row
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        with patch("app.core.importer.store", MemoryStore()):
            result = confirm_import(batch_id)

        self.assertEqual(result, {"added": 1, "rejected": 1})
        self.assertEqual(rows["import_batches"][batch_id]["rejected_rows"], 1)
        self.assertIn("insert_conflict", rows["import_batches"][batch_id]["rejected_csv"])

    def test_confirm_import_uses_insert_many_when_available(self):
        batch_id = "test-batch-bulk"
        rows = {
            "import_batches": {
                batch_id: {
                    "id": batch_id,
                    "status": "preview",
                    "rejected_csv": json.dumps({
                        "ready": [
                            {"email": "lead1@example.com", "business_name": "Lead 1"},
                            {"email": "lead2@example.com", "business_name": "Lead 2"},
                        ],
                        "rejected": [],
                    }),
                }
            },
            "clients": {},
        }

        class BulkStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def insert_many(self, table, candidates):
                for c in candidates:
                    rows[table][c["id"]] = c
                return candidates
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        with patch("app.core.importer.store", BulkStore()):
            result = confirm_import(batch_id)

        self.assertEqual(result, {"added": 2, "rejected": 0})
        self.assertEqual(len(rows["clients"]), 2)

    def test_undo_import_preserves_campaign_email_history(self):
        rows = {
            "import_batches": {"batch": {"id": "batch", "status": "done"}},
            "clients": {"lead": {"id": "lead", "import_batch_id": "batch"}},
            "email_log": {"log": {"id": "log", "client_id": "lead", "status": "sent"}},
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000):
                values = list(rows.get(table, {}).values())
                for key, value in (filters or {}).items():
                    if isinstance(value, tuple) and value[0] == "in":
                        values = [row for row in values if row.get(key) in value[1]]
                    else:
                        values = [row for row in values if row.get(key) == value]
                return values[:limit]
            def delete(self, table, filters):
                doomed = [key for key, row in rows[table].items() if all(row.get(field) == value for field, value in filters.items())]
                for key in doomed: del rows[table][key]
                return len(doomed)
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        storage = MemoryStore()
        with patch("app.core.importer.store", storage), self.assertRaisesRegex(ValueError, "email history"):
            undo_import("batch")
        self.assertIn("lead", rows["clients"])

        rows["email_log"].clear()
        with patch("app.core.importer.store", storage):
            self.assertEqual(undo_import("batch"), 1)
        self.assertEqual(rows["import_batches"]["batch"]["status"], "undone")

    def test_gemini_json_retries_once_after_malformed_output(self):
        bad = Mock()
        bad.raise_for_status.return_value = None
        bad.json.return_value = {"candidates": [{"content": {"parts": [{"text": "not-json"}]}}]}
        good = Mock()
        good.raise_for_status.return_value = None
        good.json.return_value = {"candidates": [{"content": {"parts": [{"text": '{"Company":"business_name"}'}]}}]}
        client = Mock()
        client.post.side_effect = [bad, good]
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)

        with patch("app.core.importer.settings.GEMINI_API_KEY", "test-key"), patch("app.core.importer.httpx.Client", return_value=context):
            result = _gemini_json({"task": "map"})

        self.assertEqual(result, {"Company": "business_name"})
        self.assertEqual(client.post.call_count, 2)

    def test_gemini_mapping_rejects_duplicate_destinations(self):
        with patch("app.core.importer._gemini_json", return_value={"Primary": "email", "Backup": "email"}):
            result = gemini_mapping(["Primary", "Backup"], [])
        self.assertIsNone(result)

    def test_gemini_mapping_is_preferred_with_heuristic_fallback(self):
        rows = [{"Company": "Acme", "Mail": "hello@example.com"}]
        with patch("app.core.importer.settings.GEMINI_API_KEY", "test-key"), patch("app.core.importer.gemini_mapping", return_value={"Company": "business_name", "Mail": "email"}) as gemini, patch("app.core.importer.heuristic_mapping") as heuristic:
            result = select_mapping(["Company", "Mail"], rows)
        self.assertEqual(result, {"Company": "business_name", "Mail": "email"})
        gemini.assert_called_once()
        heuristic.assert_not_called()

        with patch("app.core.importer.settings.GEMINI_API_KEY", "test-key"), patch("app.core.importer.gemini_mapping", return_value=None), patch("app.core.importer.heuristic_mapping", return_value={"Mail": "email"}):
            self.assertEqual(select_mapping(["Company", "Mail"], rows), {"Mail": "email"})

    def test_gemini_cleanup_sends_only_messy_rows(self):
        rows = [
            {"Email": "clean@example.com", "Location": "Austin"},
            {"Email": "Jane Doe <jane@example.com>", "Location": "Dallas, TX"},
        ]
        mapping = {"Email": "email", "Location": "city"}
        response = [{"index": 1, "fields": {"email": "jane@example.com", "owner_first_name": "Jane", "city": "Dallas", "state": "TX"}}]
        with patch("app.core.importer.settings.GEMINI_API_KEY", "test-key"), patch("app.core.importer._gemini_json", return_value=response) as call:
            cleaned = gemini_cleanup_rows(rows, mapping)
        self.assertEqual(cleaned[1]["owner_first_name"], "Jane")
        sent_rows = call.call_args.args[0]["rows"]
        self.assertEqual([item["index"] for item in sent_rows], [1])

    def test_gemini_cleanup_caps_to_single_batch_on_large_messy_input(self):
        rows = [
            {"Email": f"Messy User {i} <user{i}@example.com>", "Location": f"City {i}, TX"}
            for i in range(60)
        ]
        mapping = {"Email": "email", "Location": "city"}
        response = [{"index": i, "fields": {"email": f"user{i}@example.com"}} for i in range(25)]
        with patch("app.core.importer.settings.GEMINI_API_KEY", "test-key"), patch("app.core.importer._gemini_json", return_value=response) as call:
            cleaned = gemini_cleanup_rows(rows, mapping)
        self.assertEqual(len(cleaned), 25)
        self.assertEqual(call.call_count, 1)

    def test_deterministic_cleanup_extracts_jammed_email_and_location(self):
        row = clean_row(
            {"Contact": "Jane Doe <jane@example.com>", "Location": "Dallas, TX", "Company": "Acme LLC"},
            {"Contact": "email", "Location": "city", "Company": "business_name"},
        )
        self.assertEqual(row["email"], "jane@example.com")
        self.assertEqual(row["city"], "Dallas")
        self.assertEqual(row["state"], "TX")

    def test_csv_decoding_preserves_utf8_and_supports_cp1252(self):
        utf8 = "Company,Owner\nAcme,José\n".encode("utf-8")
        headers, rows = decode_csv(utf8)
        self.assertEqual(headers, ["Company", "Owner"])
        self.assertEqual(rows[0]["Owner"], "José")

        cp1252 = "Company,Owner\nAcme,André\n".encode("cp1252")
        _, rows = decode_csv(cp1252)
        self.assertEqual(rows[0]["Owner"], "André")

    def test_csv_enforces_documented_size_and_row_limits(self):
        with self.assertRaisesRegex(ValueError, "5 MB"):
            decode_csv(b"x" * (MAX_CSV_BYTES + 1))

        too_many_rows = ("Company,Email\n" + "\n".join(
            f"Acme {index},lead{index}@example.com" for index in range(5_001)
        )).encode()
        with self.assertRaisesRegex(ValueError, "5,000 row"):
            decode_csv(too_many_rows)

    def test_mapping_and_normalization(self):
        mapping = heuristic_mapping(["Company Name", "Email Address", "Telephone", "Web"])
        row = clean_row({"Company Name": "ACME, LLC", "Email Address": " SALES@EXAMPLE.COM ", "Telephone": "(512) 555-1212", "Web": "example.com"}, mapping)
        self.assertEqual(row["short_name"], "Acme"); self.assertEqual(row["email"], "sales@example.com"); self.assertEqual(row["domain"], "example.com")
        self.assertTrue(valid_email(row["email"]))
    def test_dedupe_email_and_domain(self):
        existing = [{"email": "a@example.com", "domain": "example.com"}]
        self.assertEqual(duplicate_reason({"email": "A@EXAMPLE.COM", "domain": "other.com"}, existing), "duplicate_email")
        self.assertEqual(duplicate_reason({"email": "b@other.com", "domain": "example.com"}, existing), "duplicate_domain")


class DiscoveryTests(unittest.TestCase):
    def test_fallback_contact_search_extracts_public_contact_details(self):
        organic = [
            {
                "title": "Acme Roofing",
                "snippet": "Email owner@acmeroofing.com or call (512) 555-1212",
                "link": "https://acmeroofing.com/contact",
            }
        ]
        with patch("app.core.scraper.settings.SERP_API_KEY", "test-key"), patch("app.core.scraper._serper_search", return_value=organic):
            email, phone, website, calls = fallback_contact_search("Acme Roofing", "Austin", "TX")
        self.assertEqual(email, "owner@acmeroofing.com")
        self.assertEqual(phone, "+15125551212")
        self.assertEqual(website, "https://acmeroofing.com/contact")
        self.assertEqual(calls, 1)

    def test_crawler_reads_mailto_and_tel_links(self):
        robots = Mock(status_code=200, text="User-agent: *\nAllow: /")
        page = Mock(
            status_code=200,
            text='<html><a href="mailto:owner@example.com?subject=Hello">Email</a><a href="tel:+15125551212">Call</a></html>',
            headers={"content-type": "text/html; charset=utf-8"},
        )
        page.raise_for_status.return_value = None
        client = Mock()
        client.get.side_effect = [robots, page, page, page]
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        with patch("app.core.scraper.httpx.Client", return_value=context), patch("app.core.scraper.time.sleep"):
            email, phone = crawl_contact("https://example.com")
        self.assertEqual(email, "owner@example.com")
        self.assertEqual(phone, "+15125551212")

    def test_scrape_job_uses_one_fallback_and_records_all_serp_calls(self):
        rows = {
            "scrape_jobs": {
                "job": {
                    "id": "job", "category": "roofers", "city": "Austin", "state": "TX",
                    "result_limit": 10, "status": "queued",
                }
            },
            "settings": {1: {"scrape_required_fields": ["email", "phone"]}},
            "clients": {},
            "scrape_discards": {},
        }

        class MemoryStore:
            def get(self, table, row_id): return rows.get(table, {}).get(row_id)
            def list(self, table, filters=None, order="", limit=1000): return list(rows.get(table, {}).values())[:limit]
            def insert(self, table, row): rows.setdefault(table, {})[row["id"]] = dict(row); return row
            def update(self, table, row_id, values): rows[table][row_id].update(values); return rows[table][row_id]

        candidate = {"business_name": "Acme Roofing", "website": "", "phone": "", "city": "Austin", "state": "TX", "source_detail": "serper_maps"}
        with patch("app.core.scraper.store", MemoryStore()), patch("app.core.scraper.settings.SERP_API_KEY", "test-key"), patch("app.core.scraper.serp_candidates", return_value=([candidate], 1)), patch("app.core.scraper.find_company_website", return_value=("", 1)), patch("app.core.scraper.crawl_contact", return_value=("", "")), patch("app.core.scraper.fallback_contact_search", return_value=("owner@acme.com", "+15125551212", "https://acme.com", 1)), patch("app.core.scraper.valid_email", side_effect=lambda value, check_mx=False: bool(value)):
            result = process_scrape_job("job")

        self.assertEqual(result, {"found": 1, "saved": 1, "discarded": 0})
        self.assertEqual(rows["scrape_jobs"]["job"]["serp_calls_used"], 3)
        saved = next(iter(rows["clients"].values()))
        self.assertEqual(saved["email"], "owner@acme.com")
        self.assertEqual(saved["outreach_angle"], "roofers in Austin, TX")


if __name__ == "__main__": unittest.main()
