"""Per-user data isolation for both workspaces.

Every freight table, the Gmail sender list, freight templates and the outreach
tables (clients, campaigns, email_log, settings, scrape/import artifacts,
replies, suppression and the work queue) carry an ``owner_id``. Request
handlers and background jobs wrap the shared storage in an ``OwnerStore`` so
every read, write and lookup by id is limited to one owner. Rows written
before isolation (``owner_id`` NULL) belong to the admin owner.
"""
from __future__ import annotations

from typing import Any

from app.db import new_id

# Stable id for the env-configured admin account. Existing single-tenant data
# is backfilled to this owner by the isolation migration.
ADMIN_OWNER_ID = "00000000-0000-0000-0000-000000000001"

OWNED_TABLES = frozenset({
    "freight_truck_profiles", "freight_missions", "freight_loads", "freight_load_stops", "freight_brokers", "freight_bookings", "freight_threads",
    "freight_messages", "freight_attachments", "freight_drafts", "freight_negotiation_events", "freight_alerts",
    "freight_mail_cursors", "freight_settings", "gmail_senders", "email_templates",
    "clients", "campaigns", "email_log", "settings", "scrape_jobs", "scrape_discards", "scrape_presets",
    "import_batches", "pingram_replies", "suppression", "jobs",
})

# Tables with exactly one row per owner; callers still ask for the legacy
# singleton id, which maps to the owner's own row.
SINGLETON_TABLES = frozenset({"freight_settings", "settings"})


class OwnerStore:
    """Storage facade that confines owned tables to a single owner."""

    def __init__(self, base, owner_id: str):
        if not owner_id:
            raise ValueError("owner_id is required")
        self.base = base
        self.owner_id = str(owner_id)

    @property
    def is_admin(self) -> bool:
        return self.owner_id == ADMIN_OWNER_ID

    def _mine(self, row: dict | None) -> bool:
        # Pre-isolation rows (owner_id NULL) are the admin's legacy data.
        return bool(row) and str(row.get("owner_id") or ADMIN_OWNER_ID) == self.owner_id

    def _singleton_row(self, table: str) -> dict | None:
        rows = self.base.list(table, order="", limit=1000)
        mine = [row for row in rows if self._mine(row)]
        # Prefer the row explicitly stamped with this owner over a legacy NULL row.
        mine.sort(key=lambda row: str(row.get("owner_id") or "") != self.owner_id)
        return mine[0] if mine else None

    def list(self, table: str, filters: dict | None = None, order: str = "id desc", limit: int = 1000, select: str = "*"):
        filters = dict(filters or {})
        if table in OWNED_TABLES:
            if self.is_admin and "owner_id" not in filters:
                # Admin scope includes legacy NULL-owner rows; filter in Python
                # because SQL NULL never matches an equality check.
                rows = self.base.list(table, filters, order=order, limit=limit, select=select)
                return [row for row in rows if self._mine(row)]
            filters["owner_id"] = self.owner_id
        if select != "*":
            return self.base.list(table, filters, order=order, limit=limit, select=select)
        return self.base.list(table, filters, order=order, limit=limit)

    def get(self, table: str, row_id: Any):
        if table in SINGLETON_TABLES:
            # Callers still ask for the old singleton id; each owner has one row.
            return self._singleton_row(table)
        row = self.base.get(table, row_id)
        if table in OWNED_TABLES and not self._mine(row):
            return None
        return row

    def insert(self, table: str, row: dict[str, Any]):
        if table in OWNED_TABLES:
            row = {**row, "owner_id": self.owner_id}
            if table in SINGLETON_TABLES:
                row["id"] = new_id()
        return self.base.insert(table, row)

    def insert_many(self, table: str, rows: list[dict[str, Any]]):
        if table in OWNED_TABLES:
            rows = [{**row, "owner_id": self.owner_id} for row in rows]
        return self.base.insert_many(table, rows)

    def _require(self, table: str, row_id: Any):
        if table in SINGLETON_TABLES:
            row = self._singleton_row(table)
        else:
            row = self.base.get(table, row_id)
        if not self._mine(row):
            raise LookupError(f"{table} row not found")
        return row

    def update(self, table: str, row_id: Any, values: dict[str, Any]):
        if table in OWNED_TABLES:
            row = self._require(table, row_id)
            values = {k: v for k, v in values.items() if k != "owner_id"}
            return self.base.update(table, row["id"], values)
        return self.base.update(table, row_id, values)

    def claim_status(self, table: str, row_id: Any, expected: str, new_status: str) -> bool:
        if table in OWNED_TABLES:
            try:
                self._require(table, row_id)
            except LookupError:
                return False
        return self.base.claim_status(table, row_id, expected, new_status)

    def claim_status_not(self, table: str, row_id: Any, disallowed: str, new_status: str) -> bool:
        return self.claim_field_not(table, row_id, "status", disallowed, new_status)

    def claim_field_not(self, table: str, row_id: Any, field: str, disallowed: str, new_value: str) -> bool:
        if table in OWNED_TABLES:
            try:
                self._require(table, row_id)
            except LookupError:
                return False
        return self.base.claim_field_not(table, row_id, field, disallowed, new_value)

    def claim_booking_lease(self, row_id: Any, claim_at: str, stale_before: str) -> bool:
        if "freight_truck_profiles" in OWNED_TABLES:
            try:
                self._require("freight_truck_profiles", row_id)
            except LookupError:
                return False
        return self.base.claim_booking_lease(row_id, claim_at, stale_before)

    def delete(self, table: str, filters: dict[str, Any]):
        if table in OWNED_TABLES:
            if self.is_admin and "owner_id" not in filters:
                # Include legacy NULL-owner rows; delete by resolved id.
                rows = [row for row in self.base.list(table, filters, order="", limit=10000) if self._mine(row)]
                deleted = 0
                for row in rows:
                    deleted += self.base.delete(table, {"id": row["id"]})
                return deleted
            filters = {**filters, "owner_id": self.owner_id}
        return self.base.delete(table, filters)

    def init(self):
        return self.base.init()


def owner_store(base, owner_id: str) -> OwnerStore:
    return base if isinstance(base, OwnerStore) and base.owner_id == owner_id else OwnerStore(getattr(base, "base", base), owner_id)


def outreach_owner_ids(base) -> list[str]:
    """Owners with outreach data the email/scrape workers must visit."""
    owners: set[str] = {ADMIN_OWNER_ID}
    for table in ("campaigns", "email_log", "settings", "jobs"):
        for row in base.list(table, order="", limit=20000, select="owner_id"):
            owners.add(str(row.get("owner_id") or ADMIN_OWNER_ID))
    return sorted(owners)


def freight_owner_ids(base) -> list[str]:
    """Owners that have freight threads, drafts or senders the workers must visit."""
    owners: set[str] = set()
    for table in ("freight_threads", "freight_drafts", "gmail_senders"):
        for row in base.list(table, order="", limit=10000, select="owner_id"):
            if row.get("owner_id"):
                owners.add(str(row["owner_id"]))
    return sorted(owners)
