import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-freight-test.db"
os.environ["FREIGHT_AGENT_MODE"] = "rules"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.adapters.base import SendResult
from app.core.freight import SensitiveOutboundConfirmationRequired, auto_lane_issue, classify_reply, evaluate_inbound, extract_offer, extract_numeric_facts, load_economics, mission_price_comparison, parse_destinations, recover_uncertain_freight_sends, reevaluate_verified_load, seed_freight_template, send_draft, sensitive_outbound_fields, set_thread_state, verify_load_facts
from app.core import freight as freight_module
from app.core import freight_agent as freight_agent_module
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore
from app.db import SQLiteStore, new_id, now_iso


class FreightEconomicsTests(unittest.TestCase):
    def test_all_in_rpm_includes_deadhead(self):
        result = load_economics({"posted_rate": 2500, "loaded_miles": 1000, "deadhead_miles": 100})
        self.assertEqual(result["loaded_rpm"], 2.5)
        self.assertEqual(result["all_in_rpm"], 2.27)

    def test_offer_overrides_posted_rate(self):
        result = load_economics({"posted_rate": 2000, "loaded_miles": 800}, offer=2400)
        self.assertEqual(result["rate"], 2400)
        self.assertEqual(result["loaded_rpm"], 3.0)

    def test_comparison_waits_for_every_mission_floor(self):
        mission = {"floor_total": 2000, "floor_loaded_rpm": 2.5, "floor_all_in_rpm": 2.2}
        load = {"current_offer": 2600}
        comparison = mission_price_comparison(load, mission)
        self.assertIsNone(comparison["minimum_total"])
        self.assertEqual(comparison["known_floor"], 2000)
        self.assertEqual(comparison["missing"], ["loaded miles"])
        load.update({"loaded_miles": 1000, "loaded_miles_verified": True})
        comparison = mission_price_comparison(load, mission)
        self.assertEqual(comparison["missing"], ["deadhead miles"])
        load.update({"deadhead_miles": 200, "deadhead_miles_verified": True})
        comparison = mission_price_comparison(load, mission)
        self.assertEqual(comparison["minimum_total"], 2640)
        self.assertEqual(comparison["difference"], -40)


class FreightPolicyTests(unittest.TestCase):
    def test_sensitive_initial_email_fields_are_detected(self):
        fields = sensitive_outbound_fields(
            "Truck available",
            "MC 123456\nCall dispatch at (602) 555-0199. We can send the COI after booking.",
        )
        self.assertIn("authority ID (MC/USDOT)", fields)
        self.assertIn("phone number", fields)
        self.assertIn("insurance or financial document", fields)
        self.assertEqual(sensitive_outbound_fields("Truck available", "53 ft dry van available near Phoenix."), [])

    def test_protected_reply_stops_on_call_and_rate_confirmation(self):
        result = classify_reply("Rate confirmation attached. Please give me a call.")
        self.assertEqual(result["kind"], "protected")
        self.assertIn("call_requested", result["protected"])
        self.assertIn("rate_confirmation", result["protected"])
        self.assertIn("rate_confirmation", classify_reply("RateCon.pdf attached")["protected"])

    def test_quoted_only_email_never_extracts_a_historical_offer(self):
        self.assertIsNone(extract_offer("On Sep 23, broker wrote:\n> Rate is $4,500"))
        self.assertEqual(classify_reply("On Sep 23, broker wrote:\n> Rate is $4,500")["kind"], "other")

    def test_rate_and_profile_question_are_extracted(self):
        result = classify_reply("Can do $2,450. Is this a true team with a 53 ft dry van?")
        self.assertEqual(result["offer"], 2450)
        self.assertIn("team_status", result["questions"])
        self.assertIn("equipment_type", result["questions"])

    def test_offer_parser_does_not_treat_short_numbers_as_rate(self):
        self.assertIsNone(extract_offer("53 ft dry van, 42,000 lbs"))

    def test_freight_rate_phrases_and_non_rate_numbers(self):
        for message, expected in (
            ("Rate- 4,000.00", 4000),
            ("Max is 4200", 4200),
            ("I can get you 2450 on it", 2450),
            ("Maybe I can squeeze it to 4200", 4200),
            ("Can do $3,500", 3500),
        ):
            with self.subTest(message=message):
                self.assertEqual(extract_offer(message), expected)
        for message, unit in (
            ("Reach me at 555-0100", "phone"),
            ("Can you pick up tonight at 2200?", "time"),
            ("Cargo weight is at 42,000 lbs", "weight"),
            ("1,068 miles", "miles"),
            ("Rate is $2.45 per mile", "rate_per_mile"),
            ("Pickup 2026-09-23", "date"),
        ):
            with self.subTest(message=message):
                self.assertIsNone(extract_offer(message))
                self.assertIn(unit, {fact["unit"] for fact in extract_numeric_facts(message)})
        self.assertTrue(classify_reply("Rate is $4,000 or maybe $4,200")["ambiguous_offer"])
        self.assertTrue(classify_reply("Can do $4,200, perhaps 4300 if team")["ambiguous_offer"])
        self.assertEqual(classify_reply("Pickup 2026-09-23; weight 42,000 lbs and rate $4,200")["offer"], 4200)
        self.assertEqual(classify_reply("Reach me at 555-0100")["kind"], "protected")

    def test_configurable_destinations(self):
        result = parse_destinations(["Dallas, TX", "Southeast", ""], ["city", "region", "state"], ["75", "0", "25"])
        self.assertEqual(result, [
            {"label": "Dallas, TX", "kind": "city", "radius_miles": 75},
            {"label": "Southeast", "kind": "region", "radius_miles": 0},
        ])

    def test_city_destination_label_carries_its_state(self):
        # One source of truth: the label holds city and state together.
        result = parse_destinations(["Dallas, TX"], ["city"], ["0"])
        self.assertEqual(result, [{"label": "Dallas, TX", "kind": "city", "radius_miles": 0}])

    def test_auto_mode_requires_one_priced_lane(self):
        mission = {"origin_state": "FL", "destinations": [{"kind": "state", "label": "NJ"}], "target_total": 5000}
        self.assertIsNone(auto_lane_issue(mission))
        mission["destinations"].append({"kind": "city", "label": "Dallas, TX"})
        self.assertIn("one destination", auto_lane_issue(mission))
        mission["destinations"] = [{"kind": "state", "label": "NJ"}]
        mission["target_total"] = None
        self.assertIn("target", auto_lane_issue(mission))
        mission["target_total"] = 5000
        mission["destinations"] = [{"kind": "anywhere", "label": "Open"}]
        self.assertIn("exact city", auto_lane_issue(mission))



class FreightSettingsAndSendTests(unittest.TestCase):
    def test_default_first_touch_template_omits_authority_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = SQLiteStore(os.path.join(temp, "template.db"))
            storage.init()
            seed_freight_template(storage)
            template = storage.list("email_templates", {"vertical": "freight"}, order="", limit=10)[0]
            self.assertNotIn("mc_number", template["body"].lower())
            self.assertNotIn("dot_number", template["body"].lower())

    def test_dry_run_preserves_unique_thread_message_ids(self):
        from app.adapters.gmail_adapter import GmailProvider
        sender = GmailProvider("carrier@example.com", "test-password")
        with patch("app.adapters.gmail_adapter.settings.DRY_RUN", True):
            first = sender.send(to="broker@example.com", subject="Load", body="Info?", content_type="plain", from_address="carrier@example.com", from_name="Dispatch", reply_to="carrier@example.com", headers={"Message-ID": "<first@example.com>"})
            second = sender.send(to="broker@example.com", subject="Re: Load", body="Counter", content_type="plain", from_address="carrier@example.com", from_name="Dispatch", reply_to="carrier@example.com", headers={"Message-ID": "<second@example.com>"})
        self.assertEqual(first.provider_id, "<first@example.com>")
        self.assertEqual(second.provider_id, "<second@example.com>")

    def test_freight_settings_are_separate_from_outreach(self):
        from unittest.mock import MagicMock
        from app.core.freight import freight_config, freight_sender_context

        storage = MagicMock()
        storage.get.side_effect = lambda table, row_id: {
            "settings": {"sender_name": "Outreach Alice", "email_signature": "Outreach Signature", "reply_to": "outreach@firm.com"},
            "freight_settings": {"sender_name": "Freight Logistics", "email_signature": "Dispatch Dept\n(800) 555-0199", "reply_to": "loads@freightfirm.com"},
        }.get(table, {})

        cfg = freight_config(storage)
        self.assertEqual(cfg["sender_name"], "Freight Logistics")
        self.assertEqual(cfg["email_signature"], "Dispatch Dept\n(800) 555-0199")
        self.assertEqual(cfg["signature"], "Dispatch Dept\n(800) 555-0199")
        self.assertEqual(cfg["reply_to"], "loads@freightfirm.com")

        sender = {"id": "1", "display_name": "Personal Gmail", "email": "trucker@gmail.com", "signature": "Personal sig"}
        sender_ctx = freight_sender_context(cfg, sender)
        self.assertEqual(sender_ctx["sender_name"], "Freight Logistics")
        self.assertEqual(sender_ctx["signature"], "Dispatch Dept\n(800) 555-0199")
        self.assertEqual(sender_ctx["reply_to"], "loads@freightfirm.com")

    def test_freight_template_preview_resolves_signature(self):
        from app.core.template_engine import render_template
        template = "Truck available — {{equipment}}\nMC {{mc_number}}\n{{signature}}"
        lead = {"equipment": "53 ft dry van", "mc_number": "123456", "signature": "Dispatch Team"}
        business = {}  # No signature in business settings
        rendered, missing = render_template(template, lead, business)
        self.assertEqual(missing, [])
        self.assertIn("Dispatch Team", rendered)
        self.assertIn("53 ft dry van", rendered)


class FreightConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "freight.db"))
        self.raw.init()
        # Freight data always belongs to an owner; these fixtures belong to the admin.
        self.storage = OwnerStore(self.raw, ADMIN_OWNER_ID)
        stamp = now_iso()
        self.profile_id, self.mission_id, self.load_id, self.thread_id = (new_id() for _ in range(4))
        self.storage.insert("freight_truck_profiles", {
            "id": self.profile_id, "name": "Truck 1", "current_city": "Phoenix", "current_state": "AZ",
            "equipment_type": "dry van", "trailer_length_ft": 53, "max_weight_lbs": 45000,
            "team_status": "team", "mc_number": "123456", "shareable_fields": ["equipment_type", "team_status", "mc_number"],
            "created_at": stamp, "updated_at": stamp,
        })
        self.storage.insert("freight_missions", {
            "id": self.mission_id, "name": "Phoenix to Dallas", "truck_profile_id": self.profile_id,
            "origin_city": "Phoenix", "origin_state": "AZ", "pickup_start": "2026-09-23", "pickup_end": "2026-09-23",
            "equipment_type": "dry van", "destinations": [{"label": "Dallas, TX", "kind": "city", "radius_miles": 0}],
            "floor_total": 3900, "target_total": 4500, "maximum_counter_rounds": 2,
            "permissions": {"auto_counter": False, "auto_profile_reply": False, "auto_pass": False},
            "created_at": stamp, "updated_at": stamp,
        })
        self.storage.insert("freight_loads", {
            "id": self.load_id, "mission_id": self.mission_id, "truck_profile_id": self.profile_id,
            "broker_email": "broker@example.com", "origin_city": "Phoenix", "origin_state": "AZ",
            "destination_city": "Open destinations", "subject": "Truck available", "status": "waiting",
            "created_at": stamp, "updated_at": stamp,
        })
        self.storage.insert("freight_threads", {
            "id": self.thread_id, "load_id": self.load_id, "sender_account": "1", "recipient_email": "broker@example.com",
            "subject": "Truck available", "root_message_id": "<first@example.com>", "last_message_id": "<first@example.com>",
            "state": "waiting", "last_activity_at": stamp, "created_at": stamp, "updated_at": stamp,
        })
        self.received = 0
        self.sent = 0

    def inbound(self, body):
        self.received += 1
        message_id = f"<broker-{self.received}@example.com>"
        msg = self.storage.insert("freight_messages", {
            "id": new_id(), "thread_id": self.thread_id, "direction": "in", "provider_message_id": message_id,
            "from_email": "broker@example.com", "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": body, "classification": {}, "created_at": now_iso(),
        })
        self.storage.update("freight_threads", self.thread_id, {"last_message_id": message_id})
        return evaluate_inbound(self.storage.get("freight_threads", self.thread_id), msg, self.storage)

    def send(self, draft):
        self.sent += 1
        provider = Mock()
        provider.send.return_value = SendResult(True, f"<sent-{self.sent}@example.com>")
        sender = {"id": "1", "email": "carrier@example.com", "provider": "gmail", "display_name": "Dispatch"}
        with patch("app.core.freight.get_gmail_sender", return_value=sender), patch("app.core.freight.get_provider", return_value=provider):
            return send_draft(draft["id"], self.storage)

    def verified_facts(self, **overrides):
        return {
            "origin_city": "Phoenix", "origin_state": "AZ", "equipment_type": "dry van",
            "destination_city": "Dallas", "destination_state": "TX", "pickup_date": "2026-09-23",
            "loaded_miles": 1000, "deadhead_miles": 100, "weight_lbs": 40000,
            "origin_confirmed": "yes", "equipment_confirmed": "yes", "pickup_date_confirmed": "yes",
            "schedule_confirmed": "yes",
            **overrides,
        }

    def test_three_turn_rate_negotiation_and_counter_events(self):
        first = self.inbound("Pickup 09/23, delivery to Dallas, TX. 1,068 miles, weight 40,000 lbs. Rate- 4,000.00")
        self.assertEqual(first["action"], "draft")
        self.assertEqual(first["draft"]["policy_snapshot"]["offer"], 4000)
        self.assertTrue(first["draft"]["policy_snapshot"]["safe_to_auto_send"])
        self.assertIn("broker equipment", first["draft"]["policy_snapshot"]["booking_readiness_blockers"])
        self.send(first["draft"])
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 1)
        self.storage.update("freight_missions", self.mission_id, {"target_total": 4300})
        second = self.inbound("Maybe I can squeeze it to 4200")
        self.assertEqual(second["action"], "draft")
        self.send(second["draft"])
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 2)
        final = self.inbound("Max is 4300. That works if you can cover it.")
        self.assertEqual(final["action"], "alert")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "accepted_pending_review")
        events = self.storage.list("freight_negotiation_events", {"thread_id": self.thread_id}, order="", limit=100)
        self.assertEqual(sum(event["event_type"] == "counter" for event in events), 2)

    def test_total_floor_does_not_bypass_unresolved_per_mile_floor(self):
        self.storage.update("freight_missions", self.mission_id, {"floor_loaded_rpm": 2.5})
        verify_load_facts(self.load_id, {"destination_city": "Dallas", "destination_state": "TX"}, self.storage)
        result = self.inbound("Rate is $4,000")
        self.assertEqual(result["action"], "alert")
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_offer"], 4000)
        self.assertTrue(self.storage.list("freight_alerts", {"thread_id": self.thread_id, "kind": "miles_unverified"}, order="", limit=1))

    def test_reprocessing_after_manual_save_preserves_confirmed_facts(self):
        self.inbound("Pickup 09/23, delivery to Dallas, TX. 1,068 miles, weight 40,000 lbs. Rate- 4,000.00")
        verify_load_facts(self.load_id, self.verified_facts(loaded_miles=850, deadhead_miles=75, weight_lbs=35000), self.storage)
        latest = self.storage.list("freight_messages", {"thread_id": self.thread_id, "direction": "in"}, order="created_at desc", limit=1)[0]
        reevaluate_verified_load(self.thread_id, latest, self.storage)
        load = self.storage.get("freight_loads", self.load_id)
        self.assertEqual(load["loaded_miles"], 850)
        self.assertEqual(load["deadhead_miles"], 75)
        self.assertEqual(load["weight_lbs"], 35000)
        self.assertTrue(load["schedule_verified"])

    def test_save_facts_endpoint_rechecks_reply_without_invalid_state(self):
        from fastapi.testclient import TestClient
        from app import main

        self.inbound("Pickup 09/23, delivery to Dallas, TX. 1,068 miles, weight 40,000 lbs. Rate- 4,000.00")
        with patch.object(main, "store", self.raw):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)
            response = client.post(
                f"/freight/loads/{self.load_id}/facts",
                data=self.verified_facts(loaded_miles=850, deadhead_miles=75, weight_lbs=35000),
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        self.assertIn("Load%20details%20saved%20and%20broker%20reply%20rechecked", response.headers["location"])
        load = self.storage.get("freight_loads", self.load_id)
        self.assertEqual(load["loaded_miles"], 850)
        self.assertEqual(load["weight_lbs"], 35000)

    def test_profile_answer_does_not_consume_a_counter(self):
        result = self.inbound("Is this a true team with a 53 ft dry van?")
        self.assertEqual(result["action"], "draft")
        self.send(result["draft"])
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 0)
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "waiting")

    def test_draft_with_authority_id_requires_explicit_confirmation(self):
        draft = self.storage.insert("freight_drafts", {
            "id": new_id(), "thread_id": self.thread_id, "subject": "Re: Truck available",
            "body_text": "MC 123456 is on file.", "reason": "profile_fact_reply",
            "policy_snapshot": {}, "in_reply_to_message_id": self.storage.get("freight_threads", self.thread_id)["last_message_id"],
            "status": "pending", "created_at": now_iso(), "updated_at": now_iso(),
        })
        with self.assertRaises(SensitiveOutboundConfirmationRequired):
            send_draft(draft["id"], self.storage)

    def test_phone_time_weight_and_unverified_rpm_do_not_trigger_counters(self):
        self.assertEqual(self.inbound("Reach me at 555-0100")["action"], "alert")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "protected_review")
        set_thread_state(self.thread_id, "negotiating", self.storage)
        tonight = self.inbound("Can you pick up tonight at 2200?")
        self.assertEqual(tonight["action"], "draft")
        self.assertEqual(tonight["draft"]["reason"], "clarify_load_details")
        weight = self.inbound("Cargo weight is at 42,000 lbs")
        self.assertEqual(weight["action"], "draft")
        self.assertEqual(weight["draft"]["reason"], "clarify_load_details")
        self.assertTrue(weight["draft"]["policy_snapshot"]["manual_only"])
        rpm = self.inbound("Rate is $2.45 per mile")
        self.assertEqual(rpm["action"], "draft")
        self.assertEqual(rpm["draft"]["reason"], "clarify_load_details")
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 0)
        self.assertIsNone(self.storage.get("freight_loads", self.load_id)["current_offer"])

    def test_mismatch_target_gate_reoffer_and_close_states(self):
        mismatch = self.inbound("Need reefer, dry vans will not work. Rate is $4,000")
        self.assertEqual(mismatch["action"], "alert")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "mismatch")
        self.assertFalse(self.storage.list("freight_drafts", {"thread_id": self.thread_id}, order="", limit=1))
        set_thread_state(self.thread_id, "negotiating", self.storage)
        self.storage.update("freight_missions", self.mission_id, {"target_total": None, "counter_amount": None})
        no_target = self.inbound("Dry van works. Pickup 09/23, delivery to Dallas, TX. Weight 40,000 lbs. Rate- 3,500")
        self.assertEqual(no_target["action"], "alert")
        self.assertIn("price target", no_target["summary"])
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "needs_attention")
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 0)
        self.storage.update("freight_missions", self.mission_id, {"target_total": 4000})
        reoffer = self.inbound("Can do $4,000")
        self.assertEqual(reoffer["action"], "draft")
        self.assertEqual(reoffer["draft"]["reason"], "clarify_load_details")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "draft_ready")
        self.assertEqual(self.inbound("Load is covered now")["action"], "closed")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "closed")

    def test_ambiguous_rates_require_review_and_verified_miles_allow_rpm_conversion(self):
        ambiguous = self.inbound("Rate is $4,000 or maybe $4,200")
        self.assertEqual(ambiguous["action"], "draft")
        self.assertEqual(ambiguous["draft"]["reason"], "clarify_rate")
        self.assertEqual(ambiguous["draft"]["policy_snapshot"]["manual_only"], True)
        verify_load_facts(self.load_id, self.verified_facts(), self.storage)
        rpm = self.inbound("Rate is $2.45 per mile")
        self.assertEqual(rpm["action"], "draft")
        self.assertEqual(rpm["draft"]["policy_snapshot"]["offer"], 2450)
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_offer"], 2450)

    def test_counter_limit_and_booked_replies_never_auto_counter(self):
        verify_load_facts(self.load_id, self.verified_facts(), self.storage)
        for offer in (4000, 4100):
            result = self.inbound(f"Can do ${offer}")
            self.assertEqual(result["action"], "draft")
            self.send(result["draft"])
            self.storage.update("freight_missions", self.mission_id, {"target_total": 4400})
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 2)
        result = self.inbound("Max is 4200")
        self.assertEqual(result["action"], "alert")
        self.assertIn("Counter limit", result["summary"])
        booking = freight_module.record_agreement(self.thread_id, 4200.0, "msg-test", self.storage)
        freight_module.submit_rate_con(booking["id"], {"total_rate": "4200", "pickup_city": "Phoenix", "pickup_state": "AZ", "delivery_city": "Dallas", "delivery_state": "TX", "pickup_date": "2026-09-23"}, self.storage)
        freight_module.review_rate_con(booking["id"], True, self.storage)
        freight_module.approve_driver_handoff(booking["id"], self.storage)
        linked_broker = self.storage.get("freight_loads", self.load_id).get("broker_id")
        freight_module.update_broker(linked_broker, {"credit_status": "approved", "setup_status": "complete"}, self.storage)
        set_thread_state(self.thread_id, "booked", self.storage)
        booked = self.inbound("Can do $4,500. Where is the truck now?")
        self.assertEqual(booked["action"], "alert")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "booked")
        ratecon = self.inbound("Rate confirmation attached. Send driver info.")
        self.assertEqual(ratecon["action"], "alert")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "booked")

    def test_auto_mode_requests_unknown_destination_without_approval(self):
        mission = self.storage.get("freight_missions", self.mission_id)
        self.storage.update("freight_missions", self.mission_id, {"permissions": {**mission["permissions"], "auto_counter": True}})
        result = self.inbound("Rate- 4,000")
        # Without a configured Gmail sender, auto-send fails closed. The
        # clarification remains pending for the dispatcher rather than being
        # falsely marked sent.
        self.assertEqual(result["action"], "alert")
        self.assertIn("Gmail sender is unavailable", result["warning"])
        self.assertEqual(result["draft"]["reason"], "clarify_load_details")
        self.assertIn("delivery city", result["draft"]["body_text"])
        self.assertEqual(result["draft"]["status"], "pending")
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 0)

    def test_verified_fit_allows_one_permitted_auto_counter(self):
        verify_load_facts(self.load_id, self.verified_facts(), self.storage)
        mission = self.storage.get("freight_missions", self.mission_id)
        self.storage.update("freight_missions", self.mission_id, {"permissions": {**mission["permissions"], "auto_counter": True}})
        provider = Mock()
        provider.send.return_value = SendResult(True, "<auto-counter@example.com>")
        sender = {"id": "1", "email": "carrier@example.com", "provider": "gmail", "display_name": "Dispatch"}
        with patch("app.core.freight.get_gmail_sender", return_value=sender), patch("app.core.freight.get_provider", return_value=provider):
            result = self.inbound("Can do $4,000")
        self.assertEqual(result["action"], "sent")
        self.assertEqual(provider.send.call_count, 1)
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 1)

    def test_manual_verification_rejects_bad_fit(self):
        with self.assertRaisesRegex(ValueError, "outside this mission"):
            verify_load_facts(self.load_id, self.verified_facts(destination_city="Atlanta", destination_state="GA"), self.storage)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            verify_load_facts(self.load_id, self.verified_facts(weight_lbs=48000), self.storage)
        with self.assertRaisesRegex(ValueError, "equipment differs"):
            verify_load_facts(self.load_id, self.verified_facts(equipment_type="reefer"), self.storage)

    def test_ambiguous_thread_match_does_not_pick_a_load(self):
        from email.message import EmailMessage
        from app.core.freight import _find_thread
        stamp = now_iso()
        second_load, second_thread = new_id(), new_id()
        self.storage.insert("freight_loads", {
            "id": second_load, "mission_id": self.mission_id, "truck_profile_id": self.profile_id,
            "broker_email": "broker@example.com", "origin_city": "Phoenix", "destination_city": "Open destinations",
            "subject": "Truck available", "status": "waiting", "created_at": stamp, "updated_at": stamp,
        })
        self.storage.insert("freight_threads", {
            "id": second_thread, "load_id": second_load, "sender_account": "1", "recipient_email": "broker@example.com",
            "subject": "Truck available", "root_message_id": "<second@example.com>", "last_message_id": "<second@example.com>",
            "state": "waiting", "last_activity_at": stamp, "created_at": stamp, "updated_at": stamp,
        })
        reply = EmailMessage()
        reply["From"] = "broker@example.com"
        reply["Subject"] = "Re: Truck available"
        self.assertIsNone(_find_thread(reply, "1", self.storage))
        reply["In-Reply-To"] = "<first@example.com>"
        self.assertEqual(_find_thread(reply, "1", self.storage)["id"], self.thread_id)
        # Matching References are not proof that this is the broker. Do not
        # misroute a different sender's message to an existing load.
        forged = EmailMessage()
        forged["From"] = "unrelated@example.net"
        forged["Subject"] = "Re: Truck available"
        forged["In-Reply-To"] = "<first@example.com>"
        self.assertIsNone(_find_thread(forged, "1", self.storage))

    def test_uncertain_send_cannot_be_retried_blindly(self):
        result = self.inbound("Is this a true team?")
        draft = result["draft"]
        provider = Mock()
        provider.send.side_effect = TimeoutError("transport timeout")
        sender = {"id": "1", "email": "carrier@example.com", "provider": "gmail", "display_name": "Dispatch"}
        with patch("app.core.freight.get_gmail_sender", return_value=sender), patch("app.core.freight.get_provider", return_value=provider):
            with self.assertRaisesRegex(ValueError, "uncertain"):
                send_draft(draft["id"], self.storage)
            with self.assertRaisesRegex(ValueError, "no longer available"):
                send_draft(draft["id"], self.storage)
        self.assertEqual(provider.send.call_count, 1)
        self.assertEqual(self.storage.get("freight_drafts", draft["id"])["status"], "send_uncertain")

    def test_interrupted_send_is_quarantined(self):
        result = self.inbound("Is this a true team?")
        draft = result["draft"]
        self.storage.update("freight_drafts", draft["id"], {"status": "sending", "updated_at": "2020-01-01T00:00:00+00:00"})
        self.assertEqual(recover_uncertain_freight_sends(self.storage), 1)
        self.assertEqual(self.storage.get("freight_drafts", draft["id"])["status"], "send_uncertain")
        self.assertEqual(recover_uncertain_freight_sends(self.storage), 0)

    def test_crash_interrupted_inbound_is_recovered_by_reconcile(self):
        # A crash between insert and evaluation must not strand the broker's reply:
        # the message stays pending/failed and the next poll's reconcile replays it.
        message = self.storage.insert("freight_messages", {
            "id": new_id(), "thread_id": self.thread_id, "direction": "in",
            "provider_message_id": "<crash-1@example.com>", "from_email": "broker@example.com",
            "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": "We can do $4,200 all-in. Pickup tomorrow morning.",
            "classification": {}, "status": "received", "processing_state": "pending",
            "created_at": now_iso(),
        })
        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-evaluation")
        with patch.object(freight_module, "evaluate_inbound", side_effect=boom):
            self.assertEqual(freight_module.reconcile_unprocessed_inbound(self.storage), 0)
        failed = self.storage.get("freight_messages", message["id"])
        self.assertEqual(failed["processing_state"], "failed")
        self.assertIn("simulated crash", failed["processing_error"])
        # The next poll recovers it through the same processing path.
        self.assertEqual(freight_module.reconcile_unprocessed_inbound(self.storage), 1)
        done = self.storage.get("freight_messages", message["id"])
        self.assertEqual(done["processing_state"], "processed")
        self.assertIsNone(done["processing_error"])
        # Reprocessing the same message never stacks a second reply draft.
        thread = self.storage.get("freight_threads", self.thread_id)
        freight_module._process_inbound(thread, done, None, self.storage)
        first_pass = self.storage.list("freight_drafts", {"thread_id": self.thread_id}, order="", limit=50)
        freight_module._process_inbound(thread, done, None, self.storage)
        second_pass = self.storage.list("freight_drafts", {"thread_id": self.thread_id}, order="", limit=50)
        self.assertEqual(len(first_pass), len(second_pass))
        # A steady-state reconcile is a no-op.
        self.assertEqual(freight_module.reconcile_unprocessed_inbound(self.storage), 0)

    def test_reconcile_reaches_pending_beyond_long_processed_history(self):
        # A long processed history must never push unprocessed rows out of the window.
        rows = [{
            "id": new_id(), "thread_id": self.thread_id, "direction": "in",
            "provider_message_id": f"<old-{index}@example.com>", "from_email": "broker@example.com",
            "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": "ok", "classification": {}, "status": "received",
            "processing_state": "processed", "created_at": f"2026-09-01T00:{index % 60:02d}:{index % 60:02d}+00:00",
        } for index in range(1050)]
        self.storage.insert_many("freight_messages", rows)
        pending = self.storage.insert("freight_messages", {
            "id": new_id(), "thread_id": self.thread_id, "direction": "in",
            "provider_message_id": "<late-pending@example.com>", "from_email": "broker@example.com",
            "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": "Still interested in the load?", "classification": {}, "status": "received",
            "processing_state": "pending", "created_at": now_iso(),
        })
        self.assertEqual(freight_module.reconcile_unprocessed_inbound(self.storage), 1)
        self.assertEqual(self.storage.get("freight_messages", pending["id"])["processing_state"], "processed")

    def test_reconcile_alerts_when_thread_closed_before_processing(self):
        message = self.storage.insert("freight_messages", {
            "id": new_id(), "thread_id": self.thread_id, "direction": "in",
            "provider_message_id": "<closed-1@example.com>", "from_email": "broker@example.com",
            "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": "One more question.", "classification": {}, "status": "received",
            "processing_state": "pending", "created_at": now_iso(),
        })
        self.storage.update("freight_threads", self.thread_id, {"state": "closed"})
        self.assertEqual(freight_module.reconcile_unprocessed_inbound(self.storage), 0)
        done = self.storage.get("freight_messages", message["id"])
        self.assertEqual(done["processing_state"], "processed")
        alerts = self.storage.list("freight_alerts", {"kind": "inbound_stranded_closed"}, order="", limit=5)
        self.assertEqual(len(alerts), 1)

    def test_reconcile_escalates_repeated_failures(self):
        message = self.storage.insert("freight_messages", {
            "id": new_id(), "thread_id": self.thread_id, "direction": "in",
            "provider_message_id": "<boom@example.com>", "from_email": "broker@example.com",
            "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": "We can do $4,200 all-in.", "classification": {}, "status": "received",
            "processing_state": "pending", "created_at": now_iso(),
        })
        def boom(*args, **kwargs):
            raise RuntimeError("still broken")
        with patch.object(freight_module, "evaluate_inbound", side_effect=boom):
            self.assertEqual(freight_module.reconcile_unprocessed_inbound(self.storage), 0)
            self.assertEqual(self.storage.get("freight_messages", message["id"])["processing_state"], "failed")
            self.assertEqual(freight_module.reconcile_unprocessed_inbound(self.storage), 0)
        alerts = self.storage.list("freight_alerts", {"kind": "inbound_reprocess_failed"}, order="", limit=5)
        self.assertEqual(len(alerts), 1)
        self.assertIn("still broken", alerts[0]["summary"])
        # A permanently failing message leaves the retry window entirely.
        self.assertEqual(self.storage.get("freight_messages", message["id"])["processing_state"], "dead")

    def test_reconcile_drains_more_than_200_stuck_failures(self):
        # More stuck failures than one page must never starve the rows behind
        # them: each row leaves the pending/failed window within two passes.
        rows = [{
            "id": new_id(), "thread_id": self.thread_id, "direction": "in",
            "provider_message_id": f"<stuck-{index}@example.com>", "from_email": "broker@example.com",
            "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": "ok", "classification": {}, "status": "received",
            "processing_state": "pending", "created_at": f"2026-09-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
        } for index in range(250)]
        self.storage.insert_many("freight_messages", rows)
        def boom(*args, **kwargs):
            raise RuntimeError("permanent failure")
        with patch.object(freight_module, "evaluate_inbound", side_effect=boom):
            freight_module.reconcile_unprocessed_inbound(self.storage)
            freight_module.reconcile_unprocessed_inbound(self.storage)
        stuck = (self.storage.list("freight_messages", {"direction": "in", "processing_state": "pending"}, order="", limit=500)
                 + self.storage.list("freight_messages", {"direction": "in", "processing_state": "failed"}, order="", limit=500))
        self.assertEqual(stuck, [])
        dead = self.storage.list("freight_messages", {"direction": "in", "processing_state": "dead"}, order="", limit=500)
        self.assertEqual(len(dead), 250)
        # Alerts dedupe per thread; every one of the 250 rows still drained.
        alerts = self.storage.list("freight_alerts", {"kind": "inbound_reprocess_failed"}, order="", limit=500)
        self.assertGreaterEqual(len(alerts), 1)

    def test_review_and_state_endpoints(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.core import freight as freight_core
        with patch.object(main, "store", self.raw), patch.object(freight_core, "store", self.raw):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)
            # Test partial facts update (e.g. user only enters miles and weight without destination or schedule checkbox)
            partial_res = client.post(f"/freight/loads/{self.load_id}/facts", data={
                "origin_city": "Phoenix", "origin_state": "AZ",
                "loaded_miles": "850", "weight_lbs": "35000",
            }, follow_redirects=False)
            self.assertEqual(partial_res.status_code, 303)
            load_mid = self.storage.get("freight_loads", self.load_id)
            self.assertEqual(load_mid["loaded_miles"], 850)
            self.assertEqual(load_mid["weight_lbs"], 35000)
            self.assertTrue(load_mid["loaded_miles_verified"])
            self.assertFalse(load_mid["origin_verified"])

            response = client.post(f"/freight/loads/{self.load_id}/facts", data={
                "origin_city": "Phoenix", "origin_state": "AZ", "equipment_type": "dry van",
                "destination_city": "Dallas", "destination_state": "TX", "pickup_date": "2026-09-23",
                "loaded_miles": "1068", "deadhead_miles": "75", "weight_lbs": "40000",
                "origin_confirmed": "yes", "equipment_confirmed": "yes", "pickup_date_confirmed": "yes",
                "schedule_confirmed": "yes",
            }, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(self.storage.get("freight_loads", self.load_id)["destination_verified"])
            # Without a recorded agreement, booking is refused - the old manual shortcut bypassed every gate.
            refused = client.post(f"/freight/threads/{self.thread_id}/state", data={"state": "booked"}, follow_redirects=False)
            # Refusals land back on the load page with the reason, never a dead 400.
            self.assertEqual(refused.status_code, 303)
            self.assertIn("error=", refused.headers["location"])
            self.assertIn(f"load_id={self.load_id}", refused.headers["location"])
            booking = freight_module.record_agreement(self.thread_id, 4200.0, "msg-test", self.storage)
            freight_module.submit_rate_con(booking["id"], {"total_rate": "4200", "pickup_city": "Phoenix", "pickup_state": "AZ", "delivery_city": "Dallas", "delivery_state": "TX", "pickup_date": "2026-09-23"}, self.storage)
            freight_module.review_rate_con(booking["id"], True, self.storage)
            freight_module.approve_driver_handoff(booking["id"], self.storage)
            broker = freight_module.resolve_broker("rep@freightbroker.example", "FreightBroker", self.storage)
            self.storage.update("freight_loads", self.load_id, {"broker_id": broker["id"]})
            freight_module.update_broker(broker["id"], {"credit_status": "approved", "setup_status": "complete"}, self.storage)
            response = client.post(f"/freight/threads/{self.thread_id}/state", data={"state": "booked"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "booked")
            page = client.get(f"/freight?load_id={self.load_id}")
            self.assertEqual(page.status_code, 200)
            # A booked thread no longer offers "Reopen negotiation": the booked snapshot is immutable.
            self.assertNotIn("Reopen negotiation", page.text)
            self.assertIn("Booked", page.text)
            self.assertIn("Selected mission", page.text)
            self.assertIn("Desired delivery", page.text)
            self.assertIn("Truck weight limit", page.text)

    def test_freight_missions_and_settings_navigation(self):
        from fastapi.testclient import TestClient
        from app import main
        with patch.object(main, "store", self.raw):
            client = TestClient(main.app)
            client.post("/admin/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)

            # Test missions page renders onboarding and tabs
            res_missions = client.get("/freight/missions")
            self.assertEqual(res_missions.status_code, 200)
            # The setup journey only shows while setup is incomplete.
            self.assertIn("Missions &amp; fleet", res_missions.text)
            self.assertIn("Freight Missions", res_missions.text)
            self.assertIn("Truck Fleet", res_missions.text)

            # Test settings page renders merged identity & templates
            res_settings = client.get("/freight/settings")
            self.assertEqual(res_settings.status_code, 200)
            self.assertIn("Sender Identity & Signature", res_settings.text)
            self.assertIn("Email Templates & Subject Lines", res_settings.text)

            # Test template redirect
            res_tpl_redirect = client.get("/templates?vertical=freight", follow_redirects=False)
            self.assertEqual(res_tpl_redirect.status_code, 303)
            self.assertIn("/freight/settings?tab=templates", res_tpl_redirect.headers["location"])

    def test_mission_save_accepts_city_and_separate_destination_state(self):
        from fastapi.testclient import TestClient
        from app import main
        with patch.object(main, "store", self.raw), patch.object(main, "search_locations") as geocoder:
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)
            response = client.post("/freight/missions/save", data={
                "name": "Phoenix to Dallas",
                "truck_profile_id": self.profile_id,
                "origin_city": "Phoenix",
                "origin_state": "AZ",
                "equipment_type": "Dry van",
                "trailer_length_ft": "53",
                "max_weight_lbs": "45000",
                "destination_label": "Dallas, TX",
                "destination_kind": "city",
                "destination_radius": "0",
                "floor_total": "2000",
                "target_total": "4500",
                "maximum_counter_rounds": "3",
                "execution_mode": "auto",
                "active": "on",
            }, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn("Mission+saved", response.headers["location"])
        saved = self.raw.list("freight_missions", order="created_at desc", limit=1)[0]
        self.assertEqual(saved["destinations"], [{"label": "Dallas, TX", "kind": "city", "radius_miles": 0}])
        geocoder.assert_not_called()

    def test_freight_sender_add_workflow(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.core.gmail_check import CheckResult
        with patch.object(main, "store", self.raw), patch.object(main, "check_gmail_login", return_value=CheckResult(True, "Connected.")):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)

            # Test invalid email validation
            res_bad_email = client.post("/freight/senders/add", data={
                "email": "invalid-email",
                "app_password": "abcd efgh ijkl mnop",
            }, follow_redirects=False)
            self.assertEqual(res_bad_email.status_code, 303)
            self.assertIn("Valid+Gmail+address+is+required", res_bad_email.headers["location"])

            # Test missing password validation
            res_no_pass = client.post("/freight/senders/add", data={
                "email": "dispatch@myfleet.com",
                "app_password": "   ",
            }, follow_redirects=False)
            self.assertEqual(res_no_pass.status_code, 303)
            self.assertIn("Google+App+Password+is+required", res_no_pass.headers["location"])

            # Test successful addition and auto-selection for Freight
            res_add = client.post("/freight/senders/add", data={
                "email": "dispatch@myfleet.com",
                "app_password": "abcd efgh ijkl mnop",
                "display_name": "MyFleet Freight Dispatch",
                "reply_to": "inquiries@myfleet.com",
                "auto_select": "on",
            }, follow_redirects=False)
            self.assertEqual(res_add.status_code, 303)
            self.assertIn("Connection%20tested%20and%20saved", res_add.headers["location"])

            # Verify sender saved in store
            senders = [s for s in self.storage.list("gmail_senders") if s.get("email") == "dispatch@myfleet.com"]
            self.assertEqual(len(senders), 1)
            sender = senders[0]
            self.assertEqual(sender["display_name"], "MyFleet Freight Dispatch")
            self.assertEqual(sender["reply_to"], "inquiries@myfleet.com")
            self.assertTrue(sender["active"])

            # Verify freight settings auto-selected this sender
            fsettings = self.storage.get("freight_settings", 1)
            self.assertEqual(str(fsettings["default_sender_account"]), str(sender["id"]))
            self.assertEqual(fsettings["sender_name"], "MyFleet Freight Dispatch")
            self.assertEqual(fsettings["reply_to"], "inquiries@myfleet.com")

            # Verify freight settings page displays sender in select dropdown
            res_page = client.get("/freight/settings")
            self.assertEqual(res_page.status_code, 200)
            self.assertIn("dispatch@myfleet.com", res_page.text)
            self.assertIn("+ Add new email", res_page.text)
            self.assertIn("add-freight-email-dialog", res_page.text)



class FreightIsolationTests(unittest.TestCase):
    """User B must never see or act on user A's freight data."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "iso.db"))
        self.raw.init()
        self.a = OwnerStore(self.raw, "user-a")
        self.b = OwnerStore(self.raw, "user-b")
        stamp = now_iso()
        self.load_id, self.thread_id, self.draft_id, self.alert_id = (new_id() for _ in range(4))
        self.a.insert("freight_loads", {"id": self.load_id, "broker_email": "broker@example.com", "origin_city": "Phoenix", "destination_city": "Dallas", "subject": "Load", "status": "waiting", "created_at": stamp, "updated_at": stamp})
        self.a.insert("freight_threads", {"id": self.thread_id, "load_id": self.load_id, "sender_account": "s-a", "recipient_email": "broker@example.com", "subject": "Load", "state": "waiting", "last_activity_at": stamp, "created_at": stamp, "updated_at": stamp})
        self.a.insert("freight_drafts", {"id": self.draft_id, "thread_id": self.thread_id, "subject": "Re: Load", "body_text": "Can do 2500", "reason": "manual", "status": "pending", "created_at": stamp, "updated_at": stamp})
        self.a.insert("freight_alerts", {"id": self.alert_id, "thread_id": self.thread_id, "kind": "verify_load_facts", "summary": "x", "status": "open", "created_at": stamp})
        self.a.insert("gmail_senders", {"id": "s-a", "email": "same@gmail.com", "app_password_encrypted": "", "active": True, "provider": "gmail", "created_at": stamp, "updated_at": stamp})

    def test_reads_are_scoped(self):
        for table in ("freight_loads", "freight_threads", "freight_drafts", "freight_alerts", "gmail_senders"):
            with self.subTest(table=table):
                self.assertEqual(len(self.a.list(table)), 1)
                self.assertEqual(self.b.list(table), [])
        self.assertIsNone(self.b.get("freight_loads", self.load_id))
        self.assertIsNone(self.b.get("freight_drafts", self.draft_id))

    def test_writes_to_another_owner_are_refused(self):
        with self.assertRaises(LookupError):
            self.b.update("freight_loads", self.load_id, {"status": "booked"})
        self.assertEqual(self.b.delete("freight_alerts", {"id": self.alert_id}), 0)
        self.assertFalse(self.b.claim_status("freight_drafts", self.draft_id, "pending", "sending"))
        self.assertEqual(self.raw.get("freight_loads", self.load_id)["status"], "waiting")
        with self.assertRaises(ValueError):
            send_draft(self.draft_id, storage=self.b)
        with self.assertRaises(ValueError):
            set_thread_state(self.thread_id, "booked", storage=self.b)

    def test_same_gmail_address_is_separate_per_owner(self):
        from app.core.gmail_senders import list_gmail_senders
        self.b.insert("gmail_senders", {"id": "s-b", "email": "same@gmail.com", "app_password_encrypted": "", "active": True, "provider": "gmail", "created_at": now_iso(), "updated_at": now_iso()})
        self.assertEqual([s["id"] for s in list_gmail_senders(storage=self.a, cfg={})], ["s-a"])
        self.assertEqual([s["id"] for s in list_gmail_senders(storage=self.b, cfg={})], ["s-b"])
        # Admin outreach code (unscoped) never sees customer inboxes.
        self.assertNotIn("s-b", [s["id"] for s in list_gmail_senders(storage=self.raw, cfg={})])

    def test_each_owner_gets_own_settings_and_template(self):
        from app.core.freight import seed_freight_settings, seed_freight_template
        for scoped in (self.a, self.b):
            seed_freight_template(scoped); seed_freight_settings(scoped)
        a_cfg, b_cfg = self.a.get("freight_settings", 1), self.b.get("freight_settings", 1)
        self.assertNotEqual(a_cfg["id"], b_cfg["id"])
        self.assertNotEqual(a_cfg["default_template_id"], b_cfg["default_template_id"])
        self.b.update("freight_settings", 1, {"sender_name": "B Trucking"})
        self.assertNotEqual(self.a.get("freight_settings", 1)["sender_name"], "B Trucking")

    def test_http_routes_hide_other_owners_rows(self):
        from fastapi.testclient import TestClient
        from app import main
        with patch.object(main, "store", self.raw):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)
            page = client.get(f"/freight?load_id={self.load_id}")
            self.assertEqual(page.status_code, 200)
            self.assertNotIn("broker@example.com", page.text)
            self.assertEqual(client.post(f"/freight/drafts/{self.draft_id}/send", data={"body_text": "hi"}, follow_redirects=False).status_code, 404)
            self.assertEqual(client.post(f"/freight/alerts/{self.alert_id}/resolve", follow_redirects=False).status_code, 404)
            self.assertEqual(self.raw.get("freight_alerts", self.alert_id)["status"], "open")


if __name__ == "__main__":
    unittest.main()


class FreightKeepAliveTests(unittest.TestCase):
    """A broker turn must never end silently because part of the answer repeats."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = SQLiteStore(os.path.join(self.temp.name, "freight.db"))
        self.raw.init()
        self.storage = OwnerStore(self.raw, ADMIN_OWNER_ID)
        stamp = now_iso()
        self.profile_id, self.mission_id, self.load_id, self.thread_id = (new_id() for _ in range(4))
        self.storage.insert("freight_truck_profiles", {
            "id": self.profile_id, "name": "Truck 1", "current_city": "Phoenix", "current_state": "AZ",
            "equipment_type": "dry van", "trailer_length_ft": 53, "max_weight_lbs": 45000,
            "team_status": "team", "mc_number": "123456", "shareable_fields": ["equipment_type", "team_status", "mc_number"],
            "created_at": stamp, "updated_at": stamp,
        })
        self.storage.insert("freight_missions", {
            "id": self.mission_id, "name": "Phoenix to Dallas", "truck_profile_id": self.profile_id,
            "origin_city": "Phoenix", "origin_state": "AZ", "pickup_start": "2026-09-23", "pickup_end": "2026-09-23",
            "equipment_type": "dry van", "destinations": [{"label": "Dallas, TX", "kind": "city", "radius_miles": 0}],
            "floor_total": 3900, "target_total": 4500, "maximum_counter_rounds": 2,
            "permissions": {"auto_counter": False, "auto_profile_reply": False, "auto_pass": False},
            "created_at": stamp, "updated_at": stamp,
        })
        self.storage.insert("freight_loads", {
            "id": self.load_id, "mission_id": self.mission_id, "truck_profile_id": self.profile_id,
            "broker_email": "broker@example.com", "origin_city": "Phoenix", "origin_state": "AZ",
            "destination_city": "Open destinations", "subject": "Truck available", "status": "waiting",
            "created_at": stamp, "updated_at": stamp,
        })
        self.storage.insert("freight_threads", {
            "id": self.thread_id, "load_id": self.load_id, "sender_account": "1", "recipient_email": "broker@example.com",
            "subject": "Truck available", "root_message_id": "<first@example.com>", "last_message_id": "<first@example.com>",
            "state": "waiting", "last_activity_at": stamp, "created_at": stamp, "updated_at": stamp,
        })
        self.received = 0
        self.sent = 0

    def inbound(self, body):
        self.received += 1
        message_id = f"<broker-{self.received}@example.com>"
        msg = self.storage.insert("freight_messages", {
            "id": new_id(), "thread_id": self.thread_id, "direction": "in", "provider_message_id": message_id,
            "from_email": "broker@example.com", "to_email": "carrier@example.com", "subject": "Re: Truck available",
            "body_text": body, "classification": {}, "created_at": now_iso(),
        })
        self.storage.update("freight_threads", self.thread_id, {"last_message_id": message_id})
        return evaluate_inbound(self.storage.get("freight_threads", self.thread_id), msg, self.storage)

    def send(self, draft):
        self.sent += 1
        provider = Mock()
        provider.send.return_value = SendResult(True, f"<sent-{self.sent}@example.com>")
        sender = {"id": "1", "email": "carrier@example.com", "provider": "gmail", "display_name": "Dispatch"}
        with patch("app.core.freight.get_gmail_sender", return_value=sender), patch("app.core.freight.get_provider", return_value=provider):
            return send_draft(draft["id"], self.storage)

    def test_new_question_after_answered_one_still_goes_out(self):
        first = self.inbound("Is this a true team?")
        self.assertEqual(first["action"], "draft")
        self.assertEqual(first["draft"]["reason"], "profile_fact_reply")
        self.send(first["draft"])
        # Broker's next message repeats part of the last exchange but asks something new.
        second = self.inbound("Thanks. And is it a 53 ft dry van?")
        self.assertEqual(second["action"], "draft")
        self.assertEqual(second["draft"]["reason"], "profile_fact_reply")
        self.assertIn("dry van", second["draft"]["body_text"])
        self.assertNotIn("true team", second["draft"]["body_text"])

    def test_unrecognized_broker_message_produces_keepalive_draft_not_silence(self):
        result = self.inbound("Thirty pallets of manufactured components.")
        self.assertEqual(result["action"], "draft")
        self.assertEqual(result["draft"]["reason"], "clarify_load_details")
        self.assertTrue(result["draft"]["policy_snapshot"]["manual_only"])
        self.assertIn("delivery city and state", result["draft"]["body_text"])
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "draft_ready")

    def test_fully_covered_load_gets_followup_nudge_draft_and_alert(self):
        verify_load_facts(self.load_id, {
            "origin_city": "Phoenix", "origin_state": "AZ", "origin_confirmed": "yes",
            "destination_city": "Dallas", "destination_state": "TX",
            "equipment_type": "dry van", "equipment_confirmed": "yes",
            "pickup_date": "2026-09-23", "pickup_date_confirmed": "yes",
            "loaded_miles": 1000, "deadhead_miles": 100,
            "weight_lbs": 40000, "schedule_confirmed": "yes",
        }, self.storage)
        result = self.inbound("Ok, noted.")
        self.assertEqual(result["action"], "draft")
        self.assertEqual(result["draft"]["reason"], "follow_up_nudge")
        self.assertTrue(self.storage.list("freight_alerts", {"thread_id": self.thread_id, "kind": "ambiguous_reply"}, order="", limit=1))

    def test_driver_information_request_never_gets_keepalive(self):
        result = self.inbound("Send me full driver information.")
        self.assertEqual(result["action"], "alert")
        self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "protected_review")
        self.assertEqual(self.storage.list("freight_drafts", {"thread_id": self.thread_id, "status": "pending"}, order="", limit=10), [])

    def test_counter_restate_after_two_sends_goes_manual(self):
        verify_load_facts(self.load_id, {
            "destination_city": "Dallas", "destination_state": "TX",
            "loaded_miles": 1000, "deadhead_miles": 100,
        }, self.storage)
        first = self.inbound("Rate is $4,000 all in.")
        self.assertEqual(first["action"], "draft")
        self.assertEqual(first["draft"]["reason"], "counter_to_target")
        self.send(first["draft"])
        second = self.inbound("Could you do $4,100?")
        self.assertEqual(second["action"], "draft")
        self.assertEqual(second["draft"]["reason"], "counter_to_target")
        self.assertTrue(second["draft"]["policy_snapshot"]["safe_to_auto_send"])
        self.send(second["draft"])
        third = self.inbound("Best I can do is $4,150.")
        self.assertEqual(third["action"], "draft")
        self.assertFalse(third["draft"]["policy_snapshot"]["safe_to_auto_send"])
        self.assertTrue(self.storage.list("freight_alerts", {"thread_id": self.thread_id, "kind": "counter_restate_limit"}, order="", limit=1))
        # Restates never consume counter rounds: one initial counter, two restates.
        self.assertEqual(self.storage.get("freight_loads", self.load_id)["current_round"], 1)


class FreightStopsTests(unittest.TestCase):
    """Phase 2: ordered stops with per-stop verification."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = OwnerStore(SQLiteStore(self.tmp.name + '/stops.db'), ADMIN_OWNER_ID)
        self.storage.init()
        self.profile_id, self.mission_id = new_id(), new_id()
        stamp = now_iso()
        self.storage.insert('freight_truck_profiles', {'id': self.profile_id, 'name': 'T', 'current_city': 'Phoenix', 'current_state': 'AZ', 'equipment_type': 'dry van', 'max_weight_lbs': 45000, 'team_status': 'team', 'shareable_fields': ['team_status'], 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_missions', {'id': self.mission_id, 'name': 'M', 'truck_profile_id': self.profile_id, 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'equipment_type': 'dry van', 'destinations': [{'kind': 'city', 'label': 'Dallas, TX', 'radius_miles': 0}], 'floor_total': 3900, 'target_total': 4500, 'maximum_counter_rounds': 2, 'permissions': {}, 'active': True, 'created_at': stamp, 'updated_at': stamp})

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self):
        stamp = now_iso()
        broker = freight_module.resolve_broker('b@x.com', 'X Co', self.storage)
        freight_module.update_broker(broker['id'], {'credit_status': 'approved', 'setup_status': 'complete'}, self.storage)
        return self.storage.insert('freight_loads', {'id': new_id(), 'mission_id': self.mission_id, 'truck_profile_id': self.profile_id, 'broker_id': broker['id'], 'broker_email': 'b@x.com', 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'origin_verified': 1, 'destination_city': 'Dallas', 'destination_state': 'TX', 'destination_verified': 1, 'equipment_verified': 1, 'schedule_verified': 1, 'pickup_date': '2026-09-28', 'pickup_date_verified': 1, 'loaded_miles': 1000, 'loaded_miles_verified': 1, 'deadhead_miles': 50, 'deadhead_miles_verified': 1, 'weight_lbs': 40000, 'subject': 's', 'status': 'waiting', 'created_at': stamp, 'updated_at': stamp})

    def test_sync_inserts_stops_in_order(self):
        load = self._load()
        classification = {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': 'ABC Warehouse', 'appointment': 'Mon 8am-10am', 'evidence': 'pick up at ABC Warehouse in Phoenix, AZ Mon 8am-10am'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver to Dallas, TX Tue 1pm'},
        ]}
        freight_module._sync_load_stops(load, classification, 'msg-1', self.storage)
        stops = self.storage.list('freight_load_stops', {'load_id': load['id']}, order='seq asc', limit=10)
        self.assertEqual([s['kind'] for s in stops], ['pickup', 'delivery'])
        self.assertEqual([s['seq'] for s in stops], [1, 2])
        self.assertEqual(stops[0]['facility_name'], 'ABC Warehouse')
        self.assertFalse(stops[0]['verified'])

    def test_sync_unverifies_stop_when_appointment_changes(self):
        load = self._load()
        classification = {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 8am', 'evidence': 'pick up Phoenix, AZ Mon 8am'},
        ]}
        freight_module._sync_load_stops(load, classification, 'msg-1', self.storage)
        stop = self.storage.list('freight_load_stops', {'load_id': load['id']}, order='', limit=1)[0]
        freight_module.verify_load_stop(stop['id'], {}, self.storage)
        changed = {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Wed 6am', 'evidence': 'actually pick up Phoenix, AZ Wed 6am'},
        ]}
        freight_module._sync_load_stops(load, changed, 'msg-2', self.storage)
        stop = self.storage.get('freight_load_stops', stop['id'])
        self.assertEqual(stop['appointment'], 'Wed 6am')
        self.assertFalse(stop['verified'])

    def test_readiness_blocks_on_unverified_stops(self):
        load = self._load()
        mission = self.storage.get('freight_missions', self.mission_id)
        profile = self.storage.get('freight_truck_profiles', self.profile_id)
        self.assertEqual(freight_module._booking_readiness_blockers(load, mission, profile, self.storage), [])
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 8am', 'evidence': 'pick up Phoenix, AZ Mon 8am'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas, TX Tue 1pm'},
        ]}, 'msg-1', self.storage)
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertEqual(len(blockers), 2)
        self.assertIn('stop 1 pickup (Phoenix, AZ) confirmed', blockers)
        stop = self.storage.list('freight_load_stops', {'load_id': load['id'], 'kind': 'pickup'}, order='', limit=1)[0]
        freight_module.verify_load_stop(stop['id'], {}, self.storage)
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertEqual(blockers, ['stop 2 delivery (Dallas, TX) confirmed'])

    def test_repeat_stops_same_city_each_get_a_row(self):
        # Two pickups in the same city are real; kind+city dedupe must not collapse them.
        load = self._load()
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': 'A Warehouse', 'appointment': 'Mon 8am', 'evidence': 'pick up A Warehouse Phoenix'},
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': 'B Depot', 'appointment': 'Mon 1pm', 'evidence': 'then pick up B Depot Phoenix'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas'},
        ]}, 'msg-1', self.storage)
        stops = self.storage.list('freight_load_stops', {'load_id': load['id']}, order='seq asc', limit=10)
        self.assertEqual(len(stops), 3)
        self.assertEqual([s['facility_name'] for s in stops[:2]], ['A Warehouse', 'B Depot'])
        # A re-sync of the same route matches by occurrence and keeps both rows stable.
        first_ids = [s['id'] for s in stops]
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': 'A Warehouse', 'appointment': 'Mon 8am', 'evidence': 'pick up A Warehouse Phoenix'},
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': 'B Depot', 'appointment': 'Mon 1pm', 'evidence': 'then pick up B Depot Phoenix'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas'},
        ]}, 'msg-2', self.storage)
        stops = self.storage.list('freight_load_stops', {'load_id': load['id']}, order='seq asc', limit=10)
        self.assertEqual([s['id'] for s in stops], first_ids)

    def test_dropped_stop_is_marked_removed_with_review_alert(self):
        load = self._load()
        self.storage.insert('freight_threads', {'id': new_id(), 'load_id': load['id'], 'sender_account': '1', 'recipient_email': 'b@x.com', 'subject': 's', 'state': 'waiting', 'last_activity_at': now_iso(), 'created_at': now_iso(), 'updated_at': now_iso()})
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 8am', 'evidence': 'pick up Phoenix'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas'},
        ]}, 'msg-1', self.storage)
        pickup = self.storage.list('freight_load_stops', {'load_id': load['id'], 'kind': 'pickup'}, order='', limit=1)[0]
        freight_module.verify_load_stop(pickup['id'], {'appointment': 'Mon 8am'}, self.storage)
        # The broker's next message restates the complete route without the pickup stop.
        freight_module._sync_load_stops(load, {'source': 'gemini', 'route_scope': 'complete', 'stops': [
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas'},
        ]}, 'msg-2', self.storage)
        gone = self.storage.get('freight_load_stops', pickup['id'])
        self.assertIsNotNone(gone['removed_at'])
        alerts = self.storage.list('freight_alerts', {'kind': 'stop_removed'}, order='', limit=5)
        self.assertEqual(len(alerts), 1)
        # Removed stops no longer block booking and cannot be verified.
        mission = self.storage.get('freight_missions', self.mission_id)
        profile = self.storage.get('freight_truck_profiles', self.profile_id)
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertNotIn('stop 1 pickup (Phoenix, AZ) appointment', blockers)
        with self.assertRaises(ValueError):
            freight_module.verify_load_stop(pickup['id'], {'appointment': 'Mon 9am'}, self.storage)

    def test_reordered_route_updates_seq_without_clearing_verification(self):
        load = self._load()
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 8am', 'evidence': 'pick up Phoenix'},
            {'kind': 'pickup', 'city': 'Tempe', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 11am', 'evidence': 'then Tempe'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas'},
        ]}, 'msg-1', self.storage)
        for stop in self.storage.list('freight_load_stops', {'load_id': load['id']}, order='seq asc', limit=10):
            freight_module.verify_load_stop(stop['id'], {}, self.storage)
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Tempe', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 11am', 'evidence': 'Tempe first now'},
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 8am', 'evidence': 'then Phoenix'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas'},
        ]}, 'msg-2', self.storage)
        stops = self.storage.list('freight_load_stops', {'load_id': load['id']}, order='seq asc', limit=10)
        self.assertEqual([s['city'] for s in stops], ['Tempe', 'Phoenix', 'Dallas'])
        self.assertTrue(all(s['verified'] for s in stops))

    def test_fcfs_stop_confirms_without_appointment(self):
        load = self._load()
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': '', 'evidence': 'deliver Dallas FCFS'},
        ]}, 'msg-1', self.storage)
        stop = self.storage.list('freight_load_stops', {'load_id': load['id']}, order='', limit=1)[0]
        confirmed = freight_module.verify_load_stop(stop['id'], {'fcfs': 'yes'}, self.storage)
        self.assertTrue(confirmed['verified'])
        self.assertEqual(confirmed['appointment'], 'FCFS')
        mission = self.storage.get('freight_missions', self.mission_id)
        profile = self.storage.get('freight_truck_profiles', self.profile_id)
        self.assertEqual(freight_module._booking_readiness_blockers(load, mission, profile, self.storage), [])

    def test_partial_stop_update_keeps_other_stops(self):
        # A broker update mentioning one stop must not wipe the rest of the route.
        load = self._load()
        freight_module._sync_load_stops(load, {'source': 'gemini', 'route_scope': 'complete', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Mon 8am', 'evidence': 'pick up Phoenix, AZ Mon 8am'},
            {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility': '', 'appointment': 'Tue 1pm', 'evidence': 'deliver Dallas, TX Tue 1pm'},
        ]}, 'msg-1', self.storage)
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': 'Wed 6am', 'evidence': 'pickup moved to Wed 6am'},
        ]}, 'msg-2', self.storage)
        active = [row for row in self.storage.list('freight_load_stops', {'load_id': load['id']}, order='seq asc', limit=10) if not row['removed_at']]
        self.assertEqual(len(active), 2)
        self.assertEqual(active[0]['appointment'], 'Wed 6am')
        self.assertEqual(active[1]['city'], 'Dallas')
        self.assertEqual(self.storage.list('freight_alerts', {'kind': 'stop_removed'}, order='', limit=5), [])

    def test_repeated_city_stops_survive_model_boundary(self):
        # Two pickups in one city with different facilities are distinct stops;
        # only exact duplicates collapse.
        from app.core import freight_agent
        evidence = "Pick up Depot A Phoenix, AZ Mon 8am then Depot B Phoenix, AZ Mon 1pm, deliver Dallas, TX Tue 9am"
        raw = {"intent": "details", "route_scope": "complete", "stops": [
            {"kind": "pickup", "city": "Phoenix", "state": "AZ", "facility": "Depot A", "appointment": "Mon 8am", "evidence": evidence},
            {"kind": "pickup", "city": "Phoenix", "state": "AZ", "facility": "Depot B", "appointment": "Mon 1pm", "evidence": evidence},
            {"kind": "delivery", "city": "Dallas", "state": "TX", "facility": "", "appointment": "Tue 9am", "evidence": evidence},
        ]}
        reading = freight_agent._validated(raw, evidence)
        self.assertEqual(len(reading["stops"]), 3)
        self.assertEqual(reading["route_scope"], "complete")
        # Exact stutter collapses to one row.
        raw["stops"].append(dict(raw["stops"][0]))
        reading = freight_agent._validated(raw, evidence)
        self.assertEqual(len(reading["stops"]), 3)
        # Missing or invalid scope defaults to partial (safe: never removes stops).
        raw.pop("route_scope")
        reading = freight_agent._validated(raw, evidence)
        self.assertEqual(reading["route_scope"], "partial")

    def test_manual_add_stop_appends_confirmed(self):
        load = self._load()
        added = freight_module.add_load_stop(load['id'], {'kind': 'pickup', 'city': 'Tucson', 'state': 'AZ', 'facility_name': ' Depot ', 'appointment': '', 'fcfs': 'yes'}, self.storage)
        self.assertTrue(added['verified'])
        self.assertEqual(added['appointment'], 'FCFS')
        self.assertEqual(added['seq'], 1)
        second = freight_module.add_load_stop(load['id'], {'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'appointment': 'Tue 1pm'}, self.storage)
        self.assertEqual(second['seq'], 2)
        with self.assertRaises(ValueError):
            freight_module.add_load_stop(load['id'], {'kind': 'pickup', 'city': 'Dallas', 'state': 'Texas'}, self.storage)

    def test_verify_load_stop_requires_appointment(self):
        load = self._load()
        freight_module._sync_load_stops(load, {'source': 'gemini', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': '', 'evidence': 'pick up Phoenix, AZ'},
        ]}, 'msg-1', self.storage)
        stop = self.storage.list('freight_load_stops', {'load_id': load['id']}, order='', limit=1)[0]
        with self.assertRaises(ValueError):
            freight_module.verify_load_stop(stop['id'], {'appointment': ''}, self.storage)

    def test_gemini_stop_extraction_requires_evidence(self):
        raw = {'intent': 'details', 'stops': [
            {'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'facility': '', 'appointment': '', 'evidence': 'pick up in Phoenix, AZ Friday'},
            {'kind': 'delivery', 'city': 'Nowhere', 'state': 'ZZ', 'facility': '', 'appointment': '', 'evidence': 'not in the message at all'},
        ]}
        result = freight_agent_module._validated(raw, 'Pick up in Phoenix, AZ Friday. Rate $4,000.')
        self.assertEqual(len(result['stops']), 1)
        self.assertEqual(result['stops'][0]['city'], 'Phoenix')


class FreightDriverSafetyTests(unittest.TestCase):
    """Phase 3: driver/vehicle identity never leaves; availability gates booking."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = OwnerStore(SQLiteStore(self.tmp.name + '/driver.db'), ADMIN_OWNER_ID)
        self.storage.init()
        self.profile_id, self.mission_id = new_id(), new_id()
        stamp = now_iso()
        self.storage.insert('freight_truck_profiles', {'id': self.profile_id, 'name': 'T', 'current_city': 'Phoenix', 'current_state': 'AZ', 'equipment_type': 'dry van', 'max_weight_lbs': 45000, 'team_status': 'team', 'truck_vin': '1HGBH41JXMN109186', 'driver_name': 'Alex Rios', 'driver_cdl_number': 'D1234567', 'driver_cdl_state': 'AZ', 'driver_phone': '6025550143', 'shareable_fields': ['team_status'], 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_missions', {'id': self.mission_id, 'name': 'M', 'truck_profile_id': self.profile_id, 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'equipment_type': 'dry van', 'destinations': [{'kind': 'city', 'label': 'Dallas, TX', 'radius_miles': 0}], 'floor_total': 3900, 'target_total': 4500, 'maximum_counter_rounds': 2, 'permissions': {}, 'active': True, 'created_at': stamp, 'updated_at': stamp})

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, **over):
        stamp = now_iso()
        row = {'id': new_id(), 'mission_id': self.mission_id, 'truck_profile_id': self.profile_id, 'broker_email': 'b@x.com', 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'origin_verified': 1, 'destination_city': 'Dallas', 'destination_state': 'TX', 'destination_verified': 1, 'equipment_verified': 1, 'schedule_verified': 1, 'loaded_miles': 1000, 'loaded_miles_verified': 1, 'deadhead_miles': 50, 'deadhead_miles_verified': 1, 'weight_lbs': 40000, 'subject': 's', 'status': 'waiting', 'created_at': stamp, 'updated_at': stamp}
        row.update(over)
        return self.storage.insert('freight_loads', row)

    def test_shareable_fields_allowlist_rejects_driver_fields(self):
        crafted = ['equipment_type', 'driver_cdl_number', 'truck_vin', 'driver_name', 'mc_number']
        self.assertEqual(freight_module.filter_shareable_fields(crafted), ['equipment_type', 'mc_number'])

    def test_send_draft_blocks_stored_vin_even_with_confirmation(self):
        load = self._load()
        stamp = now_iso()
        thread = self.storage.insert('freight_threads', {'id': new_id(), 'load_id': load['id'], 'sender_account': '1', 'recipient_email': 'b@x.com', 'subject': 's', 'state': 'draft_ready', 'last_activity_at': stamp, 'created_at': stamp, 'updated_at': stamp})
        draft = self.storage.insert('freight_drafts', {'id': new_id(), 'thread_id': thread['id'], 'subject': 's', 'body_text': 'Driver Alex Rios, VIN 1HGBH41JXMN109186, CDL D1234567.', 'reason': 'manual_reply', 'status': 'pending', 'created_at': stamp, 'updated_at': stamp})
        with self.assertRaises(ValueError) as ctx:
            send_draft(draft['id'], self.storage, confirm_sensitive=True)
        self.assertIn('driver', str(ctx.exception).lower())

    def test_availability_off_blocks_booking(self):
        self.storage.update('freight_truck_profiles', self.profile_id, {'availability_status': 'off'})
        load = self._load()
        mission = self.storage.get('freight_missions', self.mission_id)
        profile = self.storage.get('freight_truck_profiles', self.profile_id)
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertIn('truck availability (Truck is marked off duty)', blockers)

    def test_booked_load_window_conflicts(self):
        # A booked 1,000-mile load picked up 9/28 occupies the truck through 9/30
        # (500 miles per transit day) - next-day pickups conflict too.
        self._load(status='booked', pickup_date='2026-09-28')
        profile = self.storage.get('freight_truck_profiles', self.profile_id)
        same_day = self._load(pickup_date='2026-09-28')
        self.assertEqual(freight_module.truck_availability(profile, same_day, self.storage)['status'], 'conflict')
        mid_transit = self._load(pickup_date='2026-09-30')
        availability = freight_module.truck_availability(profile, mid_transit, self.storage)
        self.assertEqual(availability['status'], 'conflict')
        self.assertIn('2026-09-30', availability['detail'])
        after = self._load(pickup_date='2026-10-02')
        self.assertEqual(freight_module.truck_availability(profile, after, self.storage)['status'], 'available')

    def test_delivery_stop_appointment_extends_the_window(self):
        booked = self._load(status='booked', pickup_date='2026-09-28')
        stamp = now_iso()
        self.storage.insert('freight_load_stops', {'id': new_id(), 'load_id': booked['id'], 'seq': 1, 'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'facility_name': '', 'appointment': '2026-10-05 13:00', 'evidence': 'deliver Dallas 10/5', 'source_message_id': 'm1', 'created_at': stamp, 'updated_at': stamp})
        profile = self.storage.get('freight_truck_profiles', self.profile_id)
        during = self._load(pickup_date='2026-10-04')
        self.assertEqual(freight_module.truck_availability(profile, during, self.storage)['status'], 'conflict')
        after = self._load(pickup_date='2026-10-06')
        self.assertEqual(freight_module.truck_availability(profile, after, self.storage)['status'], 'available')

    def test_available_from_future_date_conflicts(self):
        self.storage.update('freight_truck_profiles', self.profile_id, {'available_from': '2026-10-01'})
        profile = self.storage.get('freight_truck_profiles', self.profile_id)
        load = self._load(pickup_date='2026-09-28')
        self.assertEqual(freight_module.truck_availability(profile, load, self.storage)['status'], 'conflict')
        later = self._load(pickup_date='2026-10-02')
        self.assertEqual(freight_module.truck_availability(profile, later, self.storage)['status'], 'available')


class FreightBrokerTests(unittest.TestCase):
    """Phase 4: broker identity, credit and setup gating."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = OwnerStore(SQLiteStore(self.tmp.name + '/broker.db'), ADMIN_OWNER_ID)
        self.storage.init()

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_creates_then_matches_by_email(self):
        broker = freight_module.resolve_broker('Dispatch@ABCLogistics.com', 'ABC Logistics', self.storage)
        self.assertEqual(broker['domain'], 'abclogistics.com')
        self.assertEqual(broker['emails'], ['dispatch@abclogistics.com'])
        self.assertEqual(broker['credit_status'], 'unknown')
        again = freight_module.resolve_broker('dispatch@abclogistics.com', '', self.storage)
        self.assertEqual(again['id'], broker['id'])

    def test_resolve_matches_domain_and_appends_email(self):
        broker = freight_module.resolve_broker('one@xyz.com', 'XYZ', self.storage)
        second = freight_module.resolve_broker('two@xyz.com', '', self.storage)
        self.assertEqual(second['id'], broker['id'])
        self.assertEqual(sorted(second['emails']), ['one@xyz.com', 'two@xyz.com'])

    def test_free_mail_domains_never_match_by_domain(self):
        # Two brokers at gmail.com are not the same company; domain matching
        # would let one inherit the other's credit approval.
        first = freight_module.resolve_broker('one@gmail.com', 'One Co', self.storage)
        second = freight_module.resolve_broker('two@gmail.com', 'Two Co', self.storage)
        self.assertNotEqual(first['id'], second['id'])
        again = freight_module.resolve_broker('one@gmail.com', '', self.storage)
        self.assertEqual(again['id'], first['id'])

    def test_update_broker_validates_statuses(self):
        broker = freight_module.resolve_broker('a@b.com', 'B Co', self.storage)
        updated = freight_module.update_broker(broker['id'], {'credit_status': 'approved', 'credit_score': '92', 'setup_status': 'complete', 'mc_number': 'MC-123'}, self.storage)
        self.assertEqual(updated['credit_status'], 'approved')
        self.assertEqual(updated['credit_score'], 92.0)
        with self.assertRaises(ValueError):
            freight_module.update_broker(broker['id'], {'credit_status': 'great'}, self.storage)

    def test_readiness_blocks_until_credit_and_setup_done(self):
        stamp = now_iso()
        self.storage.insert('freight_truck_profiles', {'id': 'p1', 'name': 'T', 'max_weight_lbs': 45000, 'shareable_fields': [], 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_missions', {'id': 'm1', 'name': 'M', 'truck_profile_id': 'p1', 'permissions': {}, 'active': True, 'created_at': stamp, 'updated_at': stamp})
        broker = freight_module.resolve_broker('a@b.com', 'B Co', self.storage)
        load = self.storage.insert('freight_loads', {'id': new_id(), 'mission_id': 'm1', 'truck_profile_id': 'p1', 'broker_id': broker['id'], 'broker_email': 'a@b.com', 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'origin_verified': 1, 'destination_city': 'Dallas', 'destination_state': 'TX', 'destination_verified': 1, 'equipment_verified': 1, 'schedule_verified': 1, 'pickup_date': '2026-09-28', 'pickup_date_verified': 1, 'loaded_miles': 1000, 'loaded_miles_verified': 1, 'deadhead_miles': 50, 'deadhead_miles_verified': 1, 'weight_lbs': 40000, 'subject': 's', 'status': 'waiting', 'created_at': stamp, 'updated_at': stamp})
        mission = self.storage.get('freight_missions', 'm1')
        profile = self.storage.get('freight_truck_profiles', 'p1')
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertIn('broker credit approval', blockers)
        self.assertIn('broker setup packet', blockers)
        freight_module.update_broker(broker['id'], {'credit_status': 'approved', 'setup_status': 'complete'}, self.storage)
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertEqual(blockers, [])
        freight_module.update_broker(broker['id'], {'blocked': True}, self.storage)
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertIn('blocked broker (B Co)', blockers)

    def test_domain_matched_email_requires_identity_confirmation(self):
        stamp = now_iso()
        self.storage.insert('freight_truck_profiles', {'id': 'p1', 'name': 'T', 'max_weight_lbs': 45000, 'shareable_fields': [], 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_missions', {'id': 'm1', 'name': 'M', 'truck_profile_id': 'p1', 'permissions': {}, 'active': True, 'created_at': stamp, 'updated_at': stamp})
        broker = freight_module.resolve_broker('alice@bigbroker.com', 'Big Broker', self.storage)
        freight_module.update_broker(broker['id'], {'credit_status': 'approved', 'setup_status': 'complete'}, self.storage)
        load = self.storage.insert('freight_loads', {'id': new_id(), 'mission_id': 'm1', 'truck_profile_id': 'p1', 'broker_id': broker['id'], 'broker_email': 'bob@bigbroker.com', 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'origin_verified': 1, 'destination_city': 'Dallas', 'destination_state': 'TX', 'destination_verified': 1, 'equipment_verified': 1, 'schedule_verified': 1, 'pickup_date': '2026-09-28', 'pickup_date_verified': 1, 'loaded_miles': 1000, 'loaded_miles_verified': 1, 'deadhead_miles': 50, 'deadhead_miles_verified': 1, 'weight_lbs': 40000, 'subject': 's', 'status': 'waiting', 'created_at': stamp, 'updated_at': stamp})
        thread = self.storage.insert('freight_threads', {'id': new_id(), 'load_id': load['id'], 'sender_account': '1', 'recipient_email': 'bob@bigbroker.com', 'subject': 's', 'state': 'waiting', 'last_activity_at': stamp, 'created_at': stamp, 'updated_at': stamp})
        # A new email matching by company domain inherits credit/setup only after
        # a dispatcher confirms it is the same company. Confirmation is per
        # email link: the alias alone is blocked, the broker is not.
        matched = freight_module.resolve_broker('bob@bigbroker.com', '', self.storage, thread_id=thread['id'])
        self.assertEqual(matched['id'], broker['id'])
        self.assertIn('bob@bigbroker.com', matched['unconfirmed_emails'])
        alerts = self.storage.list('freight_alerts', {'kind': 'broker_identity_review'}, order='', limit=5)
        self.assertEqual(len(alerts), 1)
        mission = self.storage.get('freight_missions', 'm1')
        profile = self.storage.get('freight_truck_profiles', 'p1')
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertIn('broker identity confirmation', blockers)
        # The broker's existing verified address keeps working while the new
        # alias waits for confirmation.
        load_alice = self.storage.insert('freight_loads', {**load, 'id': new_id(), 'broker_email': 'alice@bigbroker.com'})
        blockers_alice = freight_module._booking_readiness_blockers(load_alice, mission, profile, self.storage)
        self.assertNotIn('broker identity confirmation', blockers_alice)
        # Confirming one alias confirms only that alias.
        freight_module.update_broker(broker['id'], {'identity_confirm_emails': ['bob@bigbroker.com']}, self.storage)
        confirmed = self.storage.get('freight_brokers', broker['id'])
        self.assertEqual(confirmed['unconfirmed_emails'], [])
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertEqual(blockers, [])
        # An exact-email match on the broker stays confirmed.
        again = freight_module.resolve_broker('alice@bigbroker.com', '', self.storage)
        self.assertEqual(again['unconfirmed_emails'], [])

    def test_internal_blockers_never_asked_of_broker(self):
        labels = freight_module._broker_detail_labels(['broker credit approval', 'broker setup packet', 'blocked broker (B Co)', 'truck availability (Truck is marked off duty)', 'stop 1 pickup (Phoenix, AZ) confirmed', 'broker load weight'], [])
        self.assertEqual(labels, ['load weight'])


class FreightBookingTests(unittest.TestCase):
    """Phase 5: agreed -> rate con review -> booked with immutable snapshot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = OwnerStore(SQLiteStore(self.tmp.name + '/booking.db'), ADMIN_OWNER_ID)
        self.storage.init()
        stamp = now_iso()
        self.storage.insert('freight_truck_profiles', {'id': 'p1', 'name': 'T', 'max_weight_lbs': 45000, 'shareable_fields': [], 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_missions', {'id': 'm1', 'name': 'M', 'truck_profile_id': 'p1', 'permissions': {}, 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.broker = freight_module.resolve_broker('a@b.com', 'B Co', self.storage)
        freight_module.update_broker(self.broker['id'], {'credit_status': 'approved', 'setup_status': 'complete'}, self.storage)
        self.load = self.storage.insert('freight_loads', {'id': new_id(), 'mission_id': 'm1', 'truck_profile_id': 'p1', 'broker_id': self.broker['id'], 'broker_email': 'a@b.com', 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'origin_verified': 1, 'destination_city': 'Dallas', 'destination_state': 'TX', 'destination_verified': 1, 'equipment_type': 'dry van', 'equipment_verified': 1, 'pickup_date': '2026-09-23', 'pickup_date_verified': 1, 'schedule_verified': 1, 'loaded_miles': 1000, 'loaded_miles_verified': 1, 'deadhead_miles': 50, 'deadhead_miles_verified': 1, 'weight_lbs': 40000, 'subject': 's', 'status': 'waiting', 'created_at': stamp, 'updated_at': stamp})
        self.thread = self.storage.insert('freight_threads', {'id': new_id(), 'load_id': self.load['id'], 'sender_account': '1', 'recipient_email': 'a@b.com', 'subject': 's', 'state': 'waiting', 'last_activity_at': stamp, 'created_at': stamp, 'updated_at': stamp})

    def tearDown(self):
        self.tmp.cleanup()

    def _full_con(self, **overrides):
        con = {'total_rate': '4200', 'pickup_city': 'Phoenix', 'pickup_state': 'AZ',
               'delivery_city': 'Dallas', 'delivery_state': 'TX', 'pickup_date': '2026-09-23',
               'equipment': 'dry van', 'weight_lbs': '40000'}
        con.update(overrides)
        return con

    def test_agreement_snapshot_immutable_and_idempotent(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        self.assertEqual(booking['status'], 'agreed')
        self.assertEqual(booking['snapshot']['origin'], 'Phoenix, AZ')
        self.assertEqual(booking['snapshot']['destination'], 'Dallas, TX')
        again = freight_module.record_agreement(self.thread['id'], 4500.0, 'msg-2', self.storage)
        self.assertEqual(again['id'], booking['id'])
        self.assertEqual(again['agreed_rate'], 4200.0)
        self.storage.update('freight_loads', self.load['id'], {'destination_city': 'Houston'})
        kept = self.storage.get('freight_bookings', booking['id'])
        self.assertEqual(kept['snapshot']['destination'], 'Dallas, TX')

    def test_rate_con_exact_diffs(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        updated = freight_module.submit_rate_con(booking['id'], {'total_rate': '4000', 'delivery_city': 'Houston', 'delivery_state': 'TX', 'pickup_city': 'Phoenix', 'pickup_state': 'AZ'}, self.storage)
        self.assertEqual(updated['status'], 'rate_con_review')
        fields = {d['field']: d for d in updated['rate_con_diffs']}
        self.assertEqual(fields['total rate']['agreed'], 4200.0)
        self.assertEqual(fields['total rate']['rate_con'], 4000.0)
        self.assertEqual(fields['delivery']['rate_con'], 'Houston, TX')
        self.assertNotIn('pickup', fields)

    def test_clean_rate_con_has_no_diffs(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        updated = freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
        self.assertEqual(updated['rate_con_diffs'], [])

    def test_gates_enforced_in_order(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        with self.assertRaises(ValueError):
            freight_module.approve_driver_handoff(booking['id'], self.storage)
        with self.assertRaises(ValueError):
            freight_module.mark_booked(booking['id'], self.storage)
        freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        done = freight_module.mark_booked(booking['id'], self.storage)
        self.assertEqual(done['status'], 'booked')
        self.assertEqual(self.storage.get('freight_loads', self.load['id'])['status'], 'booked')
        # The commitment is the load's occupancy window, not a permanent profile
        # flag: availability returns to its prior state and the mutex is released.
        profile = self.storage.get('freight_truck_profiles', 'p1')
        self.assertEqual(profile['availability_status'], 'available')
        self.assertIsNone(profile['booking_claim_at'])
        self.assertEqual(self.storage.get('freight_threads', self.thread['id'])['state'], 'booked')

    def test_mark_booked_blocked_by_unverified_facts(self):
        self.storage.update('freight_loads', self.load['id'], {'equipment_verified': 0})
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        with self.assertRaises(ValueError) as ctx:
            freight_module.mark_booked(booking['id'], self.storage)
        self.assertIn('broker equipment', str(ctx.exception))

    def test_partial_rate_con_is_unverified_not_matching(self):
        # A con that omits agreed terms is not an exact match: unknown is not equal.
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        updated = freight_module.submit_rate_con(booking['id'], {'total_rate': '4200'}, self.storage)
        fields = {d['field']: d for d in updated['rate_con_diffs']}
        self.assertEqual(fields['pickup date']['status'], 'unverified')
        self.assertIsNone(fields['pickup date']['rate_con'])
        self.assertEqual(fields['equipment']['status'], 'unverified')

    def test_approval_refused_when_required_terms_missing(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        freight_module.submit_rate_con(booking['id'], {'total_rate': '4200'}, self.storage)
        with self.assertRaises(ValueError) as ctx:
            freight_module.review_rate_con(booking['id'], True, self.storage)
        self.assertIn('pickup city', str(ctx.exception))

    def test_rate_con_dates_compare_normalized(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        updated = freight_module.submit_rate_con(booking['id'], self._full_con(pickup_date='09/23/2026'), self.storage)
        self.assertEqual(updated['rate_con_diffs'], [])
        updated = freight_module.submit_rate_con(booking['id'], self._full_con(pickup_date='Sep 24, 2026'), self.storage)
        fields = {d['field']: d for d in updated['rate_con_diffs']}
        self.assertEqual(fields['pickup date']['status'], 'mismatch')

    def test_new_rate_con_resets_review_and_handoff_gates(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        # A replacement con invalidates both approvals; booking must stop.
        freight_module.submit_rate_con(booking['id'], self._full_con(total_rate='4100'), self.storage)
        reset = self.storage.get('freight_bookings', booking['id'])
        self.assertFalse(reset['rate_con_reviewed'])
        self.assertFalse(reset['driver_handoff_approved'])
        with self.assertRaises(ValueError):
            freight_module.mark_booked(booking['id'], self.storage)
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        done = freight_module.mark_booked(booking['id'], self.storage)
        self.assertEqual(done['status'], 'booked')

    def test_mark_booked_refused_without_broker_identity(self):
        self.storage.update('freight_loads', self.load['id'], {'broker_id': None})
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        # record_agreement re-resolves identity from the broker email; clear it to simulate an unidentified broker.
        broker_id = self.storage.get('freight_loads', self.load['id']).get('broker_id')
        self.assertIsNotNone(broker_id)
        self.storage.update('freight_loads', self.load['id'], {'broker_id': None})
        freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        with self.assertRaises(ValueError) as ctx:
            freight_module.mark_booked(booking['id'], self.storage)
        self.assertIn('verified broker identity', str(ctx.exception))

    def test_mark_booked_claim_is_atomic(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        # A concurrent booking claimed the load between the gate check and commit.
        self.storage.update('freight_loads', self.load['id'], {'status': 'booked'})
        with self.assertRaises(ValueError) as ctx:
            freight_module.mark_booked(booking['id'], self.storage)
        self.assertIn('already booked', str(ctx.exception))
        # The losing booking is not marked booked.
        self.assertNotEqual(self.storage.get('freight_bookings', booking['id'])['status'], 'booked')

    def test_rate_con_source_reference_retained(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        updated = freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage, source='pdf', source_ref='gmail:<m1>:con.pdf')
        self.assertEqual(updated['rate_con_source'], 'pdf')
        self.assertEqual(updated['rate_con_source_ref'], 'gmail:<m1>:con.pdf')
        again = freight_module.submit_rate_con(booking['id'], self._full_con(total_rate='4300'), self.storage)
        self.assertEqual(again['rate_con_source'], 'manual')
        self.assertEqual(again['rate_con_source_ref'], '')

    def test_mark_booked_requires_recorded_terms(self):
        # Rows that predate version-bound gates must re-submit and re-review.
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        self.storage.update('freight_bookings', booking['id'], {'rate_con_reviewed': 1, 'driver_handoff_approved': 1})
        with self.assertRaises(ValueError) as ctx:
            freight_module.mark_booked(booking['id'], self.storage)
        self.assertIn('Rate confirmation terms are required', str(ctx.exception))

    def test_readiness_requires_pickup_date(self):
        # No pickup date means no occupancy window, so booking must block.
        self.storage.update('freight_loads', self.load['id'], {'pickup_date': None})
        load = self.storage.get('freight_loads', self.load['id'])
        mission = self.storage.get('freight_missions', 'm1')
        profile = self.storage.get('freight_truck_profiles', 'p1')
        blockers = freight_module._booking_readiness_blockers(load, mission, profile, self.storage)
        self.assertIn('broker pickup date', blockers)

    def test_concurrent_bookings_on_one_truck_serialize(self):
        import threading
        stamp = now_iso()
        load2 = self.storage.insert('freight_loads', {**self.load, 'id': new_id(), 'status': 'waiting', 'created_at': stamp, 'updated_at': stamp})
        thread2 = self.storage.insert('freight_threads', {**self.thread, 'id': new_id(), 'load_id': load2['id'], 'created_at': stamp, 'updated_at': stamp})
        def gated(thread_id):
            booking = freight_module.record_agreement(thread_id, 4200.0, 'msg-' + thread_id, self.storage)
            freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
            freight_module.review_rate_con(booking['id'], True, self.storage)
            freight_module.approve_driver_handoff(booking['id'], self.storage)
            return booking
        first, second = gated(self.thread['id']), gated(thread2['id'])
        results = {}
        def attempt(key, booking_id):
            try:
                freight_module.mark_booked(booking_id, self.storage)
                results[key] = 'booked'
            except ValueError as exc:
                results[key] = str(exc)
        workers = [threading.Thread(target=attempt, args=('one', first['id'])),
                   threading.Thread(target=attempt, args=('two', second['id']))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        outcomes = list(results.values())
        self.assertEqual(outcomes.count('booked'), 1, results)
        loser = [value for value in outcomes if value != 'booked'][0]
        self.assertTrue(loser.startswith('Another booking is in progress') or loser.startswith('Booking is not ready: truck availability'), loser)
        booked = self.storage.list('freight_loads', {'truck_profile_id': 'p1', 'status': 'booked'}, order='', limit=10)
        self.assertEqual(len(booked), 1)
        # The truck never gets stuck behind the transient mutex state.
        self.assertEqual(self.storage.get('freight_truck_profiles', 'p1')['availability_status'], 'available')

    def test_stale_booking_claim_recovers(self):
        # A crashed booking flow leaves availability 'booking' behind; a stale
        # or timestamp-less claim must recover instead of stranding the truck.
        stamp = now_iso()
        base = {'id': 'p9', 'name': 'T', 'availability_status': 'booking', 'shareable_fields': [], 'active': True, 'created_at': stamp, 'updated_at': stamp}
        self.assertEqual(freight_module.truck_availability(base)['status'], 'available')
        fresh = {**base, 'booking_claim_at': now_iso()}
        self.assertEqual(freight_module.truck_availability(fresh)['status'], 'conflict')
        old = {**base, 'booking_claim_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
        self.assertEqual(freight_module.truck_availability(old)['status'], 'available')

    def test_removed_stop_blocks_booking_until_route_confirmed(self):
        stamp = now_iso()
        self.storage.insert('freight_load_stops', {'id': new_id(), 'load_id': self.load['id'], 'seq': 1, 'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'verified': 1, 'appointment': '2026-09-23 08:00', 'appointment_verified': 1, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_load_stops', {'id': new_id(), 'load_id': self.load['id'], 'seq': 2, 'kind': 'delivery', 'city': 'Dallas', 'state': 'TX', 'verified': 1, 'appointment': '2026-09-24 08:00', 'appointment_verified': 1, 'created_at': stamp, 'updated_at': stamp})
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        freight_module.submit_rate_con(booking['id'], self._full_con(), self.storage)
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        # The broker restates the full route without the delivery stop.
        classification = {'source': 'gemini', 'route_scope': 'complete',
                          'stops': [{'kind': 'pickup', 'city': 'Phoenix', 'state': 'AZ', 'evidence': 'e'}]}
        freight_module._sync_load_stops(self.storage.get('freight_loads', self.load['id']), classification, 'msg-2', self.storage)
        alerts = self.storage.list('freight_alerts', {'kind': 'stop_removed', 'status': 'open'}, order='', limit=5)
        self.assertEqual(len(alerts), 1)
        # Approvals made against the old route are unbound...
        kept = self.storage.get('freight_bookings', booking['id'])
        self.assertFalse(kept['rate_con_reviewed'])
        self.assertFalse(kept['driver_handoff_approved'])
        # ...and even a fresh review cannot book while the route change is unconfirmed.
        freight_module.review_rate_con(booking['id'], True, self.storage)
        freight_module.approve_driver_handoff(booking['id'], self.storage)
        with self.assertRaises(ValueError) as ctx:
            freight_module.mark_booked(booking['id'], self.storage)
        self.assertIn('route change review', str(ctx.exception))
        # The dispatcher confirms the new route and the gate lifts.
        self.storage.update('freight_alerts', alerts[0]['id'], {'status': 'resolved', 'resolved_at': now_iso()})
        done = freight_module.mark_booked(booking['id'], self.storage)
        self.assertEqual(done['status'], 'booked')

    def test_rejected_rate_con_returns_to_agreed(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        freight_module.submit_rate_con(booking['id'], self._full_con(total_rate='4000'), self.storage)
        back = freight_module.review_rate_con(booking['id'], False, self.storage)
        self.assertEqual(back['status'], 'agreed')
        self.assertEqual(back['rate_con_diffs'], [])
        self.assertFalse(back['rate_con_reviewed'])
        self.assertFalse(back['driver_handoff_approved'])


def _make_pdf(lines):
    """Minimal one-page PDF containing the given text lines."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 11 Tf 40 780 Td 14 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n"
    out += f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF"
    return out.encode("latin-1")


RATE_CON_LINES = [
    "RATE CONFIRMATION",
    "Load #: 12345",
    "Total Rate: $4,200.00",
    "Shipper: ABC Warehouse",
    "Pickup: Phoenix, AZ 85001",
    "Pickup Date: 09/28/2026",
    "Consignee: XYZ Distribution",
    "Delivery: Dallas, TX 75201",
    "Equipment: Dry Van",
    "Weight: 40,000 lbs",
]


class FreightRateConPdfTests(unittest.TestCase):
    """Phase 6: rate-con PDF parsing into the booking compare."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = OwnerStore(SQLiteStore(self.tmp.name + '/ratecon.db'), ADMIN_OWNER_ID)
        self.storage.init()
        stamp = now_iso()
        self.storage.insert('freight_truck_profiles', {'id': 'p1', 'name': 'T', 'shareable_fields': [], 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.storage.insert('freight_missions', {'id': 'm1', 'name': 'M', 'truck_profile_id': 'p1', 'permissions': {}, 'active': True, 'created_at': stamp, 'updated_at': stamp})
        self.load = self.storage.insert('freight_loads', {'id': new_id(), 'mission_id': 'm1', 'truck_profile_id': 'p1', 'broker_email': 'a@b.com', 'origin_city': 'Phoenix', 'origin_state': 'AZ', 'destination_city': 'Dallas', 'destination_state': 'TX', 'subject': 's', 'status': 'waiting', 'created_at': stamp, 'updated_at': stamp})
        self.thread = self.storage.insert('freight_threads', {'id': new_id(), 'load_id': self.load['id'], 'sender_account': '1', 'recipient_email': 'a@b.com', 'subject': 's', 'state': 'waiting', 'last_activity_at': stamp, 'created_at': stamp, 'updated_at': stamp})

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_extracts_booking_fields(self):
        from app.core.rate_con import parse_rate_con_pdf
        fields = parse_rate_con_pdf(_make_pdf(RATE_CON_LINES))
        self.assertEqual(fields['total_rate'], 4200.0)
        self.assertEqual(fields['pickup_city'], 'Phoenix')
        self.assertEqual(fields['pickup_state'], 'AZ')
        self.assertEqual(fields['delivery_city'], 'Dallas')
        self.assertEqual(fields['delivery_state'], 'TX')
        self.assertEqual(fields['pickup_date'], '09/28/2026')
        self.assertEqual(fields['equipment'], 'dry van')
        self.assertEqual(fields['weight_lbs'], 40000.0)

    def test_parse_rejects_unreadable_pdf(self):
        from app.core.rate_con import parse_rate_con_pdf
        with self.assertRaises(ValueError):
            parse_rate_con_pdf(b"not a pdf at all")

    def test_matching_rate_con_alerts_exact_match(self):
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        freight_module._handle_rate_con_attachments(self.thread, {}, [('ratecon.pdf', _make_pdf(RATE_CON_LINES), 'att-1')], self.storage)
        updated = self.storage.get('freight_bookings', booking['id'])
        self.assertEqual(updated['status'], 'rate_con_review')
        self.assertEqual(updated['rate_con_amount'], 4200.0)
        self.assertEqual(updated['rate_con_diffs'], [])
        alerts = self.storage.list('freight_alerts', {'thread_id': self.thread['id'], 'kind': 'rate_con_compared'}, order='', limit=5)
        self.assertEqual(len(alerts), 1)
        self.assertIn('matches the agreement', alerts[0]['summary'])

    def test_mismatched_rate_con_alerts_with_diffs(self):
        booking = freight_module.record_agreement(self.thread['id'], 4500.0, 'msg-1', self.storage)
        freight_module._handle_rate_con_attachments(self.thread, {}, [('ratecon.pdf', _make_pdf(RATE_CON_LINES), 'att-1')], self.storage)
        updated = self.storage.get('freight_bookings', booking['id'])
        fields = {d['field'] for d in updated['rate_con_diffs']}
        self.assertIn('total rate', fields)
        alerts = self.storage.list('freight_alerts', {'thread_id': self.thread['id'], 'kind': 'rate_con_compared'}, order='', limit=5)
        self.assertIn('issue', alerts[0]['summary'])

    def test_rate_con_pdf_recovered_from_durable_store_after_crash(self):
        # A crash between inserting the email and parsing its PDF must not lose
        # the attachment: the reconcile path re-reads the stored bytes.
        import base64 as b64
        booking = freight_module.record_agreement(self.thread['id'], 4200.0, 'msg-1', self.storage)
        message = self.storage.insert('freight_messages', {
            'id': new_id(), 'thread_id': self.thread['id'], 'direction': 'in',
            'provider_message_id': '<rc-1@example.com>', 'from_email': 'a@b.com',
            'to_email': 'c@d.com', 'subject': 'Re: s', 'body_text': 'rate confirmation attached',
            'classification': {}, 'status': 'received', 'processing_state': 'pending',
            'created_at': now_iso()})
        pdf = _make_pdf(RATE_CON_LINES)
        freight_module._store_attachments(message, [('ratecon.pdf', pdf)], self.storage)
        result = {'classification': {'protected': ['rate_confirmation']}}
        with patch.object(freight_module, 'evaluate_inbound', return_value=result):
            freight_module._process_inbound(self.thread, message, None, self.storage)
        updated = self.storage.get('freight_bookings', booking['id'])
        self.assertEqual(updated['status'], 'rate_con_review')
        self.assertTrue(updated['rate_con_source_ref'].startswith('att:'), updated['rate_con_source_ref'])
        self.assertEqual(updated['rate_con_amount'], 4200.0)
        # The retained bytes are the original PDF, resolvable by the booking's ref.
        attachment_id = updated['rate_con_source_ref'].split(':')[1]
        row = self.storage.get('freight_attachments', attachment_id)
        self.assertEqual(b64.b64decode(row['content_b64']), pdf)
        # A replay after a completed review never resets the review gates.
        freight_module.review_rate_con(booking['id'], True, self.storage)
        with patch.object(freight_module, 'evaluate_inbound', return_value=result):
            freight_module._process_inbound(self.thread, message, None, self.storage)
        kept = self.storage.get('freight_bookings', booking['id'])
        self.assertTrue(kept['rate_con_reviewed'])

    def test_line_haul_is_not_the_total(self):
        from app.core.rate_con import parse_rate_con_pdf
        fields = parse_rate_con_pdf(_make_pdf([
            "RATE CONFIRMATION",
            "Line Haul: $3,900.00",
            "Fuel Surcharge: $300.00",
            "Total Rate: $4,200.00",
            "Rate per mile: $3.93",
        ]))
        self.assertEqual(fields['total_rate'], 4200.0)
        self.assertEqual(fields.get('line_haul'), 3900.0)
        # Without a total label, line haul alone must not become the total.
        fields = parse_rate_con_pdf(_make_pdf([
            "RATE CONFIRMATION",
            "Line Haul: $3,900.00",
            "Rate per mile: $3.93",
            "Pickup: Phoenix, AZ 85001",
        ]))
        self.assertNotIn('total_rate', fields)
        self.assertEqual(fields.get('line_haul'), 3900.0)

    def test_rate_con_without_booking_alerts(self):
        freight_module._handle_rate_con_attachments(self.thread, {}, [('ratecon.pdf', _make_pdf(RATE_CON_LINES))], self.storage)
        alerts = self.storage.list('freight_alerts', {'thread_id': self.thread['id'], 'kind': 'rate_con_no_booking'}, order='', limit=5)
        self.assertEqual(len(alerts), 1)

    def test_pdf_attachments_filters_non_pdf(self):
        import email
        raw = (b"From: a@b.com\r\nSubject: RC\r\nContent-Type: multipart/mixed; boundary=BB\r\n\r\n"
               b"--BB\r\nContent-Type: text/plain\r\n\r\nSee attached.\r\n"
               b"--BB\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=rc.pdf\r\n\r\n" + _make_pdf(RATE_CON_LINES) + b"\r\n"
               b"--BB\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename=notes.txt\r\n\r\nhello\r\n"
               b"--BB--\r\n")
        message = email.message_from_bytes(raw)
        found = freight_module._pdf_attachments(message)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], 'rc.pdf')
