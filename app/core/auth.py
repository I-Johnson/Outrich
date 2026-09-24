"""Password hashing and account helpers for self-serve signup.

Uses scrypt from the standard library so no new dependency is needed.
Stored format: ``scrypt$<salt_hex>$<hash_hex>``.
"""
from __future__ import annotations

import hashlib
import hmac
import os

MIN_PASSWORD_LENGTH = 8
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = (stored or "").split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P, dklen=_DKLEN)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)
