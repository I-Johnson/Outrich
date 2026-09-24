from __future__ import annotations

import json
from pathlib import Path
from app.db import store, new_id, now_iso

ROOT = Path(__file__).resolve().parent.parent

RENAME_MAP = {
    "remodelers_meta_angle_1": "Remodelers - Meta Angle 1: How We Found You",
    "remodelers_meta_angle_2": "Remodelers - Meta Angle 2: Looking for More Leads",
    "remodelers_meta_angle_3": "Remodelers - Meta Angle 3: How We Got Your Email",
    "remodelers_meta_angle_4": "Remodelers - Meta Angle 4: Exclusive Commercial Leads",
    "remodeling_1 (with URL)": "Remodelers - AI Receptionist Intake (with URL)",
    "remodeling_1_no_url": "Remodelers - AI Receptionist Intake (No URL)",
    "remodeling_2 (with URL)": "Remodelers - Estimating & Proposals (with URL)",
    "remodeling_2_no_url": "Remodelers - Estimating & Proposals (No URL)",
    "remodeling_3 (with URL)": "Remodelers - Lead Maximizer (with URL)",
    "remodeling_3_no_url": "Remodelers - Lead Maximizer (No URL)",
    "remodeling_4 (with URL)": "Remodelers - Full Suite & 3D Design (with URL)",
    "remodeling_4_no_url": "Remodelers - Full Suite & 3D Design (No URL)",
    "Remodeler 3D Design Feature Pitch": "Remodelers - 3D Visualizer Pitch",
    "Founder story": "General - Founder Story & Family Business",
    "Direct intro": "General - Direct Intro & Demo Pitch",
    "Feature rundown": "General - Feature Rundown & Local Proof",
    "Question opener": "General - Question Opener (Spreadsheet vs CRM)",
    "Call back": "General - Short Callback Request",
    "Simple HTML intro": "General - Simple HTML Intro Card",
    "Admin Automation Angle": "General - Admin Automation in State",
}

HVAC_TEMPLATES = [
    {
        "name": "HVAC - Cold Season Surge & Dispatch",
        "angle_tag": "hvac_cold_season",
        "type": "plain",
        "subject": "Ready for the upcoming cold season, {{owner_first_name}}?",
        "body": """Hey {{owner_first_name}},

When we are past September, the calls come in faster than any office can route them. The shops that win the season are the ones with scheduling, dispatch, and estimates running in one place instead of on sticky notes.

{{product_name}} ({{product_url}}) handles the intake, books the visit, sends market based estimates, and follows up so no job slips while your team is slammed.

Worth a 15 minute chat before the next spike? {{booking_link}}

{{signature}}""",
    },
    {
        "name": "HVAC - Quoting Speed & Fast Estimates",
        "angle_tag": "hvac_quoting_speed",
        "type": "plain",
        "subject": "how fast does {{short_name}} send estimates?",
        "body": """Hey {{owner_first_name}},

Honest question. After a site visit, how long does it take {{short_name}} to get a professional estimate in the homeowner's inbox? The first shop to send a clean proposal usually wins the job, and most {{category}} teams lose days to manual quoting.

{{product_name}} generates market based estimates, builds the proposal, and automates the follow up so you are always first.

Curious how you are handling it today, and happy to show you ours: {{booking_link}}

{{signature}}""",
    },
    {
        "name": "HVAC - Meta Angle 1: Commercial Maintenance Scouting",
        "angle_tag": "hvac_meta_angle",
        "type": "plain",
        "subject": "Curious how we found you, {{owner_first_name}}?",
        "body": """Hey {{owner_first_name}},

Curious how this email reached you? It wasn't me, it was our AI.

We built an outreach engine that autonomously scouts local markets for commercial HVAC opportunities from property managers, HOAs, and facility directors, drafts personalized pitches, and delivers them. It just found {{short_name}}, wrote this email, and hit send.

Getting the commercial lead is only half the battle. We also built {{product_name}} ({{product_url}}) to handle the rest, like an AI receptionist that answers emergency service calls 24/7, market-based equipment estimates, technician dispatching, and automated proposal follow-ups so you never lose a job to phone tag.

Want me to run a quick search for your service area and show you the commercial HVAC maintenance contracts our AI can find?

{{signature}}""",
    },
    {
        "name": "HVAC - Meta Angle 2: Exclusive Service Contracts vs Shared Leads",
        "angle_tag": "hvac_meta_angle",
        "type": "plain",
        "subject": "tired of fighting over shared HVAC leads?",
        "body": """Hey {{owner_first_name}},

I'll let you in on a secret: I didn't manually type this email. Our AI searched your area, found {{short_name}}, and handled the outreach autonomously.

HVAC contractors are using this exact system to find and pitch commercial service contracts and property managers directly, so they can stop burning cash on expensive, shared residential leads.

And once those commercial opportunities respond, {{product_name}} ({{product_url}}) handles the operations: an AI receptionist that answers after-hours emergency calls, automated quote generation for unit changeouts, and instant technician dispatch.

We're working with about {{client_count}} {{client_noun}} right now to refine this. Open to a brief 15-minute chat to share your thoughts on what we've built?

{{booking_link}}

{{signature}}""",
    },
    {
        "name": "HVAC - Meta Angle 3: How We Found You (24/7 Dispatch & Intake)",
        "angle_tag": "hvac_meta_angle",
        "type": "plain",
        "subject": "How we found {{short_name}}",
        "body": """Hey {{owner_first_name}},

Wondering how we got your email? We didn't buy a stale list or bid on a shared lead.

We built an AI that actively scans your local market for commercial properties, reaches out to facility managers, and sets up high-ticket HVAC service agreements automatically. It identified {{short_name}}, and it's the exact engine we're handing to HVAC shops to bring commercial service contracts straight to their inbox.

Beyond outbound leads, {{product_name}} ({{product_url}}) runs the daily intake: a 24/7 AI receptionist that collects equipment details from frantic homeowners, schedules technician visits on your calendar, and automates proposal follow-ups before competitors even call back.

Mind if I send over a quick screenshot of the commercial facilities and property managers our AI can pull up around {{city}}?

{{signature}}""",
    },
]


def run():
    print("=== Step 1: Renaming existing templates in DB ===")
    existing = store.list("email_templates")
    renamed_count = 0
    for t in existing:
        current_name = t.get("name")
        if current_name in RENAME_MAP:
            new_name = RENAME_MAP[current_name]
            store.update("email_templates", t["id"], {"name": new_name, "updated_at": now_iso()})
            print(f"Renamed: '{current_name}' -> '{new_name}'")
            renamed_count += 1
    print(f"Renamed {renamed_count} existing templates.")

    print("\n=== Step 2: Inserting 5 HVAC templates into DB ===")
    existing_after = store.list("email_templates")
    existing_names = {t.get("name") for t in existing_after}
    inserted_count = 0
    for h in HVAC_TEMPLATES:
        if h["name"] in existing_names:
            print(f"Template '{h['name']}' already exists, skipping.")
            continue
        stamp = now_iso()
        record = {
            "id": new_id(),
            "name": h["name"],
            "angle_tag": h["angle_tag"],
            "type": h["type"],
            "subject": h["subject"],
            "body": h["body"],
            "active": True,
            "created_at": stamp,
            "updated_at": stamp,
        }
        store.insert("email_templates", record)
        print(f"Inserted: '{h['name']}' (Tag: {h['angle_tag']})")
        inserted_count += 1
    print(f"Inserted {inserted_count} new HVAC templates.")

    print("\n=== Step 3: Updating seed/templates.json ===")
    all_db = store.list("email_templates")
    seed_data = []
    for t in sorted(all_db, key=lambda x: (x.get("name") or "").lower()):
        seed_data.append({
            "name": t.get("name"),
            "angle_tag": t.get("angle_tag") or "",
            "type": t.get("type") or "plain",
            "subject": t.get("subject"),
            "body": t.get("body"),
        })
    seed_path = ROOT / "seed" / "templates.json"
    seed_path.write_text(json.dumps(seed_data, indent=2))
    print(f"Wrote {len(seed_data)} templates to {seed_path}")


if __name__ == "__main__":
    run()
