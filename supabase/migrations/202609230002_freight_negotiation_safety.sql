alter table freight_loads
  add column if not exists loaded_miles_verified boolean not null default false,
  add column if not exists deadhead_miles_verified boolean not null default false,
  add column if not exists destination_verified boolean not null default false,
  add column if not exists origin_verified boolean not null default false,
  add column if not exists equipment_verified boolean not null default false,
  add column if not exists pickup_date_verified boolean not null default false,
  add column if not exists schedule_verified boolean not null default false,
  add column if not exists weight_lbs numeric;

create table if not exists freight_negotiation_events (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references freight_threads on delete cascade,
  source_id uuid not null,
  event_type text not null check (event_type in ('offer', 'counter')),
  amount numeric not null,
  unit text not null default 'total_rate',
  details jsonb not null default '{}',
  created_at timestamptz not null default now(),
  unique(event_type, source_id)
);
create index if not exists freight_negotiation_events_thread_idx
  on freight_negotiation_events(thread_id, created_at);
alter table freight_negotiation_events enable row level security;
