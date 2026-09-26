"""Replay harness: drive recorded broker conversations through the real evaluator.

Fixtures are JSON files with a mission setup and a list of broker turns. Every
turn must end in a visible outcome - a draft, a permitted send, an alert, or a
close. A broker message that produces nothing is a replay failure by default.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.agent_test import agent_test_state, create_local_test_session, inject_broker_reply

VISIBLE_ACTIONS = {"sent", "draft", "alert", "closed", "ignored"}


def run_replay(storage, fixture: dict[str, Any]) -> dict[str, Any]:
    """Replay one conversation fixture. Returns the per-turn trace."""
    session = create_local_test_session(storage, {"mission_id": fixture["mission_id"]})
    load_id = session["load"]["id"] if "load" in session else session["id"]
    trace = []
    for turn in fixture.get("turns") or []:
        result = inject_broker_reply(storage, load_id, turn["body"])
        decision = result["decision"]
        state = result["state"]
        trace.append({
            "body": turn["body"],
            "action": decision.get("action"),
            "reason": decision.get("reason"),
            "summary": decision.get("summary") or decision.get("warning"),
            "state": state["thread"]["state"],
            "alerts": [a["kind"] for a in state.get("alerts", []) if a.get("status") == "open"],
            "pending_drafts": [d.get("reason") for d in state.get("pending_drafts", [])],
            "expect": turn.get("expect") or {},
        })
    return {"fixture": fixture.get("name"), "load_id": load_id, "trace": trace, "state": agent_test_state(storage, load_id)}


def validate_replay(replay: dict[str, Any]) -> list[str]:
    """Violations: the universal keep-alive invariant plus fixture expectations."""
    violations: list[str] = []
    for index, turn in enumerate(replay["trace"], start=1):
        label = f"turn {index} ({turn['body'][:50]!r})"
        if turn["action"] not in VISIBLE_ACTIONS:
            violations.append(f"{label}: silent stall - action={turn['action']!r} with no draft, send, alert, or close")
        expect = turn["expect"]
        if expect.get("action") and turn["action"] not in expect["action"]:
            violations.append(f"{label}: action {turn['action']!r} not in expected {expect['action']}")
        if expect.get("state") and turn["state"] != expect["state"]:
            violations.append(f"{label}: state {turn['state']!r} != expected {expect['state']!r}")
        if expect.get("alert_kind") and expect["alert_kind"] not in turn["alerts"]:
            violations.append(f"{label}: expected alert {expect['alert_kind']!r}, open alerts {turn['alerts']}")
        if expect.get("draft_reason") and expect["draft_reason"] not in turn["pending_drafts"]:
            violations.append(f"{label}: expected draft {expect['draft_reason']!r}, pending {turn['pending_drafts']}")
        if expect.get("no_draft") and turn["pending_drafts"]:
            violations.append(f"{label}: expected no pending draft, got {turn['pending_drafts']}")
    return violations


def load_fixtures(directory: str | Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(Path(directory).glob("*.json"))]
