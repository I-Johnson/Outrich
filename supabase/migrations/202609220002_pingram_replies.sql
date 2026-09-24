CREATE TABLE IF NOT EXISTS pingram_replies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id uuid REFERENCES clients(id) ON DELETE CASCADE,
  from_email text NOT NULL,
  subject text,
  body_text text,
  received_at timestamptz NOT NULL
);
