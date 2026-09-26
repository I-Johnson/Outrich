"""Local agent-test helpers.

Agent-test uses the production freight decision engine, but deliberately keeps
transport out of the loop. The user supplies broker replies in the UI and the
engine evaluates them against a real saved mission and truck profile.
"""
from __future__ import annotations

from typing import Any

from app.core.freight import (
    _auto_send_blockers,
    _booking_readiness_blockers,
    evaluate_inbound,
    format_freight_message,
    load_economics,
    mission_price_comparison,
    record_test_send,
    reevaluate_verified_load,
    verify_load_facts,
)
from app.db import new_id, now_iso


def scenario_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": "all-in-rpm-deadhead",
            "name": "All-in RPM + deadhead",
            "description": "Check pickup city, destination, equipment, schedule, weight, loaded miles, deadhead miles, and rate before negotiating.",
            "required_facts": ["origin_city", "destination", "equipment", "pickup_schedule", "weight", "loaded_miles", "deadhead_miles"],
            "price_basis": "all_in_rpm",
        },
        {
            "id": "loaded-rpm",
            "name": "Loaded RPM only",
            "description": "Use the mission's loaded-mile RPM floor while keeping deadhead visible in the conversation.",
            "required_facts": ["origin_city", "destination", "equipment", "pickup_schedule", "weight", "loaded_miles"],
            "price_basis": "loaded_rpm",
        },
        {
            "id": "minimum-total",
            "name": "Minimum total + full load facts",
            "description": "Use the mission's minimum total rate and verify the broker's load details before responding.",
            "required_facts": ["origin_city", "destination", "equipment", "pickup_schedule", "weight", "loaded_miles", "deadhead_miles"],
            "price_basis": "total",
        },
        {
            "id": "protected-replies",
            "name": "Protected replies always pause",
            "description": "Rate confirmations, calls, driver data, and accepted rates stay manual review items.",
            "required_facts": [],
            "price_basis": "mission_rules",
        },
    ]


def create_local_test_session(storage, payload: dict[str, Any]) -> dict[str, Any]:
    """Create durable test rows without contacting an email provider."""
    mission_id = str(payload.get("mission_id") or "")
    mission = storage.get("freight_missions", mission_id)
    if not mission:
        raise ValueError("Choose an existing mission")
    profile_id = str(payload.get("truck_profile_id") or mission.get("truck_profile_id") or "")
    profile = storage.get("freight_truck_profiles", profile_id) if profile_id else None
    if not profile:
        raise ValueError("Choose an existing truck profile")
    if not mission.get("active", True):
        raise ValueError("The selected mission is inactive")
    if not profile.get("active", True):
        raise ValueError("The selected truck profile is inactive")
    stamp = now_iso()
    load_id = new_id()
    thread_id = new_id()
    # These values satisfy the shared freight schema but are never shown as an
    # email address and never passed to Gmail.
    row = {
        "id": load_id,
        "mission_id": mission_id,
        "truck_profile_id": profile_id,
        "broker_email": "agent-test-local",
        "broker_company": "Local broker test",
        "origin_city": str(mission.get("origin_city") or profile.get("current_city") or "").strip(),
        "origin_state": str(mission.get("origin_state") or profile.get("current_state") or "").strip().upper(),
        "destination_city": "Open destinations",
        "destination_state": "",
        "pickup_date": mission.get("pickup_start") or None,
        "loaded_miles": None,
        "deadhead_miles": None,
        "posted_rate": None,
        "current_offer": None,
        "dat_reference": "agent-test",
        "equipment_type": str(mission.get("equipment_type") or profile.get("equipment_type") or "").strip(),
        "template_id": None,
        "sender_account": "agent-test",
        "subject": f"Agent test · {mission.get('name') or 'Freight mission'}",
        "status": "waiting",
        "current_round": 0,
        "created_at": stamp,
        "updated_at": stamp,
    }
    storage.insert("freight_loads", row)
    storage.insert("freight_threads", {
        "id": thread_id,
        "load_id": load_id,
        "sender_account": "agent-test",
        "recipient_email": "agent-test-local",
        "subject": row["subject"],
        "root_message_id": None,
        "last_message_id": None,
        "last_imap_uid": None,
        "state": "waiting",
        "last_activity_at": stamp,
        "created_at": stamp,
        "updated_at": stamp,
    })
    return agent_test_state(storage, load_id)


def inject_broker_reply(storage, load_id: str, body: str) -> dict[str, Any]:
    """Put one user-entered broker reply through the real evaluator."""
    load = storage.get("freight_loads", str(load_id))
    if not load or load.get("dat_reference") != "agent-test":
        raise ValueError("Local agent-test session not found")
    text = str(body or "").strip()
    if not text:
        raise ValueError("Enter the broker reply")
    thread_rows = storage.list("freight_threads", {"load_id": load["id"]}, order="updated_at desc", limit=1)
    thread = thread_rows[0] if thread_rows else None
    if not thread:
        raise ValueError("Agent-test thread not found")
    stamp = now_iso()
    message = storage.insert("freight_messages", {
        "id": new_id(),
        "thread_id": thread["id"],
        "direction": "in",
        "provider_message_id": f"agent-test-in:{new_id()}",
        "from_email": "",
        "to_email": "",
        "subject": thread.get("subject") or "Agent test",
        "body_text": text,
        "classification": {},
        "status": "received",
        "created_at": stamp,
    })
    storage.update("freight_threads", thread["id"], {
        "last_message_id": message["provider_message_id"],
        "last_activity_at": stamp,
        "updated_at": stamp,
    })
    decision = evaluate_inbound({**thread, "last_message_id": message["provider_message_id"]}, message, storage, transport="test")
    return {"decision": decision, "state": agent_test_state(storage, load_id)}


def approve_test_draft(storage, draft_id: str) -> dict[str, Any]:
    draft = storage.get("freight_drafts", draft_id)
    if not draft or draft.get("status") != "pending":
        raise ValueError("Draft is no longer available")
    thread = storage.get("freight_threads", draft.get("thread_id")) or {}
    load = storage.get("freight_loads", thread.get("load_id")) or {}
    if load.get("dat_reference") != "agent-test":
        raise ValueError("This draft is not part of a local agent-test session")
    sent = record_test_send(draft_id, storage=storage)
    return {"send": sent, "state": agent_test_state(storage, thread.get("load_id"))}


def confirm_test_facts(storage, load_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """Confirm broker details, then re-run the latest reply using local transport."""
    load = storage.get("freight_loads", str(load_id))
    if not load or load.get("dat_reference") != "agent-test":
        raise ValueError("Local agent-test session not found")
    verify_load_facts(load["id"], values, storage=storage)
    threads = storage.list("freight_threads", {"load_id": load["id"]}, order="updated_at desc", limit=1)
    decision = {"action": "saved"}
    if threads:
        thread = threads[0]
        for alert in storage.list("freight_alerts", {"thread_id": thread["id"], "status": "open"}, order="", limit=100):
            if alert.get("kind") in {"miles_unverified", "conflicting_load_facts", "verify_load_facts"}:
                storage.update("freight_alerts", alert["id"], {"status": "resolved", "resolved_at": now_iso()})
        inbound = storage.list("freight_messages", {"thread_id": thread["id"], "direction": "in"}, order="created_at desc", limit=1)
        if inbound:
            decision = reevaluate_verified_load(thread["id"], inbound[0], storage=storage, transport="test")
    return {"decision": decision, "state": agent_test_state(storage, load["id"])}


def agent_test_state(storage, load_id: str) -> dict[str, Any]:
    load = storage.get("freight_loads", str(load_id))
    if not load or load.get("dat_reference") != "agent-test":
        raise ValueError("Local agent-test session not found")
    mission = storage.get("freight_missions", load.get("mission_id")) or {}
    profile = storage.get("freight_truck_profiles", load.get("truck_profile_id") or mission.get("truck_profile_id")) or {}
    thread_rows = storage.list("freight_threads", {"load_id": load["id"]}, order="updated_at desc", limit=1)
    thread = thread_rows[0] if thread_rows else None
    messages = storage.list("freight_messages", {"thread_id": thread["id"]}, order="created_at asc", limit=500) if thread else []
    drafts = storage.list("freight_drafts", {"thread_id": thread["id"]}, order="created_at desc", limit=100) if thread else []
    alerts = storage.list("freight_alerts", {"thread_id": thread["id"]}, order="created_at desc", limit=100) if thread else []
    pending_drafts = [draft for draft in drafts if draft.get("status") == "pending"]
    open_alerts = [alert for alert in alerts if alert.get("status") == "open"]
    if pending_drafts:
        waiting_for = "approval"
    elif thread and thread.get("state") in {"booked", "closed", "passed"}:
        waiting_for = "complete"
    elif thread and thread.get("state") in {"needs_attention", "mismatch", "protected_review", "offer_review"}:
        waiting_for = "review"
    else:
        waiting_for = "broker_reply"
    return {
        "load": load,
        "mission": mission,
        "profile": profile,
        "thread": thread,
        "messages": [format_freight_message(dict(message)) for message in messages],
        "drafts": drafts,
        "alerts": alerts,
        "pending_drafts": pending_drafts,
        "open_alerts": open_alerts,
        "economics": load_economics(load),
        "price_comparison": mission_price_comparison(load, mission),
        "auto_send_blockers": _auto_send_blockers(load, mission, profile),
        "booking_readiness_blockers": _booking_readiness_blockers(load, mission, profile, storage),
        "stops": storage.list("freight_load_stops", {"load_id": load["id"]}, order="seq asc", limit=50),
        "waiting_for": waiting_for,
        "transport": "local",
    }
