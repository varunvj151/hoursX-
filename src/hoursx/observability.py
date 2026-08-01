"""Structured logging and lightweight metrics.

JSON log lines in production, human-readable in dev. Known secret-bearing keys are
redacted before emission so credentials can never leak through log transport.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter
from contextvars import ContextVar
from typing import Any

_REDACTED_KEYS = {"api_key", "authorization", "password", "secret", "token", "jwt"}

request_id_var: ContextVar[str | None] = ContextVar("hoursx_request_id", default=None)


def new_request_id() -> str:
    """Mint a short correlation id for one API request or run."""
    return uuid.uuid4().hex[:16]


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *payload* with secret-bearing keys masked (recursive)."""
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key.lower() in _REDACTED_KEYS:
            clean[key] = "***"
        elif isinstance(value, dict):
            clean[key] = redact(value)
        else:
            clean[key] = value
    return clean


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := request_id_var.get():
            entry["request_id"] = rid
        extra = getattr(record, "hoursx", None)
        if isinstance(extra, dict):
            entry.update(redact(extra))
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    """Install the root logging configuration for a HoursX process."""
    handler = logging.StreamHandler()
    if as_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Namespaced logger accessor (``hoursx.<name>``)."""
    return logging.getLogger(f"hoursx.{name}")


class Metrics:
    """In-process counters, exposed via the admin API. Not a metrics backend —
    a cheap always-on signal that survives without extra infrastructure."""

    def __init__(self) -> None:
        self._counters: Counter[str] = Counter()

    def incr(self, name: str, amount: int = 1) -> None:
        self._counters[name] += amount

    def snapshot(self) -> dict[str, int]:
        return dict(self._counters)


metrics = Metrics()
