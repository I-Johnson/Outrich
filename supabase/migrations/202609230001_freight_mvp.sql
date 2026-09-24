alter table email_templates
  add column if not exists vertical text not null default 'outreach';

create table if not exists freight_truck_profiles (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  current_city text not null default '',
  current_state text not null default '',
  equipment_type text not null default '',
  trailer_length_ft integer,
  max_weight_lbs integer,
  team_status text not null default '',
  mc_number text not null default '',
  dot_number text not null default '',
  dispatcher_name text not null default '',
  dispatcher_phone text not null default '',
  shareable_fields jsonb not null default '[]',
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists freight_settings (
  id smallint primary key default 1 check (id = 1),
  sender_name text not null default 'Freight Dispatch',
  email_signature text not null default 'Freight Dispatch',
  reply_to text not null default '',
  default_sender_account text not null default '1',
  default_template_id uuid references email_templates on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists freight_missions (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  truck_profile_id uuid references freight_truck_profiles on delete set null,
  origin_city text not null default '',
  origin_state text not null default '',
  origin_deadhead_miles integer not null default 0,
  pickup_start date,
  pickup_end date,
  equipment_type text not null default '',
  trailer_length_ft integer,
  max_weight_lbs integer,
  destinations jsonb not null default '[]',
  floor_total numeric,
  target_total numeric,
  floor_loaded_rpm numeric,
  floor_all_in_rpm numeric,
  target_all_in_rpm numeric,
  counter_amount numeric,
  maximum_counter_rounds integer not null default 2,
  permissions jsonb not null default '{}',
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists freight_loads (
  id uuid primary key default gen_random_uuid(),
  mission_id uuid references freight_missions on delete set null,
  truck_profile_id uuid references freight_truck_profiles on delete set null,
  broker_email text not null,
  broker_company text not null default '',
  origin_city text not null,
  origin_state text not null default '',
  destination_city text not null,
  destination_state text not null default '',
  pickup_date date,
  loaded_miles numeric,
  deadhead_miles numeric,
  posted_rate numeric,
  current_offer numeric,
  dat_reference text not null default '',
  equipment_type text not null default '',
  template_id uuid references email_templates on delete set null,
  sender_account text not null default '1',
  subject text not null,
  status text not null default 'draft',
  current_round integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists freight_loads_status_idx on freight_loads(status, updated_at desc);

create table if not exists freight_threads (
  id uuid primary key default gen_random_uuid(),
  load_id uuid not null unique references freight_loads on delete cascade,
  sender_account text not null,
  recipient_email text not null,
  subject text not null,
  root_message_id text,
  last_message_id text,
  last_imap_uid bigint,
  state text not null default 'sent',
  last_activity_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists freight_messages (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references freight_threads on delete cascade,
  direction text not null check (direction in ('out', 'in')),
  provider_message_id text unique,
  from_email text not null default '',
  to_email text not null default '',
  subject text not null default '',
  body_text text not null default '',
  classification jsonb not null default '{}',
  status text not null default 'received',
  created_at timestamptz not null default now()
);

create table if not exists freight_drafts (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references freight_threads on delete cascade,
  in_reply_to_message_id text,
  subject text not null,
  body_text text not null,
  reason text not null default '',
  policy_snapshot jsonb not null default '{}',
  status text not null default 'pending',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists freight_alerts (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references freight_threads on delete cascade,
  kind text not null,
  summary text not null,
  status text not null default 'open',
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

create table if not exists freight_mail_cursors (
  id uuid primary key default gen_random_uuid(),
  sender_account text not null unique,
  last_imap_uid bigint not null default 0,
  last_checked_at timestamptz,
  error text,
  updated_at timestamptz not null default now()
);

alter table freight_truck_profiles enable row level security;
alter table freight_settings enable row level security;
alter table freight_missions enable row level security;
alter table freight_loads enable row level security;
alter table freight_threads enable row level security;
alter table freight_messages enable row level security;
alter table freight_drafts enable row level security;
alter table freight_alerts enable row level security;
alter table freight_mail_cursors enable row level security;

insert into email_templates (name, subject, body, type, angle_tag, active, vertical)
select
  'Freight — Load inquiry',
  'Truck available — {{equipment}}',
  E'Hi,\n\nI am interested in the load you posted. I have a {{equipment}} available near {{truck_location}}.\n\nMC {{mc_number}}\nPlease send the pickup, delivery, miles, weight, and rate.\n\n{{signature}}',
  'plain',
  'freight_first_touch',
  true,
  'freight'
where not exists (
  select 1 from email_templates where vertical = 'freight' and angle_tag = 'freight_first_touch'
);

update email_templates
set subject = 'Truck available — {{equipment}}',
    body = E'Hi,\n\nI am interested in the load you posted. I have a {{equipment}} available near {{truck_location}}.\n\nMC {{mc_number}}\nPlease send the pickup, delivery, miles, weight, and rate.\n\n{{signature}}',
    updated_at = now()
where vertical = 'freight' and angle_tag = 'freight_first_touch';

insert into freight_settings(id)
values(1)
on conflict (id) do nothing;

update freight_settings
set default_template_id = (
  select id from email_templates where vertical = 'freight' and angle_tag = 'freight_first_touch' limit 1
)
where default_template_id is null;
