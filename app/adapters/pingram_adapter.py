import html

import httpx

from app.adapters.base import EmailProvider, SendResult
from app.config import settings


class PingramProvider(EmailProvider):
    name = "pingram"
    def send(self, *, to, subject, body, content_type, from_address, from_name, reply_to, headers=None):
        if settings.DRY_RUN: return SendResult(True, "dry-run")
        if not settings.PINGRAM_API_KEY: return SendResult(False, error="PINGRAM_API_KEY is missing")
        message = body if content_type == "html" else f"<div style='white-space:pre-wrap'>{html.escape(body)}</div>"
        payload = {"type": settings.PINGRAM_NOTIFICATION_TYPE, "to": to, "subject": subject, "html": message,
                   "fromName": from_name, "fromAddress": from_address or settings.PINGRAM_FROM_EMAIL,
                   "replyToAddresses": [reply_to] if reply_to else []}
        try:
            with httpx.Client(timeout=30) as client:
                res = client.post(settings.PINGRAM_API_URL, headers={"Authorization": f"Bearer {settings.PINGRAM_API_KEY}"}, json=payload)
            data = res.json(); res.raise_for_status()
            if data.get("error"): return SendResult(False, error=str(data["error"]))
            return SendResult(True, str(data.get("trackingId", "")))
        except Exception as exc: return SendResult(False, error=f"{type(exc).__name__}: {exc}")
