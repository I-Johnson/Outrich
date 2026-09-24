"""Dynamic Gmail sender accounts with backwards-compatible legacy fallbacks."""
from __future__ import annotations

from app.config import settings as env
from app.core.crypto import decrypt_secret, encrypt_secret
from app.db import now_iso, store


def _legacy_sender(sender_id: str, cfg: dict) -> dict | None:
    sender_id = str(sender_id)
    if sender_id == "1":
        email = cfg.get("gmail_user") or env.GMAIL_USER
        password = decrypt_secret(cfg.get("gmail_app_password_encrypted", "")) or env.GMAIL_APP_PASSWORD
    elif sender_id == "2":
        email = env.GMAIL_USER_2
        password = env.GMAIL_APP_PASSWORD_2
    else:
        return None
    if not email:
        return None
    return {
        "id": sender_id,
        "email": email,
        "display_name": cfg.get("sender_name") or "Outreach",
        "signature": cfg.get("email_signature") or "",
        "reply_to": cfg.get("reply_to") or email,
        "app_password_encrypted": encrypt_secret(password) if password else "",
        "active": True, "provider": "gmail",
    }


def seed_legacy_gmail_senders(storage=None) -> None:
    """Persist the two pre-dynamic accounts once without overwriting user edits."""
    storage = storage or store
    cfg = storage.get("settings", 1) or {}
    existing = {str(row["id"]): row for row in storage.list("gmail_senders", order="", limit=1000)}
    for sender_id in ("1", "2"):
        sender = _legacy_sender(sender_id, cfg)
        if not sender:
            continue
        current = existing.get(sender_id)
        if current:
            if not current.get("app_password_encrypted") and sender.get("app_password_encrypted"):
                storage.update("gmail_senders", sender_id, {"app_password_encrypted": sender["app_password_encrypted"], "updated_at": now_iso()})
            continue
        stamp = now_iso()
        storage.insert("gmail_senders", {**sender, "created_at": stamp, "updated_at": stamp})


def list_gmail_senders(*, active_only: bool = False, storage=None, cfg: dict | None = None) -> list[dict]:
    storage = storage or store
    cfg = cfg if cfg is not None else (storage.get("settings", 1) or {})
    rows = storage.list("gmail_senders", order="created_at asc", limit=1000)
    by_id = {str(row["id"]): row for row in rows}
    for sender_id in ("1", "2"):
        if sender_id not in by_id:
            legacy = _legacy_sender(sender_id, cfg)
            if legacy:
                by_id[sender_id] = legacy
    result = list(by_id.values())
    result.sort(key=lambda row: (str(row.get("id")) not in {"1", "2"}, row.get("created_at") or "", str(row.get("id"))))
    return [row for row in result if row.get("active", True)] if active_only else result


def get_gmail_sender(sender_id: str, *, storage=None, cfg: dict | None = None) -> dict | None:
    sender_id = str(sender_id or "1")
    return next((row for row in list_gmail_senders(storage=storage, cfg=cfg) if str(row.get("id")) == sender_id), None)


def sender_password(sender: dict | None) -> str:
    return decrypt_secret((sender or {}).get("app_password_encrypted", ""))


def sender_context(cfg: dict, sender: dict | None) -> dict:
    """Overlay per-account identity fields used by template rendering and SMTP."""
    sender = sender or {}
    signature = sender.get("signature") or cfg.get("email_signature") or ""
    email = sender.get("email") or cfg.get("sender_email") or cfg.get("gmail_user") or ""
    return {
        **cfg,
        "sender_name": sender.get("display_name") or cfg.get("sender_name") or "Outreach",
        "sender_email": email,
        "reply_to": sender.get("reply_to") or cfg.get("reply_to") or email,
        "email_signature": signature,
        "signature": signature,
    }
