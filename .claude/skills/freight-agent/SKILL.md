---
name: freight-agent
description: How the Outrich freight copilot works - missions, broker threads, drafts, bookings - and how to exercise it without email. Use for any change under freight routes or app/core/freight*.
---

# Freight agent

Code: `app/core/freight.py`, `app/core/freight_agent.py`,
`app/core/freight_replay.py`, `app/core/rate_con.py`; routes under
`/freight/...` in `app/main.py`; templates `freight*.html`,
`agent_test.html`.

## Model

- **Truck profiles**: equipment facts; `shareable_fields` controls what
  brokers may see.
- **Missions**: lanes (`destinations`), targets (total or all-in RPM), and
  `permissions`. Auto missions may send permitted replies immediately;
  approve missions leave drafts for a human. Auto mode requires exactly one
  destination lane and a saved target; ambiguous missions pause for review.
- **Threads**: inbound broker mail is polled by the scheduler
  (`poll_freight_replies`), interpreted by Gemini with recent thread context
  (`FREIGHT_AGENT_MODE=rules` is the offline fallback), then code - not the
  model - enforces rates, rules, and send permission. Model failure pauses
  the thread; it never guesses a send.
- **Bookings**: agreed terms snapshot, rate-con upload and diff
  (`rate_con_diffs`), route changes gated on verification; full event audit
  in `freight_negotiation_events`.

## Working on it safely

- Exercise changes through **Freight > Agent test**
  (`/freight/agent-test/...`): real `evaluate_inbound` path, pasted broker
  replies, no email transport. `GET .../scenarios` returns descriptive
  rule-checklist presets for API clients.
- Uncertainty always routes to a human-visible state (`paused` / draft /
  alert), never to a silent drop or an automatic send.
- Sends recover like outreach: `recover_uncertain_freight_sends` marks
  unknowable outcomes failed rather than re-sending.
