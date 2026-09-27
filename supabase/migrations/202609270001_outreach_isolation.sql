-- Outreach account isolation: every outreach table gains an owner_id, and
-- pre-existing rows are assigned to the admin owner. Uniqueness for leads and
-- suppression entries moves from global to per-owner.

alter table clients add column if not exists owner_id text;
alter table campaigns add column if not exists owner_id text;
alter table email_log add column if not exists owner_id text;
alter table scrape_jobs add column if not exists owner_id text;
alter table scrape_discards add column if not exists owner_id text;
alter table scrape_presets add column if not exists owner_id text;
alter table import_batches add column if not exists owner_id text;
alter table pingram_replies add column if not exists owner_id text;
alter table suppression add column if not exists owner_id text;
alter table jobs add column if not exists owner_id text;
alter table email_templates add column if not exists owner_id text;

-- Settings becomes one row per owner (like freight_settings).
alter table settings add column if not exists owner_id text;
alter table settings alter column id type text using id::text;
alter table settings drop constraint if exists settings_id_check;

update clients set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update campaigns set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update email_log set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update scrape_jobs set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update scrape_discards set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update scrape_presets set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update import_batches set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update pingram_replies set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update suppression set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update jobs set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update email_templates set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;
update settings set owner_id = '00000000-0000-0000-0000-000000000001' where owner_id is null;

-- Per-owner uniqueness replaces the old global indexes.
drop index if exists clients_email_unique;
drop index if exists clients_domain_unique;
create unique index if not exists clients_email_unique on clients(owner_id, lower(email)) where email is not null and email <> '';
create unique index if not exists clients_domain_unique on clients(owner_id, lower(domain)) where domain is not null and domain <> '';
drop index if exists suppression_email_unique;
create unique index if not exists suppression_email_unique on suppression(owner_id, lower(email)) where email is not null and email <> '';
create unique index if not exists settings_owner_unique on settings(owner_id);

create index if not exists clients_owner_idx on clients(owner_id);
create index if not exists campaigns_owner_idx on campaigns(owner_id);
create index if not exists email_log_owner_idx on email_log(owner_id);
create index if not exists scrape_jobs_owner_idx on scrape_jobs(owner_id);
create index if not exists scrape_discards_owner_idx on scrape_discards(owner_id);
create index if not exists scrape_presets_owner_idx on scrape_presets(owner_id);
create index if not exists import_batches_owner_idx on import_batches(owner_id);
create index if not exists pingram_replies_owner_idx on pingram_replies(owner_id);
create index if not exists suppression_owner_idx on suppression(owner_id);
create index if not exists jobs_owner_idx on jobs(owner_id);
create index if not exists settings_owner_idx on settings(owner_id);
