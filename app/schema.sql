CREATE TABLE IF NOT EXISTS app_users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL,
  last_login_at TEXT
);
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS clients (
  id TEXT PRIMARY KEY, business_name TEXT NOT NULL, short_name TEXT NOT NULL,
  owner_first_name TEXT, category TEXT DEFAULT '', city TEXT DEFAULT '', state TEXT DEFAULT '', zip TEXT,
  website TEXT, domain TEXT, email TEXT, phone TEXT, source TEXT NOT NULL DEFAULT 'manual',
  source_detail TEXT, import_batch_id TEXT, scrape_job_id TEXT, outreach_angle TEXT, notes TEXT,
  extra TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'new', last_contacted_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS clients_email_unique ON clients(lower(email)) WHERE email IS NOT NULL AND email <> '';
CREATE UNIQUE INDEX IF NOT EXISTS clients_domain_unique ON clients(lower(domain)) WHERE domain IS NOT NULL AND domain <> '';
CREATE INDEX IF NOT EXISTS clients_filter_idx ON clients(status, category, state, city);

CREATE TABLE IF NOT EXISTS email_templates (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'plain', angle_tag TEXT, active INTEGER NOT NULL DEFAULT 1,
  vertical TEXT NOT NULL DEFAULT 'outreach',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, target_filter TEXT NOT NULL DEFAULT '{}', template_ids TEXT NOT NULL DEFAULT '[]',
  provider TEXT NOT NULL DEFAULT 'gmail', gmail_accounts TEXT NOT NULL DEFAULT '["1"]', daily_cap INTEGER NOT NULL DEFAULT 20,
  send_window TEXT NOT NULL DEFAULT '{}', min_delay_minutes INTEGER NOT NULL DEFAULT 3,
  max_delay_minutes INTEGER NOT NULL DEFAULT 15, resend_block_days INTEGER NOT NULL DEFAULT 90,
  state TEXT NOT NULL DEFAULT 'draft', stopped_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_log (
  id TEXT PRIMARY KEY, client_id TEXT REFERENCES clients(id) ON DELETE CASCADE,
  template_id TEXT REFERENCES email_templates(id) ON DELETE SET NULL,
  campaign_id TEXT REFERENCES campaigns(id) ON DELETE SET NULL, provider TEXT NOT NULL, sender_account TEXT NOT NULL DEFAULT '1',
  subject_sent TEXT NOT NULL, body_sent TEXT, scheduled_for TEXT, sent_at TEXT,
  status TEXT NOT NULL DEFAULT 'queued', attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT, error TEXT, provider_message_id TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS email_log_due_idx ON email_log(status, scheduled_for, next_attempt_at);
CREATE TABLE IF NOT EXISTS scrape_jobs (
  id TEXT PRIMARY KEY, parent_id TEXT, preset_id TEXT, category TEXT NOT NULL, state TEXT NOT NULL,
  city TEXT NOT NULL, result_limit INTEGER NOT NULL DEFAULT 30, status TEXT NOT NULL DEFAULT 'queued',
  found_count INTEGER NOT NULL DEFAULT 0, saved_count INTEGER NOT NULL DEFAULT 0,
  discarded_count INTEGER NOT NULL DEFAULT 0, serp_calls_used INTEGER NOT NULL DEFAULT 0,
  error TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scrape_discards (
  id TEXT PRIMARY KEY, scrape_job_id TEXT REFERENCES scrape_jobs(id) ON DELETE CASCADE,
  business_name TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scrape_presets (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_batches (
  id TEXT PRIMARY KEY, filename TEXT NOT NULL, uploaded_at TEXT NOT NULL, total_rows INTEGER NOT NULL DEFAULT 0,
  imported_rows INTEGER NOT NULL DEFAULT 0, duplicate_rows INTEGER NOT NULL DEFAULT 0,
  rejected_rows INTEGER NOT NULL DEFAULT 0, column_mapping TEXT NOT NULL DEFAULT '{}',
  rejected_csv TEXT, status TEXT NOT NULL DEFAULT 'preview'
);
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK (id = 1), sender_name TEXT DEFAULT '', sender_email TEXT DEFAULT '', reply_to TEXT DEFAULT '',
  sender_business TEXT DEFAULT '', sender_business_url TEXT DEFAULT '', product_name TEXT DEFAULT '', product_url TEXT DEFAULT '',
  booking_link TEXT DEFAULT '', callback_number TEXT DEFAULT '', client_count INTEGER NOT NULL DEFAULT 20,
  client_noun TEXT DEFAULT 'clients', email_signature TEXT DEFAULT '', business_context TEXT DEFAULT '', timezone TEXT NOT NULL DEFAULT 'America/New_York',
  send_days TEXT NOT NULL DEFAULT '[0,1,2,3,4]', send_start TEXT NOT NULL DEFAULT '09:00', send_end TEXT NOT NULL DEFAULT '17:00',
  email_provider TEXT DEFAULT 'gmail', gmail_user TEXT DEFAULT '', gmail_app_password_encrypted TEXT DEFAULT '',
  daily_cap INTEGER NOT NULL DEFAULT 20, min_delay_minutes INTEGER NOT NULL DEFAULT 3,
  max_delay_minutes INTEGER NOT NULL DEFAULT 15, resend_block_days INTEGER NOT NULL DEFAULT 90,
  pingram_daily_cap INTEGER NOT NULL DEFAULT 30, pingram_min_delay INTEGER NOT NULL DEFAULT 5,
  pingram_max_delay INTEGER NOT NULL DEFAULT 20,
  csv_required_fields TEXT NOT NULL DEFAULT '["email"]', scrape_required_fields TEXT NOT NULL DEFAULT '["email","phone"]',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gmail_senders (
  id TEXT PRIMARY KEY, owner_id TEXT, email TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
  signature TEXT NOT NULL DEFAULT '', reply_to TEXT NOT NULL DEFAULT '',
  app_password_encrypted TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
  provider TEXT NOT NULL DEFAULT 'gmail',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppression (
  id TEXT PRIMARY KEY, email TEXT, domain TEXT, reason TEXT, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS suppression_email_unique ON suppression(lower(email)) WHERE email IS NOT NULL AND email <> '';
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0, run_after TEXT NOT NULL, locked_at TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO settings(id, created_at, updated_at) VALUES(1, datetime('now'), datetime('now'));

CREATE TABLE IF NOT EXISTS pingram_replies (
  id TEXT PRIMARY KEY,
  client_id TEXT REFERENCES clients(id) ON DELETE CASCADE,
  from_email TEXT NOT NULL,
  subject TEXT,
  body_text TEXT,
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS freight_truck_profiles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  current_city TEXT NOT NULL DEFAULT '',
  current_state TEXT NOT NULL DEFAULT '',
  equipment_type TEXT NOT NULL DEFAULT '',
  trailer_length_ft INTEGER,
  max_weight_lbs INTEGER,
  team_status TEXT NOT NULL DEFAULT '',
  mc_number TEXT NOT NULL DEFAULT '',
  dot_number TEXT NOT NULL DEFAULT '',
  dispatcher_name TEXT NOT NULL DEFAULT '',
  dispatcher_phone TEXT NOT NULL DEFAULT '',
  truck_vin TEXT NOT NULL DEFAULT '',
  driver_name TEXT NOT NULL DEFAULT '',
  driver_cdl_number TEXT NOT NULL DEFAULT '',
  driver_cdl_state TEXT NOT NULL DEFAULT '',
  driver_phone TEXT NOT NULL DEFAULT '',
  availability_status TEXT NOT NULL DEFAULT 'available',
  booking_claim_at TEXT,
  available_from TEXT,
  shareable_fields TEXT NOT NULL DEFAULT '[]',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS freight_settings (
  id TEXT PRIMARY KEY,
  owner_id TEXT UNIQUE,
  sender_name TEXT NOT NULL DEFAULT 'Freight Dispatch',
  email_signature TEXT NOT NULL DEFAULT 'Freight Dispatch',
  reply_to TEXT NOT NULL DEFAULT '',
  default_sender_account TEXT NOT NULL DEFAULT '1',
  default_template_id TEXT REFERENCES email_templates(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO freight_settings(id, owner_id, created_at, updated_at) VALUES('1', '00000000-0000-0000-0000-000000000001', datetime('now'), datetime('now'));

CREATE TABLE IF NOT EXISTS freight_missions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  truck_profile_id TEXT REFERENCES freight_truck_profiles(id) ON DELETE SET NULL,
  origin_city TEXT NOT NULL DEFAULT '',
  origin_state TEXT NOT NULL DEFAULT '',
  origin_deadhead_miles INTEGER NOT NULL DEFAULT 0,
  pickup_start TEXT,
  pickup_end TEXT,
  equipment_type TEXT NOT NULL DEFAULT '',
  trailer_length_ft INTEGER,
  max_weight_lbs INTEGER,
  destinations TEXT NOT NULL DEFAULT '[]',
  floor_total REAL,
  target_total REAL,
  floor_loaded_rpm REAL,
  floor_all_in_rpm REAL,
  target_all_in_rpm REAL,
  counter_amount REAL,
  maximum_counter_rounds INTEGER NOT NULL DEFAULT 3,
  permissions TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS freight_loads (
  id TEXT PRIMARY KEY,
  mission_id TEXT REFERENCES freight_missions(id) ON DELETE SET NULL,
  truck_profile_id TEXT REFERENCES freight_truck_profiles(id) ON DELETE SET NULL,
  broker_email TEXT NOT NULL,
  broker_company TEXT NOT NULL DEFAULT '',
  broker_id TEXT,
  origin_city TEXT NOT NULL,
  origin_state TEXT NOT NULL DEFAULT '',
  origin_verified INTEGER NOT NULL DEFAULT 0,
  destination_city TEXT NOT NULL,
  destination_state TEXT NOT NULL DEFAULT '',
  destination_verified INTEGER NOT NULL DEFAULT 0,
  pickup_date TEXT,
  pickup_date_verified INTEGER NOT NULL DEFAULT 0,
  schedule_verified INTEGER NOT NULL DEFAULT 0,
  loaded_miles REAL,
  loaded_miles_verified INTEGER NOT NULL DEFAULT 0,
  deadhead_miles REAL,
  deadhead_miles_verified INTEGER NOT NULL DEFAULT 0,
  weight_lbs REAL,
  posted_rate REAL,
  current_offer REAL,
  dat_reference TEXT NOT NULL DEFAULT '',
  equipment_type TEXT NOT NULL DEFAULT '',
  equipment_verified INTEGER NOT NULL DEFAULT 0,
  template_id TEXT REFERENCES email_templates(id) ON DELETE SET NULL,
  sender_account TEXT NOT NULL DEFAULT '1',
  subject TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  current_round INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS freight_brokers (
  id TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL DEFAULT '',
  mc_number TEXT NOT NULL DEFAULT '',
  domain TEXT NOT NULL DEFAULT '',
  emails TEXT NOT NULL DEFAULT '[]',
  credit_status TEXT NOT NULL DEFAULT 'unknown',
  credit_score REAL,
  credit_notes TEXT NOT NULL DEFAULT '',
  setup_status TEXT NOT NULL DEFAULT 'not_started',
  blocked INTEGER NOT NULL DEFAULT 0,
  identity_confirmed INTEGER NOT NULL DEFAULT 1,
  unconfirmed_emails TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS freight_brokers_domain_idx ON freight_brokers(domain);

CREATE INDEX IF NOT EXISTS freight_loads_status_idx ON freight_loads(status, updated_at);

CREATE TABLE IF NOT EXISTS freight_load_stops (
  id TEXT PRIMARY KEY,
  load_id TEXT NOT NULL REFERENCES freight_loads(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('pickup', 'delivery')),
  facility_name TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT '',
  appointment TEXT,
  appointment_verified INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 0,
  evidence TEXT NOT NULL DEFAULT '',
  source_message_id TEXT NOT NULL DEFAULT '',
  removed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS freight_load_stops_load_idx ON freight_load_stops(load_id, seq);

CREATE TABLE IF NOT EXISTS freight_bookings (
  id TEXT PRIMARY KEY,
  load_id TEXT NOT NULL REFERENCES freight_loads(id) ON DELETE CASCADE,
  thread_id TEXT NOT NULL REFERENCES freight_threads(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'agreed' CHECK (status IN ('agreed', 'rate_con_review', 'booked', 'cancelled')),
  agreed_rate REAL NOT NULL,
  snapshot TEXT NOT NULL DEFAULT '{}',
  rate_con_amount REAL,
  rate_con_terms TEXT NOT NULL DEFAULT '{}',
  rate_con_version TEXT NOT NULL DEFAULT '',
  rate_con_source TEXT NOT NULL DEFAULT '',
  rate_con_source_ref TEXT NOT NULL DEFAULT '',
  rate_con_diffs TEXT NOT NULL DEFAULT '[]',
  rate_con_reviewed INTEGER NOT NULL DEFAULT 0,
  rate_con_review_version TEXT NOT NULL DEFAULT '',
  driver_handoff_approved INTEGER NOT NULL DEFAULT 0,
  driver_handoff_version TEXT NOT NULL DEFAULT '',
  source_message_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS freight_bookings_thread_idx ON freight_bookings(thread_id, created_at);

CREATE TABLE IF NOT EXISTS freight_threads (
  id TEXT PRIMARY KEY,
  load_id TEXT NOT NULL REFERENCES freight_loads(id) ON DELETE CASCADE,
  sender_account TEXT NOT NULL,
  recipient_email TEXT NOT NULL,
  subject TEXT NOT NULL,
  root_message_id TEXT,
  last_message_id TEXT,
  last_imap_uid INTEGER,
  state TEXT NOT NULL DEFAULT 'sent',
  last_activity_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS freight_threads_load_unique ON freight_threads(load_id);

CREATE TABLE IF NOT EXISTS freight_messages (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES freight_threads(id) ON DELETE CASCADE,
  direction TEXT NOT NULL,
  provider_message_id TEXT,
  from_email TEXT NOT NULL DEFAULT '',
  to_email TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  body_text TEXT NOT NULL DEFAULT '',
  classification TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'received',
  processing_state TEXT,
  processing_error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS freight_attachments (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES freight_threads(id) ON DELETE CASCADE,
  message_id TEXT NOT NULL REFERENCES freight_messages(id) ON DELETE CASCADE,
  filename TEXT NOT NULL DEFAULT '',
  content_b64 TEXT NOT NULL DEFAULT '',
  byte_size INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS freight_attachments_message_idx ON freight_attachments(message_id);

-- freight_messages provider uniqueness is per owner; created in SQLiteStore.init after owner_id exists.

CREATE TABLE IF NOT EXISTS freight_drafts (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES freight_threads(id) ON DELETE CASCADE,
  in_reply_to_message_id TEXT,
  subject TEXT NOT NULL,
  body_text TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  policy_snapshot TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS freight_negotiation_events (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES freight_threads(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('offer', 'counter')),
  amount REAL NOT NULL,
  unit TEXT NOT NULL DEFAULT 'total_rate',
  details TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(event_type, source_id)
);
CREATE INDEX IF NOT EXISTS freight_negotiation_events_thread_idx ON freight_negotiation_events(thread_id, created_at);

CREATE TABLE IF NOT EXISTS freight_alerts (
  id TEXT PRIMARY KEY,
  thread_id TEXT NOT NULL REFERENCES freight_threads(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS freight_mail_cursors (
  id TEXT PRIMARY KEY,
  sender_account TEXT NOT NULL UNIQUE,
  last_imap_uid INTEGER NOT NULL DEFAULT 0,
  last_checked_at TEXT,
  error TEXT,
  updated_at TEXT NOT NULL
);
