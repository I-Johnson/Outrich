"""Per-user data isolation for the freight workspace.

Every freight table, the Gmail sender list and freight templates carry an
``owner_id``. Request handlers and background jobs wrap the shared storage in
an ``OwnerStore`` so every read, write and lookup by id is limited to one
owner. Outreach tables stay admin-only and unscoped.
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
})


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
        return bool(row) and str(row.get("owner_id") or "") == self.owner_id

    def _settings_row(self) -> dict | None:
        rows = self.base.list("freight_settings", {"owner_id": self.owner_id}, order="", limit=1)
        return rows[0] if rows else None

    def list(self, table: str, filters: dict | None = None, order: str = "id desc", limit: int = 1000, select: str = "*"):
        filters = dict(filters or {})
        if table in OWNED_TABLES:
            filters["owner_id"] = self.owner_id
        if select != "*":
            return self.base.list(table, filters, order=order, limit=limit, select=select)
        return self.base.list(table, filters, order=order, limit=limit)

    def get(self, table: str, row_id: Any):
        if table == "freight_settings":
            # Callers still ask for the old singleton id; each owner has one row.
            return self._settings_row()
        row = self.base.get(table, row_id)
        if table in OWNED_TABLES and not self._mine(row):
            return None
        return row

    def insert(self, table: str, row: dict[str, Any]):
        if table in OWNED_TABLES:
            row = {**row, "owner_id": self.owner_id}
            if table == "freight_settings":
                row["id"] = new_id()
        return self.base.insert(table, row)

    def insert_many(self, table: str, rows: list[dict[str, Any]]):
        if table in OWNED_TABLES:
            rows = [{**row, "owner_id": self.owner_id} for row in rows]
        return self.base.insert_many(table, rows)

    def _require(self, table: str, row_id: Any):
        if table == "freight_settings":
            row = self._settings_row()
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
            filters = {**filters, "owner_id": self.owner_id}
        return self.base.delete(table, filters)

    def init(self):
        return self.base.init()


def owner_store(base, owner_id: str) -> OwnerStore:
    return base if isinstance(base, OwnerStore) and base.owner_id == owner_id else OwnerStore(getattr(base, "base", base), owner_id)


def freight_owner_ids(base) -> list[str]:
    """Owners that have freight threads, drafts or senders the workers must visit."""
    owners: set[str] = set()
    for table in ("freight_threads", "freight_drafts", "gmail_senders"):
        for row in base.list(table, order="", limit=10000, select="owner_id"):
            if row.get("owner_id"):
                owners.add(str(row["owner_id"]))
    return sorted(owners)
