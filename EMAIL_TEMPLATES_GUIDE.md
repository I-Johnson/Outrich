# Cold Outreach Email Templates & Variable Guide

Comprehensive reference for email templates, dynamic variables, fallback rules, resolution lifecycle, and existing template catalog in the Outreach Pipeline.

---

## 1. How Email Templates Work

The Outreach Pipeline uses a custom lightweight template engine ([`app/core/template_engine.py`](file:///Users/priyanshupyakurel/Projects/outreach/app/core/template_engine.py)).

Templates support double-curly-brace variable placeholders (e.g., `{{owner_first_name}}`, `{{short_name}}`, `{{product_name}}`).

### Deliverability & Anti-Spam Design Principles:
1. **Strict Variable Validation**: Only whitelisted variables are permitted. Any unknown placeholder (like `{{first_name}}` or `{{company}}`) is rejected at save time to prevent unrendered variables from reaching prospects.
2. **Automated Em-Dash / En-Dash Sanitization**: The engine automatically converts em-dashes (`—`) and en-dashes (`–`) into natural commas (`, `). Modern spam filters and spam scanners flag em-dashes as a common AI copywriter fingerprint.
3. **Plain-Text Preference**: Plain text emails bypass promotional/marketing tab classification in Google Workspace and Outlook. 24 of the 25 built-in templates use `plain`.

---

## 2. Dynamic Variables & Fallback Rules

All variables are case-insensitive (e.g. `{{Signature}}` and `{{signature}}` resolve identically).

### A. Lead / Prospect Variables

| Variable | Description | Resolution & Fallback Hierarchy | Example |
|---|---|---|---|
| `{{owner_first_name}}` | Prospect's personal first name | 1. `lead.owner_first_name`<br>2. Inferred from lead email (e.g., `blair@...` &rarr; `Blair`)<br>3. `"{{short_name}} Team"` (if no personal name detected)<br>4. `"there"` (if no company name either) | `Blair` or `Elite Remodeling Team` |
| `{{person_name}}` | Alias for `{{owner_first_name}}` | Same resolution as `{{owner_first_name}}` | `Bryan` |
| `{{short_name}}` | Cleaned business name | 1. `lead.short_name`<br>2. `lead.business_name`<br>3. Fallback: `"there"` | `Blair Jones` (from *Blair Jones Co.*) |
| `{{category}}` | Trade, specialty, or niche | 1. `lead.category`<br>2. Fallback: `"contractor"` | `remodeling`, `HVAC`, `contractor` |
| `{{city}}` | Prospect city | 1. `lead.city`<br>2. Fallback: `"your area"` | `San Antonio`, `Dallas`, `Austin` |
| `{{state}}` | Prospect 2-letter state | 1. `lead.state`<br>2. Fallback: `""` (empty string) | `TX`, `CA`, `FL` |

### B. Sender & Business Variables

| Variable | Description | Resolution Source | Example |
|---|---|---|---|
| `{{sender_name}}` | Representative's display name | Active sender persona display name; fallback to default settings | `Sofia`, `Brooke`, `Mariana` |
| `{{sender_email}}` | Representative's email address | Active sender email address; fallback to default settings | `sofia@contractorops.ai` |
| `{{signature}}` | Representative's email signature | Active sender persona signature; fallback to default signature | `Sofia\nContractor Success @ ContractorOps` |
| `{{email_signature}}` | Alias for `{{signature}}` | Same as `{{signature}}` | `Brooke\nContractor Growth Specialist @ ContractorOps` |
| `{{product_name}}` | Product / Service name | Business settings `product_name` | `ContractorOps` or `ContractorOps AI` |
| `{{product_url}}` | Website or product landing page | Business settings `product_url` | `https://www.contractorops.ai/` |
| `{{booking_link}}` | Calendly / Cal.com demo booking URL | Business settings `booking_link` | `https://cal.com/contractorops/demo` |
| `{{calendar_link}}` | Alias for `{{booking_link}}` | Same as `{{booking_link}}` | `https://cal.com/contractorops/demo` |
| `{{sender_business}}` | Company name | Business settings `sender_business` | `ContractorOps` |
| `{{sender_business_url}}` | Business homepage | Business settings `sender_business_url` | `https://contractorops.ai` |
| `{{client_count}}` | Active client / social proof count | Business settings `client_count` | `30` |
| `{{client_noun}}` | Social proof noun | Business settings `client_noun` | `contractors`, `remodelers` |
| `{{callback_number}}` | Contact phone number | Business settings `callback_number` | `(555) 234-5678` |

---

## 3. Critical Nuances & The Variable Resolution Lifecycle

Understanding *when* variables resolve is crucial for multi-sender rotation and deliverability:

```
[Template Saved] 
       │ (Strict regex validation: unknown variables rejected immediately)
       ▼
[Campaign Queued]
       │ (Merged with initial assigned sender & lead data)
       ▼
[Conveyor Belt Rebalance / Reschedule]
       │ (Pingram: rotates rep persona; updates {{sender_name}}, {{sender_email}}, {{signature}})
       ▼
[Dispatch via SMTP / Pingram API]
       │ (Final rendered text sent directly to recipient)
```

1. **Re-Rendering During Conveyor Belt Rotation**: When Pingram rotates through its 20 domain representatives on the 24/7 conveyor belt, the subject, body, and signature are dynamically re-rendered for each scheduled slot to match the assigned representative.
2. **Forbidden Variables**: Do **NOT** use `{{first_name}}` (use `{{owner_first_name}}`), `{{company}}` (use `{{short_name}}`), or `{{name}}`.
3. **Em-Dash Replacement**: Any `—` or `–` in template text is automatically sanitized to `, ` upon send.

---

## 4. Full Catalog of All 25 Templates

Summary:
- **HVAC Specialized**: 5 templates
- **Remodelers Specialized**: 13 templates
- **General Contractor & Universal Angles**: 7 templates

### Category 1: HVAC Specialized Templates (5 Templates)

#### 1. HVAC - Cold Season Surge & Dispatch
- **ID**: `fd5f5f65-1097-45b5-8550-71cce09ee6aa`
- **Angle Tag**: `hvac_cold_season`
- **Type**: `plain`
- **Subject**: `Ready for the upcoming cold season, {{owner_first_name}}?`
- **Body**:
```text
Hey {{owner_first_name}},

When we are past September, the calls come in faster than any office can route them. The shops that win the season are the ones with scheduling, dispatch, and estimates running in one place instead of on sticky notes.

{{product_name}} ({{product_url}}) handles the intake, books the visit, sends market based estimates, and follows up so no job slips while your team is slammed.

Worth a 15 minute chat before the next spike? {{booking_link}}

{{signature}}
```

---

#### 2. HVAC - Meta Angle 1: Commercial Maintenance Scouting
- **ID**: `80d1cee2-983b-44d2-90a9-d8f4ba9a744f`
- **Angle Tag**: `hvac_meta_angle`
- **Type**: `plain`
- **Subject**: `Curious how we found you, {{owner_first_name}}?`
- **Body**:
```text
Hey {{owner_first_name}},

Curious how this email reached you? It wasn't me, it was our AI.

We built an outreach engine that autonomously scouts local markets for commercial HVAC opportunities from property managers, HOAs, and facility directors, drafts personalized pitches, and delivers them. It just found {{short_name}}, wrote this email, and hit send.

Getting the commercial lead is only half the battle. We also built {{product_name}} ({{product_url}}) to handle the rest, like an AI receptionist that answers emergency service calls 24/7, market-based equipment estimates, technician dispatching, and automated proposal follow-ups so you never lose a job to phone tag.

Want me to run a quick search for your service area and show you the commercial HVAC maintenance contracts our AI can find?

{{signature}}
```

---

#### 3. HVAC - Meta Angle 2: Exclusive Service Contracts vs Shared Leads
- **ID**: `b7f98869-3084-45bd-88df-8cc9547e7f84`
- **Angle Tag**: `hvac_meta_angle`
- **Type**: `plain`
- **Subject**: `tired of fighting over shared HVAC leads?`
- **Body**:
```text
Hey {{owner_first_name}},

I'll let you in on a secret: I didn't manually type this email. Our AI searched your area, found {{short_name}}, and handled the outreach autonomously.

HVAC contractors are using this exact system to find and pitch commercial service contracts and property managers directly, so they can stop burning cash on expensive, shared residential leads.

And once those commercial opportunities respond, {{product_name}} ({{product_url}}) handles the operations: an AI receptionist that answers after-hours emergency calls, automated quote generation for unit changeouts, and instant technician dispatch.

We're working with about {{client_count}} {{client_noun}} right now to refine this. Open to a brief 15-minute chat to share your thoughts on what we've built?

{{booking_link}}

{{signature}}
```

---

#### 4. HVAC - Meta Angle 3: How We Found You (24/7 Dispatch & Intake)
- **ID**: `93342db1-6a2e-4b8e-9233-0ff4f3bae4d4`
- **Angle Tag**: `hvac_meta_angle`
- **Type**: `plain`
- **Subject**: `How we found {{short_name}}`
- **Body**:
```text
Hey {{owner_first_name}},

Wondering how we got your email? We didn't buy a stale list or bid on a shared lead.

We built an AI that actively scans your local market for commercial properties, reaches out to facility managers, and sets up high-ticket HVAC service agreements automatically. It identified {{short_name}}, and it's the exact engine we're handing to HVAC shops to bring commercial service contracts straight to their inbox.

Beyond outbound leads, {{product_name}} ({{product_url}}) runs the daily intake: a 24/7 AI receptionist that collects equipment details from frantic homeowners, schedules technician visits on your calendar, and automates proposal follow-ups before competitors even call back.

Mind if I send over a quick screenshot of the commercial facilities and property managers our AI can pull up around {{city}}?

{{signature}}
```

---

#### 5. HVAC - Quoting Speed & Fast Estimates
- **ID**: `7c664184-196d-45aa-b68e-65a357f21fd4`
- **Angle Tag**: `hvac_quoting_speed`
- **Type**: `plain`
- **Subject**: `how fast does {{short_name}} send estimates?`
- **Body**:
```text
Hey {{owner_first_name}},

Honest question. After a site visit, how long does it take {{short_name}} to get a professional estimate in the homeowner's inbox? The first shop to send a clean proposal usually wins the job, and most {{category}} teams lose days to manual quoting.

{{product_name}} generates market based estimates, builds the proposal, and automates the follow up so you are always first.

Curious how you are handling it today, and happy to show you ours: {{booking_link}}

{{signature}}
```

---

### Category 2: Remodelers Specialized Templates (13 Templates)

#### 1. Remodelers - 3D Visualizer Pitch
- **ID**: `f2988d3e-9605-46c4-8205-86090845bc17`
- **Angle Tag**: `remodeler_3d_feature`
- **Type**: `plain`
- **Subject**: `Streamlining leads + projects at {{short_name}}`
- **Body**:
```text
Hey {{owner_first_name}} ,

I built {{product_name}} ({{product_url}}) to help remodeling businesses  like yours manage leads and projects in one place.

We work with about {{client_count}} {{client_noun}}. Some of the main features include:

• AI receptionist: Answers customer calls, collects project details, texts leads back, and schedules visits.

• 3D project design: Create 3D designs to help customers visualize their projects.

• Estimates and proposals: Generate market based estimates, create professional proposals, and automate follow ups.

• Lead and team management: Find commercial leads through HOAs and property managers, and manage subcontractor availability and dispatch.

I'd love to get your feedback on what we've built. Are you open to a brief 15 minute chat?

You can grab a time here: {{booking_link}}

{{Signature}}

```

---

#### 2. Remodelers - AI Receptionist Intake (No URL)
- **ID**: `d31f4309-34c4-419d-bba4-13f131a2cdf5`
- **Angle Tag**: `remodeling_1`
- **Type**: `plain`
- **Subject**: `Quick question about {{short_name}}`
- **Body**:
```text
Hey {{owner_first_name}},

I came across {{short_name}} and wanted to reach out.

We built {{product_name}} for remodeling companies to help capture and manage leads without adding more work for the team.

Our AI receptionist answers calls, collects project details, texts leads back, and can schedule visits automatically.

Curious, how are you currently handling calls when nobody is available to answer?

{{signature}}
```

---

#### 3. Remodelers - AI Receptionist Intake (with URL)
- **ID**: `48fdc6e8-f4ca-434d-9bd1-0a787fab808d`
- **Angle Tag**: `remodeling_1`
- **Type**: `plain`
- **Subject**: `Quick question about {{short_name}}`
- **Body**:
```text
Hey {{owner_first_name}},

I came across {{short_name}} and wanted to reach out.

We built {{product_name}} ({{product_url}}) for remodeling companies to help capture and manage leads without adding more work for the team.

Our AI receptionist answers calls, collects project details, texts leads back, and can schedule visits automatically.

Curious, how are you currently handling calls when nobody is available to answer?

{{signature}}
```

---

#### 4. Remodelers - Estimating & Proposals (No URL)
- **ID**: `295b8715-1a2f-46cc-a5e4-53b5663ff286`
- **Angle Tag**: `remodeling_2`
- **Type**: `plain`
- **Subject**: `How are you handling estimates?`
- **Body**:
```text
Hey {{owner_first_name}},

Quick question. How are you currently handling estimates and proposals at {{short_name}}?

We built {{product_name}} to help remodelers go from lead to estimate to proposal in one place, with market based estimates and automated follow ups.

I'd be curious to see how you're doing this today.

{{signature}}
```

---

#### 5. Remodelers - Estimating & Proposals (with URL)
- **ID**: `667b2a2c-949e-4bd2-a685-2279fd91f814`
- **Angle Tag**: `remodeling_2`
- **Type**: `plain`
- **Subject**: `How are you handling estimates?`
- **Body**:
```text
Hey {{owner_first_name}},

Quick question. How are you currently handling estimates and proposals at {{short_name}}?

We built {{product_name}} ({{product_url}}) to help remodelers go from lead to estimate to proposal in one place, with market based estimates and automated follow ups.

I'd be curious to see how you're doing this today.

{{signature}}
```

---

#### 6. Remodelers - Full Suite & 3D Design (No URL)
- **ID**: `64b05877-896a-4c91-9ac1-025c92c0b9f8`
- **Angle Tag**: `remodeling_4`
- **Type**: `plain`
- **Subject**: `Streamlining leads + projects at {{short_name}}`
- **Body**:
```text
Hey {{owner_first_name}},

I built {{product_name}} to help remodeling businesses like {{short_name}} manage leads and projects in one place.

We work with about {{client_count}} {{client_noun}}.

It includes:

• AI receptionist that answers calls, collects project details, texts leads back, and schedules visits.

• 3D project design for showing homeowners what their project could look like.

• Estimates, proposals, and automated follow ups in one workflow.

Would you be open to a brief 15 minute chat? I'd love to get your feedback on what we're building.

{{booking_link}}

{{signature}}
```

---

#### 7. Remodelers - Full Suite & 3D Design (with URL)
- **ID**: `cb3d05ec-a653-48d5-834e-444a7877288f`
- **Angle Tag**: `remodeling_4`
- **Type**: `plain`
- **Subject**: `Streamlining leads + projects at {{short_name}}`
- **Body**:
```text
Hey {{owner_first_name}},

I built {{product_name}} ({{product_url}}) to help remodeling businesses like {{short_name}} manage leads and projects in one place.

We work with about {{client_count}} {{client_noun}}.

It includes:

• AI receptionist that answers calls, collects project details, texts leads back, and schedules visits.

• 3D project design for showing homeowners what their project could look like.

• Estimates, proposals, and automated follow ups in one workflow.

Would you be open to a brief 15 minute chat? I'd love to get your feedback on what we're building.

{{booking_link}}

{{signature}}
```

---

#### 8. Remodelers - Lead Maximizer (No URL)
- **ID**: `b98f9f12-3b9d-4331-b288-3503480f4dc7`
- **Angle Tag**: `remodeling_3`
- **Type**: `plain`
- **Subject**: `More from the leads you already have`
- **Body**:
```text
Hey {{owner_first_name}},

I built {{product_name}} for remodeling companies that want to make more of the leads they're already getting.

It helps handle incoming calls, collect project details, follow up with homeowners, and move opportunities through estimates and proposals.

We're working with about {{client_count}} {{client_noun}} so far.

Would you be open to a quick look?

{{signature}}
```

---

#### 9. Remodelers - Lead Maximizer (with URL)
- **ID**: `17fd578c-d0c1-4d84-858d-2740a074a2ff`
- **Angle Tag**: `remodeling_3`
- **Type**: `plain`
- **Subject**: `More from the leads you already have`
- **Body**:
```text
Hey {{owner_first_name}},

I built {{product_name}} ({{product_url}}) for remodeling companies that want to make more of the leads they're already getting.

It helps handle incoming calls, collect project details, follow up with homeowners, and move opportunities through estimates and proposals.

We're working with about {{client_count}} {{client_noun}} so far.

Would you be open to a quick look?

{{signature}}
```

---

#### 10. Remodelers - Meta Angle 1: How We Found You
- **ID**: `f1d82698-1263-4f33-9578-709fd0ad8cc6`
- **Angle Tag**: `remodelers_meta_angle_1`
- **Type**: `plain`
- **Subject**: `Curious how we found you?`
- **Body**:
```text
Hey {{owner_first_name}},

Curious how this email reached you? It wasn't me, it was our AI.

We built an outreach engine that autonomously scouts local markets for commercial opportunities from HOAs and property managers, drafts personalized pitches, and delivers them. It just found {{short_name}}, wrote this email, and hit send.

But getting the lead is only half the battle. We also built ContractorOps ({{product_url}}) to handle the rest of the job, like an AI receptionist that answers your calls 24/7, automated market-based estimates, 3D project designs, and team dispatching so you can manage everything in one place.

Want me to run a quick search for your service area and show you the commercial jobs the AI can find?

{{signature}}
```

---

#### 11. Remodelers - Meta Angle 2: Looking for More Leads
- **ID**: `268d24a8-120c-4319-a50b-1e063968fc22`
- **Angle Tag**: `remodelers_meta_angle_2`
- **Type**: `plain`
- **Subject**: `Are you looking for more leads? `
- **Body**:
```text
Hey {{owner_first_name}},

I’ll let you in on a secret, I didn't manually write this email. Our AI searched your area, found {{short_name}}, and handled the outreach.

Contractors are using this exact system to automatically find and pitch commercial jobs so they can stop fighting over expensive shared leads. And it doesn't just stop at outreach. Once the AI finds those jobs, the platform also gives you an AI receptionist to handle incoming calls, quick proposal generators, and even 3D design software to help close the deal.

We're working with about 30 contractors right now to refine this. Open to a brief 15-minute chat to share your feedback on what we've built?

{{signature}}
```

---

#### 12. Remodelers - Meta Angle 3: How We Got Your Email
- **ID**: `ea40072d-8873-43a0-b1fe-6200ba4f9205`
- **Angle Tag**: `remodelers_meta_angle_3`
- **Type**: `plain`
- **Subject**: `How we got your email`
- **Body**:
```text
Hey {{owner_first_name}},

Curious how we got your email? We didn't buy a list or pay for a shared lead.

We built an AI that actively hunts for commercial opportunities, finds the right contact, and sends a personalized message automatically. It found you, and it’s the exact system we’re equipping remodeling businesses with to bring warm deals straight to their inbox.

Not only does it find the leads, but ContractorOps ({{product_url}}) actually helps you run the business, too. We baked in features like an AI receptionist that texts leads back instantly, professional proposal automation, 3D design visualization, and subcontractor management.

Mind if I send over a quick screenshot of the commercial jobs our AI can pull up around your zip code?

{{signature}}
```

---

#### 13. Remodelers - Meta Angle 4: Exclusive Commercial Leads
- **ID**: `9b4a73b6-2db1-4796-a787-ceae6b098cc4`
- **Angle Tag**: `remodelers_meta_angle_4`
- **Type**: `plain`
- **Subject**: `Curious how we found {{short_name}}?`
- **Body**:
```text
Hey {{owner_first_name}},

Wondering how this email reached you? Our AI outreach engine found {{short_name}}, realized you were a great fit, and reached out automatically.

We built this engine to help remodeling businesses scale by finding their own exclusive commercial leads. But we didn't stop there, we also built out the rest of the platform to include a 24/7 AI receptionist, automated estimates, 3D project design, and crew dispatching so you can manage the entire lifecycle of those new leads.

I’d love to get your eyes on it and hear your thoughts as a local expert. Would you be open to a quick 15-minute chat this week?

{{signature}}
```

---

### Category 3: General Contractor & Universal Angles (7 Templates)

#### 1. General - Admin Automation in State
- **ID**: `b81ced53-77b3-4de4-9600-e0f25e1ee3b8`
- **Angle Tag**: `admin_automation`
- **Type**: `plain`
- **Subject**: `{{category}} ops in {{city}}`
- **Body**:
```text
Hey {{owner_first_name}},

I'm building a tool specifically for {{category}} businesses in {{state}} to automate the admin work and estimate follow-ups.

We're already helping around {{client_count}} {{client_noun}} streamline their operations so they can focus on actual jobs instead of paperwork.

Would you be open to a quick 15-minute chat to see if {{product_name}} makes sense for {{short_name}}? You can grab a time here: {{booking_link}}

Thanks,
{{sender_name}}
```

---

#### 2. General - Direct Intro & Demo Pitch
- **ID**: `60cb7bde-f3ee-4a35-a44e-915431ff6524`
- **Angle Tag**: `direct_intro`
- **Type**: `plain`
- **Subject**: `{{short_name}}`
- **Body**:
```text
Hey {{short_name}},

{{sender_name}} here. We built {{product_name}} to help {{category}} businesses handle leads, estimates, proposals, and follow-ups without stitching together five different tools. We're currently working with over {{client_count}} {{client_noun}} and would love your feedback.

Worth a quick 20-30 minute demo? {{booking_link}}

{{sender_name}}
```

---

#### 3. General - Feature Rundown & Local Proof
- **ID**: `e2b369c3-d143-49f0-97a4-1dc2698ed8b8`
- **Angle Tag**: `feature_rundown`
- **Type**: `plain`
- **Subject**: `helping {{category}} businesses in {{city}}`
- **Body**:
```text
Hey {{short_name}},

{{product_name}} helps teams capture leads, generate estimates, create proposals, follow up automatically, and coordinate their work.

We work with over {{client_count}} {{client_noun}} and would value your feedback. Quick demo: {{booking_link}}, or take a look: {{product_url}}

{{Signature}}
```

---

#### 4. General - Founder Story & Family Business
- **ID**: `ed2b0c35-3260-4a6d-86e2-d723ab2941a6`
- **Angle Tag**: `founder_story`
- **Type**: `plain`
- **Subject**: `quick question for {{owner_first_name}}`
- **Body**:
```text
Hey {{owner_first_name}},

Growing up in my family's {{category}} business https://www.topscorelandscaping.com/, I got tired of juggling a dozen different tools. Being an engineer, I built {{product_name}} ({{product_url}}) to handle everything in one simple place.

It helps {{client_noun}} capture leads, send quick estimates, and automate follow-ups without the busywork.

Right now, {{client_count}}+ {{client_noun}}  across Texas and California are using it to run their day-to-day smoothly.

Open to checking out a quick 10-minute demo this week? You can grab a spot here: {{booking_link}}

{{Signature}}
```

---

#### 5. General - Question Opener (Spreadsheet vs CRM)
- **ID**: `39ec1532-954d-4cbe-9176-60d3e99d22fd`
- **Angle Tag**: `question`
- **Type**: `plain`
- **Subject**: `how do you handle new leads?`
- **Body**:
```text
Hey {{short_name}},

Quick question: when a new lead comes in, how are you handling the estimate and follow-up today, spreadsheets, a CRM, or mostly by hand?

I built {{product_name}} to make that workflow simpler. Happy to show you if it's relevant: {{booking_link}}

{{sender_name}}
```

---

#### 6. General - Short Callback Request
- **ID**: `2afa3fb5-05fd-41be-a854-048d0f7d08ff`
- **Angle Tag**: `call_back`
- **Type**: `plain`
- **Subject**: `quick one for {{short_name}}`
- **Body**:
```text
Hey {{short_name}},

I'm {{sender_name}}. I built a system that helps teams capture leads, send estimates and proposals, and follow up automatically. If that sounds useful, just reply here and I'll walk you through it.

{{sender_name}}
```

---

#### 7. General - Simple HTML Intro Card
- **ID**: `c1edb774-f4a7-4092-a6a2-6b10aea8f9e2`
- **Angle Tag**: `html_styled`
- **Type**: `html`
- **Subject**: `{{short_name}}`
- **Body**:
```html
<div style="font:16px/1.55 Arial,sans-serif;color:#172033;max-width:560px"><p>Hey {{short_name}},</p><p>{{sender_name}} here. We built <strong>{{product_name}}</strong> to help {{category}} businesses handle leads, estimates, proposals, and follow-ups.</p><p><a href="{{booking_link}}" style="display:inline-block;background:#2563eb;color:white;text-decoration:none;padding:10px 16px;border-radius:6px">Book a demo</a></p><p>{{sender_name}}</p></div>
```

---

