---
name: campaign-send
description: How Outrich campaign queueing, eligibility, scheduling, and sending fit together. Use before changing sender.py, schedule.py, campaign routes, or the builder's audience logic.
---

# Campaign send pipeline

Read order: `app/core/sender.py` -> `app/core/schedule.py` -> campaign routes
in `app/main.py` -> `app/web/templates/customer/campaigns.html`.

## Invariants (do not break)

- **Eligibility has one definition**: `eligible_client_ids` +
  `AUDIENCE_SCAN_LIMIT` in `sender.py`. The builder's count endpoint
  (`/campaigns/audience-count`) and `queue_campaign` both use them. If you
  change an exclusion (status, suppression, resend-block), change it there
  only - a second copy is a lie to the user.
- **Empty means off**: a saved `send_days: []` disables sending.
  `_schedule_config` defaults only when the key is missing/`None`;
  `next_send_time` raises `ValueError` on empty days. The route catches it
  and redirects with the notice; the campaign stays draft, zero log rows.
  Regression test: `test_empty_send_days_blocks_queueing`.
- **Fail closed on empty audiences**: `queue_campaign` raises when nothing
  matches or nothing is eligible, unless the campaign still has pending
  sends (resumes must keep working). Save and start routes surface the
  message as `?notice=`.
- **Queue, don't send**: handlers only write durable `email_log` rows
  (status `queued`, `scheduled_for`, assigned `sender_account`).
  `send_due` (scheduler) sends. `DRY_RUN` short-circuits real transport.
- **Interrupted sends are unknowable**: stale `sending` rows go to `failed`
  with a review path (`recover_interrupted_sends`). Never auto-requeue them.
- Scheduling math honors, per account: send window + timezone, send days,
  per-sender jitter (min/max delay), per-sender and global daily caps
  (`daily_cap`, pingram variants). Sender assignment is durable per log row
  so retries keep the same identity.

## When adding a campaign state or log status

Update: `campaign_detail` bucket map in `app/main.py`, the campaign detail
tabs in `customer/campaign.html`, `campaign_performance` in
`app/core/campaign_reporting.py`, and the state transitions in
`queue_campaign` / `refresh_campaign_states`.
