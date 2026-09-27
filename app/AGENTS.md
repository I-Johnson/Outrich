# AGENTS.md - app/

## main.py (all routes)

- Every route is here. ~2k lines: grep for the path, don't scroll.
- **Scoping**: request handlers get their store from `outreach_store(request)`
  or `freight_store(request)` (both return `OwnerStore` over the session
  owner, 401 when signed out). The bare module `store` is for startup seeding
  and admin-only legacy paths - using it for customer data is the isolation
  bug this codebase already fixed once; `importer.py` and campaign
  start/state/delete needed explicit `storage=` threading for the same reason.
  When you call into `app/core` from a route, pass `storage=s`.
- **Access control**: the `admin_auth` middleware runs first. Signed-out
  users get the landing page at `/` and redirects to `/login` elsewhere.
  Non-admins are limited by `customer_allowed(path)` - when you add a route,
  decide deliberately which bucket it falls into: `USER_PATHS`,
  `CUSTOMER_OUTREACH_EXACT/PREFIXES`, `CUSTOMER_ADMIN_ONLY_PREFIXES`, or
  admin-only by default. Freight paths are open to all signed-in users.
- **Notices**: user-facing outcomes ride redirects as `?notice=...`
  (URL-quoted). Mutations that can fail domain checks (`queue_campaign`,
  `delete_campaign`, `set_client_status`) raise `ValueError`; catch it and
  redirect with the message. Don't swallow these into 500s.
- **`page()`** injects `workspace`, `is_admin`, and the owner's scoped
  settings into every template context - use it rather than `TemplateResponse`
  directly.

## config.py / db.py

- `Settings` reads env once at import. New env vars need an entry here, a line
  in `.env.example`, and (for prod) `railway variable set` at deploy time.
- `db.py` is the whole storage layer: `SQLiteStore` locally/tests, Supabase
  REST in prod, behind one interface (`get/list/insert/update/delete`,
  `list` supports exact filters plus `("in", [...])` / `("lte", ...)`
  tuples). JSON columns must be registered in `JSON_FIELDS` or Supabase
  returns them as strings.
- Schema changes: update `app/schema.sql` AND add a numbered file in
  `supabase/migrations/`. Tests and local dev only see `schema.sql`.

## jobs/scheduler.py

- Single-replica APScheduler worker: `send_due` per owner, scrape jobs,
  freight reply polling, stale-job recovery (15-min leases, 3 attempts,
  exponential requeue). `SCHEDULER_ENABLED=false` in tests.
- Anything that sends email, scrapes, or polls runs here on durable rows -
  never as fire-and-forget async from a request handler.
