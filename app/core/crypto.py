import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    if not settings.ENCRYPTION_KEY:
        raise ValueError("ENCRYPTION_KEY is required before storing credentials")
    try:
        return Fernet(settings.ENCRYPTION_KEY.encode())
    except ValueError:
        derived = base64.urlsafe_b64encode(hashlib.sha256(settings.ENCRYPTION_KEY.encode()).digest())
        return Fernet(derived)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt_secret(value: str) -> str:
    if not value: return ""
    try: return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError): return ""
