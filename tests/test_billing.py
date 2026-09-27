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
            create.return_value = type("Session", (), {"url": "https://checkout.stripe.com/test-session"})()
            response = self.client.post("/billing/checkout", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "https://checkout.stripe.com/test-session")
        params = create.call_args.kwargs
        self.assertEqual(params["mode"], "subscription")
        self.assertEqual(params["line_items"], [{"price": "price_test_25", "quantity": 1}])
        self.assertEqual(params["client_reference_id"], str(user["id"]))
        self.assertEqual(params["customer_email"], "new@example.com")

    def test_checkout_reuses_existing_customer(self):
        _, user = self.signup()
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_123"})
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("Session", (), {"url": "https://checkout.stripe.com/test-session"})()
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

    def test_pending_checkout_banner_is_neutral_until_webhook_confirms(self):
        _, user = self.signup()
        page = self.client.get("/billing?billing=pending")
        self.assertIn("Payment submitted", page.text)
        self.assertIn("refresh this page", page.text)
        self.assertIn("mailto:", page.text)
        self.assertNotIn("Welcome aboard", page.text)
        # No second Checkout while the first payment is confirming.
        self.assertNotIn("/billing/checkout", page.text)
        # The plain Plan page keeps the retry path open.
        self.assertIn("/billing/checkout", self.client.get("/billing").text)
        self.raw.update("app_users", user["id"], {"stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
                                                  "stripe_subscription_status": "active", "stripe_current_period_end": self.future(30)})
        page = self.client.get("/billing?billing=pending")
        self.assertIn("Subscription active. Welcome aboard.", page.text)

    def test_checkout_success_url_uses_pending_state(self):
        _, user = self.signup()
        with patch("stripe.checkout.Session.create") as create:
            create.return_value = type("Session", (), {"url": "https://checkout.stripe.com/test-session"})()
            self.client.post("/billing/checkout", follow_redirects=False)
        self.assertTrue(create.call_args.kwargs["success_url"].endswith("/billing?billing=pending"))

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


if __name__ == "__main__":
    unittest.main()
