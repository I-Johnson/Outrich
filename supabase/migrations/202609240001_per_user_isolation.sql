-- Per-user data isolation for the freight workspace.
-- Adds app_users, puts owner_id (+ vertical) on every freight table, the Gmail
-- sender list and freight templates, and backfills existing single-tenant data
-- to the env admin (fixed id 00000000-0000-0000-0000-000000000001).

create table if not exists app_users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,          -- lowercased + trimmed
  password_hash text not null default '',
  name text not null default '',
  role text not null default 'user',   -- 'user' | 'admin'
  created_at timestamptz not null default now(),
  last_login_at timestamptz
);
alter table app_users enable row level security;

-- The env admin. The app syncs the email from ADMIN_EMAIL at startup and never
-- accepts a password for this row (admin signs in with ADMIN_PASSWORD).
insert into app_users (id, email, name, role)
values ('00000000-0000-0000-0000-000000000001', 'admin@outrich.local', 'Admin', 'admin')
on conflict (id) do nothing;

do $$
declare
  t text;
begin
  foreach t in array array[
    'freight_truck_profiles', 'freight_missions', 'freight_loads', 'freight_threads',
    'freight_messages', 'freight_drafts', 'freight_negotiation_events', 'freight_alerts',
    'freight_mail_cursors'
  ] loop
    execute format('alter table %I add column if not exists owner_id uuid references app_users(id) on delete cascade', t);
    execute format('alter table %I add column if not exists vertical text not null default ''freight''', t);
    execute format('update %I set owner_id = %L where owner_id is null', t, '00000000-0000-0000-0000-000000000001');
    execute format('create index if not exists %I on %I(owner_id)', t || '_owner_idx', t);
  end loop;
end $$;

-- Gmail senders: unique per owner instead of globally.
alter table gmail_senders add column if not exists owner_id uuid references app_users(id) on delete cascade;
update gmail_senders set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
alter table gmail_senders drop constraint if exists gmail_senders_email_key;
create unique index if not exists gmail_senders_owner_email_unique on gmail_senders(owner_id, lower(email));
create index if not exists gmail_senders_owner_idx on gmail_senders(owner_id);

-- Templates: freight templates are per owner; outreach templates stay admin-only (owner_id null).
alter table email_templates add column if not exists owner_id uuid references app_users(id) on delete cascade;
update email_templates set owner_id = '00000000-0000-0000-0000-000000000001'
where vertical = 'freight' and owner_id is null;
create index if not exists email_templates_owner_idx on email_templates(owner_id);

-- Freight messages: the same broker email can land in two customers' inboxes.
alter table freight_messages drop constraint if exists freight_messages_provider_message_id_key;
create unique index if not exists freight_messages_owner_provider_unique
  on freight_messages(owner_id, provider_message_id)
  where provider_message_id is not null and provider_message_id <> '';

-- freight_settings: one row per owner instead of a singleton.
alter table freight_settings drop constraint if exists freight_settings_id_check;
alter table freight_settings alter column id drop default;
alter table freight_settings alter column id type text using id::text;
alter table freight_settings alter column id set default gen_random_uuid()::text;
alter table freight_settings add column if not exists owner_id uuid references app_users(id) on delete cascade;
update freight_settings set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
create unique index if not exists freight_settings_owner_unique on freight_settings(owner_id);
