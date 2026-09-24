create extension if not exists pgcrypto;
create type client_status as enum ('new','queued','contacted','replied','demo_booked','do_not_contact','bounced');
create type campaign_state as enum ('draft','running','paused','done','stopped');
create type work_status as enum ('queued','running','done','failed');

create table clients (
 id uuid primary key default gen_random_uuid(), business_name text not null, short_name text not null,
 owner_first_name text, category text not null default '', city text not null default '', state text not null default '', zip text,
 website text, domain text, email text, phone text, source text not null check(source in ('scrape','csv_import','manual')),
 source_detail text, import_batch_id uuid, scrape_job_id uuid, outreach_angle text, notes text, extra jsonb not null default '{}',
 status client_status not null default 'new', last_contacted_at timestamptz, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create unique index clients_email_unique on clients(lower(email)) where nullif(email,'') is not null;
create unique index clients_domain_unique on clients(lower(domain)) where nullif(domain,'') is not null;
create index clients_filter_idx on clients(status, category, state, city);

create table email_templates (
 id uuid primary key default gen_random_uuid(), name text not null, subject text not null, body text not null,
 type text not null default 'plain' check(type in ('plain','html')), angle_tag text, active boolean not null default true,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table campaigns (
 id uuid primary key default gen_random_uuid(), name text not null, target_filter jsonb not null default '{}',
 template_ids uuid[] not null default '{}', provider text not null, gmail_accounts text[] not null default array['1'], daily_cap integer not null default 20,
 send_window jsonb not null default '{}', min_delay_minutes integer not null default 3, max_delay_minutes integer not null default 15,
 resend_block_days integer not null default 90, state campaign_state not null default 'draft', stopped_at timestamptz,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table email_log (
 id uuid primary key default gen_random_uuid(), client_id uuid references clients on delete cascade,
 template_id uuid references email_templates on delete set null, campaign_id uuid references campaigns on delete set null,
 provider text not null, sender_account text not null default '1', subject_sent text not null, body_sent text, scheduled_for timestamptz, sent_at timestamptz,
 status text not null default 'queued' check(status in ('queued','sending','sent','failed','bounced','replied','skipped')),
 attempt_count integer not null default 0, next_attempt_at timestamptz, error text, provider_message_id text,
 created_at timestamptz not null default now()
);
create index email_log_due_idx on email_log(status, scheduled_for, next_attempt_at);
create table scrape_presets (id uuid primary key default gen_random_uuid(), name text not null, config jsonb not null default '{}', created_at timestamptz not null default now(), updated_at timestamptz not null default now());
create table scrape_jobs (
 id uuid primary key default gen_random_uuid(), parent_id uuid, preset_id uuid references scrape_presets on delete set null,
 category text not null, state text not null, city text not null, result_limit integer not null default 30,
 status work_status not null default 'queued', found_count integer not null default 0, saved_count integer not null default 0,
 discarded_count integer not null default 0, serp_calls_used integer not null default 0, error text,
 created_at timestamptz not null default now(), started_at timestamptz, completed_at timestamptz, updated_at timestamptz not null default now()
);
create table scrape_discards (id uuid primary key default gen_random_uuid(), scrape_job_id uuid references scrape_jobs on delete cascade, business_name text, reason text not null, created_at timestamptz not null default now());
create table import_batches (
 id uuid primary key default gen_random_uuid(), filename text not null, uploaded_at timestamptz not null default now(), total_rows integer not null default 0,
 imported_rows integer not null default 0, duplicate_rows integer not null default 0, rejected_rows integer not null default 0,
 column_mapping jsonb not null default '{}', rejected_csv text, status text not null default 'preview'
);
create table settings (
 id smallint primary key default 1 check(id=1), sender_name text default '', sender_email text default '', reply_to text default '',
 sender_business text default '', sender_business_url text default '', product_name text default '', product_url text default '',
 booking_link text default '', callback_number text default '', client_count integer not null default 20, client_noun text default 'clients',
 email_signature text default '', timezone text not null default 'America/New_York', send_days jsonb not null default '[0,1,2,3,4]',
 send_start text not null default '09:00', send_end text not null default '17:00', email_provider text default 'gmail',
 gmail_user text default '', gmail_app_password_encrypted text default '', daily_cap integer not null default 20,
 min_delay_minutes integer not null default 3, max_delay_minutes integer not null default 15, resend_block_days integer not null default 90,
 csv_required_fields jsonb not null default '["email"]', scrape_required_fields jsonb not null default '["email","phone"]',
 created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
insert into settings(id) values(1) on conflict do nothing;
create table suppression (id uuid primary key default gen_random_uuid(), email text, domain text, reason text, created_at timestamptz not null default now());
create unique index suppression_email_unique on suppression(lower(email)) where nullif(email,'') is not null;
create table jobs (id uuid primary key default gen_random_uuid(), kind text not null, payload jsonb not null default '{}', status work_status not null default 'queued', attempts integer not null default 0, run_after timestamptz not null default now(), locked_at timestamptz, error text, created_at timestamptz not null default now(), updated_at timestamptz not null default now());

alter table clients add constraint clients_import_batch_fk foreign key(import_batch_id) references import_batches(id) on delete set null;
alter table clients add constraint clients_scrape_job_fk foreign key(scrape_job_id) references scrape_jobs(id) on delete set null;

alter table clients enable row level security;
alter table email_templates enable row level security;
alter table campaigns enable row level security;
alter table email_log enable row level security;
alter table scrape_jobs enable row level security;
alter table scrape_discards enable row level security;
alter table scrape_presets enable row level security;
alter table import_batches enable row level security;
alter table settings enable row level security;
alter table suppression enable row level security;
alter table jobs enable row level security;
-- No anon/authenticated policies: only the server's service-role key has access.
