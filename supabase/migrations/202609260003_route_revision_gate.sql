-- Route-revision gate: when a broker restatement drops a stop, the booking's
-- immutable snapshot still holds the old route. This flag blocks booking until
-- a dispatcher approves the revised route, which re-captures the snapshot.
alter table freight_bookings add column if not exists route_revision_pending boolean not null default false;
