-- Booking pipeline: agreed -> rate con review -> booked, with immutable snapshot.
create table if not exists freight_bookings (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid references app_users(id) on delete cascade,
  vertical text not null default 'freight',
  load_id uuid not null references freight_loads(id) on delete cascade,
  thread_id uuid not null references freight_threads(id) on delete cascade,
  status text not null default 'agreed' check (status in ('agreed', 'rate_con_review', 'booked', 'cancelled')),
  agreed_rate numeric not null,
  snapshot jsonb not null default '{}',
  rate_con_amount numeric,
  rate_con_diffs jsonb not null default '[]',
  rate_con_reviewed boolean not null default false,
  driver_handoff_approved boolean not null default false,
  source_message_id text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists freight_bookings_thread_idx on freight_bookings(thread_id, created_at);
create index if not exists freight_bookings_owner_idx on freight_bookings(owner_id);
