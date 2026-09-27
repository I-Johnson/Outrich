-- Stripe billing: subscription state on accounts, plus grandfathering.
-- Every account that exists when this migration runs keeps free access
-- (billing_exempt = 1). Accounts created after launch keep the column
-- default 0 and need an active subscription once Stripe keys are set.

alter table app_users add column if not exists stripe_customer_id text;
alter table app_users add column if not exists stripe_subscription_id text;
alter table app_users add column if not exists stripe_subscription_status text;
alter table app_users add column if not exists stripe_price_id text;
alter table app_users add column if not exists stripe_current_period_end text;
alter table app_users add column if not exists stripe_checkout_session_id text;
alter table app_users add column if not exists stripe_checkout_at text;
alter table app_users add column if not exists stripe_checkout_state text;
alter table app_users add column if not exists billing_exempt integer not null default 0;

-- Grandfather every pre-launch account.
update app_users set billing_exempt = 1 where billing_exempt = 0;

create index if not exists app_users_stripe_customer_idx on app_users(stripe_customer_id);
