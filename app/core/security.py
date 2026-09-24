import hashlib
import hmac
from app.config import settings


def unsub_token(lead_id: int) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"u{lead_id}".encode(), hashlib.sha256).hexdigest()[:24]


def verify(lead_id: int, token: str) -> bool:
    return hmac.compare_digest(unsub_token(lead_id), token or "")


def unsub_url(lead_id: int) -> str:
    return f"{settings.PUBLIC_BASE_URL}/unsubscribe/{lead_id}/{unsub_token(lead_id)}"
