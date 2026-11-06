"""Central password hashing and verification helpers."""
from __future__ import annotations

import hashlib
import hmac
import secrets as pysecrets


def hash_password(pw: str) -> str:
    salt = pysecrets.token_hex(16)
    digest = hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()
    return f"{salt}:{digest}"


def verify_password(pw: str, hashed: str) -> bool:
    if not hashed or ":" not in hashed:
        return False
    salt, expected = hashed.split(":", 1)
    computed = hashlib.sha256((salt + pw).encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, expected)

