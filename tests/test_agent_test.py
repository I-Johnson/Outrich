import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DATABASE_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = "/tmp/outreach-agent-test.db"
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")
os.environ["FREIGHT_AGENT_MODE"] = "rules"

from app.core.agent_test import approve_test_draft, agent_test_state, confirm_test_facts, create_local_test_session, inject_broker_reply, scenario_catalog
from app.core.freight import classify_reply
from app.core.freight_agent import _validated, interpret_broker_reply
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

    def test_repeated_profile_question_offers_manual_restate_instead_of_stopping(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        first = inject_broker_reply(self.storage, load_id, "Do you have a dry van?")
        second = inject_broker_reply(self.storage, load_id, "Is it a dry van?")
        self.assertEqual(first["decision"]["action"], "sent")
        # A repeated question never kills the turn and never auto-resends: a
        # manual restate draft is prepared and an alert explains the repeat.
        self.assertEqual(second["decision"]["action"], "draft")
        self.assertEqual(second["decision"]["draft"]["reason"], "profile_fact_restate")
        self.assertTrue(second["decision"]["draft"]["policy_snapshot"]["manual_only"])
        self.assertEqual([row["direction"] for row in second["state"]["messages"]], ["in", "out", "in"])
        self.assertTrue(self.storage.list("freight_alerts", {"kind": "repeated_question"}, order="", limit=1))

    def test_confirmed_facts_enable_auto_counter_with_bounded_restate(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        self.storage.update("freight_missions", self.mission_id, {"target_total": 4500})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        first = inject_broker_reply(self.storage, load_id, "Rate is $4,000 all in.")
        self.assertEqual(first["decision"]["action"], "sent")
        self.assertIn("delivery city", first["state"]["messages"][-1]["body_text"])
        self.assertIn("broker pickup city", first["state"]["booking_readiness_blockers"])
        self.assertEqual(first["state"]["auto_send_blockers"], [])
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
        # A new broker message resets the turn: the same counter is restated,
        # flagged as a restate, and never consumes another counter round.
        self.assertEqual(repeated["decision"]["action"], "sent")
        self.assertEqual([row["direction"] for row in repeated["state"]["messages"]], ["in", "out", "out", "in", "out"])
        events = self.storage.list("freight_negotiation_events", {"event_type": "counter"}, order="", limit=10)
        self.assertEqual(len(events), 2)
        self.assertEqual([(event.get("details") or {}).get("restate", False) for event in events], [False, True])
        self.assertEqual(self.storage.get("freight_loads", load_id)["current_round"], 1)

    def test_auto_mode_sends_missing_load_details_request(self):
        self.add_mission({"auto_profile_reply": True, "auto_counter": True, "auto_pass": True})
        load_id = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": self.profile_id})["load"]["id"]
        reading = {
            "kind": "offer", "intent": "offer", "source": "gemini", "summary": "Broker offered $4,000 for Dallas, TX.",
            "protected": [], "questions": [], "offer": 4000, "rate_per_mile": None, "ambiguous_offer": False,
            "numeric_facts": [
                {"unit": "miles", "value": 1000, "evidence": "1000 loaded miles"},
                {"unit": "deadhead_miles", "value": 100, "evidence": "100 deadhead miles"},
            ],
            "origin": None, "destination": {"city": "Dallas", "state": "TX", "evidence": "deliver to Dallas, TX"},
            "equipment": None, "pickup_date": {"value": "2026-09-24", "evidence": "pickup 9/24"},
            "pickup_schedule_evidence": "", "delivery_schedule_evidence": "", "required_equipment": None,
            "suggested_reply": "",
        }
        with patch("app.core.freight.settings.FREIGHT_AGENT_MODE", "model"), patch("app.core.freight.interpret_broker_reply", return_value=reading):
            result = inject_broker_reply(self.storage, load_id, "pickup 9/24, deliver to Dallas, TX, 1000 loaded miles, 100 deadhead miles, can you do $4000")
        self.assertEqual(result["decision"]["action"], "sent")
        self.assertEqual(result["decision"]["transport"], "local")
        self.assertEqual(result["state"]["pending_drafts"], [])
        self.assertIn("$4,500", result["state"]["messages"][-1]["body_text"])
        self.assertIn("pickup city and state", result["state"]["messages"][-1]["body_text"])
        self.assertIn("required trailer", result["state"]["messages"][-1]["body_text"])
        self.assertIn("load weight", result["state"]["messages"][-1]["body_text"])
        self.assertIn("pickup appointment time or window", result["state"]["messages"][-1]["body_text"])
        self.assertEqual(result["state"]["open_alerts"], [])
        self.assertEqual(result["state"]["drafts"][0]["policy_snapshot"]["counter"], 4500)
        self.assertEqual(result["state"]["load"]["current_round"], 1)
        self.storage.update("freight_missions", self.mission_id, {"permissions": {}})
        manual_id = create_local_test_session(self.storage, {"mission_id": self.mission_id})["load"]["id"]
        with patch("app.core.freight.settings.FREIGHT_AGENT_MODE", "model"), patch("app.core.freight.interpret_broker_reply", return_value=reading):
            manual = inject_broker_reply(self.storage, manual_id, "pickup 9/24, deliver to Dallas, TX, 1000 loaded miles, 100 deadhead miles, can you do $4000")
        self.assertEqual(manual["decision"]["action"], "draft")
        draft = manual["state"]["pending_drafts"][0]
        self.assertEqual(draft["reason"], "counter_to_target")
        self.assertEqual(draft["policy_snapshot"]["counter"], 4500)
        self.assertEqual(manual["state"]["open_alerts"], [])
        approved = approve_test_draft(self.storage, draft["id"])
        self.assertEqual(approved["state"]["load"]["current_round"], 1)

    def test_reload_preserves_approvals_and_reset_cascades_only_this_test(self):
        from fastapi.testclient import TestClient
        from app import main

        self.add_mission({"auto_profile_reply": False, "auto_counter": False, "auto_pass": False})
        started = create_local_test_session(self.storage, {"mission_id": self.mission_id})
        load_id, thread_id = started["load"]["id"], started["thread"]["id"]
        result = inject_broker_reply(self.storage, load_id, "Delivery to Dallas, TX. Rate is $4,000 all in.")
        draft_id = result["state"]["pending_drafts"][0]["id"]
        self.assertEqual(result["state"]["open_alerts"], [])
        self.assertTrue(self.storage.list("freight_negotiation_events", {"thread_id": thread_id}))
        other = create_local_test_session(self.storage, {"mission_id": self.mission_id})
        with patch.object(main, "store", self.storage.base):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD})
            reloaded = client.get("/freight/agent-test/state", params={"load_id": load_id})
            self.assertEqual(reloaded.status_code, 200)
            self.assertEqual(reloaded.json()["pending_drafts"][0]["id"], draft_id)
            reset = client.post("/freight/agent-test/reset", json={"load_id": load_id})
            self.assertEqual(reset.status_code, 200)
            self.assertEqual(client.get("/freight/agent-test/state", params={"load_id": load_id}).status_code, 404)
            self.assertEqual(client.post("/freight/agent-test/approve", json={"draft_id": draft_id}).status_code, 400)
            fresh = client.post("/freight/agent-test/start", json={"mission_id": self.mission_id}).json()["state"]
        self.assertIsNone(self.storage.get("freight_threads", thread_id))
        for table in ("freight_messages", "freight_drafts", "freight_alerts", "freight_negotiation_events"):
            self.assertEqual(self.storage.list(table, {"thread_id": thread_id}), [], table)
        self.assertTrue(self.storage.get("freight_loads", other["load"]["id"]))
        self.assertTrue(self.storage.get("freight_missions", self.mission_id))
        self.assertTrue(self.storage.get("freight_truck_profiles", self.profile_id))
        self.assertEqual(fresh["messages"], [])
        self.assertEqual(fresh["pending_drafts"], [])
        self.assertEqual(fresh["open_alerts"], [])
        self.assertIsNone(fresh["load"]["current_offer"])
        self.assertFalse(fresh["load"]["origin_verified"])

    def test_reset_rejects_real_loads_and_other_owners_tests(self):
        from fastapi.testclient import TestClient
        from app import main

        self.add_mission({})
        local = create_local_test_session(self.storage, {"mission_id": self.mission_id})
        real = create_local_test_session(self.storage, {"mission_id": self.mission_id})
        self.storage.update("freight_loads", real["load"]["id"], {"dat_reference": "real-load"})
        with patch.object(main, "store", self.storage.base):
            client = TestClient(main.app)
            client.post("/login", data={"email": main.env.ADMIN_EMAIL, "password": main.env.ADMIN_PASSWORD})
            self.assertEqual(client.post("/freight/agent-test/reset", json={"load_id": real["load"]["id"]}).status_code, 404)
            other = TestClient(main.app)
            other.post("/signup", data={"email": "reset-test@example.com", "password": "test-password-123", "name": "Other"})
            self.assertEqual(other.post("/freight/agent-test/reset", json={"load_id": local["load"]["id"]}).status_code, 404)
        self.assertTrue(self.storage.get("freight_loads", local["load"]["id"]))
        self.assertTrue(self.storage.get("freight_loads", real["load"]["id"]))

    def test_model_receives_mission_and_only_allowed_truck_facts_with_verification(self):
        self.add_mission({"auto_counter": True})
        self.storage.update("freight_truck_profiles", self.profile_id, {"mc_number": "123456", "dispatcher_phone": "6025550100"})
        state = create_local_test_session(self.storage, {"mission_id": self.mission_id})
        response = Mock()
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": json.dumps({"intent": "question"})}]}}]}
        with patch("app.core.freight_agent.settings.GEMINI_API_KEY", "test-key"), patch("app.core.freight_agent.httpx.Client") as client:
            client.return_value.__enter__.return_value.post.return_value = response
            interpret_broker_reply("What equipment do you have?", [], state["load"], state["mission"], state["profile"])
        payload = client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
        context = json.loads(payload["contents"][0]["parts"][0]["text"].split("Conversation data:\n", 1)[1])
        self.assertEqual(context["mission"]["destinations"], state["mission"]["destinations"])
        self.assertEqual(context["mission"]["origin_city"], "Phoenix")
        self.assertEqual(context["shareable_truck_facts"], {"equipment_type": "dry van"})
        self.assertEqual(context["load"]["origin_city"], "Phoenix")
        self.assertFalse(context["load_fact_verification"]["origin_verified"])
        self.assertFalse(context["load_fact_verification"]["equipment_verified"])
        self.assertNotIn("floor_total", context["mission"])
        self.assertNotIn("123456", json.dumps(context))
        self.assertNotIn("6025550100", json.dumps(context))

    def test_existing_conversation_uses_updated_mission_rules_and_selected_truck(self):
        self.add_mission({"auto_counter": False})
        stamp = now_iso()
        other_id = new_id()
        self.storage.insert("freight_truck_profiles", {"id": other_id, "name": "Selected truck", "equipment_type": "dry van",
            "team_status": "solo", "max_weight_lbs": 42000, "shareable_fields": ["team_status"], "active": True,
            "created_at": stamp, "updated_at": stamp})
        state = create_local_test_session(self.storage, {"mission_id": self.mission_id, "truck_profile_id": other_id})
        load_id = state["load"]["id"]
        self.storage.update("freight_truck_profiles", other_id, {"team_status": "team"})
        self.storage.update("freight_missions", self.mission_id, {"permissions": {"auto_profile_reply": True}, "target_total": 4800})
        reply = inject_broker_reply(self.storage, load_id, "Are you a true team?")
        self.assertEqual(reply["decision"]["action"], "sent")
        self.assertEqual(reply["state"]["messages"][-1]["body_text"], "Yes, this is a true team.")
        self.assertEqual(reply["state"]["profile"]["id"], other_id)
        confirm_test_facts(self.storage, load_id, {"origin_city": "Phoenix", "origin_state": "AZ", "origin_confirmed": True,
            "destination_city": "Dallas", "destination_state": "TX", "equipment_type": "dry van", "equipment_confirmed": True,
            "loaded_miles": 1000, "deadhead_miles": 100, "weight_lbs": 40000, "schedule_confirmed": True})
        offer = inject_broker_reply(self.storage, load_id, "Can do $4,000 all in.")
        self.assertEqual(offer["decision"]["action"], "draft")
        self.assertIn("$4,800", offer["state"]["pending_drafts"][0]["body_text"])

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
        # Booking readiness now always requires a pickup date, so the counter asks for it.
        self.assertEqual(result["state"]["messages"][-1]["body_text"], "Could you meet us at $4,500 all in? Also, please confirm pickup date.")
        self.assertEqual(compose.call_args.args[2], 4500)

    def test_details_followup_never_resends_counter_for_same_message(self):
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
        self.assertEqual(first["decision"]["action"], "sent")
        self.assertIn("delivery city", first["state"]["messages"][-1]["body_text"])
        self.assertEqual(second["decision"]["action"], "sent")
        self.assertIn("$4,500", second["state"]["messages"][-1]["body_text"])
        self.assertEqual(second["state"]["messages"][-2]["classification"]["active_offer"], 4000)
        # Re-evaluating the same broker message after manual fact confirmation
        # must not resend the counter that already went out for it.
        self.assertEqual(final["decision"]["action"], "duplicate")
        self.assertEqual(final["state"]["messages"][-1]["body_text"], "Could you do $4,500 all in? Also, please confirm pickup date.")
        self.assertEqual(model.call_count, 2)
        events = self.storage.list("freight_negotiation_events", {"event_type": "offer"}, order="", limit=10)
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
