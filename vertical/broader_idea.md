# Broader Idea: Multi-Vertical Outreach Engine

Companion to `problem.md` and `product.md`. Those two files spec the first vertical (truckers on DAT One). This file describes the engine that vertical is the first instance of, so the code written for trucking lands in the right shape from day one.

Read this first for architecture boundaries. Read `product.md` for the concrete trucking build.

---

## 1. The idea in one paragraph

Selling still runs on manual work in a lot of industries. A business owner has to find the opportunity, figure out if it fits, pick a channel, write the message, send it fast enough, follow up, read the replies, and decide what to do next. Software covers pieces of this (a lead list here, an email writer there, a CRM somewhere else), but a person still wires it together and does the judgment.

The engine takes that job. A business connects the platform where its opportunities already show up, sets rules for what it wants and what it will accept, and turns on playbooks. Agents watch for opportunities, reach out within seconds, handle routine replies, and hand the human only what needs a decision. The core is shared across industries. Each industry plugs in as a **vertical pack**: connectors, a data schema, playbooks, reply labels and commit rules.

Goal of the product: **produce qualified opportunities with as little human time as possible.** Not email volume.

---

## 2. Where this came from

- Johnson has been building ContractorOps (https://contractorops.ai/), an AI back office for service businesses. Growing it, he hit the customer-finding problem himself, and contractors, real estate, insurance and trucking contacts kept asking: "Can this do the outreach for my business too?"
- The existing ContractorOps outreach pipeline already has: SERP API + LLM lead pipeline, a template engine with whitelisted `{{variables}}` rejected at save time if unknown, em/en-dash sanitization, plain-text-first templates (19 of 20 built-ins), sender personas, and paced Gmail sends (5-8 minute gaps).
- A trucker who pays about $200/month for DAT One emails brokers from inside the app. Loads are first-come, so the speed of that email decides who gets the load. That is the cleanest example of the pattern and became vertical #1.

---

## 3. The general pattern

Every target business has the same loop:

```
opportunity appears -> is it a fit? -> contact the counterparty fast
-> counterparty replies -> answer routine stuff -> human decides anything binding
-> outcome recorded -> rules get better
```

What changes per industry:

| | Trucking (DAT) | Contractors (lead inbox) | Later: real estate, insurance |
|---|---|---|---|
| Where opportunities appear | DAT match-alarm emails, load board | Lead notification emails, website form emails, marketplace leads | Broker CRM, MLS via licensed access, renewal windows |
| Counterparty | Freight broker | Homeowner / GC / property manager | Buyer, seller, business owner |
| Time pressure | Minutes | Hours (first responder often wins the job) | Days |
| Fit rules | Lane, equipment, rate/mile floor, dates | Trade, service area, job size | Price band, geography, product line |
| Things the agent may never agree to | Rate outside band, pickup/delivery commitment, contract terms | Quote, scope, start date, warranty | Price, terms, advice (regulated) |
| Documents on request | COI, W-9, MC authority | License, insurance, portfolio | Disclosures (regulated) |

The engine owns the left-to-right loop. The vertical pack owns every cell in that table.

---

## 4. Two ways opportunities enter

The engine needs to support both. Build the first one now; design the store so the second fits later.

**A. React mode (event-driven, the DAT case).** Opportunities are pushed to the customer by a platform they already pay for. The engine's job is speed and correctness. Source: alert emails, webhooks, API feeds.

**B. Discover mode (goal-driven, the original "autonomous acquisition" thesis).** The customer states a goal ("find Bay Area remodelers that fit ContractorOps", "find reefer shippers on the CA-TX corridor") and the engine goes looking: SERP API, public registries, licensed data, the customer's CRM. The engine's job is targeting quality and evidence.

Both produce the same normalized `Opportunity` record. Everything downstream (matcher, playbooks, sender, conversation handler, escalation) does not care which mode produced it.

---

## 5. Architecture

```
            ┌──────────── vertical pack (per industry) ─────────────┐
 source ──► │ SourceConnector: match -> parse -> normalize          │
 events     │ DiscoveryAdapter (later): query -> fetch -> extract   │
            │ payload schema, templates, playbook presets,          │
            │ reply labels, commit rules, docs catalog              │
            └───────────────────────────┬───────────────────────────┘
                                        │ Opportunity
            ┌──────────── core engine (shared) ─────────────────────┐
            │ Ingest + dedupe ──► Opportunity store (Postgres)      │
            │ Matcher (customer rules) ──► Send queue (paced)       │
            │ Channel executors: email now, SMS/voice later         │
            │ Conversation handler: thread match, classify, reply   │
            │ Policy gate: commit rule, caps, suppression           │
            │ Escalation inbox: SMS now, web later                  │
            │ Outcome tracker + audit log                           │
            └───────────────────────────────────────────────────────┘
            ┌──────────── integrations (shared) ────────────────────┐
            │ MailProvider (Gmail, Outlook), SmsProvider (Twilio),  │
            │ token vault, queue, scheduler, object storage         │
            └───────────────────────────────────────────────────────┘
```

### 5.1 Core components (never vertical-specific)

1. **Ingest + dedupe.** Takes `Opportunity[]` from any connector. Enforces `unique(customer_id, dedupe_key)`. Also suppresses re-contacting the same counterparty for the same thing inside a window, even if the source reposts. Latency target: seconds from source event to queued send.
2. **Opportunity store.** Postgres. Core columns are shared; vertical detail lives in `payload jsonb` validated against the pack's JSON schema. Raw source events kept separately so parsers can be re-run.
3. **Matcher.** Evaluates each opportunity against the customer's enabled playbooks. Rules are data (JSON), evaluated by deterministic code. The LLM does not decide whether something matches a hard rule.
4. **Playbook engine.** Playbook = trigger filter + template + sending identity + pacing + follow-up sequence + allowed auto-actions + autonomy level.
5. **Channel executors.** Email first, through the customer's own mailbox. SMS and voice are additional executors behind the same interface, not separate products. Every send is idempotent (keyed by `thread_id + step`) and logged.
6. **Conversation handler.** Matches inbound replies to threads, classifies, auto-answers inside the playbook's scope, otherwise escalates. Per-thread state machine.
7. **Policy gate.** Runs before every outbound message. Enforces the commit rule, volume caps, send windows, suppression/opt-out, and the human-takeover freeze. Hard-coded, not a prompt.
8. **Escalation inbox.** One place the human sees only what needs them, with full thread context, and can answer in one reply that goes back into the thread. SMS first.
9. **Outcome tracker.** Joins every opportunity to what happened: contacted, replied, booked, won, lost, and value where known. This is the data that later tunes rules and targeting.
10. **Audit log.** Every agent action, every rule decision, every human override.

### 5.2 Vertical pack (everything industry-specific)

A pack is versioned code + data in its own directory. It is not a long system prompt.

- `connectors/` - one `SourceConnector` per source (e.g. `dat_one_alarm`). Pure parse functions, fixture-tested against real samples.
- `schema/payload.json` - JSON schema for `opportunities.payload`.
- `profile_schema.json` - facts the agent is allowed to state about the customer (trucking: MC#, DOT#, equipment, home base, insurance carrier).
- `rules_schema.json` - what customers can configure (trucking: lanes, equipment, rate/mile floor, deadhead max).
- `templates/` - default templates per playbook kind, using only whitelisted variables.
- `playbooks/presets.json` - default playbook configs.
- `reply_labels.json` - vertical labels added to the shared classifier set.
- `commit_rules.ts` - what counts as a commitment in this industry. Always escalates.
- `documents.json` - document kinds the agent may attach on request.
- `fixtures/` - real source samples and gold labels for tests.
- Later: `discovery/` adapters for discover mode, and optional computer-use routines.

### 5.3 Interfaces

These match `product.md`. Keep them stable; the whole point is that a new vertical only implements these.

```ts
// Normalized unit of work, shared by every vertical
interface Opportunity {
  customerId: string;
  vertical: string;               // 'trucking' | 'contractors' | ...
  source: string;                 // 'dat_one_alarm' | ...
  externalId?: string;            // platform ID when present
  dedupeKey: string;
  counterparty: { org?: string; name?: string; email?: string; phone?: string };
  postedAt?: string;              // when the source says it appeared
  receivedAt: string;             // when we saw it
  expiresAt?: string;             // opportunity TTL (loads: hours; leads: days)
  payload: Record<string, unknown>; // validated by pack schema
  sourceEventId: string;
}

interface SourceConnector {
  vertical: string;
  source: string;
  matches(msg: RawMailMessage): boolean;
  parse(msg: RawMailMessage): Opportunity[];   // pure, no I/O
  payloadSchema: JSONSchema;
}

interface MailProvider {                        // gmail now, outlook later
  watch(account: MailAccount): Promise<void>;
  fetchNew(account: MailAccount, cursor: string): Promise<RawMailMessage[]>;
  send(account: MailAccount, draft: Draft): Promise<SentRef>;
  createDraft(account: MailAccount, draft: Draft): Promise<DraftRef>;
}

interface ChannelExecutor {                     // email now; sms/voice later
  channel: 'email' | 'sms' | 'voice';
  execute(action: OutboundAction): Promise<ActionResult>; // idempotent
}

interface CommitRule {                          // per vertical, plus shared ones
  id: string;                                   // 'rate_out_of_band', 'date_commit', ...
  check(draft: Draft, ctx: ThreadContext): { blocked: boolean; reason?: string };
}

// Later, for discover mode
interface DiscoveryAdapter {
  vertical: string;
  source: string;                               // 'serp', 'cslb', 'fmcsa', ...
  search(goal: GoalContract): Promise<RawRecord[]>;
  extract(rec: RawRecord): Opportunity[];
}
```

Connectors are the only place that knows what a DAT email looks like. If trucking-specific field names show up anywhere in the core, that is a bug.

---

## 6. Data flow (react mode)

1. Customer connects their mailbox with OAuth (no passwords). One connection is the sending identity, the reply channel, and the source feed.
2. Mail provider pushes a change notification (Gmail: `users.watch` to Pub/Sub, then `history.list`). A poll fallback covers missed pushes.
3. Each new message is stored as a `source_event`, then offered to every connector registered for that customer's vertical. First `matches()` wins.
4. Connector `parse()` returns `Opportunity[]`. Schema-validated, deduped, stored.
5. Matcher evaluates enabled playbooks. Match -> render template -> policy gate -> send queue (or draft, in draft-only mode).
6. Sender sends from the customer's mailbox. Thread row created.
7. Reply arrives by the same push path. Conversation handler matches it to the thread by provider thread ID, classifies, then auto-replies inside scope or escalates.
8. Escalation goes out by SMS with a short summary. The customer's SMS answer is turned into a reply in the same email thread (and passes the policy gate as a human-authorized message).
9. Outcome recorded when known.

Discover mode replaces steps 2-4 with: goal contract -> discovery adapters -> evidence extraction -> entity resolution -> qualification -> `Opportunity[]`. Steps 5-9 are identical.

---

## 7. Playbooks

Customers configure playbooks from a small set of email-type primitives. They never write prompts.

| Kind | What it does | Default autonomy |
|---|---|---|
| `first_touch` | First message on a new opportunity. Trucking: "saw your load [lane/rate/ref], truck available [when/where], MC/DOT + insurance on file." | draft-only, then auto |
| `followup_bump` | One-line nudge in the same thread after 24-48h (trucking: much shorter, loads expire). | auto after graduation |
| `rate_reply` | Answer a price question only inside the customer-set band. | auto inside band, escalate outside |
| `document_reply` | Attach COI, W-9, authority docs, license on request. High value, low risk. | auto |
| `booking_confirmation` | Confirm next steps after the human agreed. Never the agreement itself. | auto after human commit |
| `nurture` | Long-cycle re-engagement for counterparties that went quiet. | draft-only |

Per-customer knobs: which playbooks are on, variable values, the price band, send windows, daily volume cap, autonomy level per playbook (`draft_only` / `send_approved` / `auto`).

Every customer starts in draft-only for the first N sends and graduates per playbook. This catches bad templates before they burn a sender's reputation, and it is how the customer learns to trust the agent.

Template rules carried over from the ContractorOps engine: whitelisted variables only, unknown placeholders rejected at save time, no unrendered placeholders at send time, plain text, em/en-dash sanitization, paced sends.

---

## 8. Replies and the commit rule

### 8.1 Classification

Shared labels: `interested_question`, `negotiation`, `document_request`, `booking_intent`, `not_now`, `decline`, `unsubscribe`, `angry`, `unclear`. Packs add vertical labels (trucking: `load_covered`, `need_eta`; contractors: `site_visit_request`).

Auto-handle only with high confidence and inside playbook scope: document requests, availability questions answerable from the customer profile, polite declines (log and stop), not-now (schedule nurture), unsubscribes (suppress).

### 8.2 The commit rule

**The agent can inform and schedule. It cannot commit.**

Always escalate:
- price outside the customer's band
- contract or legal language
- anything that sounds like a commitment: dates, guarantees, exclusivity, scope
- angry or confused replies
- low-confidence classifications
- a reply on a thread already marked lost, or a thread past the end of its follow-up sequence

Implemented as shared `CommitRule`s plus pack-specific ones, run by the policy gate on every outbound draft. A blocked draft becomes an escalation, never a send.

**Kill switch:** any message the customer sends themselves in a thread freezes the agent on that thread.

### 8.3 Escalation

SMS with: who, what they said (short), what the agent recommends, and how to answer. Unanswered escalations get one nag, then roll into a daily digest. Later: a web inbox with the full thread and one-tap takeover.

---

## 9. Getting data from customer platforms

Ranked by preference. The rule: never ask for platform credentials when an alert email or forwarding rule does the same job.

1. **Official API or webhook.** Best when available. DAT has partner APIs, but a carrier subscription generally does not include API access, so do not plan on it for truckers.
2. **The platform's own alert emails.** The default for react mode. DAT sends match-alarm emails for saved searches. Zero scraping, no ToS risk, the customer keeps their normal account.
3. **Push/SMS alerts,** parsed the same way where offered.
4. **Customer-authorized browser session (computer use).** Works anywhere, but fragile: session expiry, rate limits, ToS risk, and a banned account costs the customer the subscription they pay for. Last resort, narrow capability, sane intervals, stop on any challenge.
5. **CSV/paste import.** Fine day-one fallback and a cheap way to test demand before writing a connector.

### 9.1 Computer-use boundary (when it is needed)

Johnson's original thesis includes agents operating the software customers already use (load boards, realtor dashboards, CRMs). Keep it behind an adapter so it can be added per source without touching the core:

- APIs and exports first, browser second
- isolated browser per customer, encrypted credentials, domain allowlist
- read/extract separated from act (click/send/update); acting needs approval until reliability is proven per workflow
- deterministic scripts for stable paths, model vision only for variable UI and recovery
- snapshot before and after each material action; idempotency keys so retries never double-send
- benchmark 20 repeated runs (success, interventions, time, cost) before promising it to customers

A logged-in account is not permission to bulk copy a platform's data.

---

## 10. Discover mode: data sourcing and quality

For when the engine goes looking instead of reacting. From the autonomous acquisition research.

### 10.1 Goal contract

Every discover run starts as a structured object, even if the user types it in plain language:

```ts
interface GoalContract {
  customerId: string;
  offer: string;
  job: string;                     // "find reefer shippers CA->TX needing capacity"
  vertical: string;
  geography?: string[];
  qualification: Rule[];           // hard gates
  requiredEvidence: { field: string; maxAgeDays: number }[];
  exclusions: string[];            // suppression, existing customers
  allowedSources: string[];
  allowedChannels: ('email' | 'sms' | 'voice')[];
  limits: { contactsPerDay: number; budgetUsd?: number };
  stopConditions: string[];
  approvalPolicy: Record<string, 'auto' | 'approve'>;
  handoff: { destination: string; slaHours?: number };
}
```

Without this, a general agent becomes an unbounded prompt runner.

### 10.2 Three layers of lead data

1. **Registry truth:** identity, license, legal status. (FMCSA for carriers/brokers, CSLB for California contractors, Secretary of State filings, NIPR for insurance producers.)
2. **Fit:** what the company does, owns, ships, builds.
3. **Timing:** a fresh event that makes outreach relevant now.

A contact without layers 1-2 is a name. Layers 1-2 without timing is a static list. All three, with sources and freshness, is a real opportunity.

### 10.3 Storage shape

```text
RawSnapshot(source, source_record_id, retrieved_at, content_hash, license_policy)
Claim(entity_id, field, value, observed_or_inferred, confidence,
      observed_at, expires_at, source_snapshot_id, extractor_version)
Entity(stable_id, type, canonical_name)
ContactPoint(entity_id, channel, value, verification_state, verified_at,
             source, suppression_state)
```

Keep raw and normalized separate. Never let an LLM overwrite an observed value without keeping both.

### 10.4 Rules

- Source order: official API -> licensed bulk/export -> customer-authorized integration -> polite public fetch -> browser only when permitted. Paywalls and CAPTCHAs are boundaries, not puzzles.
- Hard gates before any ranking. A perfect-fit lead with no permitted contact route is not ready.
- Score parts separately (identity, contactability, fit, timing, freshness, provenance, compliance, novelty) with reason codes. Not one LLM number.
- The unit the user sees is a **lead evidence card**: why it fits, why now, how to contact, sources, what is inferred, when facts expire, proposed next action.

---

## 11. Vertical roadmap

**Note on order:** the original thesis had contractors as the first proving ground and freight second. The current build puts trucking/DAT first because it has the sharpest pain (minutes matter), a clean data path (alarm emails), and a real trucker to pilot with. Contractors come right after and are the test of whether the core really generalizes.

### Vertical #1: Truckers on DAT One (react mode)

Fully specced in `product.md`. Summary:
- DAT One is the largest North American load board (roughly 722,500 loads posted per business day, per DAT). Carriers search by lane/equipment, save searches, and get match-alarm emails every 5 minutes.
- Many loads list an email contact. DAT's Connected Email lets a carrier link Gmail/Outlook and send a pre-filled email per load from inside DAT. Today's flow: alarm email -> open app -> open load -> send -> edit -> send.
- Carrier plans run roughly $54-$339/month depending on tier and promo.
- dispatchGo, a Chrome extension for one-click broker emails on DAT, shows people already pay to respond faster.
- Unverified: whether alarm emails include the broker's email. Step 1 is collecting 20+ real alarm emails.

### Vertical #2: Contractors, their own lead inboxes (react mode)

Same engine, new pack:
- Sources (to verify with real samples from ContractorOps customers): lead notification emails from marketplaces and directories, website contact-form emails, missed-call/voicemail transcripts from existing ContractorOps phone infra.
- Payload: job type, address/zip, job size hints, timeline, homeowner contact.
- Fit rules: trade, service area, minimum job size.
- Playbooks: fast first response, site-visit scheduling (propose times only), document reply (license, insurance), follow-up.
- Commit rules: quotes, scope, start dates, warranties always escalate.
- Proof point: if trucking and contractors share ingest, store, matcher, sender, conversation handler and escalation unchanged, the architecture generalizes. If the core has to fork, write down why. That is the real result.

### Dogfood: ContractorOps selling itself (discover mode)

Johnson already runs outbound for ContractorOps with the SERP + LLM pipeline and the template engine. Moving that pipeline onto the engine (as a discover-mode pack) tests discover mode on a market he can judge, with no customer at risk.

### Later: real estate, insurance, freight brokers

Do not add a third vertical until two work. Both need a heavier policy layer:
- Real estate: MLS data only through licensed access (RESO Web API is the transport standard, it does not grant rights). No protected-class inference or steering.
- Insurance: state producer licensing, product/jurisdiction approval, consent and do-not-contact status. Legal review is part of entering, not cleanup.
- Freight brokers finding shippers (discover mode): FMCSA for identity, BTS FAF for lane sizing, shipper signals from filings, facilities and job posts.

---

## 12. Repo layout (suggested)

```
/core
  ingest/          dedupe, source_events, opportunity store
  matcher/         rule evaluation (JSON rules, deterministic)
  playbooks/       engine, template renderer, variable whitelist, pacing
  conversation/    thread matching, classifier, state machine
  policy/          commit rules (shared), caps, suppression, kill switch
  escalation/      SMS flow, digest, nag
  outcomes/        outcome tracking
  audit/
/integrations
  mail/gmail/      OAuth, watch, history, send, drafts
  mail/outlook/    later
  sms/twilio/
  vault/           encrypted token storage
/verticals
  trucking/        connectors/dat_one_alarm, schema, templates, presets,
                   reply_labels, commit_rules, documents, fixtures
  contractors/     same shape
/discovery         later: goal contract, adapters, evidence store
/tests
  contract/        every pack must pass the same connector contract tests
```

Add a CI check: `/core` may not import from `/verticals`. That keeps the boundary honest.

---

## 13. Positioning (from the market research)

The "find, research, personalize, sequence, book meetings" promise is crowded: 11x (Alice), Artisan (Ava), AiSDR, Reply.io (Jason), Regie.ai, HubSpot's Prospecting Agent, Clay, Unify, Common Room, Relevance AI, Salesforce Agentforce. In freight specifically: FleetGen, FreightLeads.ai, GetFreight, breadd.ai, Ten8. Treat prospect discovery, personalization, sequencing and reply classification as table stakes, not the moat.

Where this engine is different:
- It starts from where the customer's opportunities already arrive (their load board, their lead inbox), not from a contact list and a campaign.
- Speed on time-sensitive inbound opportunities, which campaign tools are not built for.
- Sends from the customer's own mailbox, in their name, inside their existing threads.
- Vertical packs that know what "a commitment" means in each industry.
- Learns from real outcomes (loads booked, jobs won), not opens and meetings.
- Built for small operators who live in inboxes, portals and phones, not a CRM stack.

This gap is plausible, not proven. The pilot has to show it.

---

## 14. Risks

- **Platform ToS.** Scraping a paid board can get the customer banned. Alert-email ingestion avoids it.
- **Deliverability.** New senders plus volume goes to spam fast. Plain text, pacing, daily caps, per-customer warmup.
- **Made-up commitments.** Agent inventing a rate, location or date. Locked variables, customer profile as the only fact source, commit rule in code.
- **Wrong-send damage lands on the customer's business.** Draft-only onboarding, per-playbook caps.
- **Mailbox access.** Gmail read scopes are restricted by Google; public launch needs Google verification and likely a security assessment. MVP runs in OAuth testing mode. Confirm current requirements before scaling.
- **Compliance.** CAN-SPAM (identity, unsubscribe) for email; TCPA if SMS/voice outreach to counterparties is added.
- **Multi-tenant security.** Tokens encrypted per customer, hard isolation, no passwords.
- **Ignored escalations.** Nag loop and daily digest.

---

## 15. Metrics

Per vertical, measured from day one:
- time from source event to first outreach (trucking target: under 60 seconds)
- share of matched opportunities contacted
- reply rate, positive reply rate
- booked / won (loads, jobs) and value
- escalations per 100 threads, and share answered
- human minutes per won opportunity
- commit-rule blocks (should be caught, never sent)
- cost per opportunity (LLM, SMS, infra)

For the architecture itself: percent of code shared vs forked between trucking and contractors.

---

## 16. Riskiest assumptions to test first

1. Speed alone wins more loads/jobs (trucking: compare reply and book rates vs the trucker's manual baseline).
2. Source emails carry enough to act on (broker email in DAT alarms; contact info in lead emails).
3. Customers will let an agent send in their name after a draft-only period.
4. Replies are routine often enough that auto-handling saves real time.
5. The core really is shared: vertical #2 ships as a pack with no core changes.
6. Unit economics hold at the price truckers and contractors will pay.

---

## Sources

- DAT load boards and pricing: https://www.dat.com/load-boards
- DAT carriers page (older promo pricing): https://www.dat.com/carriers
- DAT Connected Email: https://one.support.dat.com/7-search-trucks-1e508202/connected-email-3d563b08
- DAT One carrier quick start: https://one.support.dat.com/dat-one-for-carrier-quick-start-guide-e203241a
- dispatchGo: https://dispatchgo.app
- 11x Alice: https://docs.11x.ai/alice/overview
- Artisan: https://www.artisan.co/pricing
- AiSDR: https://aisdr.com/pricing/
- HubSpot Prospecting Agent: https://www.hubspot.com/products/sales/ai-prospecting-agent
- FleetGen: https://www.fleetgen.ai/os
- FreightLeads.ai: https://freightleads.ai/
- GetFreight: https://getfreight.ai/
- breadd.ai: https://www.breadd.ai/
- Ten8: https://www.ten8.ai/
- FMCSA APIs: https://mobile.fmcsa.dot.gov/QCDevsite/docs/apiAccess
- BTS Freight Analysis Framework: https://www.bts.gov/faf
- CSLB data portal: https://www.cslb.ca.gov/onlineservices/dataportal/
- RESO Web API: https://www.reso.org/reso-web-api/
- NIPR (via NAIC): https://content.naic.org/cipr-topics/national-insurance-producer-registry-nipr

