---
name: testing
description: How to run and extend the Outrich test suite. Use whenever changing behavior, before pushing, or when a test fails unexpectedly.
---

# Testing

Run the whole suite before every push - it is the merge gate (no CI):

```bash
python -m pytest tests/ -q
```

## Layout

- `test_core.py` - sender/scheduler unit tests with an in-memory
  `MemoryStore` (patch `app.core.sender.store` or pass `storage=`).
- `test_customer_ia.py` - the customer surface end to end through
  `fastapi.testclient` (signup, campaigns, settings, imports).
- `test_outreach_isolation.py` - cross-owner isolation proofs.
- `test_freight*.py`, `test_agent_test.py` - freight engine and agent-test.
- `frontend_race.js` - node test for `count-gate.js`, driven from pytest;
  skips when node is missing.

## Rules

- Env at module import before app imports; `DATABASE_BACKEND=sqlite`, a
  per-FILE `DB_PATH`, `SCHEDULER_ENABLED=false`.
- Per-test isolation via a fresh owner id, not table wipes. Filter shared
  tables by owner and add `ORDER BY` in assertions.
- NOT NULL gotchas: `clients.short_name`, `email_log.subject_sent` /
  `body_sent`; `email_log.campaign_id` is a real FK - insert the campaign.
  Queue tests need an `active` template and explicit seeded `gmail_senders`.
- Domain rejections get two tests: the direct `ValueError` and the route's
  303 + `?notice=`.
- A flake is a bug: shared per-file DBs mean order-dependent assertions will
  eventually fail - fix the assertion, don't retry the run.
