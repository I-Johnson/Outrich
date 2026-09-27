# AGENTS.md - app/core/

Domain logic. No HTTP in here: functions take `storage=` (an `OwnerStore`)
and raise `ValueError` with user-readable messages for domain rejections.

## sender.py - queueing and sending

- **Single source of truth for eligibility**: `eligible_client_ids` (status
  exclusions, suppression, resend-block window) and `AUDIENCE_SCAN_LIMIT`
  (10k, `created_at asc`). The `/campaigns/audience-count` endpoint and the
  builder's promises mirror these exactly - change them here, not a copy.
- `queue_campaign` is fail-closed: zero matching or zero eligible leads raise
  (unless the campaign still has pending sends, so resumes keep working).
- `_schedule_config` defaults only on missing/`None`. A saved `send_days: []`
  stays empty and `next_send_time` raises "Sending is off" - an empty set is
  a real user choice, never a falsy-default away.
- Sender rotation assigns each queued row a durable `sender_account`; retries
  keep it. Send cursors honor per-account jitter, daily caps, and the global
  window. Never send from a request handler.
- `recover_interrupted_sends` marks stale `sending` rows `failed` for review.
  An unknowable outcome must never be re-sent automatically.

## schedule.py

- `parse_days("")` and `parse_days([])` both mean "no days". `next_send_time`
  raises `ValueError` on an empty day set rather than looping - keep it total
  (bounded scan, no infinite loops) and keep the error message actionable.

## tenancy.py - isolation

- `OwnerStore` re-scopes every operation to one owner id; legacy rows without
  `owner_id` belong to `ADMIN_OWNER_ID`. Freight tables additionally filter
  by vertical (`gmail_senders`, `email_templates`, `freight_settings` are
  handled specially). New tables with customer data need owner scoping here
  AND in `app/db.py` (`JSON_FIELDS`, freight table list) as applicable.
- `outreach_owner_ids` / `freight_owner_ids` drive the scheduler's per-owner
  loops. New owner-scoped work queues must be added there.

## importer.py

- All functions take `storage=` - the module-level store is never correct for
  customer imports. Column mapping, preview, confirm, remap, and undo all
  operate inside one account.

## freight.py / freight_agent.py

- Gemini interprets broker replies; code enforces missions, rate math, and
  send permissions. Model failure pauses the thread for review - it never
  guesses a send. `FREIGHT_AGENT_MODE=rules` is the offline fallback.
- The agent-test routes exercise the real `evaluate_inbound` path; keep test
  hooks honest (no parallel logic that can drift).

## crypto.py / security.py / auth.py

- Gmail app passwords are Fernet-encrypted at rest (`ENCRYPTION_KEY`);
  passwords are scrypt-hashed. Never log secrets, and never return decrypted
  app passwords to templates or API responses.
