alter table campaigns
  add column if not exists gmail_accounts text[] not null default array['1'];

alter table email_log
  add column if not exists sender_account text not null default '1';

comment on column campaigns.gmail_accounts is
  'Gmail sender account IDs selected for this campaign; daily_cap applies per selected sender.';
comment on column email_log.sender_account is
  'Gmail account ID assigned when the durable send row is queued.';
