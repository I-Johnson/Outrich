"""Stripe billing: plan gating, grandfathering, checkout, portal, webhooks."""
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outrich-billing-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.core import billing
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore
from app.db import SQLiteStore, new_id, now_iso

STAMP = "2026-09-26T12:00:00+00:00"
WEBHOOK_SECRET = "whsec_test_secret"


def sign(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    stamp = int(time.time())
    digest = hmac.new(secret.encode(), str(stamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


class BillingTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.core import freight as freight_core
        self.main = main
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "billing.db"))
        self.raw.init()
        from app import db as app_db
        for target in (patch.object(main, "store", self.raw), patch.object(freight_core, "store", self.raw), patch.object(app_db, "store", self.raw)):
            target.start(); self.addCleanup(target.stop)
        main.LOGIN_ATTEMPTS.clear(); self.addCleanup(main.LOGIN_ATTEMPTS.clear)
        # Stripe configured with test keys; restored after each test.
        self.env = billing.env
        self._saved = (self.env.STRIPE_SECRET_KEY, self.env.STRIPE_WEBHOOK_SECRET, self.env.STRIPE_PRICE_ID)
        def restore():
            (self.env.STRIPE_SECRET_KEY, self.env.STRIPE_WEBHOOK_SECRET, self.env.STRIPE_PRICE_ID) = self._saved
        self.addCleanup(restore)
        self.env.STRIPE_SECRET_KEY = "sk_test_fake"
        self.env.STRIPE_WEBHOOK_SECRET = WEBHOOK_SECRET
        self.env.STRIPE_PRICE_ID = "price_test_25"
        self.client = TestClient(main.app)

    def billing_off(self):
        self.env.STRIPE_SECRET_KEY = ""
        self.env.STRIPE_PRICE_ID = ""

    def signup(self, email="new@example.com"):
        response = self.client.post("/signup", data={"name": "New", "email": email, "password": "longenough1"}, follow_redirects=False)
        user = self.raw.list("app_users", {"email": email}, order="", limit=1)[0]
        return response, user

    def grandfather(self, user):
        self.raw.update("app_users", user["id"], {"billing_exempt": 1})
        return self.raw.get("app_users", user["id"])

    def post_webhook(self, event: dict, secret: str = WEBHOOK_SECRET):
        payload = json.dumps(event).encode()
        return self.client.post("/webhooks/stripe", content=payload,
                                headers={"stripe-signature": sign(payload, secret), "content-type": "application/json"})

    def future(self, days=30):
        return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    # --- access rules (unit) ---

    def test_admin_always_has_access(self):
        _, user = self.signup()
        self.assertTrue(billing.has_access({**user, "role": "admin"}))

    def test_billing_off_when_unconfigured(self):
        self.billing_off()
        _, user = self.signup()
        self.assertTrue(billing.has_access(user))
        self.assertFalse(billing.configured())

    def test_new_account_without_subscription_has_no_access(self):
        _, user = self.signup()
        self.assertEqual(int(user["billing_exempt"]), 0)
        self.assertFalse(billing.has_access(user))

    def test_grandfathered_account_keeps_access(self):
        _, user = self.signup()
        self.assertTrue(billing.has_access(self.grandfather(user)))

    def test_active_and_trialing_have_access(self):
        _, user = self.signup()
        for status in ("active", "trialing"):
            self.assertTrue(billing.has_access({**user, "stripe_subscription_status": status}))

    def test_canceled_keeps_access_until_period_end(self):
        _, user = self.signup()
        paid = {**user, "stripe_subscription_status": "canceled", "stripe_current_period_end": self.future(10)}
        self.assertTrue(billing.has_access(paid))
        lapsed = {**user, "stripe_subscription_status": "canceled", "stripe_current_period_end": self.future(-1)}
        self.assertFalse(billing.has_access(lapsed))

    def test_past_due_keeps_access_until_period_end(self):
        _, user = self.signup()
        grace = {**user, "stripe_subscription_status": "past_due", "stripe_current_period_end": self.future(10)}
        self.assertTrue(billing.has_access(grace))
        lapsed = {**user, "stripe_subscription_status": "past_due", "stripe_current_period_end": self.future(-1)}
        self.assertFalse(billing.has_access(lapsed))
        no_end = {**user, "stripe_subscription_status": "past_due"}
        self.assertFalse(billing.has_access(no_end))

    # --- gating (routes) ---

    def test_signup_lands_on_billing_when_subscription_required(self):
        response, _ = self.signup()
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/billing"))
        locked = self.client.get("/", follow_redirects=False)
        self.assertEqual(locked.status_code, 303)
        self.assertEqual(locked.headers["location"], "/billing")
        freight = self.client.get("/freight", follow_redirects=False)
        self.assertEqual(freight.status_code, 303)
        self.assertEqual(freight.headers["location"], "/billing")

    def test_signup_lands_home_when_billing_off(self):
        self.billing_off()
        response, _ = self.signup()
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/?"))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_grandfathered_user_passes_the_gate(self):
        _, user = self.signup()
        self.grandfather(user)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_billing_page_reachable_without_subscription(self):
        self.signup()
        page = self.client.get("/billing")
        self.assertEqual(page.status_code, 200)
        self.assertIn("$25", page.text)
        self.assertIn("Subscribe", page.text)

    def test_grandfathered_plan_page_shows_no_sales_card(self):
        _, user = self.signup()
        self.grandfather(user)
        page = self.client.get("/billing")
        self.assertIn("Early access", page.text)
        self.assertIn("$0", page.text)
        self.assertIn("free forever", page.text)
        self.assertNotIn("$25", page.text)
        self.assertNotIn("Cancel anytime", page.text)
        self.assertNotIn("/billing/checkout", page.text)
        settings = self.client.get("/settings")
        self.assertIn("Early access", settings.text)

    def test_canceled_plan_page_drops_the_evergreen_cancel_pitch(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "canceled", "stripe_current_period_end": self.future(10)})
        page = self.client.get("/billing")
        self.assertIn("Access until", page.text)
        self.assertNotIn("Cancel anytime", page.text)

    def test_settings_shows_plan_card(self):
        _, user = self.signup()
        self.grandfather(user)
        page = self.client.get("/settings")
        self.assertIn("Plan", page.text)
        self.assertIn("Early access - free forever", page.text)

    # --- checkout / portal ---

    def test_checkout_redirects_to_stripe(self):
        _, user = self.signup()
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("Session", (), {"id": "cs_test_1", "url": "https://checkout.stripe.com/test-session"})()
            response = self.client.post("/billing/checkout", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "https://checkout.stripe.com/test-session")
        params = create.call_args.kwargs
        self.assertEqual(params["mode"], "subscription")
        self.assertEqual(params["line_items"], [{"price": "price_test_25", "quantity": 1}])
        self.assertEqual(params["client_reference_id"], str(user["id"]))
        self.assertEqual(params["customer_email"], "new@example.com")
        # The open session is recorded on the account row.
        self.assertEqual(params["idempotency_key"], f"checkout-{user['id']}-0")
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_checkout_session_id"], "cs_test_1")
        self.assertTrue(updated["stripe_checkout_at"])
        self.assertEqual(updated["stripe_checkout_state"], "open")
        self.assertTrue(billing.checkout_open(updated))
        self.assertFalse(billing.checkout_pending(updated))

    def test_checkout_reuses_existing_customer(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_123"})
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("Session", (), {"id": "cs_test_1", "url": "https://checkout.stripe.com/test-session"})()
            self.client.post("/billing/checkout", follow_redirects=False)
        self.assertEqual(create.call_args.kwargs.get("customer"), "cus_123")
        self.assertNotIn("customer_email", create.call_args.kwargs)

    def test_checkout_fails_closed_when_unconfigured(self):
        self.billing_off()
        self.signup()
        response = self.client.post("/billing/checkout", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("/billing?notice=", response.headers["location"])

    def test_portal_requires_a_customer(self):
        self.signup()
        response = self.client.post("/billing/portal", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=", response.headers["location"])

    def test_portal_redirects_to_stripe(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_123"})
        with patch("stripe.billing_portal.Session.create") as create:
            create.return_value = type("Session", (), {"url": "https://billing.stripe.com/test-portal"})()
            response = self.client.post("/billing/portal", follow_redirects=False)
        self.assertEqual(response.headers["location"], "https://billing.stripe.com/test-portal")
        self.assertEqual(create.call_args.kwargs["customer"], "cus_123")

    # --- worker entitlement ---

    def test_unknown_owner_is_paused_not_trusted(self):
        _, user = self.signup()
        self.assertFalse(billing.owner_has_access(self.raw, "owner-with-no-account"))
        from app.core.tenancy import ADMIN_OWNER_ID
        self.assertTrue(billing.owner_has_access(self.raw, ADMIN_OWNER_ID))

    def seed_queued_work(self, user):
        from app.core.tenancy import OwnerStore
        scoped = OwnerStore(self.raw, str(user["id"]))
        scoped.insert("campaigns", {"id": "camp_1", "name": "C", "state": "running", "created_at": now_iso(), "updated_at": now_iso()})
        scoped.insert("email_log", {"id": "log_1", "campaign_id": "camp_1", "provider": "gmail", "subject_sent": "Hi",
                                    "status": "queued", "scheduled_for": "2020-01-01T00:00:00+00:00", "created_at": now_iso()})
        scoped.insert("gmail_senders", {"id": "sender_1", "email": "sender@example.com", "created_at": now_iso(), "updated_at": now_iso()})
        return scoped

    def test_lapsed_account_queued_email_never_sends(self):
        from app.core import sender as sender_core
        _, user = self.signup()
        scoped = self.seed_queued_work(user)
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "active", "stripe_current_period_end": self.future(10)})
        self.assertTrue(billing.owner_has_access(self.raw, str(user["id"])))
        # The subscription lapses: past due beyond the paid period.
        self.raw.update("app_users", user["id"], {"stripe_subscription_status": "past_due", "stripe_current_period_end": self.future(-1)})
        self.assertFalse(billing.owner_has_access(self.raw, str(user["id"])))
        result = sender_core.send_due(limit=10, storage=scoped)
        self.assertTrue(result.get("paused"))
        self.assertEqual(result["sent"], 0)
        # The queued message is preserved, not dropped or marked sent.
        self.assertEqual(scoped.get("email_log", "log_1")["status"], "queued")
        # Access resumes on a confirmed-active subscription.
        self.raw.update("app_users", user["id"], {"stripe_subscription_status": "active", "stripe_current_period_end": self.future(10)})
        self.assertTrue(billing.owner_has_access(self.raw, str(user["id"])))

    def test_worker_skips_lapsed_owners_in_both_workspaces(self):
        from app.jobs import scheduler
        _, user = self.signup()
        self.seed_queued_work(user)
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "past_due", "stripe_current_period_end": self.future(-1)})
        with patch.object(scheduler, "store", self.raw), \
             patch.object(scheduler, "send_due") as send_due, \
             patch.object(scheduler, "poll_freight_replies") as poll, \
             patch.object(scheduler, "recover_uncertain_freight_sends"):
            scheduler.tick()
        for call in send_due.call_args_list:
            self.assertNotEqual(call.kwargs["storage"].owner_id, str(user["id"]))
        poll.assert_not_called()
        # Grandfathered accounts are never paused.
        self.grandfather(user)
        with patch.object(scheduler, "store", self.raw), \
             patch.object(scheduler, "send_due") as send_due, \
             patch.object(scheduler, "poll_freight_replies"), \
             patch.object(scheduler, "recover_uncertain_freight_sends"):
            scheduler.tick()
        owners = [call.kwargs["storage"].owner_id for call in send_due.call_args_list]
        self.assertIn(str(user["id"]), owners)

    # --- webhooks ---

    def test_webhook_rejects_bad_signature(self):
        payload = json.dumps({"type": "ping"}).encode()
        response = self.client.post("/webhooks/stripe", content=payload,
                                    headers={"stripe-signature": "t=1,v1=bogus", "content-type": "application/json"})
        self.assertEqual(response.status_code, 400)

    def test_webhook_checkout_completed_activates_account(self):
        _, user = self.signup()
        subscription = {
            "id": "sub_1", "status": "active", "customer": "cus_1",
            "current_period_end": int(time.time()) + 30 * 86400,
            "items": {"data": [{"price": {"id": "price_test_25"}}]},
        }
        event = {"type": "checkout.session.completed",
                 "data": {"object": {"client_reference_id": str(user["id"]), "customer": "cus_1", "subscription": "sub_1", "metadata": {}}}}
        with patch("stripe.Subscription.retrieve", return_value=subscription):
            response = self.post_webhook(event)
        self.assertEqual(response.status_code, 200)
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_customer_id"], "cus_1")
        self.assertEqual(updated["stripe_subscription_status"], "active")
        self.assertEqual(updated["stripe_price_id"], "price_test_25")
        self.assertTrue(billing.has_access(updated))
        self.assertEqual(updated["stripe_checkout_session_id"], "")
        self.assertEqual(updated["stripe_checkout_state"], "")
        self.assertEqual(int(updated["stripe_checkout_attempt"]), 1)
        self.assertFalse(billing.checkout_pending(updated))
        # The previously locked user now passes the gate.
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_webhook_subscription_deleted_revokes_after_period(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "active", "stripe_current_period_end": self.future(10)})
        event = {"type": "customer.subscription.deleted",
                 "data": {"object": {"id": "sub_1", "customer": "cus_1", "status": "canceled",
                                     "current_period_end": int(time.time()) - 60, "metadata": {}}}}
        response = self.post_webhook(event)
        self.assertEqual(response.status_code, 200)
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_subscription_status"], "canceled")
        self.assertFalse(billing.has_access(updated))

    def test_webhook_invoice_payment_failed_marks_past_due(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "active", "stripe_current_period_end": self.future(10)})
        event = {"type": "invoice.payment_failed", "data": {"object": {"customer": "cus_1", "subscription": "sub_1"}}}
        response = self.post_webhook(event)
        self.assertEqual(response.status_code, 200)
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_subscription_status"], "past_due")
        # Grace: the account already paid for this period, so it stays in.
        self.assertTrue(billing.has_access(updated))
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_past_due_in_grace_passes_the_gate_and_sees_the_banner(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "past_due", "stripe_current_period_end": self.future(10)})
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("Your last payment failed", home.text)
        self.assertIn("update your card", home.text)
        plan = self.client.get("/billing")
        self.assertIn("payment failed", plan.text.lower())
        self.assertIn("Manage billing", plan.text)
        # No Subscribe button while a subscription exists, even a failing one.
        self.assertNotIn("/billing/checkout", plan.text)

    def test_past_due_after_period_end_is_locked(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "past_due", "stripe_current_period_end": self.future(-1)})
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/billing")
        plan = self.client.get("/billing")
        self.assertIn("paid period has ended", plan.text)
        self.assertIn("Manage billing", plan.text)

    def test_open_session_is_continue_checkout_not_payment_submitted(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_open",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "open"}):
            page = self.client.get("/billing")
        self.assertIn("Continue checkout", page.text)
        self.assertNotIn("Payment submitted", page.text)
        self.assertNotIn("Payment confirmation in progress", page.text)

    def test_reconcile_marks_completed_session_pending(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_done",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "complete"}):
            page = self.client.get("/billing")
        self.assertIn("Payment submitted", page.text)
        self.assertIn("Payment confirmation in progress", page.text)
        self.assertEqual(self.raw.get("app_users", user["id"])["stripe_checkout_state"], "complete")

    def test_reconcile_completed_session_self_repairs_missed_webhook(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_done",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        subscription = {
            "id": "sub_1", "status": "active", "customer": "cus_1",
            "current_period_end": int(time.time()) + 30 * 86400,
            "items": {"data": [{"price": {"id": "price_test_25"}}]},
        }
        with patch("stripe.checkout.Session.retrieve", return_value={
            "status": "complete", "customer": "cus_1", "subscription": "sub_1",
        }), patch("stripe.Subscription.retrieve", return_value=subscription):
            page = self.client.get("/billing?billing=pending")
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_customer_id"], "cus_1")
        self.assertEqual(updated["stripe_subscription_id"], "sub_1")
        self.assertEqual(updated["stripe_subscription_status"], "active")
        self.assertEqual(updated["stripe_price_id"], "price_test_25")
        self.assertEqual(updated["stripe_checkout_session_id"], "")
        self.assertTrue(billing.has_access(updated))
        self.assertIn("Subscription active. Welcome aboard.", page.text)

    def test_reconcile_completed_session_retries_when_subscription_fetch_fails(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_done",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={
            "status": "complete", "customer": "cus_1", "subscription": "sub_1",
        }), patch("stripe.Subscription.retrieve", side_effect=Exception("stripe unavailable")):
            page = self.client.get("/billing?billing=pending")
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_checkout_state"], "complete")
        self.assertEqual(updated["stripe_checkout_session_id"], "cs_done")
        self.assertIsNone(updated["stripe_subscription_status"])
        self.assertIn("Payment submitted", page.text)

    def test_reconcile_clears_expired_session_and_restores_subscribe(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_dead",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "expired"}):
            page = self.client.get("/billing")
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_checkout_session_id"], "")
        self.assertIn("/billing/checkout", page.text)
        self.assertNotIn("Payment submitted", page.text)

    def test_canceled_checkout_clears_the_session_and_offers_retry(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_open",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "open"}), \
             patch("stripe.checkout.Session.expire") as expire:
            response = self.client.get("/billing?checkout=canceled", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("Checkout+canceled", response.headers["location"])
        expire.assert_called_once_with("cs_open")
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_checkout_session_id"], "")
        self.assertEqual(updated["stripe_checkout_state"], "")
        self.assertEqual(int(updated["stripe_checkout_attempt"]), 1)
        page = self.client.get("/billing")
        self.assertIn("/billing/checkout", page.text)
        self.assertNotIn("Payment submitted", page.text)

    def test_subscribe_after_cancel_mints_a_new_session_with_a_fresh_key(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_open",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "open"}), \
             patch("stripe.checkout.Session.expire"):
            self.client.get("/billing?checkout=canceled", follow_redirects=False)
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("Session", (), {"id": "cs_new", "url": "https://checkout.stripe.com/new-session"})()
            response = self.client.post("/billing/checkout", follow_redirects=False)
        self.assertEqual(response.headers["location"], "https://checkout.stripe.com/new-session")
        self.assertEqual(create.call_args.kwargs["idempotency_key"], f"checkout-{user['id']}-1")
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_checkout_session_id"], "cs_new")
        self.assertEqual(updated["stripe_checkout_state"], "open")
        self.assertEqual(int(updated["stripe_checkout_attempt"]), 1)

    def test_cancel_keeps_the_record_when_stripe_cannot_confirm_expiry(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_open",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "open"}), \
             patch("stripe.checkout.Session.expire", side_effect=Exception("stripe unreachable")):
            response = self.client.get("/billing?checkout=canceled", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("closing%20out%20the%20session", response.headers["location"])
        # Record kept: no instant retry while the old session may be live.
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_checkout_session_id"], "cs_open")
        self.assertEqual(int(updated["stripe_checkout_attempt"]), 0)
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "open"}):
            page = self.client.get("/billing")
        self.assertIn("Continue checkout", page.text)
        self.assertNotIn("Subscribe - $25", page.text)

    def test_canceled_url_with_a_completed_session_shows_pending(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_done",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "complete"}):
            response = self.client.get("/billing?checkout=canceled", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/billing?billing=pending")

    def test_pending_checkout_banner_is_neutral_until_webhook_confirms(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_test_1",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "complete"})
        with patch("stripe.checkout.Session.retrieve", return_value={"status": "complete"}):
            page = self.client.get("/billing?billing=pending")
        self.assertIn("Payment submitted", page.text)
        self.assertIn("refresh this page", page.text)
        self.assertIn("mailto:", page.text)
        self.assertNotIn("Welcome aboard", page.text)
        # No second Checkout while the first payment is confirming, on any
        # view of the Plan page; the retry path returns when the session expires.
        self.assertNotIn("/billing/checkout", page.text)
        plain = self.client.get("/billing")
        self.assertNotIn("/billing/checkout", plain.text)
        self.assertIn("Payment confirmation in progress", plain.text)
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "active", "stripe_current_period_end": self.future(30),
                                                  "stripe_checkout_session_id": "", "stripe_checkout_at": "", "stripe_checkout_state": ""})
        page = self.client.get("/billing?billing=pending")
        self.assertIn("Subscription active. Welcome aboard.", page.text)

    def test_checkout_success_url_uses_pending_state(self):
        _, user = self.signup()
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("Session", (), {"id": "cs_test_1", "url": "https://checkout.stripe.com/test-session"})()
            self.client.post("/billing/checkout", follow_redirects=False)
        self.assertTrue(create.call_args.kwargs["success_url"].endswith("/billing?billing=pending"))

    def test_checkout_never_mints_a_second_open_session(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_open",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        existing = type("Session", (dict,), {"url": "https://checkout.stripe.com/open-session"})(status="open")
        with patch("stripe.checkout.Session.retrieve", return_value=existing) as retrieve, \
             patch("stripe.checkout.Session.create") as create:
            response = self.client.post("/billing/checkout", follow_redirects=False)
        self.assertEqual(response.headers["location"], "https://checkout.stripe.com/open-session")
        retrieve.assert_called_once_with("cs_open")
        create.assert_not_called()

    def test_checkout_refuses_when_the_recorded_session_cannot_be_verified(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_open",
                                                  "stripe_checkout_at": datetime.now(timezone.utc).isoformat(),
                                                  "stripe_checkout_state": "open"})
        with patch("stripe.checkout.Session.retrieve", side_effect=Exception("stripe unreachable")), \
             patch("stripe.checkout.Session.create") as create:
            response = self.client.post("/billing/checkout", follow_redirects=False)
        self.assertIn("/billing?notice=", response.headers["location"])
        create.assert_not_called()

    def test_expired_recorded_session_unlocks_subscribe(self):
        _, user = self.signup()
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.raw.update("app_users", user["id"], {"stripe_checkout_session_id": "cs_old", "stripe_checkout_at": stale})
        page = self.client.get("/billing")
        self.assertNotIn("Payment confirmation in progress", page.text)
        self.assertIn("/billing/checkout", page.text)
        # A bare query flag without a recorded session shows no pending state.
        flagged = self.client.get("/billing?billing=pending")
        self.assertNotIn("Payment submitted", flagged.text)
        self.assertIn("/billing/checkout", flagged.text)

    def test_webhook_invoice_paid_refreshes_subscription(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "past_due"})
        subscription = {"id": "sub_1", "status": "active", "customer": "cus_1",
                        "current_period_end": int(time.time()) + 30 * 86400, "items": {"data": []}}
        event = {"type": "invoice.paid", "data": {"object": {"customer": "cus_1", "subscription": "sub_1"}}}
        with patch("stripe.Subscription.retrieve", return_value=subscription):
            response = self.post_webhook(event)
        self.assertEqual(response.status_code, 200)
        updated = self.raw.get("app_users", user["id"])
        self.assertEqual(updated["stripe_subscription_status"], "active")
        self.assertTrue(billing.has_access(updated))

    def test_webhook_touches_only_the_matching_account(self):
        _, user_a = self.signup("a@example.com")
        _, user_b = self.signup("b@example.com")
        self.raw.update("app_users", user_b["id"], {"stripe_customer_id": "cus_b", "stripe_subscription_id": "sub_b",
                                                    "stripe_subscription_status": "active"})
        event = {"type": "customer.subscription.deleted",
                 "data": {"object": {"id": "sub_b", "customer": "cus_b", "status": "canceled",
                                     "current_period_end": int(time.time()) - 60, "metadata": {}}}}
        self.post_webhook(event)
        self.assertEqual(self.raw.get("app_users", user_b["id"])["stripe_subscription_status"], "canceled")
        self.assertNotEqual(self.raw.get("app_users", user_a["id"])["stripe_customer_id"], "cus_b")

    def test_webhook_unknown_user_is_accepted_without_writes(self):
        event = {"type": "customer.subscription.deleted",
                 "data": {"object": {"id": "sub_ghost", "customer": "cus_ghost", "status": "canceled", "metadata": {}}}}
        response = self.post_webhook(event)
        self.assertEqual(response.status_code, 200)
        self.assertIn("unknown user", response.json()["outcome"])


class SupportContactStartupTests(unittest.TestCase):
    def test_billing_requires_a_support_contact_at_startup(self):
        import subprocess, sys
        env = {**os.environ, "STRIPE_SECRET_KEY": "sk_test_x", "STRIPE_PRICE_ID": "price_x",
               "SUPPORT_EMAIL": "", "ADMIN_EMAIL": "", "SCHEDULER_ENABLED": "false",
               "DATABASE_BACKEND": "sqlite", "DB_PATH": "/tmp/outrich-support-test.db",
               "PYTHON_DOTENV_DISABLED": "1"}
        result = subprocess.run([sys.executable, "-c", "import app.config"],
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SUPPORT_EMAIL", result.stderr)
        env["SUPPORT_EMAIL"] = "support@example.com"
        result = subprocess.run([sys.executable, "-c", "import app.config"],
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
