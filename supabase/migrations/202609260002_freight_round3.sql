-- Critic round 3: crash-safety and per-identity fixes.

-- Leased booking claim: a crashed booking flow leaves availability_status
-- 'booking' behind; the claim timestamp lets the truck recover as stale
-- instead of stranding forever.
alter table freight_truck_profiles add column if not exists booking_claim_at timestamptz;

-- Broker identity confirmation is per email link, never broker-wide: a new
-- same-domain alias blocks only itself until a dispatcher confirms it.
alter table freight_brokers add column if not exists unconfirmed_emails jsonb not null default '[]';

-- Durable inbound attachments: PDF bytes are persisted before processing so a
-- crash between inserting the email and parsing its attachment never loses
-- the document, and the booking's rate-con source reference stays resolvable.
create table if not exists freight_attachments (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid references app_users(id) on delete cascade,
  thread_id uuid not null references freight_threads(id) on delete cascade,
  message_id uuid not null references freight_messages(id) on delete cascade,
  filename text not null default '',
  content_b64 text not null default '',
  byte_size integer not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists freight_attachments_message_idx on freight_attachments(message_id);
create index if not exists freight_attachments_owner_idx on freight_attachments(owner_id);
alter table freight_attachments enable row level security;
