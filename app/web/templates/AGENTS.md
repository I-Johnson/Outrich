# AGENTS.md - app/web/templates/

Server-rendered Jinja. Two surfaces share one base design language:

- `customer/` - the customer surface (Overview, Campaigns, Campaign detail,
  Settings, import preview), extends `customer/base.html`. This is what
  signed-in non-admins see.
- Root templates - admin tools (leads, scraper, queue, inbox, template
  library) plus auth pages, landing, and all Freight screens, extend
  `base.html`.

## Rules

1. **No emojis in the UI.** Anywhere. Use words or the existing icon includes.
2. **Dynamic values go into DOM text nodes, never `innerHTML`** - the
   discovery poller builds rows with `createElement`/`textContent` for exactly
   this reason. Server-side, Jinja autoescape stays on; `|safe` needs a
   comment justifying it.
3. **Client-side state is fail-closed.** If a fetch fails or is in flight,
   block the dependent action and say so in words. Invalidate in-flight
   requests the moment inputs change (see `static/count-gate.js` - reuse it
   for any latest-wins fetch instead of rolling a new debounce).
4. **Copy is honest about semantics.** Snapshot vs funnel, matching records
   vs eligible-to-send ("before message checks"). If the backend can still
   skip a lead later, the UI says so.
5. **Design tokens** live at the top of `static/outreach.css` and are shared
   by `freight.css`: pine/fog/paper palette, Bricolage Grotesque for
   headings, Instrument Sans for body. Freight blue accents are `#1e5aa8`.
   Reuse tokens; don't invent new colors or fonts.
6. **No page-reload polling.** Scoped `fetch` + DOM updates with backoff and
   a stop condition (completion, and a failure cap with a visible note).
7. Freight templates render under `workspace == 'freight'` (sidebar layout,
   `freight.css`); Outreach under the top-nav. `page()` in `app/main.py`
   supplies `workspace` and `is_admin` - branch on those, not on URL
   sniffing in templates.
8. Accessibility: steppers/tabs get real roles and `aria-current`; status
   dots always pair with words, never color alone.
