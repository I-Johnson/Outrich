---
name: ui-style
description: Outrich UI rules - design tokens, customer vs admin templates, fail-closed JavaScript, honest copy. Use for any template, CSS, or client-side JS change.
---

# UI style

## Tokens (reuse, don't invent)

Defined at the top of `app/web/static/outreach.css`, shared by `freight.css`:

- Colors: `--fog #dce1db`, `--paper #eef1ec`, `--card #f8f9f6`,
  `--ink #15201c`, `--ink2 #48544e`, `--muted #77827b`, `--line #c6cec5`,
  `--pine #1d4a3e`, `--leaf #2d8a58`, `--amber #b87a0c`; freight blue `#1e5aa8`.
- Type: Bricolage Grotesque (headings), Instrument Sans (body).

## Hard rules

- No emojis in the UI.
- No `innerHTML` with dynamic values - build DOM with `textContent` nodes.
- No page-reload polling; scoped fetch + DOM updates, exponential backoff,
  stop on completion and after repeated failures (with a visible note).
- Latest-wins fetches go through `app/web/static/count-gate.js` (UMD, also
  node-testable). Invalidate in-flight requests the moment inputs change -
  before any debounce.
- Copy matches semantics: "eligible to send, before message checks";
  snapshots are not funnels; "Queued is not reached".
- Status is never color alone (dot + word). Steppers/tabs carry ARIA roles.

## Surfaces

- Customer: `templates/customer/` (Overview, Campaigns, Campaign, Settings,
  import preview) on `customer/base.html` - top nav, calm and focused.
- Admin: root templates on `base.html`; Freight screens render with
  `workspace == 'freight'` (sidebar, `freight.css`). Branch on the
  `workspace` / `is_admin` context from `page()`, not on URLs.

## Verify

Mock first for new screens. Before calling UI work done, render the affected
pages headlessly and look at the screenshots (the project pattern: seed a
local instance, drive headless Chrome, capture each page).
