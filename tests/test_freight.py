import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-freight-test.db"
os.environ["FREIGHT_AGENT_MODE"] = "rules"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from app.adapters.base import SendResult
from app.core.freight import SensitiveOutboundConfirmationRequired, auto_lane_issue, classify_reply, evaluate_inbound, extract_offer, extract_numeric_facts, load_economics, mission_price_comparison, parse_destinations, recover_uncertain_freight_sends, reevaluate_verified_load, seed_freight_template, send_draft, sensitive_outbound_fields, set_thread_state, verify_load_facts
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

    def test_city_destination_state_is_combined_before_auto_validation(self):
        result = parse_destinations(["Dallas"], ["city"], ["0"], ["tx"])
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
        self.assertEqual(self.inbound("Can you pick up tonight at 2200?")["action"], "alert")
        self.assertEqual(self.inbound("Cargo weight is at 42,000 lbs")["action"], "alert")
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
                "schedule_confirmed": "yes",
            }, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(self.storage.get("freight_loads", self.load_id)["destination_verified"])
            response = client.post(f"/freight/threads/{self.thread_id}/state", data={"state": "booked"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(self.storage.get("freight_threads", self.thread_id)["state"], "booked")
            page = client.get(f"/freight?load_id={self.load_id}")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Reopen negotiation", page.text)
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
                "destination_label": "Dallas",
                "destination_state": "TX",
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
