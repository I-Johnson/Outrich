"""AI-assisted cold email generation and refinement via Gemini."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.core.template_engine import ALLOWED_VARIABLES, unknown_variables

logger = logging.getLogger(__name__)

ALLOWED_VARS_DOC = """
- {{person_name}}: Prospect's first name if known or inferred from email (e.g. 'Roy'), falling back to business short name
- {{short_name}}: Prospect's cleaned business name (e.g. 'Apex Roofing')
- {{owner_first_name}}: Prospect's first name (or fallback to 'there')
- {{category}}: Prospect's trade or niche (e.g. 'roofing', 'HVAC')
- {{city}}: Prospect's city
- {{state}}: Prospect's state (2-letter abbreviation)
- {{sender_name}}: The sender's name (e.g. 'Johnson')
- {{product_name}}: The product being offered (e.g. 'ContractorOps')
- {{product_url}}: The product website URL
- {{booking_link}}: Meeting/demo booking link (e.g. Cal.com link)
- {{client_count}}: Current client count number (e.g. 30)
- {{client_noun}}: Client noun (e.g. 'contractors')
- {{callback_number}}: Optional phone number
- {{signature}}: Optional email signature
"""

SYSTEM_PROMPT = f"""You are a world-class cold email strategist and copywriter specializing in high-converting, low-volume B2B outreach.
Your emails read like genuine 1-on-1 messages sent by a real founder from personal Gmail, never an automated marketing blast.

Rules to follow:
1. TONE: Direct, casual, conversational, humble, and peer-to-peer. Never use corporate marketing fluff (forbidden: 'Hope this finds you well', 'I wanted to reach out', 'revolutionize', 'game changer', 'synergy', 'spearhead', 'best-in-class', 'touch base').
2. LENGTH: Short and respect the prospect's time. Keep body between 50 and 110 words. Use short, 1-2 sentence paragraphs with whitespace.
3. SUBJECT LINE: Natural, conversational, short (2-6 words), often lowercase. Examples: 'quick question for {{{{short_name}}}}', '{{{{short_name}}}} + {{{{product_name}}}}', 'estimate follow-ups in {{{{city}}}}'.
4. CALL TO ACTION (CTA): Low-friction, soft ask. Ask for feedback or a brief 15-20 min chat/demo using {{{{booking_link}}}}.
5. DYNAMIC VARIABLES: STRICT REQUIREMENT. Only use variables from this exact list:
{ALLOWED_VARS_DOC}
DO NOT invent any other variables (e.g., do NOT use {{{{first_name}}}}, {{{{company}}}}, {{{{business_name}}}}). Use only the exact allowed syntax.
6. BUSINESS KNOWLEDGE BASE: When a 'Detailed Business Knowledge Base & Product Context' is provided, mine its concrete value propositions, specific contractor pain points (e.g., proposal delays, emergency dispatching, lost leads), and unique advantages. Use these authentic details to make each email credible, sharp, and focused on real operational bottlenecks.

Return a JSON object with this exact structure:
{{
  "name": "Concise template title (e.g. Estimate Follow-up Angle)",
  "angle_tag": "short_snake_case_tag (e.g. estimate_followup, founder_story, problem_solution)",
  "subject": "The email subject line",
  "body": "The email body using allowed {{variables}}",
  "type": "plain"
}}
"""


def generate_or_improve(
    mode: str,
    instructions: str,
    business_context: dict[str, Any],
    existing_subject: str = "",
    existing_body: str = "",
    existing_name: str = "",
    existing_angle_tag: str = "",
) -> dict[str, Any]:
    """Generates a new template or improves an existing one using Gemini."""
    if not settings.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured in environment")

    biz_info = (
        f"Sender Name: {business_context.get('sender_name') or 'Founder'}\n"
        f"Product Name: {business_context.get('product_name') or 'ContractorOps'}\n"
        f"Product URL: {business_context.get('product_url') or 'https://www.contractorops.ai/'}\n"
        f"Booking Link: {business_context.get('booking_link') or 'https://cal.com/demo'}\n"
        f"Client Count: {business_context.get('client_count') or 30} {business_context.get('client_noun') or 'contractors'}\n"
    )
    raw_context = (business_context.get("business_context") or "").strip()
    if raw_context:
        biz_info += f"\nDetailed Business Knowledge Base & Product Context:\n\"\"\"\n{raw_context}\n\"\"\"\n"

    if mode == "improve":
        user_prompt = f"""Task: Improve and refine an existing cold email template.

Business Context & Knowledge Base:
{biz_info}

Existing Template:
- Name: {existing_name}
- Angle Tag: {existing_angle_tag}
- Subject: {existing_subject}
- Body:
{existing_body}

Improvement Instructions:
{instructions.strip() or 'Make it punchier, more conversational, and improve the hook and call-to-action.'}

Leverage specific details and pain points from the Business Knowledge Base. Preserve or enhance the appropriate {{{{variables}}}}. Ensure the tone feels like a human founder writing from a personal Gmail.
Return the updated JSON object.
"""
    else:
        user_prompt = f"""Task: Write a fresh high-converting cold outreach email template.

Business Context & Knowledge Base:
{biz_info}

Specific Instructions / Angle from User:
{instructions.strip() or 'Direct problem-solving angle helping trade contractors streamline estimates and follow-ups.'}

Draw from the Business Knowledge Base to ensure the pitch is grounded, specific, and tailored to real contractor challenges.
Return the complete JSON object with name, angle_tag, subject, body, and type ('plain').
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent"
        f"?key={settings.GEMINI_API_KEY}"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": SYSTEM_PROMPT + "\n\n" + user_prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.7,
        },
    }

    try:
        with httpx.Client(timeout=35) as client:
            res = client.post(url, json=payload)
            if res.status_code != 200:
                err_data = {}
                try: err_data = res.json().get("error", {})
                except Exception: pass
                msg = err_data.get("message") or res.text
                if res.status_code == 402 or "prepayment credits" in msg.lower():
                    raise ValueError("Gemini credits depleted: Please add prepayment credits at https://ai.studio/projects or update GEMINI_API_KEY in .env/Railway.")
                raise ValueError(f"Gemini API Error ({res.status_code}): {msg}")
    except httpx.RequestError as exc:
        raise ValueError(f"Network error connecting to Gemini API: {exc}")

    resp_json = res.json()
    candidates = resp_json.get("candidates", [])
    if not candidates:
        raise ValueError("Gemini returned no response candidates.")

    raw_text = candidates[0]["content"]["parts"][0]["text"]
    result = json.loads(raw_text)

    # Sanitize variables if model hallucinated any unsupported variables
    # e.g., {{first_name}} -> {{owner_first_name}}, {{company}} -> {{short_name}}
    replacements = {
        "{{first_name}}": "{{owner_first_name}}",
        "{{name}}": "{{owner_first_name}}",
        "{{company}}": "{{short_name}}",
        "{{company_name}}": "{{short_name}}",
        "{{business}}": "{{short_name}}",
        "{{business_name}}": "{{short_name}}",
        "{{Signature}}": "{{signature}}",
        "{{email_signature}}": "{{signature}}",
    }
    for field in ("subject", "body"):
        text = result.get(field, "")
        for old, new in replacements.items():
            text = text.replace(old, new)
        result[field] = text

    # Verify no invalid unknown variables remain
    unknown = unknown_variables(result.get("subject", "") + result.get("body", ""))
    if unknown:
        logger.warning("Unknown variables generated by Gemini: %s", unknown)

    return {
        "name": result.get("name") or (f"Improved {existing_name}" if mode == "improve" else "Custom Template"),
        "angle_tag": result.get("angle_tag") or (existing_angle_tag if mode == "improve" else "custom"),
        "subject": result.get("subject", ""),
        "body": result.get("body", ""),
        "type": result.get("type", "plain"),
    }
