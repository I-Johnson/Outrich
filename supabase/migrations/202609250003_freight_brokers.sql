-- Broker identity with credit and setup tracking.
create table if not exists freight_brokers (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid references app_users(id) on delete cascade,
  vertical text not null default 'freight',
  legal_name text not null default '',
  mc_number text not null default '',
  domain text not null default '',
  emails jsonb not null default '[]',
  credit_status text not null default 'unknown',
  credit_score numeric,
  credit_notes text not null default '',
  setup_status text not null default 'not_started',
  blocked boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists freight_brokers_domain_idx on freight_brokers(domain);
create index if not exists freight_brokers_owner_idx on freight_brokers(owner_id);
alter table freight_loads add column if not exists broker_id uuid references freight_brokers(id) on delete set null;
