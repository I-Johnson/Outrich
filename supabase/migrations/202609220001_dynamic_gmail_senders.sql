create table if not exists gmail_senders (
  id text primary key,
  email text not null unique,
  display_name text not null default '',
  signature text not null default '',
  reply_to text not null default '',
  app_password_encrypted text not null default '',
  active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into gmail_senders (
  id, email, display_name, signature, reply_to, app_password_encrypted
)
select
  '1', gmail_user, coalesce(sender_name, ''), coalesce(email_signature, ''),
  coalesce(nullif(reply_to, ''), gmail_user), coalesce(gmail_app_password_encrypted, '')
from settings
where id = 1 and nullif(gmail_user, '') is not null
on conflict (id) do nothing;

alter table gmail_senders enable row level security;

comment on table gmail_senders is
  'Encrypted, independently branded Gmail SMTP senders selectable by campaigns.';
