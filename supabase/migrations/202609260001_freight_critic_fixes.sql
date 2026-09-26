-- Critic-round fixes: columns that existed only in the SQLite schema.
-- freight_load_stops.removed_at: soft-removal when a broker restates a route
-- without a stop (review alert is created alongside).
alter table freight_load_stops add column if not exists removed_at timestamptz;

-- freight_bookings: persisted rate-con terms, content-hash versions that bind
-- review/handoff approvals to exact terms, and the con source reference.
alter table freight_bookings add column if not exists rate_con_terms jsonb not null default '{}';
alter table freight_bookings add column if not exists rate_con_version text not null default '';
alter table freight_bookings add column if not exists rate_con_source text not null default '';
alter table freight_bookings add column if not exists rate_con_source_ref text not null default '';
alter table freight_bookings add column if not exists rate_con_review_version text not null default '';
alter table freight_bookings add column if not exists driver_handoff_version text not null default '';

-- freight_messages: crash-safe ingestion state (pending/processed/failed).
alter table freight_messages add column if not exists processing_state text;
alter table freight_messages add column if not exists processing_error text;
create index if not exists freight_messages_reconcile_idx
  on freight_messages(owner_id, direction, processing_state)
  where processing_state in ('pending', 'failed');

-- freight_brokers.identity_confirmed: a new email matched to an existing
-- broker by company domain stays unconfirmed until a dispatcher confirms it;
-- unconfirmed identity blocks booking readiness.
alter table freight_brokers add column if not exists identity_confirmed boolean not null default true;
