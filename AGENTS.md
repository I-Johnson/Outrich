# AGENTS.md - Outrich

Rules for coding agents working in this repo. Subfolder AGENTS.md files add
local rules for `app/`, `app/core/`, `app/web/templates/`, and `tests/`.
Reusable playbooks live in `.claude/skills/`.

## What this app is

Outrich is one FastAPI app with two workspaces: **Outreach** (lead discovery +
drip cold email) and **Freight** (dispatch copilot). Multi-account with
per-owner data isolation; one verified env admin keeps the full toolset.
Read README.md for the product-level picture; trust the code over any doc.

## Non-negotiables

1. **Owner isolation is load-bearing.** Every request-scoped read/write goes
   through `outreach_store(request)` / `freight_store(request)` (an
   `OwnerStore`). Never touch the module-level `store` for customer data in a
   route, and never add a route that takes an owner id from client input.
   Legacy rows belong to `ADMIN_OWNER_ID`. See `.claude/skills/data-isolation/`.
2. **Run the full suite before pushing**: `python -m pytest tests/ -q`.
   There is no CI; the local suite is the merge gate. Keep it green - no
   "pre-existing failure" excuses.
3. **Shared-database falsy defaults are a bug class here.** A saved empty
   list (`send_days: []`) means "off", not "use the default". Default only on
   missing/`None`. This exact bug shipped once (the scheduler kept sending on
   weekdays while the UI said sending was off). Grep for `or [`, `or {` when
   you touch config reads.
4. **Fail closed.** UI promises must match backend behavior: if the builder
   says an audience is eligible, the queue must reach exactly those leads
   (`AUDIENCE_SCAN_LIMIT`, `eligible_client_ids` are the single source of
   truth - do not fork the rules). If a check can't run, block the action and
   say so; never proceed on a stale or assumed pass.
5. **No new external dependencies** without a clear need - the requirements
   list is deliberately small. No Redis, no ORM, no frontend framework.

## Conventions

- **Routes**: all HTTP routes live in `app/main.py` (one file, ~2k lines -
  navigate by grep). Redirects carry user-facing notices as `?notice=...`.
- **Auth**: session cookie via `SessionMiddleware`; `is_admin(request)` is the
  only admin check; `customer_allowed(path)` gates the customer surface.
- **Storage**: `app/db.py` facade. JSON columns are declared in `JSON_FIELDS`;
  add new JSON columns there or they come back as strings on Supabase.
- **Migrations**: numbered SQL in `supabase/migrations/`, applied manually to
  prod. Local SQLite/tests use `app/schema.sql` - keep the two in sync when
  you change schema.
- **UI**: no emojis anywhere in the product UI. Server-rendered Jinja;
  dynamic values go into DOM text nodes, never `innerHTML`. Design tokens
  (pine/fog/paper palette, Bricolage Grotesque + Instrument Sans) live at the
  top of `app/web/static/outreach.css` and are shared by `freight.css`.
- **Sending**: never send email in a request handler. Queue durable
  `email_log` rows and let the scheduler send. `DRY_RUN` must stay honored.
- **Errors in async work**: interrupted sends and stale jobs become `failed`
  with a review path - never silently requeue something that may have sent.

## Workflow

- Branch, commit, open a PR to `main`, merge when the suite is green.
- Commits: imperative subject, body explains *why* when it isn't obvious.
- UI changes: mock first, and verify rendered output (screenshot the affected
  pages headlessly) before calling it done.
- Docs: update README/AGENTS.md in the same PR when behavior changes. No
  aspirational docs - describe what the code does today.

## Where things live

| Area | Start here |
| :--- | :--- |
| Routes + middleware | `app/main.py` (see `app/AGENTS.md`) |
| Queue/send/schedule rules | `app/core/sender.py`, `app/core/schedule.py` (`app/core/AGENTS.md`) |
| Owner isolation | `app/core/tenancy.py` |
| Customer UI | `app/web/templates/customer/` (`app/web/templates/AGENTS.md`) |
| Freight agent | `app/core/freight.py`, `app/core/freight_agent.py` |
| Background worker | `app/jobs/scheduler.py` |
| Tests | `tests/` (`tests/AGENTS.md`) |
