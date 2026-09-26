-- Ordered load stops (pickups and deliveries) with per-stop verification.
create table if not exists freight_load_stops (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid references app_users(id) on delete cascade,
  vertical text not null default 'freight',
  load_id uuid not null references freight_loads(id) on delete cascade,
  seq integer not null,
  kind text not null check (kind in ('pickup', 'delivery')),
  facility_name text not null default '',
  city text not null,
  state text not null default '',
  appointment text,
  appointment_verified boolean not null default false,
  verified boolean not null default false,
  evidence text not null default '',
  source_message_id text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists freight_load_stops_load_idx on freight_load_stops(load_id, seq);
create index if not exists freight_load_stops_owner_idx on freight_load_stops(owner_id);
