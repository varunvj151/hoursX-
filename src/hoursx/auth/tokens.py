"""JWT issuance and verification (HS256)."""

from __future__ import annotations

import time
from typing import Any

import jwt


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or forged."""


def issue_token(*, user_id: str, secret: str, ttl_seconds: int) -> str:
    """Issue a signed access token for *user_id*."""
    now = int(time.time())
    claims: dict[str, Any] = {"sub": user_id, "iat": now, "exp": now + ttl_seconds, "iss": "hoursx"}
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_token(token: str, *, secret: str) -> str:
    """Return the user id from a valid token, or raise :class:`TokenError`."""
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], issuer="hoursx")
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise TokenError("token missing subject")
    return sub
