# AGENTS.md - tests/

pytest, unittest-style classes, SQLite only. No external services, no network.

## Running

```bash
python -m pytest tests/ -q
```

The whole suite is the merge gate - run all of it, not just your file.

## Conventions

- **Env is set at module import, before importing app modules** - see the top
  of `test_customer_ia.py`: `SCHEDULER_ENABLED=false`,
  `DATABASE_BACKEND=sqlite`, `DB_PATH` pointing at a per-FILE database under
  `/tmp`. Each test file owns its DB file; never share one across files.
- **Per-test isolation** comes from a fresh owner per test
  (`OwnerStore(self.raw, <new owner id>)`), not from wiping tables mid-file.
  When querying shared tables in assertions, filter by owner and add explicit
  `ORDER BY` - row order across tests is not stable.
- **`now_iso()`-style stamps** use the file's `STAMP` constant.
- Seeding gotchas: `clients.short_name` is NOT NULL; `email_log.subject_sent`
  / `body_sent` are NOT NULL; `email_log.campaign_id` is a real FK - insert a
  campaign row first. Templates used in queue tests need `active: True`.
- When testing core functions directly, patch or pass `storage=` with an
  in-memory/Owner store (see `MemoryStore` in `test_core.py`) - never rely on
  ambient env credentials like `GMAIL_USER`; seed explicit `gmail_senders`.

## What to test when behavior changes

- Every domain rejection (`ValueError` in `app/core`) gets a route-level test
  asserting the 303 + notice AND a direct unit test of the raise.
- Isolation regressions: for any new owner-scoped flow, assert the same read
  through a different owner comes back empty (pattern:
  `OwnerStore(self.raw, ADMIN_OWNER_ID)` or a second owner in
  `test_outreach_isolation.py`).
- UI state machines with races: extract the logic into a plain JS module
  under `app/web/static/` (UMD pattern like `count-gate.js`) and test it
  under node from pytest (`test_customer_ia.py::...::test_count_gate_latest_filter_wins_race`
  shows the subprocess pattern; skip cleanly when node is absent).
