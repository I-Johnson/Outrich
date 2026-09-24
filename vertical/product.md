# Product: Outreach Autopilot

Read `problem.md` first.

## Summary

An outreach and negotiation autopilot per customer. In the practical first version, a trucker:

1. connects Gmail with a dedicated Google app password,
2. pastes a broker email address and the available load details into our platform,
3. sends the first email from our platform, and
4. defines a negotiation envelope and saved truck profile that govern every agent reply.

The system detects the outgoing Gmail conversation, monitors incoming replies, evaluates offers using total pay, loaded miles, deadhead miles and rate per all-in mile, then drafts or sends only the counters permitted by the trucker's envelope. It answers factual questions only from the saved truck profile. Missing facts, calls, sensitive driver-information requests, apparent price acceptance and rate confirmations are escalated to the trucker.

**Wedge vertical:** owner-operator truckers on DAT One. Build it vertically: trucking-specific connector and playbooks on top of a vertical-agnostic core. A new vertical = a new connector + new playbooks. Nothing downstream changes.

**The safety rule, everywhere:** the agent may negotiate inside the customer's explicit envelope, inform using verified profile facts, and ask clarifying questions. It may not invent facts or complete a binding booking. Calls, sensitive information, apparent acceptance, rate confirmations, contract language, dates and guarantees always escalate.

---

## 1. Domain background: DAT One

What a coding agent needs to know about the source.

- DAT One is the largest freight load board in North America. Brokers/shippers post loads; carriers search and contact brokers. About 722,500 loads per business day.
- Carriers search by lane (origin/destination), equipment type, dates, and filters. DAT documents real-time results and load-match alarms inside DAT One. Public documentation does not establish a general email feed for new open-market matches.
- Each load lists a **contact method**: email (a large share) or phone.
- **Connected Email:** the carrier links their Gmail/Outlook once inside DAT, then on any email-contact load hits "Send Email," gets a pre-filled message with load details, edits, and sends from their own address. This is exactly the workflow we automate.
- The web app (`one.dat.com/search-loads-ow`) is behind login.
- Carrier pricing (monthly, flat): Standard $59, Enhanced $149 (adds broker credit scores, 15-day lane rate averages), Pro $169 (7-day rate averages, TriHaul), Select $259 (fleets 3+), Office $339 (fleets 10+). DAT's carriers page has shown older promo prices ($54/$119/$239), so treat these as approximate.
- DAT has official Load Board APIs and certified Connexion integrations. Automatic DAT discovery is a later approved integration track; it is not required for the first version's email workflow.
- Competitor / proof of niche: dispatchGo, a Chrome extension adding one-click broker emails on DAT listings via Gmail.

Sources:
- Connected Email: https://one.support.dat.com/7-search-trucks-1e508202/connected-email-3d563b08
- Carrier quick start: https://one.support.dat.com/dat-one-for-carrier-quick-start-guide-e203241a
- Pricing: https://www.dat.com/load-boards and https://www.dat.com/carriers
- dispatchGo: https://dispatchgo.app

The first version does not depend on DAT alarm emails or browser automation. The trucker finds a load, copies the broker email address and enters the load facts needed for evaluation. An approved DAT API/alarm connector can automate this intake later without changing the downstream conversation engine.

---

## 2. Architecture overview

```
Broker email + load facts pasted into our platform
                    │
                    ▼
          Opportunity + negotiation envelope
                    │
                    ▼
       First email sent through Gmail SMTP
                    │
                    ▼
       Store Message-ID + References + thread state
                    │
                    ▼
      Gmail IMAP monitor detects broker replies
                    │
                    ▼
       Classify reply + extract rate/questions
                    │
       ┌────────────┼─────────────────┐
       ▼            ▼                 ▼
Evaluate rate   Answer verified   Alert trucker
and counter     profile facts     for protected events
       │            │
       └──────► platform draft or permitted auto-send
```

Five components:

1. **Opportunity intake** (vertical-specific). The first version is a small form for broker email and load facts; approved DAT automation is a later connector.
2. **Ingest + validation + dedupe** (core). Validate required facts, calculate economics, and avoid duplicate broker contact.
3. **Playbook engine** (core engine, vertical-specific content). Trigger filter + template + sending identity + pacing + follow-up sequence.
4. **Conversation handler** (core). Match replies to threads, classify, auto-answer within scope or escalate.
5. **Escalation inbox** (core). SMS first; a web view later. The human sees only what needs them, with context, and can take over in one reply.

---

## 3. Customer connections

### 3.1 Gmail pilot connection

- The customer enters their Gmail address and a dedicated 16-character Google app password. Never request or store their normal Google password.
- Store the app password encrypted at rest and never return it through the API after setup.
- Use Gmail SMTP over TLS to send from the platform and Gmail IMAP over SSL to read replies and existing conversations.
- The app password requires 2-Step Verification and may be unavailable for some managed or Advanced Protection accounts.
- A Google password change revokes existing app passwords, so the connection must expose a clear `needs_reconnect` state.
- Put this behind a `MailProvider` interface. Replace the pilot credential flow with Gmail OAuth before broad public onboarding.

### 3.2 Outgoing thread tracking and incoming monitoring

- Every platform send creates and stores a stable RFC `Message-ID` plus the normalized subject, recipient and opportunity ID.
- Replies are matched using `In-Reply-To`, `References`, sender/recipient and Gmail's IMAP thread identifiers where available.
- An IMAP monitor checks for new messages and fetches only messages connected to threads initiated or explicitly imported by our platform. Unrelated inbox contents are ignored and not stored.
- Use IMAP IDLE where the worker runtime supports a durable connection, with a short periodic poll and cursor as recovery.
- Process every provider message idempotently so reconnects and repeated IMAP results cannot trigger duplicate replies.
- A draft in the pilot means a draft stored and reviewable inside our platform. Gmail-native drafts and Gmail push notifications belong to the later OAuth connector.

### 3.3 Load intake in the first version

- The user pastes the broker email address into our platform.
- The form captures origin, destination, pickup date, loaded miles, deadhead miles, offered/posted pay when known, equipment and optional DAT reference.
- Before sending, the platform shows calculated loaded-mile RPM and all-in RPM (`pay / (loaded miles + deadhead miles)`).
- The user may send immediately, save an internal draft, or enable the configured first-touch template.
- We never ask for DAT credentials and do not automate the DAT website.

### 3.4 SMS (escalation channel)

- Twilio (or similar) number. Outbound SMS for escalations, inbound webhook `POST /webhooks/sms` for the customer's replies.
- Customer's phone number is verified at onboarding (one-time code).

---

## 4. Opportunity intake and later DAT connector

### 4.1 Responsibility

First-version input: broker email address plus load facts entered by the user. Output: one normalized `Opportunity` linked to the outgoing conversation.

Later input: an authorized DAT API/alarm event. It produces the same `Opportunity`, so the negotiation workflow does not change.

### 4.2 Intake validation

- Require a valid broker email address and enough lane information to identify the load in the subject/body.
- Allow unknown values, but show exactly which economics or policy checks cannot run without them.
- Never infer missing mileage, equipment, location or driver facts in an outgoing message.
- Recalculate the opportunity whenever pay, loaded miles or deadhead changes during negotiation.

### 4.4 Normalization (trucking payload)

```json
{
  "vertical": "trucking",
  "source": "manual_dat_email",
  "external_id": "DAT load ID or hash(origin,dest,pickup,broker,rate)",
  "counterparty": { "org": "ABC Logistics", "name": null, "email": "loads@abclogistics.com", "phone": "+15555550100" },
  "posted_at": null,
  "received_at": "2026-09-22T19:04:12-07:00",
  "payload": {
    "origin": { "city": "Dallas", "state": "TX" },
    "destination": { "city": "Atlanta", "state": "GA" },
    "pickup_date": "2026-09-23",
    "equipment": "V",
    "length_ft": 53,
    "weight_lbs": 42000,
    "rate_usd": 2100,
    "trip_miles": 781,
    "deadhead_miles": 42,
    "loaded_rate_per_mile": 2.69,
    "all_in_rate_per_mile": 2.55,
    "deep_link": "https://..."
  }
}
```

---

## 5. Data model sketch (Postgres)

Core tables are vertical-agnostic. Vertical specifics live in `payload JSONB`, validated by a per-vertical JSON schema.

```sql
customers (
  id uuid pk, name text, vertical text,            -- 'trucking'
  phone_e164 text, timezone text,
  mode text,                                       -- 'draft_only' | 'auto_send'
  created_at timestamptz
)

customer_profile (                                 -- facts the agent may state
  customer_id uuid pk fk,
  data jsonb                                       -- trucking: MC#, DOT#, equipment, home base,
                                                   -- signature, insurance carrier, etc.
)

mail_accounts (
  id uuid pk, customer_id fk, provider text,       -- 'gmail' | 'outlook'
  email text, auth_type text,                      -- 'app_password' | 'oauth'
  credential_enc bytea,                           -- encrypted app password or refresh token
  inbound_cursor text, status text, last_checked_at timestamptz
)

documents (                                        -- files the agent may attach
  id uuid pk, customer_id fk, kind text,           -- 'coi' | 'w9' | 'mc_authority'
  storage_key text, expires_on date, updated_at timestamptz
)

source_events (                                    -- raw inbound, for re-parse/audit
  id uuid pk, customer_id fk, source text,
  provider_message_id text unique, received_at timestamptz,
  parser_version text, parse_status text, error text
)

opportunities (
  id uuid pk, customer_id fk, vertical text, source text,
  external_id text, dedupe_key text,
  counterparty_org text, counterparty_email text, counterparty_phone text,
  posted_at timestamptz, received_at timestamptz,
  payload jsonb, status text,                      -- 'new' | 'matched' | 'skipped' | 'contacted' | 'won' | 'lost' | 'expired'
  source_event_id fk,
  unique (customer_id, dedupe_key)
)

negotiation_envelopes (
  id uuid pk, opportunity_id fk,
  floor_total numeric, target_total numeric,
  floor_all_in_rpm numeric, target_all_in_rpm numeric,
  counter_strategy text, counter_value numeric,
  maximum_rounds int, current_round int,
  permissions jsonb,                              -- auto-pass/counter/factual-reply flags
  created_at timestamptz, updated_at timestamptz
)

playbooks (
  id uuid pk, customer_id fk, kind text,           -- see section 6
  enabled bool, trigger jsonb,                     -- filter rules
  template_id fk, pacing jsonb, followups jsonb,
  allowed_actions jsonb                            -- what auto-replies may do
)

templates (
  id uuid pk, customer_id fk null,                 -- null = vertical default
  vertical text, kind text, subject text, body text,
  variables text[]                                 -- whitelist
)

threads (
  id uuid pk, customer_id fk, opportunity_id fk,
  mail_account_id fk, provider_thread_id text,
  root_message_id text, last_message_id text,
  state text,                                      -- 'drafted' | 'sent' | 'replied' | 'escalated' | 'closed'
  last_activity_at timestamptz
)

messages (
  id uuid pk, thread_id fk, direction text,        -- 'out' | 'in'
  provider_message_id text, playbook_id fk null,
  body_text text, classification jsonb null,
  sent_by text,                                    -- 'agent' | 'customer' | 'counterparty'
  created_at timestamptz
)

escalations (
  id uuid pk, customer_id fk, thread_id fk,
  reason text,                                     -- 'rate_out_of_band' | 'contract' | 'date_commit' | ...
  summary text, status text,                       -- 'open' | 'answered' | 'expired'
  sms_message_id text, created_at timestamptz, resolved_at timestamptz
)

audit_log (id, customer_id, actor, action, ref, data jsonb, at)
```

Dedupe key (trucking): DAT load ID when present, else `hash(origin, destination, pickup_date, equipment, counterparty_org, rate)`. Also suppress re-contacting the same broker for the same lane within N hours even if the load was reposted.

---

## 6. Playbooks

A playbook = trigger + template + allowed actions + pacing + follow-ups. Engine is generic; content is per vertical.

### 6.1 Trucking playbooks

| Kind | Trigger | What it does | Auto-send allowed? |
|---|---|---|---|
| `first_touch` | User enters a broker email and load facts, then chooses Send | Short email containing the identifying lane/date/reference and verified carrier facts | User-triggered send in the MVP |
| `followup_bump` | No reply after X minutes and load not expired | One short nudge in the same thread | Yes, max 1 |
| `rate_counter` | Broker provides an offer and the negotiation envelope permits a counter | Calculates loaded/all-in RPM and drafts or sends the permitted pass/counter response | Yes, only inside the explicit envelope and round limits |
| `profile_fact_reply` | Broker asks about equipment, team status, MC/DOT or another approved fact | Answers from the selected truck/carrier profile | Yes if the fact exists, is current and is marked shareable |
| `document_reply` | Broker asks for insurance cert, W-9, MC authority, etc. | Attaches the stored document | Yes, if doc exists and not expired; else escalate |
| `booking_confirmation` | Customer explicitly approves the final action | Sends the customer's approved words into the thread | Only relaying the customer's decision |
| `nurture` | Thread closed without a load, broker was responsive | Periodic "truck available on your lanes" note | Yes, low frequency, opt-in |

### 6.2 Rules the matcher evaluates (trucking)

```json
{
  "lanes": [{ "origin_states": ["TX"], "dest_states": ["GA","FL"] }],
  "equipment": ["V"],
  "min_rate_usd": 1800,
  "min_rate_per_mile": 2.25,
  "rate_basis": "all_in_miles",
  "target_rate_usd": 2400,
  "counter_strategy": "configured_amount",
  "counter_amount_usd": 2500,
  "maximum_counter_rounds": 3,
  "allow_automatic_pass": true,
  "allow_automatic_counter": false,
  "pickup_within_hours": 48,
  "exclude_counterparties": ["Bad Broker LLC"],
  "rate_band": { "floor_per_mile": 2.25, "target_per_mile": 2.60 }
}
```

The envelope is configuration, not inferred judgment. The user may choose total dollars, loaded-mile RPM, all-in RPM, or a combination. If required mileage is missing, the agent drafts but does not auto-send a rate response.

### 6.3 Templates

- Plain text, short, sounds like a trucker, not a marketer.
- Whitelisted variables only (`{{origin_city}}`, `{{pickup_date}}`, `{{mc_number}}`, `{{truck_location}}`, ...). Rendering fails closed if a variable is missing. No free-form LLM text in first-touch for MVP.
- LLM may classify terse or misspelled broker replies, but pricing calculations and permission checks are deterministic. Generated text is checked against the envelope and protected-event rules before sending.
- Every reply runs a profile preflight. If the reply needs a fact that is missing, stale or not marked shareable, the agent pauses and asks the user to complete that profile field.
- Reuse from ContractorOps: template engine (whitelisted variables, plain text, paced sends) and dedupe/queue discipline.

### 6.4 Pacing and deliverability

- First-touch goes out immediately (speed is the product) but with a per-customer cap per minute/hour and a per-broker cap per day.
- Respect Gmail sending limits; back off on 429s.
- Send as a normal reply-able message (proper `Message-ID`, stored `threadId`) so broker replies thread correctly.

---

## 7. On-demand search ("find me loads Dallas to Atlanta")

Three tiers, in order:

1. **Answer from our store.** Every alarm is ingested, so recent loads (last hours) are already in `opportunities`. Query by lane/equipment/date, rank by rate/$ per mile/freshness. Fast and free. This handles most asks.
2. **Adjust saved searches.** If the lane is not covered, guide the customer (or later, do it with their permission) to add a DAT saved search with alarms. From then on the stream covers it continuously.
3. **Live browser query (last resort, not MVP).** Drive the customer's DAT session with browser automation for a one-off search. Fragile (session expiry, rate limits, template changes) and risks the customer's account under DAT's terms. Only with explicit customer consent, low frequency, never as a background poller.

The customer asks by SMS (later: web/app). A small intent parser turns the ask into a store query.

---

## 8. Conversation handling and escalation

### 8.1 Reply pipeline

1. Gmail IMAP monitor → message in a known thread → load thread + opportunity + negotiation envelope.
2. Classify the reply (LLM with a fixed label set + extracted fields), e.g.:
   - `doc_request` (which doc)
   - `availability_question`
   - `rate_offer` (amount) / `rate_question`
   - `booked_elsewhere` / `decline`
   - `not_now`
   - `price_appears_accepted`
   - `rate_confirmation_attached`
   - `call_requested`
   - `sensitive_driver_info_requested`
   - `equipment_or_profile_question`
   - `contract_or_terms`
   - `date_or_time_commitment`
   - `other`
3. Recalculate the current offer using loaded and all-in mileage when the required inputs exist.
4. Decide: create a platform draft, auto-send a permitted counter/factual response, close the thread, or alert the trucker.

### 8.2 The commit rule (hard-coded, not a prompt)

The agent may **inform** using saved profile facts and **negotiate** only inside the customer's explicit envelope. It may not invent a missing fact or complete a binding booking.

Always escalate:
- Broker asks to call or moves the conversation to phone.
- Broker requests sensitive driver information.
- Broker appears to accept the carrier's price or asks for final acceptance.
- Any contract, rate confirmation, terms, or agreement language or attachment.
- Any pickup/delivery date or time commitment.
- Any guarantee (on-time, capacity, claims).
- A required truck/profile fact is missing, stale or not authorized for sharing.
- The proposed counter falls outside the configured envelope or exceeds the maximum rounds.
- Classifier confidence below threshold, or the reply is ambiguous.
- Missing or expired document.

Implement as a deterministic policy check on the classifier output and every outgoing draft. A dollar amount is allowed only when it is the exact result of the active envelope. Unapproved dates, commitments, sensitive fields and acceptance language are blocked and escalated.

### 8.3 Escalation by SMS

```
ABC Logistics, Dallas TX -> Atlanta GA, pickup Wed 9/23
They offered $1,950: $2.50/loaded mi and $2.36/all-in mi including 45 deadhead mi.
Your floor is $2.25/all-in mi and target is $2.60. Draft counter: $2,150.
Review, send, edit, or take over.
```

- Customer's SMS reply is mapped to the open escalation (one open escalation at a time per customer for MVP, or short codes like `#12 YES`).
- `YES/NO` map to prewritten playbook replies; free text is sent into the thread as written (light formatting only, no rewording of commitments).
- Escalations expire when the load is stale; the customer is told.

---

## 9. Modes

- **Review mode (onboarding default).** Every agent response is stored as a platform draft for review. The original user-triggered first email can still be sent directly from the platform.
- **Permitted auto-send.** The customer enables individual actions such as profile answers, passes or counters. The envelope and protected-event rules still apply.
- Global kill switch per customer (SMS `STOP AUTOPILOT`).

---

## 10. Vertical extensibility

Contract every connector implements:

```ts
interface OpportunitySource {
  vertical: string;                 // 'trucking' | 'contractors' | ...
  source: string;                   // 'manual_dat_email' | 'dat_api_alarm' | ...
  normalize(input: unknown): Opportunity[];
  payloadSchema: JSONSchema;
}

interface MailProvider {            // gmail now, outlook later
  monitor(account, cursor): Promise<RawMailMessage[]>;
  fetchNew(account, cursor): Promise<RawMailMessage[]>;
  send(account, draft): Promise<SentRef>;
}
```

Platform drafts are stored by the core conversation service rather than delegated to the pilot mail provider.

A new vertical ships:
- one or more `SourceConnector`s,
- a payload JSON schema,
- default templates and playbook presets,
- vertical-specific classifier labels (added to the shared set) and commit rules (e.g. contractors: quotes, scope, start dates).

Nothing in ingest, store, matcher, send queue, conversation handler, or escalation changes. ContractorOps lead inboxes are the natural second vertical.

---

## 11. Services and integrations needed

| Need | Choice (suggested) |
|---|---|
| Pilot mail read/send | Gmail IMAP + SMTP using an encrypted dedicated app password |
| Production Gmail | Gmail OAuth/API with push notifications after verification work |
| Mail (later) | Microsoft Graph mail + change notifications |
| SMS | Twilio Programmable Messaging |
| DB | Postgres |
| Queue | Postgres-backed queue (e.g. pg-boss / graphile-worker) or Redis/BullMQ |
| Secrets | Cloud KMS for token encryption |
| Document storage | S3/GCS (COI, W-9) |
| LLM | Reply classification + constrained reply filling only |
| Web | Minimal onboarding UI: connect Gmail, verify phone, set rules, upload docs |

Workers: Gmail IMAP monitor, poll/reconnect recovery, send queue, stale-thread expiration and follow-up scheduler. Webhook: `POST /webhooks/sms` if SMS alerts are enabled.

---

## 12. MVP scope

**In:**
- One customer (the trucker).
- Gmail connection using a dedicated app password, encrypted at rest.
- Compose screen where the user pastes the broker email, enters load facts, sees pay/mileage calculations and sends from the platform.
- SMTP sending with stored `Message-ID`/thread metadata.
- IMAP monitoring of incoming replies and existing threads explicitly imported by the user.
- Opportunity store, dedupe, truck profile and negotiation envelope.
- Platform drafts plus optional auto-send for individually permitted counters and factual replies.
- Reply classifier with deterministic pricing/policy checks.
- Alerts when the broker requests a call, requests sensitive driver information, appears to accept a price, or sends a rate confirmation.
- Profile preflight on every reply; missing facts block sending and ask the user to set them up.
- Audit log of every send and decision.

**Out (next):** automatic DAT discovery, Gmail OAuth/API, Gmail-native drafts, Outlook, phone automation, booking inside DAT, broad multi-customer onboarding and second vertical.

**Success metrics:**
- Broker reply received → classified/draft/alert ready: p50 under 15 seconds.
- 100% of sent rate messages pass deterministic envelope validation.
- Zero factual replies containing values absent from the saved profile.
- Zero automatic sends after a protected event.
- Customer-reported reduction in time spent on broker email.

## 13. Build order

1. Add Gmail app-password connection and encrypted credential storage.
2. Add the platform compose screen and load/economics form.
3. Send through SMTP and store message/thread identifiers.
4. Add IMAP reply monitoring, cursoring, idempotency and thread matching.
5. Add truck profile and per-opportunity negotiation envelope.
6. Add reply classification, deterministic rate evaluation and protected-event alerts.
7. Add platform drafts, review/send UI and per-action auto-send controls.
8. Pilot on real conversations, then pursue the approved DAT source connector separately.

## 14. Configuration collected during onboarding

These are user-editable parameters rather than research blockers:

- Truck/equipment profile and which fields are shareable.
- Target rate, floor, rate basis, counter amount/strategy and maximum rounds.
- Whether passes, counters and factual replies are draft-only or eligible for auto-send.
- Alert destination and quiet-hours behavior.
- Which sensitive requests and documents always require manual handling.
