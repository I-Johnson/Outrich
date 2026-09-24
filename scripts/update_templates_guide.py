from __future__ import annotations

from pathlib import Path
from app.db import store

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT_PATH = Path("/Users/priyanshupyakurel/.gemini/antigravity/brain/507d01a7-d82d-4a06-9505-8c0d0ea7056c/email_templates_guide.md")
REPO_GUIDE_PATH = ROOT / "EMAIL_TEMPLATES_GUIDE.md"

def generate():
    templates = store.list("email_templates")
    
    # Sort: HVAC, then Remodelers, then General
    def sort_key(t):
        name = t.get("name") or ""
        if name.startswith("HVAC"):
            return (0, name.lower())
        elif name.startswith("Remodelers"):
            return (1, name.lower())
        return (2, name.lower())
        
    sorted_templates = sorted(templates, key=sort_key)
    
    hvac_list = [t for t in sorted_templates if (t.get("name") or "").startswith("HVAC")]
    remodel_list = [t for t in sorted_templates if (t.get("name") or "").startswith("Remodelers")]
    general_list = [t for t in sorted_templates if not (t.get("name") or "").startswith("HVAC") and not (t.get("name") or "").startswith("Remodelers")]
    
    doc = f"""# Cold Outreach Email Templates & Variable Guide

Comprehensive reference for email templates, dynamic variables, fallback rules, resolution lifecycle, and existing template catalog in the Outreach Pipeline.

---

## 1. How Email Templates Work

The Outreach Pipeline uses a custom lightweight template engine ([`app/core/template_engine.py`](file:///Users/priyanshupyakurel/Projects/outreach/app/core/template_engine.py)).

Templates support double-curly-brace variable placeholders (e.g., `{{{{owner_first_name}}}}`, `{{{{short_name}}}}`, `{{{{product_name}}}}`).

### Deliverability & Anti-Spam Design Principles:
1. **Strict Variable Validation**: Only whitelisted variables are permitted. Any unknown placeholder (like `{{{{first_name}}}}` or `{{{{company}}}}`) is rejected at save time to prevent unrendered variables from reaching prospects.
2. **Automated Em-Dash / En-Dash Sanitization**: The engine automatically converts em-dashes (`—`) and en-dashes (`–`) into natural commas (`, `). Modern spam filters and spam scanners flag em-dashes as a common AI copywriter fingerprint.
3. **Plain-Text Preference**: Plain text emails bypass promotional/marketing tab classification in Google Workspace and Outlook. 24 of the 25 built-in templates use `plain`.

---

## 2. Dynamic Variables & Fallback Rules

All variables are case-insensitive (e.g. `{{{{Signature}}}}` and `{{{{signature}}}}` resolve identically).

### A. Lead / Prospect Variables

| Variable | Description | Resolution & Fallback Hierarchy | Example |
|---|---|---|---|
| `{{{{owner_first_name}}}}` | Prospect's personal first name | 1. `lead.owner_first_name`<br>2. Inferred from lead email (e.g., `blair@...` &rarr; `Blair`)<br>3. `"{{{{short_name}}}} Team"` (if no personal name detected)<br>4. `"there"` (if no company name either) | `Blair` or `Elite Remodeling Team` |
| `{{{{person_name}}}}` | Alias for `{{{{owner_first_name}}}}` | Same resolution as `{{{{owner_first_name}}}}` | `Bryan` |
| `{{{{short_name}}}}` | Cleaned business name | 1. `lead.short_name`<br>2. `lead.business_name`<br>3. Fallback: `"there"` | `Blair Jones` (from *Blair Jones Co.*) |
| `{{{{category}}}}` | Trade, specialty, or niche | 1. `lead.category`<br>2. Fallback: `"contractor"` | `remodeling`, `HVAC`, `contractor` |
| `{{{{city}}}}` | Prospect city | 1. `lead.city`<br>2. Fallback: `"your area"` | `San Antonio`, `Dallas`, `Austin` |
| `{{{{state}}}}` | Prospect 2-letter state | 1. `lead.state`<br>2. Fallback: `""` (empty string) | `TX`, `CA`, `FL` |

### B. Sender & Business Variables

| Variable | Description | Resolution Source | Example |
|---|---|---|---|
| `{{{{sender_name}}}}` | Representative's display name | Active sender persona display name; fallback to default settings | `Sofia`, `Brooke`, `Mariana` |
| `{{{{sender_email}}}}` | Representative's email address | Active sender email address; fallback to default settings | `sofia@contractorops.ai` |
| `{{{{signature}}}}` | Representative's email signature | Active sender persona signature; fallback to default signature | `Sofia\\nContractor Success @ ContractorOps` |
| `{{{{email_signature}}}}` | Alias for `{{{{signature}}}}` | Same as `{{{{signature}}}}` | `Brooke\\nContractor Growth Specialist @ ContractorOps` |
| `{{{{product_name}}}}` | Product / Service name | Business settings `product_name` | `ContractorOps` or `ContractorOps AI` |
| `{{{{product_url}}}}` | Website or product landing page | Business settings `product_url` | `https://www.contractorops.ai/` |
| `{{{{booking_link}}}}` | Calendly / Cal.com demo booking URL | Business settings `booking_link` | `https://cal.com/contractorops/demo` |
| `{{{{calendar_link}}}}` | Alias for `{{{{booking_link}}}}` | Same as `{{{{booking_link}}}}` | `https://cal.com/contractorops/demo` |
| `{{{{sender_business}}}}` | Company name | Business settings `sender_business` | `ContractorOps` |
| `{{{{sender_business_url}}}}` | Business homepage | Business settings `sender_business_url` | `https://contractorops.ai` |
| `{{{{client_count}}}}` | Active client / social proof count | Business settings `client_count` | `30` |
| `{{{{client_noun}}}}` | Social proof noun | Business settings `client_noun` | `contractors`, `remodelers` |
| `{{{{callback_number}}}}` | Contact phone number | Business settings `callback_number` | `(555) 234-5678` |

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
       │ (Pingram: rotates rep persona; updates {{{{sender_name}}}}, {{{{sender_email}}}}, {{{{signature}}}})
       ▼
[Dispatch via SMTP / Pingram API]
       │ (Final rendered text sent directly to recipient)
```

1. **Re-Rendering During Conveyor Belt Rotation**: When Pingram rotates through its 20 domain representatives on the 24/7 conveyor belt, the subject, body, and signature are dynamically re-rendered for each scheduled slot to match the assigned representative.
2. **Forbidden Variables**: Do **NOT** use `{{{{first_name}}}}` (use `{{{{owner_first_name}}}}`), `{{{{company}}}}` (use `{{{{short_name}}}}`), or `{{{{name}}}}`.
3. **Em-Dash Replacement**: Any `—` or `–` in template text is automatically sanitized to `, ` upon send.

---

## 4. Full Catalog of All {len(sorted_templates)} Templates

Summary:
- **HVAC Specialized**: {len(hvac_list)} templates
- **Remodelers Specialized**: {len(remodel_list)} templates
- **General Contractor & Universal Angles**: {len(general_list)} templates

"""

    def render_section(title, items):
        s = f"### {title}\n\n"
        for idx, t in enumerate(items, 1):
            s += f"#### {idx}. {t.get('name')}\n"
            s += f"- **ID**: `{t.get('id')}`\n"
            s += f"- **Angle Tag**: `{t.get('angle_tag') or 'general'}`\n"
            s += f"- **Type**: `{t.get('type') or 'plain'}`\n"
            s += f"- **Subject**: `{t.get('subject')}`\n"
            s += "- **Body**:\n"
            code_lang = "html" if t.get("type") == "html" else "text"
            s += f"```{code_lang}\n{t.get('body')}\n```\n\n---\n\n"
        return s

    doc += render_section("Category 1: HVAC Specialized Templates (5 Templates)", hvac_list)
    doc += render_section("Category 2: Remodelers Specialized Templates (13 Templates)", remodel_list)
    doc += render_section("Category 3: General Contractor & Universal Angles (7 Templates)", general_list)

    REPO_GUIDE_PATH.write_text(doc)
    print(f"Updated {REPO_GUIDE_PATH}")
    
    if ARTIFACT_PATH.parent.exists():
        ARTIFACT_PATH.write_text(doc)
        print(f"Updated {ARTIFACT_PATH}")

if __name__ == "__main__":
    generate()
