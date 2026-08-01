"""Domain error taxonomy.

Business logic raises these; the API layer maps them to HTTP once, in one
place. Handlers therefore never build status codes inline, and non-HTTP callers
(worker, CLI, tests) get meaningful typed errors instead of `HTTPException`.

Each error carries a stable machine-readable ``code`` so clients can branch on
it without string-matching human prose.
"""

from __future__ import annotations

from typing import Any


class HoursXError(Exception):
    """Base for every domain error. ``status`` is the HTTP mapping."""

    code: str = "internal_error"
    status: int = 500

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def as_payload(self) -> dict[str, Any]:
        """The wire shape: stable code, human message, optional context."""
        payload: dict[str, Any] = {"code": self.code, "detail": self.message}
        if self.context:
            payload["context"] = self.context
        return payload


class NotFoundError(HoursXError):
    """Resource missing — or present in another workspace.

    Cross-tenant reads deliberately raise this rather than a permission error:
    a 403 would confirm the resource exists, which is itself a disclosure.
    """

    code = "not_found"
    status = 404


class ConflictError(HoursXError):
    """Uniqueness or state conflict (duplicate handle, already-decided approval)."""

    code = "conflict"
    status = 409


class ValidationError(HoursXError):
    """Input failed a domain rule that the schema alone cannot express."""

    code = "validation_failed"
    status = 422


class AuthenticationError(HoursXError):
    """Missing, malformed, or expired credentials."""

    code = "unauthenticated"
    status = 401


class PermissionError_(HoursXError):
    """Authenticated, but the role lacks the required permission."""

    code = "forbidden"
    status = 403


class QuotaExceededError(HoursXError):
    """A workspace limit would be breached by this request."""

    code = "quota_exceeded"
    status = 429


class ProviderUnavailableError(HoursXError):
    """Every model provider in the chain failed or is circuit-broken."""

    code = "provider_unavailable"
    status = 503


class RunConflictError(ConflictError):
    """The run is not in a state that permits this transition."""

    code = "run_state_conflict"
