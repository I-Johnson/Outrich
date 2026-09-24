#!/usr/bin/env python3
"""Create (or update) the outreach campaigns.

Edit the copy here and re-run to update it:
    docker compose exec web python seed_campaign.py

Only {{company}} is personalized per lead. {{sender_first}} and {{sender_title}}
resolve to whichever persona actually sends, so the signature always matches the
From address.

Two variants exist on purpose:

  A "styled"  — the original: headed bullets, bold labels, inline CSS.
  B "plain"   — the same offer written the way a person types an email.

Gmail sorts on the *shape* of the message as much as the words. Bulleted feature
blocks, bold labels and a styled wrapper are the exact fingerprint of a bulk
marketing template, which is what pushes mail into Promotions. Variant B drops
all of it. Send both to yourself and keep whichever lands in Primary.
"""
from app.db import connect, init_db

# ── Variant A: the original styled template ──────────────────────────────────
A_NAME = "New LLC — discovery call (styled)"
A_SUBJECT = "quick note for {{company}}"
A_BODY = """
<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#1a1d21">
<p>Congrats on getting {{company}} off the ground.</p>

<p>I am {{sender_first}} from ContractorOps. Our team works closely with new
contractors to take that weight off your shoulders. Instead of figuring it all
out through trial and error, we roll up our sleeves and help you ramp up the
right way:</p>

<ul style="padding-left:20px">
  <li style="margin-bottom:8px"><b>Get your presence dialed in:</b> We build you a
  clean, professional website from scratch so homeowners trust you and reach out
  immediately.</li>
  <li style="margin-bottom:8px"><b>Never miss a lead:</b> Our AI receptionist handles
  calls, texts, and escalation instantly, while our outreach engine helps source
  local jobs so you aren't fighting over expensive shared leads.</li>
  <li style="margin-bottom:8px"><b>Run like a pro from day one:</b> We set you up with
  instant estimating templates for each job, professional proposals, organized
  client management, and automated follow-ups so you operate like a seasoned crew
  without burning out.</li>
</ul>

<p>Our team is already working side by side with 40+ contractors across parts of
Texas and California, helping them get organized and stay busy.</p>

<p>Would you be open to a quick chat with our team sometime this week to see if we
can help you get things moving faster?</p>

<p>Schedule a free demo with us:
<a href="https://cal.com/johnson-subedi/30min">https://cal.com/johnson-subedi/30min</a></p>

<p style="margin-top:22px">{{sender_first}}<br>
<span style="color:#666">{{sender_title}}, ContractorOps</span></p>
</div>
""".strip()


# ── Variant B: written like a person, not a template ─────────────────────────
# No wrapper div, no inline CSS, no bullet list, no bold labels, one link.
# Same offer, ~40% fewer words.
B_NAME = "New LLC — discovery call (plain)"
B_SUBJECT = "{{company}}"
B_BODY = """
<p>Hi, saw {{company}} just got going. Congrats.</p>

<p>I'm {{sender_first}} with ContractorOps. We help new contractors get the
business side sorted early: a real website, an AI receptionist that picks up
calls and texts while you're on a job, and estimate templates so you can quote
without sitting down at night.</p>

<p>We're doing this with 40+ contractors around Texas and California right now.</p>

<p>Worth a quick call this week? You can reply here, or grab a slot:
<a href="https://cal.com/johnson-subedi/30min">cal.com/johnson-subedi/30min</a></p>

<p>{{sender_first}}<br>ContractorOps</p>
""".strip()


def upsert(con, name: str, subject: str, body: str) -> tuple[int, str]:
    row = con.execute("SELECT id FROM campaigns WHERE name=?", (name,)).fetchone()
    if row:
        cid = row["id"]
        con.execute("UPDATE steps SET subject=?, body_html=? WHERE campaign_id=? AND step_no=1",
                    (subject, body, cid))
        return cid, "updated"
    cur = con.execute("INSERT INTO campaigns(name,status) VALUES(?,'active')", (name,))
    cid = cur.lastrowid
    con.execute("INSERT INTO steps(campaign_id,step_no,subject,body_html,delay_days) "
                "VALUES(?,1,?,?,0)", (cid, subject, body))
    return cid, "created"


def main() -> None:
    init_db()
    with connect() as con:
        # Rename the original so the two variants sit side by side.
        con.execute("UPDATE campaigns SET name=? WHERE name=?",
                    (A_NAME, "New LLC — discovery call"))
        for name, subj, body in ((A_NAME, A_SUBJECT, A_BODY), (B_NAME, B_SUBJECT, B_BODY)):
            cid, action = upsert(con, name, subj, body)
            print(f"campaign {action}: #{cid}  {name}")
        con.commit()


if __name__ == "__main__":
    main()
