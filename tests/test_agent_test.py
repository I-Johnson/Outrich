import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-agent-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
os.environ["FREIGHT_AGENT_MODE"] = "rules"

from app.core.agent_test import approve_test_draft, agent_test_state, confirm_test_facts, create_local_test_session, inject_broker_reply, scenario_catalog
from app.core.freight import classify_reply
from app.core.freight_agent import _validated
from app.core.tenancy import ADMIN_OWNER_ID, OwnerStore
from app.db import SQLiteStore, new_id, now_iso


class AgentTestWorkspaceTests(unittest.TestCase):
    def test_catalog_contains_real_rule_checklists_without_fake_turns(self):
        scenarios = scenario_catalog()
        self.assertEqual(
            {"all-in-rpm-deadhead", "loaded-rpm", "minimum-total", "protected-replies"},
            {row["id"] for row in scenarios},
        )
        for scenario in scenarios:
            self.assertNotIn("messages", scenario)
            self.assertNotIn("profile", scenario)
            self.assertNotIn("mission", scenario)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = OwnerStore(SQLiteStore(os.path.join(self.temp.name, "freight.db")), ADMIN_OWNER_ID)
        self.storage.init()
        stamp = now_iso()
        self.profile_id, self.mission_id = new_id(), new_id()
        self.storage.insert("freight_truck_profiles", {
            "id": self.profile_id, "name": "Test truck", "current_city": "Phoenix", "current_state": "AZ",
            "equipment_type": "dry van", "trailer_length_ft": 53, "max_weight_lbs": 45000,
            "shareable_fields": ["equipment_type"], "active": True, "created_at": stamp, "updated_at": stamp,
        })

    def add_mission(self, permissions):
        stamp = now_iso()
        self.storage.insert("freight_missions", {
            "id": self.mission_id, "name": "Phoenix to Dallas", "truck_profile_id": self.profile_id,
            "origin_city": "Phoenix", "origin_state": "AZ", "equipment_type": "dry van",
            "destinations": [{"label": "Dallas, TX", "kind": "city", "radius_miles": 0}],
            "floor_total": 3900, "target_total": 4500, "permissions": permissions, "active": True,
            "created_at": stamp, "updated_at": stamp,
        })

    def test_auto_mode_records_local_response_without_email(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        state = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})
        result = inject_broker_reply(self.storage, state["load"]["id"], "Do you have a dry van?")
        self.assertEqual(result["decision"]["action"], "sent")
        self.assertEqual(result["decision"]["transport"], "local")
        self.assertEqual(result["state"]["waiting_for"], "broker_reply")
        self.assertEqual([message["direction"] for message in result["state"]["messages"]], ["in", "out"])

    def test_approve_mode_keeps_draft_until_local_approval(self):
        self.add_mission({"auto_profile_reply": False, "auto_counter": False, "auto_pass": False})
        state = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})
        result = inject_broker_reply(self.storage, state["load"]["id"], "Do you have a dry van?")
        self.assertEqual(result["decision"]["action"], "draft")
        self.assertEqual(result["state"]["waiting_for"], "approval")
        approved = approve_test_draft(self.storage, result["state"]["pending_drafts"][0]["id"])
        self.assertEqual(approved["send"]["transport"], "local")
        self.assertEqual([message["direction"] for message in approved["state"]["messages"]], ["in", "out"])

    def test_loaded_miles_wording_is_not_treated_as_an_ambiguous_rate(self):
        result = classify_reply("1,000 loaded miles, deadhead 100 miles. Rate is $4,000 all in.")
        self.assertFalse(result["ambiguous_offer"])
        self.assertEqual(result["offer"], 4000)

    def test_equipment_load_fact_is_not_treated_as_a_truck_question(self):
        self.assertNotIn("equipment_type", classify_reply("This is a dry van load. Rate is $4,000 all in.")["questions"])
        self.assertIn("equipment_type", classify_reply("Do you have a dry van?")["questions"])

    def test_repeated_profile_question_does_not_send_same_answer_twice(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        first = inject_broker_reply(self.storage, load_id, "Do you have a dry van?")
        second = inject_broker_reply(self.storage, load_id, "Is it a dry van?")
        self.assertEqual(first["decision"]["action"], "sent")
        self.assertEqual(second["decision"]["action"], "alert")
        self.assertEqual([row["direction"] for row in second["state"]["messages"]], ["in", "out", "in"])

    def test_confirmed_facts_enable_auto_counter_without_repeating_it(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        self.storage.update("freight_missions", self.mission_id, {"target_total": 4500})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        first = inject_broker_reply(self.storage, load_id, "Rate is $4,000 all in.")
        self.assertEqual(first["decision"]["action"], "draft")
        self.assertIn("broker pickup city", first["state"]["auto_send_blockers"])
        confirmed = confirm_test_facts(self.storage, load_id, {
            "origin_city": "Phoenix", "origin_state": "AZ", "origin_confirmed": True,
            "destination_city": "Dallas", "destination_state": "TX",
            "equipment_type": "dry van", "equipment_confirmed": True,
            "loaded_miles": 1000, "deadhead_miles": 100, "weight_lbs": 40000,
            "schedule_confirmed": True,
        })
        self.assertEqual(confirmed["decision"]["action"], "sent")
        self.assertEqual(confirmed["state"]["auto_send_blockers"], [])
        self.assertEqual(len(confirmed["state"]["pending_drafts"]), 0)
        repeated = inject_broker_reply(self.storage, load_id, "Could you do $4,100 all in?")
        self.assertEqual(repeated["decision"]["action"], "alert")
        self.assertEqual([row["direction"] for row in repeated["state"]["messages"]], ["in", "out", "in"])
        self.assertEqual(len(self.storage.list("freight_negotiation_events", {"event_type": "counter"}, order="", limit=10)), 1)

    def test_page_and_facts_endpoint_show_live_checks(self):
        from fastapi.testclient import TestClient
        from app import main

        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        with patch.object(main, "store", self.storage.base):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)
            page = client.get("/freight/agent-test")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Live rule checks", page.text)
            self.assertNotIn("scenario-picker", page.text)
            started = client.post("/freight/agent-test/start", json={"mission_id": self.mission_id, "truck_profile_id": self.profile_id})
            self.assertEqual(started.status_code, 200)
            load_id = started.json()["load_id"]
            saved = client.post("/freight/agent-test/facts", json={"load_id": load_id, "facts": {
                "origin_city": "Phoenix", "origin_state": "AZ", "origin_confirmed": True,
                "destination_city": "Dallas", "destination_state": "TX",
                "equipment_type": "dry van", "equipment_confirmed": True,
                "loaded_miles": 1000, "deadhead_miles": 100, "weight_lbs": 40000,
                "schedule_confirmed": True,
            }})
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["state"]["auto_send_blockers"], [])

    def test_auto_mission_requires_lane_target_while_approve_can_leave_it_open(self):
        from fastapi.testclient import TestClient
        from app import main

        payload = {
            "name": "Florida to New Jersey", "truck_profile_id": self.profile_id,
            "origin_city": "Miami", "origin_state": "FL", "equipment_type": "dry van",
            "destinations": [{"kind": "state", "label": "NJ", "radius_miles": 0}],
            "floor_total": 3000, "execution_mode": "auto", "active": True,
        }
        with patch.object(main, "store", self.storage.base):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD}, follow_redirects=False)
            missing_target = client.post("/freight/agent-test/mission", json=payload)
            self.assertEqual(missing_target.status_code, 400)
            self.assertIn("target", missing_target.json()["detail"])
            multiple_lanes = client.post("/freight/agent-test/mission", json={**payload, "target_total": 5000,
                "destinations": payload["destinations"] + [{"kind": "city", "label": "Dallas, TX"}]})
            self.assertEqual(multiple_lanes.status_code, 400)
            approve = client.post("/freight/agent-test/mission", json={**payload, "execution_mode": "approve"})
            self.assertEqual(approve.status_code, 200)

    def test_existing_auto_mission_without_target_pauses_before_reply(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        self.storage.update("freight_missions", self.mission_id, {"target_total": None})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        result = inject_broker_reply(self.storage, load_id, "Do you have a dry van?")
        self.assertEqual(result["decision"]["action"], "alert")
        self.assertIn("target", result["decision"]["summary"])
        self.assertEqual(result["state"]["pending_drafts"], [])

    def test_approve_mode_does_not_use_one_target_for_two_destinations(self):
        self.add_mission({"auto_profile_reply": False, "auto_counter": False, "auto_pass": False})
        self.storage.update("freight_missions", self.mission_id, {"destinations": [
            {"kind": "city", "label": "Dallas, TX", "radius_miles": 0},
            {"kind": "city", "label": "Newark, NJ", "radius_miles": 0},
        ]})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        result = inject_broker_reply(self.storage, load_id, "Delivery to Newark, NJ. Rate is $4,000 all in.")
        self.assertEqual(result["decision"]["action"], "alert")
        self.assertIn("specific lane", result["decision"]["summary"])
        self.assertEqual(result["state"]["pending_drafts"], [])

    def test_model_interpretation_handles_pu_and_4k_with_evidence(self):
        latest = "PU Fri in Phoenix, deliver Dallas, TX. 40k lbs and 1,000 loaded miles. Can do 4k all in."
        interpreted = _validated({
            "intent": "offer", "summary": "Broker offered $4,000 and gave pickup and load details.",
            "protected": [], "questions": [], "total_rate": 4000, "total_rate_evidence": "4k all in",
            "rate_per_mile": None, "ambiguous_rate": False,
            "numeric_facts": [
                {"unit": "weight", "value": 40000, "evidence": "40k lbs"},
                {"unit": "miles", "value": 1000, "evidence": "1,000 loaded miles"},
            ],
            "destination": {"city": "Dallas", "state": "TX", "evidence": "deliver Dallas, TX"},
            "pickup_date": None, "suggested_reply": "",
        }, latest)
        self.assertEqual(interpreted["offer"], 4000)
        self.assertEqual([fact["value"] for fact in interpreted["numeric_facts"]], [40000, 1000])
        self.assertEqual(interpreted["destination"]["city"], "Dallas")
        self.assertIsNone(_validated({"intent": "offer", "total_rate": 4500, "total_rate_evidence": "4k all in"}, latest)["offer"])
        self.assertEqual(_validated({"intent": "offer", "total_rate": 4000, "total_rate_evidence": "four grand"}, "Can do four grand all in")["offer"], 4000)

    def test_model_reading_creates_contextual_manual_draft(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        reading = {
            "kind": "question", "intent": "question", "source": "gemini", "summary": "Broker asks whether Friday pickup works.",
            "protected": [], "questions": [], "offer": None, "rate_per_mile": None, "ambiguous_offer": False,
            "numeric_facts": [], "destination": None, "pickup_date": None, "required_equipment": None,
            "suggested_reply": "What time is the pickup appointment on Friday?",
        }
        with patch("app.core.freight.settings.FREIGHT_AGENT_MODE", "model"), patch("app.core.freight.interpret_broker_reply", return_value=reading) as model:
            result = inject_broker_reply(self.storage, load_id, "PU Fri okay?")
        self.assertEqual(result["decision"]["action"], "draft")
        self.assertEqual(result["state"]["pending_drafts"][0]["body_text"], "What time is the pickup appointment on Friday?")
        self.assertEqual(result["state"]["messages"][0]["classification"]["summary"], reading["summary"])
        self.assertEqual(model.call_args.args[0], "PU Fri okay?")

    def test_model_failure_pauses_without_sending(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        with patch("app.core.freight.settings.FREIGHT_AGENT_MODE", "model"), patch("app.core.freight.interpret_broker_reply", side_effect=ValueError("model unavailable")):
            result = inject_broker_reply(self.storage, load_id, "Can do 4k")
        self.assertEqual(result["decision"]["action"], "alert")
        self.assertEqual(result["state"]["pending_drafts"], [])

    def test_model_acceptance_handoffs_even_without_keyword_match(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        reading = {
            "kind": "acceptance", "intent": "acceptance", "source": "gemini", "summary": "Broker agrees to the previous price.",
            "protected": [], "questions": [], "offer": None, "rate_per_mile": None, "ambiguous_offer": False,
            "numeric_facts": [], "origin": None, "destination": None, "equipment": None, "pickup_date": None,
            "required_equipment": None, "suggested_reply": "",
        }
        with patch("app.core.freight.settings.FREIGHT_AGENT_MODE", "model"), patch("app.core.freight.interpret_broker_reply", return_value=reading):
            result = inject_broker_reply(self.storage, load_id, "Sounds good")
        self.assertEqual(result["decision"]["action"], "alert")
        self.assertEqual(result["state"]["thread"]["state"], "accepted_pending_review")

    def test_model_can_phrase_policy_approved_counter(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        self.storage.update("freight_missions", self.mission_id, {"target_total": 4500})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        confirm_test_facts(self.storage, load_id, {
            "origin_city": "Phoenix", "origin_state": "AZ", "origin_confirmed": True,
            "destination_city": "Dallas", "destination_state": "TX",
            "equipment_type": "dry van", "equipment_confirmed": True,
            "loaded_miles": 1000, "deadhead_miles": 100, "weight_lbs": 40000, "schedule_confirmed": True,
        })
        reading = {
            "kind": "offer", "intent": "offer", "source": "gemini", "summary": "Broker offers $4,000 all in.",
            "protected": [], "questions": [], "offer": 4000, "rate_per_mile": None, "ambiguous_offer": False,
            "numeric_facts": [], "origin": None, "destination": None, "equipment": None, "pickup_date": None,
            "required_equipment": None, "suggested_reply": "",
        }
        with patch("app.core.freight.settings.FREIGHT_AGENT_MODE", "model"), patch("app.core.freight.interpret_broker_reply", return_value=reading), patch("app.core.freight.compose_counter_reply", return_value="Could you meet us at $4,500 all in?") as compose:
            result = inject_broker_reply(self.storage, load_id, "Can do 4k all in")
        self.assertEqual(result["decision"]["action"], "sent")
        self.assertEqual(result["state"]["messages"][-1]["body_text"], "Could you meet us at $4,500 all in?")
        self.assertEqual(compose.call_args.args[2], 4500)

    def test_details_followup_uses_active_offer_once(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        self.storage.update("freight_missions", self.mission_id, {"target_total": 4500})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        common = {"source": "gemini", "protected": [], "questions": [], "rate_per_mile": None,
                  "ambiguous_offer": False, "suggested_reply": "", "pickup_date": None, "required_equipment": None}
        offer = {**common, "kind": "offer", "intent": "offer", "summary": "Broker offers $4,000.", "offer": 4000,
                 "numeric_facts": [], "origin": None, "destination": None, "equipment": None}
        details = {**common, "kind": "details", "intent": "details", "summary": "Broker supplied load details.", "offer": None,
                   "numeric_facts": [{"unit": "weight", "value": 40000}, {"unit": "miles", "value": 1000}, {"unit": "deadhead_miles", "value": 100}],
                   "origin": {"city": "Phoenix", "state": "AZ"}, "destination": {"city": "Dallas", "state": "TX"},
                   "equipment": {"type": "dry van"}, "pickup_schedule_evidence": "PU Friday at 8 AM",
                   "delivery_schedule_evidence": "delivery Sunday at noon"}
        with patch("app.core.freight.settings.FREIGHT_AGENT_MODE", "model"), patch("app.core.freight.interpret_broker_reply", side_effect=[offer, details]) as model, patch("app.core.freight.compose_counter_reply", return_value="Could you do $4,500 all in?"):
            first = inject_broker_reply(self.storage, load_id, "Can do 4k all in")
            second = inject_broker_reply(self.storage, load_id, "PU Phoenix AZ Friday at 8 AM; delivery Dallas TX Sunday at noon. Dry van, 40k lbs, 1000 loaded miles, 100 DH.")
            final = confirm_test_facts(self.storage, load_id, {"schedule_confirmed": True})
        self.assertEqual(first["decision"]["action"], "draft")
        self.assertEqual(second["decision"]["action"], "draft")
        self.assertIn("$4,500", second["state"]["pending_drafts"][0]["body_text"])
        self.assertEqual(second["state"]["messages"][-1]["classification"]["active_offer"], 4000)
        self.assertEqual(final["decision"]["action"], "sent")
        self.assertEqual(final["state"]["messages"][-1]["body_text"], "Could you do $4,500 all in?")
        self.assertEqual(model.call_count, 2)
        events = self.storage.list("freight_negotiation_events", {"event_type": "offer"}, order="", limit=10)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
