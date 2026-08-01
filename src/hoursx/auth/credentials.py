"""Password and API-key credential handling.

PBKDF2-HMAC-SHA256 from the standard library: no native-wheel dependency, ships
everywhere, and the iteration count is stored per-hash so it can be raised later
without invalidating existing credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ITERATIONS = 310_000
_API_KEY_PREFIX = "hx_"


def hash_password(password: str) -> str:
    """Return ``pbkdf2$<iterations>$<salt>$<digest>`` for storage."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS
    ).hex()
    return f"pbkdf2${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of *password* against a stored hash."""
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except (ValueError, TypeError):
        return False


def new_api_key() -> str:
    """Mint a fresh API key. Shown to the caller once; only its hash is stored."""
    return _API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """Deterministic hash for API-key lookup (keys are high-entropy, so an
    unsalted SHA-256 is appropriate and enables indexed exact-match lookup)."""
    return hashlib.sha256(key.encode()).hexdigest()
