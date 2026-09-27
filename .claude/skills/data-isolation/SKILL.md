---
name: data-isolation
description: Owner-scoping rules for Outrich. Use when adding or touching any route, core function, table, or query that reads or writes account data.
---

# Data isolation

Every account's data is isolated by `owner_id` through `OwnerStore`
(`app/core/tenancy.py`). Isolation comes from the store, not from filters
remembered per query.

## Checklist for any new data path

1. In a route: `s = outreach_store(request)` or `freight_store(request)` -
   never the module-level `store` from `app.db`.
2. Calling into `app/core`? Pass `storage=s`. Core functions default to the
   module store for legacy/admin callers; that default is wrong for customer
   requests. (History: `importer.py` preview/confirm/remap/undo and campaign
   start/state/delete all shipped this bug.)
3. New table with customer data:
   - `owner_id` column + index in `app/schema.sql` AND a numbered
     `supabase/migrations/` file (applied to prod manually).
   - Register in `app/core/tenancy.py` scoping (and the freight-table list in
     `app/db.py` if it is a freight table; JSON columns in `JSON_FIELDS`).
   - Background work on it must join the per-owner loops in
     `app/jobs/scheduler.py` via `outreach_owner_ids` / `freight_owner_ids`.
4. Never accept an owner id from client input. Owner comes from the session
   only (`current_owner`).
5. Legacy rows have no `owner_id`; they belong to `ADMIN_OWNER_ID`. Admin
   sees them through the same `OwnerStore` path.

## Test it

For every new owner-scoped flow, assert a second owner sees nothing - the
pattern is one line: `self.assertEqual(OwnerStore(self.raw, other_owner).list("table"), [])`.
Isolation regressions live in `tests/test_outreach_isolation.py`.
