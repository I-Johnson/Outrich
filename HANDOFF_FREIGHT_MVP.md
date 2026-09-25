# Freight MVP handoff

Last updated: 2026-09-23

## User direction

- Keep existing Outreach behavior intact.
- Add a top-left Outreach / Freight workspace switch.
- Freight supports multiple missions and truck profiles.
- A mission stores the truck's operating rules, destinations, and negotiation envelope.
- The first email is initiated by the user from the platform.
- After setup, the send flow must be extremely fast: select **truck profile**, select **mission**, paste **broker email**, send. Do not ask for load details in the composer.
- Freight and Outreach settings must be separate.
- Monitor Gmail replies, draft or send only explicitly permitted counters/profile facts, and create in-app alerts for protected events.
- Do not push or commit code.

## Implemented before the latest adjustment

- Freight database model for profiles, missions, loads, threads, messages, drafts, alerts, and IMAP cursors.
- Supabase migration: `supabase/migrations/202609230001_freight_mvp.sql`.
- Freight-only template filtering; Outreach campaigns only receive Outreach templates.
- Gmail SMTP threading headers and Gmail IMAP reply monitoring.
- Reply classification, rate extraction, negotiation-envelope evaluation, protected alerts, and permission-gated auto-send.
- DAT-style load/conversation table.
- Mission/profile configuration UI.
- Outreach/Freight workspace switch.
- Tests in `tests/test_freight.py`.
- Before the latest request, all 52 tests passed and all Jinja templates compiled.

## Latest request: work already applied

The user reported two issues:

1. Freight was inheriting Outreach sender/signature settings, causing `Missing: signature`.
2. The new-load composer had too many fields.

Changes already applied for this request:

- Added a separate singleton `freight_settings` table in SQLite schema and the Supabase migration.
- Added Freight settings helpers in `app/core/freight.py`:
  - `seed_freight_settings`
  - `freight_config`
  - `freight_sender_context`
- Freight sender name, signature, reply-to, default Gmail sender, and default Freight template are now separate from Outreach identity settings.
- Added `/freight/settings` GET and POST routes.
- Added `app/web/templates/freight_settings.html`.
- Changed the Freight sidebar link from Outreach settings to Freight settings.
- Simplified the Freight composer to only:
  - truck profile
  - mission
  - broker email
  - Send email
- The send route now derives origin, equipment, pickup window, sender, and template from the selected profile/mission/Freight settings.
- Changed the seeded first-touch template to a generic fast inquiry that does not require manually entering route/rate/miles:
  - Subject: `Truck available — {{equipment}}`
  - Requests pickup, delivery, miles, weight, and rate from the broker.

## Verification passed

1. All 54 tests pass:
   ```bash
   DATABASE_BACKEND=sqlite SCHEDULER_ENABLED=false GMAIL_TRANSPORT=smtp RAILWAY_ENVIRONMENT= DB_PATH=/tmp/outreach-freight-test.db uv run --with-requirements requirements.txt python -m unittest discover -s tests -v
   ```
2. All 16 Jinja templates compiled successfully.
3. `git diff --check` passed clean.
4. Smoke-tested with authenticated test client:
   - `/freight` dashboard: renders loads and fast composer.
   - `/freight/settings`: separate sender name, email signature, reply-to, default template, and default sender account.
   - `/templates/{id}/preview` (GET and POST): renders Freight preview with Freight signature without `Missing: signature`.
   - Fast load inquiry send (`/freight/loads/send`) with only `truck_profile_id`, `mission_id`, and `broker_email`: passes validation, auto-populates load details from mission/profile, and dispatches first touch in dry-run mode.

## Local development

Run the app against the shared Supabase database so local and production show
the same freight data. Keep the scheduler off locally to avoid duplicate work
with Railway:

`http://127.0.0.1:8000`

Command:

```bash
DATABASE_BACKEND=supabase SCHEDULER_ENABLED=false DRY_RUN=true uv run --with-requirements requirements.txt uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Deployment status

- No live email was sent during development.
- Local development uses the same Supabase database as production.

## Main files changed

- `app/core/freight.py`
- `app/main.py`
- `app/schema.sql`
- `app/db.py`
- `app/jobs/scheduler.py`
- `app/web/templates/base.html`
- `app/web/templates/freight.html`
- `app/web/templates/freight_missions.html`
- `app/web/templates/freight_settings.html`
- `app/web/templates/templates.html`
- `app/web/templates/template_preview.html`
- `app/web/static/style.css`
- `app/adapters/base.py`
- `app/adapters/gmail_adapter.py`
- `app/adapters/pingram_adapter.py`
- `supabase/functions/send-gmail/index.ts`
- `supabase/migrations/202609230001_freight_mvp.sql`
- `tests/test_freight.py`
- `vertical/product.md`, `vertical/problem.md`, `vertical/broader_idea.md`

## Safety behavior that must remain

- Calls, sensitive driver info, apparent price acceptance, and rate confirmations always alert the trucker.
- Facts are only answered from the saved profile and only when explicitly shareable.
- Automatic counters/profile answers/passes only send when the matching mission permission is enabled.
- Anything ambiguous becomes an in-app alert or draft.
- Do not scrape DAT or automate its UI in this MVP.

## 2026-09-23 negotiation hardening

- Added typed numeric extraction: total rate, rate per mile, phone, time, weight, miles, date, and unknown. Unknown or conflicting rates require review; per-mile offers require confirmed loaded miles.
- Added durable offer/counter events. Only sent counter drafts advance the counter count; factual replies and passes do not.
- Added explicit `negotiating`, `passed`, `mismatch`, `offer_review`, `accepted_pending_review`, `protected_review`, `booked`, and `closed` states. A better offer can reopen a passed load; booked and protected conversations cannot auto-counter.
- Added post-reply load verification for pickup, destination, equipment, timing, miles, deadhead, and weight. The initial inquiry still requires only truck profile, mission, and broker email. Auto-counters remain drafts until fit is verified.
- Added stale-draft checks, atomic draft send claims, interrupted-send quarantine, unique dry-run message IDs, and unambiguous Gmail thread matching.
- Added scenario replays for freight language and multi-turn negotiation in `tests/test_freight.py`.

### Before enabling the pilot

1. Apply `supabase/migrations/202609230001_freight_mvp.sql`, then `supabase/migrations/202609230002_freight_negotiation_safety.sql` to production Supabase.
2. Confirm the sender can send a first inquiry and that a reply appears in the matching Freight conversation. Use a mailbox you control for this smoke test.
3. Begin with mission auto-send permissions off. Review drafts and alerts. Enable automatic factual replies or counters for a mission only after its profile, price boundaries, and load verification flow are checked.
4. Keep price acceptance, rate confirmations, calls, sensitive driver details, ambiguous numbers, and mismatches in human review.

No production migration or live email send was performed during this hardening work.
