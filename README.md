# Outrich

One app, two workspaces:

- **Outreach** - lead discovery, CSV import, and drip cold outreach. Customers
  build campaigns in a five-step builder (Find, Audience, Message, Sender,
  Review), and a durable scheduler sends personalized 1-to-1 email through
  Gmail or Pingram inside each account's send window and daily caps.
- **Freight** - a dispatch copilot. Truck profiles and missions define what the
  agent may do; inbound broker email is interpreted (Gemini, with a rules-only
  fallback), rate math and mission rules are enforced in code, and permitted
  replies are drafted or sent. Loads move toward booking with rate-con
  comparison, multi-stop routes, and a full audit trail.

Self-serve signup is open; every account's data is isolated per owner. One
verified admin (env credentials) keeps the full toolset - lead lists, the
discovery/scraper console, the template library, queue and inbox views - while
customers see a focused surface: Overview, Campaigns, one Settings page, plus
the Freight workspace.

> New to the team? [ONBOARDING.md](ONBOARDING.md) covers the 5-minute local
> setup. Working on this codebase with an agent? Start with [AGENTS.md](AGENTS.md).

---

## Quick start

### Docker (recommended for team)

```bash
cp .env.example .env   # paste your keys
docker compose up --build
```

Open http://localhost:8000. The `./app` directory is mounted for live reload.

### Local Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Both modes connect to the same Supabase project configured in `.env`
(`DATABASE_BACKEND=supabase`). With `DATABASE_BACKEND=sqlite` the app runs
entirely on a local file (`DB_PATH`, default under `/tmp`) - this is how the
test suite runs.

### Safety default

`DRY_RUN=true` in `.env.example` means no real email leaves and no credits are
consumed until you explicitly toggle it off.

---

## Testing

```bash
python -m pytest tests/ -q
```

The suite runs on SQLite, no external services needed. One frontend race test
(`tests/frontend_race.js`) runs under node when node is installed and skips
cleanly otherwise. See [tests/AGENTS.md](tests/AGENTS.md) for conventions.

## Deploy (Railway)

```bash
railway up            # build in the cloud, redeploy in ~1 minute
railway logs          # live logs
railway status
```

Railway builds the Dockerfile, runs a single replica (which owns the
APScheduler worker), and healthchecks `/health`. Your local `.env` is never
uploaded - set new variables with `railway variable set KEY=VALUE` or in the
Railway dashboard.

## Database migrations

Schema lives in `supabase/migrations/`, numbered in order. Migrations are
applied manually against the production Supabase project (SQL editor or
`supabase db push`) - deploying code does not migrate the database. Local
SQLite dev and tests create their schema from `app/schema.sql` and do not use
the migration files. The `send-gmail` Edge Function lives in
`supabase/functions/send-gmail/`.

## Environment variables

The full list with comments is in [.env.example](.env.example). The ones that
change how the app behaves:

| Variable | What it controls |
| :--- | :--- |
| `DATABASE_BACKEND` | `supabase` (production) or `sqlite` (local/tests) |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | The single verified admin login |
| `SESSION_SECRET` / `SECRET_KEY` / `ENCRYPTION_KEY` | Sessions and at-rest encryption of Gmail app passwords |
| `DRY_RUN` | When true, nothing sends and no credits are consumed |
| `GMAIL_TRANSPORT` | `auto`: direct SMTP locally, `send-gmail` Edge Function on Railway |
| `GEMINI_API_KEY` / `FREIGHT_AGENT_MODE` | Freight reply interpretation; `rules` is the offline legacy mode |
| `SERP_PROVIDER` / `SERP_API_KEY` | SerpApi lead discovery search (`SERP_PROVIDER=serpapi`) |
| `SCHEDULER_ENABLED` / `SCHEDULER_INTERVAL_MIN` | The in-process scheduler loop |

---

## How the app works

### Auth model

- Self-serve signup/login with scrypt-hashed passwords (`app/core/auth.py`);
  sessions are signed cookies, 30-day max age.
- The admin is one verified entitlement: env credentials (or a DB user with
  role `admin`) set `session["admin"]`. There is no second admin gate.
- Every signed-in request resolves an owner id; `OwnerStore`
  (`app/core/tenancy.py`) scopes every read and write to that owner. Cross-
  account access is impossible by construction, not by filter discipline in
  each route.
- Non-admins are limited to the customer surface (`customer_allowed` in
  `app/main.py`): Overview, Campaigns, Settings, Plan, imports, and Freight.
  Admin tools redirect to `/freight`.

### Billing

One plan: $25/month, both workspaces included (`app/core/billing.py`).

- Stripe Checkout subscribes, the Stripe Customer Portal manages the card and
  cancellation, and signed webhooks (`POST /webhooks/stripe`) keep account
  state in sync: `checkout.session.completed`,
  `customer.subscription.updated`/`deleted`, `invoice.paid`,
  `invoice.payment_failed`.
- With `STRIPE_*` unset the deployment runs with billing off - everyone has
  access and the suite runs green. Set `STRIPE_SECRET_KEY`,
  `STRIPE_WEBHOOK_SECRET`, and `STRIPE_PRICE_ID` to turn billing on.
- Grandfathering: migration `202609270002_stripe_billing.sql` sets
  `billing_exempt = 1` on every account that exists when it runs, so
  pre-launch accounts keep free access forever. Accounts created after need
  an active subscription (`trialing`/`active`, or `canceled` until the paid
  period ends).
- The middleware gate holds non-exempt accounts without access on `/billing`;
  their data stays untouched. The env admin is always exempt.

### Outreach: campaign flow

1. **Find** (builder step 1) queues bounded SERP scrape jobs per
   category/city; results land in the account's leads as they save. The
   in-process scheduler requires a SerpApi `SERP_API_KEY` on the same Railway
   service; no separate worker service is required. It fails clearly when
   search is not configured. Texas public records supplement only explicitly mapped
   construction trades; an unknown category never falls back to unrelated
   construction businesses. Candidate emails must be tied to the business
   and pass syntax plus MX checks. The page polls for status with backoff,
   shows discard reasons, and stops on completion or repeated errors.
2. **Audience** (step 2) filters the account's leads. A live count
   distinguishes *matching records* from *eligible to send* - eligible mirrors
   the queue's real exclusions (replied / do-not-contact / bounced, suppressed
   emails and domains, contacted inside the resend-block window), stated as
   "before message checks". The count is fail-closed: step 3 and final submit
   require a fresh successful count with at least one eligible lead, and the
   server re-validates eligibility at save and at start.
3. **Message / Sender / Review** (steps 3-5) pick active templates and sender
   identities (one provider per campaign), then confirm.
4. **Queueing** (`app/core/sender.py`) assigns each lead a sender account and a
   durable scheduled timestamp inside the account's send window, per-account
   and global daily caps, and per-sender delay jitter. The schedule is honest
   about settings: an empty send-day list means sending is off, not weekdays.
5. **Sending** (`send_due`, driven by the scheduler) renders the template,
   sends via Gmail (SMTP locally, the `send-gmail` Edge Function on Railway)
   or Pingram, and records every outcome on the durable `email_log` row.
   Campaign detail shows per-lead state: queued, delivered, replied, bounced,
   failed, skipped. Interrupted sends become `failed` for explicit review
   rather than risking an automatic duplicate.

### Freight workspace

Missions define lanes, targets, and permissions per account; truck profiles
carry the equipment facts brokers may see. Inbound broker threads are
interpreted by Gemini with recent context, then code enforces mission rules
and rate math - auto missions send permitted replies, approve missions leave
drafts. Bookings capture the agreed terms, compare the rate con against them,
and gate route changes on verification. **Freight > Agent test** exercises the
real decision engine locally with pasted broker replies and no email
transport; its JSON endpoints are documented under `/freight/agent-test` in
`app/main.py`.

### Architecture

- **FastAPI + Jinja2**, server-rendered; static CSS per workspace
  (`outreach.css`, `freight.css`) sharing one design-token set.
- **Storage**: one facade (`app/db.py`) - Supabase Postgres over REST with the
  service-role key in production, SQLite locally and in tests.
- **Work queues**: durable Postgres tables (`jobs`, `scrape_jobs`,
  `email_log`); no Redis. A single-replica APScheduler worker
  (`app/jobs/scheduler.py`) sends due email, processes scrape jobs, polls
  freight replies, and recovers stale work after a crash.
- **Adapters** (`app/adapters/`): Gmail, Pingram, SERP, Mapbox, public records.

## Repo layout

```
app/main.py            all HTTP routes + auth middleware
app/core/              domain logic (sender, schedule, freight, tenancy, importer, ...)
app/jobs/scheduler.py  the durable background worker
app/web/templates/     Jinja templates (customer/ holds the customer surface)
app/web/static/        CSS + the shared count-gate JS module
app/adapters/          external service adapters
tests/                 pytest suite (+ one node frontend test)
supabase/              migrations + the send-gmail Edge Function
seed/                  starter email templates
vertical/              product notes
```

## More docs

- [ONBOARDING.md](ONBOARDING.md) - team setup and Git workflow
- [DOCKER_AND_ENV_GUIDE.md](DOCKER_AND_ENV_GUIDE.md) - environment deep dive
- [EMAIL_TEMPLATES_GUIDE.md](EMAIL_TEMPLATES_GUIDE.md) - template variables and library
- [HANDOFF_FREIGHT_MVP.md](HANDOFF_FREIGHT_MVP.md) - freight MVP handoff notes
- [AGENTS.md](AGENTS.md) - rules for coding agents (root + subfolders)
