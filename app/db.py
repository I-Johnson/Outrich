"""Small storage facade: Supabase REST in production, SQLite locally/tests."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings

ROOT = Path(__file__).resolve().parent
JSON_FIELDS = {
    "clients": {"extra"}, "campaigns": {"target_filter", "template_ids", "gmail_accounts", "send_window"},
    "scrape_presets": {"config"}, "import_batches": {"column_mapping"},
    "settings": {"send_days", "csv_required_fields", "scrape_required_fields"}, "jobs": {"payload"},
    "freight_truck_profiles": {"shareable_fields"},
    "freight_missions": {"destinations", "permissions"},
    "freight_messages": {"classification"},
    "freight_drafts": {"policy_snapshot"},
    "freight_negotiation_events": {"details"},
    "freight_bookings": {"snapshot", "rate_con_diffs", "rate_con_terms"},
    "freight_brokers": {"emails"},
}
# Freight tables that carry owner_id + vertical (gmail_senders, freight_settings
# and email_templates are handled separately).
OWNED_FREIGHT_TABLES = (
    "freight_truck_profiles", "freight_missions", "freight_loads", "freight_load_stops", "freight_brokers", "freight_bookings", "freight_threads",
    "freight_messages", "freight_drafts", "freight_negotiation_events", "freight_alerts",
    "freight_mail_cursors",
)
BOOL_FIELDS = {
    "email_templates": {"active"}, "gmail_senders": {"active"},
    "freight_truck_profiles": {"active"}, "freight_missions": {"active"},
}


def now_iso() -> str:
    # Freight turns and automatic replies can be written within the same second.
    # Preserve microseconds so "latest message" queries remain deterministic.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id() -> str:
    return str(uuid.uuid4())


def _decode(table: str, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in JSON_FIELDS.get(table, set()):
        value = result.get(field)
        if isinstance(value, str):
            try:
                result[field] = json.loads(value)
            except (ValueError, TypeError):
                result[field] = [] if field in {"template_ids", "gmail_accounts", "send_days", "csv_required_fields", "scrape_required_fields"} else {}
    for field in BOOL_FIELDS.get(table, set()):
        if field in result:
            result[field] = bool(result[field])
    return result


def _encode(table: str, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in JSON_FIELDS.get(table, set()):
        if field in result and not isinstance(result[field], str):
            result[field] = json.dumps(result[field], separators=(",", ":"))
    for field in BOOL_FIELDS.get(table, set()):
        if field in result:
            result[field] = int(bool(result[field]))
    return result


class SQLiteStore:
    def __init__(self, path: str): self.path = path

    def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _upgrade_before_schema(self, con):
        """Reshape single-tenant tables that schema.sql can no longer create in place."""
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("gmail_senders", "freight_settings"):
            if table in tables and "owner_id" not in {row[1] for row in con.execute(f"PRAGMA table_info({table})")}:
                con.execute(f"DROP TABLE IF EXISTS _pre_owner_{table}")
                con.execute(f"ALTER TABLE {table} RENAME TO _pre_owner_{table}")
        con.execute("DROP INDEX IF EXISTS freight_messages_provider_unique")

    def _upgrade_after_schema(self, con):
        from app.core.tenancy import ADMIN_OWNER_ID
        for table in ("gmail_senders", "freight_settings"):
            legacy = f"_pre_owner_{table}"
            if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (legacy,)).fetchone():
                new_cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
                cols = [row[1] for row in con.execute(f"PRAGMA table_info({legacy})") if row[1] in new_cols]
                if table == "freight_settings":
                    con.execute("DELETE FROM freight_settings")
                col_sql = ",".join(cols)
                con.execute(f"INSERT OR IGNORE INTO {table} ({col_sql}, owner_id) SELECT {col_sql}, ? FROM {legacy}", (ADMIN_OWNER_ID,))
                con.execute(f"DROP TABLE {legacy}")
        for table in OWNED_FREIGHT_TABLES:
            columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
            if "owner_id" not in columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT")
                con.execute(f"UPDATE {table} SET owner_id=? WHERE owner_id IS NULL", (ADMIN_OWNER_ID,))
            if "vertical" not in columns:
                con.execute(f"ALTER TABLE {table} ADD COLUMN vertical TEXT NOT NULL DEFAULT 'freight'")
        template_columns = {row[1] for row in con.execute("PRAGMA table_info(email_templates)")}
        if "owner_id" not in template_columns:
            con.execute("ALTER TABLE email_templates ADD COLUMN owner_id TEXT")
            con.execute("UPDATE email_templates SET owner_id=? WHERE vertical='freight' AND owner_id IS NULL", (ADMIN_OWNER_ID,))
        for table in (*OWNED_FREIGHT_TABLES, "gmail_senders", "email_templates", "freight_settings"):
            con.execute(f"CREATE INDEX IF NOT EXISTS {table}_owner_idx ON {table}(owner_id)")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS gmail_senders_owner_email_unique ON gmail_senders(owner_id, lower(email))")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS freight_messages_owner_provider_unique ON freight_messages(owner_id, provider_message_id) WHERE provider_message_id IS NOT NULL AND provider_message_id <> ''")

    def init(self):
        with self.connect() as con:
            self._upgrade_before_schema(con)
            # Preserve the pre-brief campaign table under a legacy name. Its
            # integer-id/steps model is incompatible with the new durable queue.
            exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='campaigns'").fetchone()
            if exists:
                columns = {row[1] for row in con.execute("PRAGMA table_info(campaigns)")}
                if "target_filter" not in columns:
                    con.execute("ALTER TABLE campaigns RENAME TO legacy_campaigns")
            con.executescript((ROOT / "schema.sql").read_text())
            campaign_columns = {row[1] for row in con.execute("PRAGMA table_info(campaigns)")}
            if "gmail_accounts" not in campaign_columns:
                con.execute("ALTER TABLE campaigns ADD COLUMN gmail_accounts TEXT NOT NULL DEFAULT '[\"1\"]'")
            log_columns = {row[1] for row in con.execute("PRAGMA table_info(email_log)")}
            if "sender_account" not in log_columns:
                con.execute("ALTER TABLE email_log ADD COLUMN sender_account TEXT NOT NULL DEFAULT '1'")
            template_columns = {row[1] for row in con.execute("PRAGMA table_info(email_templates)")}
            if "vertical" not in template_columns:
                con.execute("ALTER TABLE email_templates ADD COLUMN vertical TEXT NOT NULL DEFAULT 'outreach'")
            load_columns = {row[1] for row in con.execute("PRAGMA table_info(freight_loads)")}
            if "loaded_miles_verified" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN loaded_miles_verified INTEGER NOT NULL DEFAULT 0")
            if "deadhead_miles_verified" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN deadhead_miles_verified INTEGER NOT NULL DEFAULT 0")
            if "destination_verified" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN destination_verified INTEGER NOT NULL DEFAULT 0")
            if "origin_verified" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN origin_verified INTEGER NOT NULL DEFAULT 0")
            if "equipment_verified" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN equipment_verified INTEGER NOT NULL DEFAULT 0")
            if "pickup_date_verified" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN pickup_date_verified INTEGER NOT NULL DEFAULT 0")
            if "schedule_verified" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN schedule_verified INTEGER NOT NULL DEFAULT 0")
            if "weight_lbs" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN weight_lbs REAL")
            if "broker_id" not in load_columns:
                con.execute("ALTER TABLE freight_loads ADD COLUMN broker_id TEXT")
            stop_columns = {row[1] for row in con.execute("PRAGMA table_info(freight_load_stops)")}
            if "removed_at" not in stop_columns:
                con.execute("ALTER TABLE freight_load_stops ADD COLUMN removed_at TEXT")
            booking_columns = {row[1] for row in con.execute("PRAGMA table_info(freight_bookings)")}
            for column, ddl in (
                ("rate_con_terms", "TEXT NOT NULL DEFAULT '{}'"),
                ("rate_con_version", "TEXT NOT NULL DEFAULT ''"),
                ("rate_con_source", "TEXT NOT NULL DEFAULT ''"),
                ("rate_con_review_version", "TEXT NOT NULL DEFAULT ''"),
                ("driver_handoff_version", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in booking_columns:
                    con.execute(f"ALTER TABLE freight_bookings ADD COLUMN {column} {ddl}")
            message_columns = {row[1] for row in con.execute("PRAGMA table_info(freight_messages)")}
            if "processing_state" not in message_columns:
                con.execute("ALTER TABLE freight_messages ADD COLUMN processing_state TEXT")
            if "processing_error" not in message_columns:
                con.execute("ALTER TABLE freight_messages ADD COLUMN processing_error TEXT")
            profile_columns = {row[1] for row in con.execute("PRAGMA table_info(freight_truck_profiles)")}
            for column, ddl in (
                ("truck_vin", "TEXT NOT NULL DEFAULT ''"),
                ("driver_name", "TEXT NOT NULL DEFAULT ''"),
                ("driver_cdl_number", "TEXT NOT NULL DEFAULT ''"),
                ("driver_cdl_state", "TEXT NOT NULL DEFAULT ''"),
                ("driver_phone", "TEXT NOT NULL DEFAULT ''"),
                ("availability_status", "TEXT NOT NULL DEFAULT 'available'"),
                ("available_from", "TEXT"),
            ):
                if column not in profile_columns:
                    con.execute(f"ALTER TABLE freight_truck_profiles ADD COLUMN {column} {ddl}")
            self._upgrade_after_schema(con)

    def list(self, table: str, filters: dict | None = None, order: str = "id desc", limit: int = 1000, select: str = "*"):
        filters = filters or {}; clauses, args = [], []
        for key, value in filters.items():
            if isinstance(value, tuple) and value[0] == "in":
                vals = list(value[1]); clauses.append(f"{key} IN ({','.join('?' for _ in vals)})"); args.extend(vals)
            elif isinstance(value, tuple) and value[0] in {"lte", "gte"}:
                clauses.append(f"{key} {'<=' if value[0]=='lte' else '>='} ?"); args.append(value[1])
            elif value is None: clauses.append(f"{key} IS NULL")
            else: clauses.append(f"{key} = ?"); args.append(value)
        sql = f"SELECT {select} FROM {table}" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        if order:
            allowed = {"created_at", "updated_at", "sent_at", "scheduled_for", "uploaded_at", "business_name", "name", "id", "seq"}
            parts = order.split(); col = parts[0] if parts[0] in allowed else "created_at"
            direction = "DESC" if len(parts) > 1 and parts[1].lower() == "desc" else "ASC"; sql += f" ORDER BY {col} {direction}"
        sql += " LIMIT ?"; args.append(limit)
        with self.connect() as con: return [_decode(table, dict(r)) for r in con.execute(sql, args).fetchall()]

    def get(self, table: str, row_id: Any):
        rows = self.list(table, {"id": row_id}, order="", limit=1); return rows[0] if rows else None

    def insert(self, table: str, row: dict[str, Any]):
        data = _encode(table, row); cols = list(data); marks = ",".join("?" for _ in cols)
        with self.connect() as con:
            con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks})", [data[c] for c in cols]); con.commit()
        return _decode(table, data)

    def insert_many(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows: return []
        encoded = [_encode(table, r) for r in rows]
        cols = list(encoded[0])
        marks = ",".join("?" for _ in cols)
        with self.connect() as con:
            for r in encoded:
                con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({marks})", [r.get(c) for c in cols])
            con.commit()
        return [_decode(table, r) for r in encoded]

    def update(self, table: str, row_id: Any, values: dict[str, Any]):
        data = _encode(table, values)
        with self.connect() as con:
            con.execute(f"UPDATE {table} SET {','.join(f'{k}=?' for k in data)} WHERE id=?", [*data.values(), row_id]); con.commit()
        return self.get(table, row_id)

    def claim_status(self, table: str, row_id: Any, expected: str, new_status: str) -> bool:
        with self.connect() as con:
            result = con.execute(f"UPDATE {table} SET status=?, updated_at=? WHERE id=? AND status=?", (new_status, now_iso(), row_id, expected))
            con.commit()
            return result.rowcount == 1

    def claim_status_not(self, table: str, row_id: Any, disallowed: str, new_status: str) -> bool:
        with self.connect() as con:
            result = con.execute(f"UPDATE {table} SET status=?, updated_at=? WHERE id=? AND status<>?", (new_status, now_iso(), row_id, disallowed))
            con.commit()
            return result.rowcount == 1

    def delete(self, table: str, filters: dict[str, Any]):
        clauses, args = [], []
        for key, value in filters.items(): clauses.append(f"{key}=?"); args.append(value)
        with self.connect() as con:
            cur = con.execute(f"DELETE FROM {table} WHERE {' AND '.join(clauses)}", args); con.commit(); return cur.rowcount


class SupabaseStore:
    def __init__(self, url: str, key: str):
        self.base = f"{url}/rest/v1"; self.headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    def init(self): return None
    def _request(self, method: str, path: str, **kwargs):
        headers = self.headers | kwargs.pop("headers", {})
        with httpx.Client(timeout=25) as client: res = client.request(method, self.base + path, headers=headers, **kwargs)
        res.raise_for_status(); return res.json() if res.content else []
    def list(self, table: str, filters: dict | None = None, order: str = "id desc", limit: int = 1000, select: str = "*"):
        params = [f"select={select}"]
        for key, value in (filters or {}).items():
            if isinstance(value, tuple):
                op, val = value
                if op == "in": val = f"({','.join(str(v) for v in val)})"
                params.append(f"{quote(key)}={op}.{quote(str(val), safe='(),')}" )
            elif value is None: params.append(f"{quote(key)}=is.null")
            else: params.append(f"{quote(key)}=eq.{quote(str(value))}")
        if order:
            parts = order.split(); params.append(f"order={quote(parts[0])}.{'desc' if len(parts)>1 and parts[1].lower()=='desc' else 'asc'}")
        if limit <= 1000:
            params.append(f"limit={int(limit)}")
            return self._request("GET", f"/{table}?{'&'.join(params)}")

        results = []
        offset = 0
        while len(results) < limit:
            batch_limit = min(1000, limit - len(results))
            page_params = params + [f"limit={batch_limit}", f"offset={offset}"]
            batch = self._request("GET", f"/{table}?{'&'.join(page_params)}")
            if not batch: break
            results.extend(batch)
            if len(batch) < batch_limit: break
            offset += len(batch)
        return results
    def get(self, table: str, row_id: Any):
        rows = self.list(table, {"id": row_id}, order="", limit=1); return rows[0] if rows else None
    def insert(self, table: str, row: dict[str, Any]):
        rows = self._request("POST", f"/{table}", json=row, headers={"Prefer": "return=representation"}); return rows[0] if rows else row
    def insert_many(self, table: str, rows: list[dict[str, Any]], batch_size: int = 100) -> list[dict[str, Any]]:
        if not rows: return []
        inserted = []
        for i in range(0, len(rows), batch_size):
            chunk = rows[i:i + batch_size]
            res = self._request("POST", f"/{table}", json=chunk, headers={"Prefer": "return=representation"})
            inserted.extend(res if isinstance(res, list) else chunk)
        return inserted
    def update(self, table: str, row_id: Any, values: dict[str, Any]):
        rows = self._request("PATCH", f"/{table}?id=eq.{quote(str(row_id))}", json=values, headers={"Prefer": "return=representation"}); return rows[0] if rows else values
    def claim_status(self, table: str, row_id: Any, expected: str, new_status: str) -> bool:
        rows = self._request("PATCH", f"/{table}?id=eq.{quote(str(row_id))}&status=eq.{quote(expected)}", json={"status": new_status, "updated_at": now_iso()}, headers={"Prefer": "return=representation"})
        return bool(rows)
    def claim_status_not(self, table: str, row_id: Any, disallowed: str, new_status: str) -> bool:
        rows = self._request("PATCH", f"/{table}?id=eq.{quote(str(row_id))}&status=neq.{quote(disallowed)}", json={"status": new_status, "updated_at": now_iso()}, headers={"Prefer": "return=representation"})
        return bool(rows)
    def delete(self, table: str, filters: dict[str, Any]):
        query = "&".join(f"{quote(k)}=eq.{quote(str(v))}" for k, v in filters.items())
        rows = self._request("DELETE", f"/{table}?{query}", headers={"Prefer": "return=representation"}); return len(rows)


if settings.using_supabase and (not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY):
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required (SQLite is test-only)")
store = SupabaseStore(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY) if settings.using_supabase else SQLiteStore(settings.DB_PATH)


def init_db(): store.init()
def count(table: str, filters: dict | None = None) -> int: return len(store.list(table, filters, order="", limit=10000))
