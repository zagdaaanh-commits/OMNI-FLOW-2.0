"""Password hashing: PBKDF2-HMAC-SHA256 (stdlib) with legacy salted-SHA256 verification."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from app.config import env_int, is_production

DEFAULT_PBKDF2_ITERATIONS = 390_000
_PREFIX = "pbkdf2_sha256"


def iterations_setting() -> int:
    """PBKDF2 work factor. ``PBKDF2_ITERATIONS`` may lower it outside production (tests); production never goes below the default."""
    configured = env_int("PBKDF2_ITERATIONS", DEFAULT_PBKDF2_ITERATIONS)
    if is_production():
        return max(DEFAULT_PBKDF2_ITERATIONS, configured)
    return max(1000, configured)


def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or iterations_setting()
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), iterations)
    return f"{_PREFIX}${iterations}${salt}${base64.b64encode(digest).decode('ascii')}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify against PBKDF2 hashes and legacy ``<salt>$<sha256hex>`` hashes."""
    if not stored_hash:
        return False
    try:
        if stored_hash.startswith(_PREFIX + "$"):
            _, iterations, salt, encoded = stored_hash.split("$", 3)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"), int(iterations))
            return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), encoded)
        salt, hashed = stored_hash.split("$", 1)
        check = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return hmac.compare_digest(hashed, check)
    except (ValueError, TypeError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    if not stored_hash.startswith(_PREFIX + "$"):
        return True
    try:
        return int(stored_hash.split("$", 2)[1]) < iterations_setting()
    except (ValueError, IndexError):
        return True
