"""Retry policy and circuit breaker for outbound provider calls.

A model provider is a network dependency that fails in two distinct ways, and
conflating them is a mistake:

- **Transient** (timeout, 429, 5xx, connection reset) — worth retrying with
  backoff, because the next attempt often succeeds.
- **Persistent** (bad credentials, unknown model, malformed request) — retrying
  only burns latency and quota; fail immediately.

On top of retries, a circuit breaker stops hammering a provider that is clearly
down. After ``failure_threshold`` consecutive failures the circuit opens and
calls fail fast for ``recovery_seconds``; one trial call then decides whether to
close it again. Without this, every run pays the full retry budget against a
dead provider.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

import httpx

from hoursx.observability import get_logger, metrics

log = get_logger("resilience")

T = TypeVar("T")

# HTTP statuses worth a second attempt: rate limiting and server-side faults.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def is_transient(exc: BaseException) -> bool:
    """True when *exc* is worth retrying.

    Network-layer errors always are. HTTP errors are retryable only for the
    statuses above — a 401 or 404 will never fix itself.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    return isinstance(exc, (asyncio.TimeoutError, ConnectionError))


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter.

    Jitter matters: without it, many runs failing at the same moment retry in
    lockstep and re-create the spike that caused the failure.
    """

    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Delay before retry *attempt* (1-based)."""
        raw = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        return random.uniform(0, raw) if self.jitter else raw


class BreakerState(StrEnum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing fast
    HALF_OPEN = "half_open"  # letting one trial call through


@dataclass
class CircuitBreaker:
    """Per-provider failure gate.

    Deliberately not thread-safe-by-lock: it is asyncio-local state mutated
    between awaits, and a rare double trial call is harmless compared to the
    cost of serializing every provider call behind a lock.
    """

    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    name: str = "provider"

    _failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _half_open: bool = field(default=False, init=False)

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if time.monotonic() - self._opened_at >= self.recovery_seconds:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def allows_call(self) -> bool:
        """False only while the circuit is fully open."""
        state = self.state
        if state is BreakerState.OPEN:
            return False
        if state is BreakerState.HALF_OPEN:
            self._half_open = True
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open = False

    def record_failure(self) -> None:
        # A failed trial call in half-open re-opens the circuit immediately
        # rather than waiting for the threshold again.
        if self._half_open:
            self._failures = self.failure_threshold
            self._opened_at = time.monotonic()
            self._half_open = False
            metrics.incr("breaker.reopened")
            return
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            metrics.incr("breaker.opened")
            log.warning("circuit opened for %s after %d failures", self.name, self._failures)

    def reset(self) -> None:
        self.record_success()


class CircuitOpenError(Exception):
    """Raised instead of calling a provider whose circuit is open."""

    def __init__(self, name: str) -> None:
        super().__init__(f"circuit open for {name}; failing fast")
        self.name = name


async def call_with_resilience(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run *operation* under retry + breaker policy.

    ``sleep`` is injectable so tests can assert backoff behavior without
    actually waiting.
    """
    if breaker is not None and not breaker.allows_call():
        metrics.incr("breaker.rejected")
        raise CircuitOpenError(breaker.name)

    last: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            result = await operation()
        except Exception as exc:  # noqa: BLE001 — classification decides the outcome
            last = exc
            if not is_transient(exc):
                if breaker is not None:
                    breaker.record_failure()
                raise
            metrics.incr("provider.transient_failure")
            if attempt == policy.attempts:
                break
            await sleep(policy.delay_for(attempt))
        else:
            if breaker is not None:
                breaker.record_success()
            return result

    if breaker is not None:
        breaker.record_failure()
    assert last is not None
    raise last
