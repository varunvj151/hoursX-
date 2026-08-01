"""Retry classification, backoff, and circuit-breaker state machine."""

import httpx
import pytest

from hoursx.resilience import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    RetryPolicy,
    call_with_resilience,
    is_transient,
)


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://models.example.com/v1/chat")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class _Recorder:
    """Collects sleep durations instead of waiting, so backoff is assertable."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


# ------------------------------------------------------------- classification


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
def test_retryable_http_statuses_are_transient(status_code):
    assert is_transient(_http_error(status_code))


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_client_errors_are_not_transient(status_code):
    # Retrying a bad key or unknown model only burns latency and quota.
    assert not is_transient(_http_error(status_code))


def test_network_faults_are_transient():
    assert is_transient(httpx.ConnectError("refused"))
    assert is_transient(httpx.ReadTimeout("slow"))
    assert is_transient(TimeoutError())
    assert is_transient(ConnectionError())


def test_arbitrary_exceptions_are_not_transient():
    assert not is_transient(ValueError("bad argument"))
    assert not is_transient(KeyError("missing"))


# -------------------------------------------------------------- retry policy


def test_backoff_grows_exponentially_and_is_capped():
    policy = RetryPolicy(base_delay=1.0, max_delay=4.0, jitter=False)
    assert [policy.delay_for(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 4.0]


def test_jitter_stays_within_the_deterministic_bound():
    policy = RetryPolicy(base_delay=2.0, max_delay=8.0, jitter=True)
    for _ in range(50):
        assert 0.0 <= policy.delay_for(2) <= 4.0


async def test_succeeds_without_retry_when_the_call_works():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return "ok"

    sleeper = _Recorder()
    result = await call_with_resilience(operation, policy=RetryPolicy(attempts=3), sleep=sleeper)
    assert result == "ok"
    assert calls == 1
    assert sleeper.delays == []


async def test_retries_transient_failure_then_succeeds():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _http_error(503)
        return "recovered"

    sleeper = _Recorder()
    result = await call_with_resilience(
        operation, policy=RetryPolicy(attempts=3, jitter=False), sleep=sleeper
    )
    assert result == "recovered"
    assert attempts == 3
    assert len(sleeper.delays) == 2  # slept between attempts, not after the last


async def test_does_not_retry_permanent_failure():
    attempts = 0

    async def operation():
        nonlocal attempts
        attempts += 1
        raise _http_error(401)

    with pytest.raises(httpx.HTTPStatusError):
        await call_with_resilience(operation, policy=RetryPolicy(attempts=5), sleep=_Recorder())
    assert attempts == 1


async def test_exhausts_attempts_then_raises_last_error():
    async def operation():
        raise _http_error(500)

    sleeper = _Recorder()
    with pytest.raises(httpx.HTTPStatusError):
        await call_with_resilience(
            operation, policy=RetryPolicy(attempts=3, jitter=False), sleep=sleeper
        )
    assert len(sleeper.delays) == 2


# ----------------------------------------------------------- circuit breaker


def test_breaker_starts_closed_and_allows_calls():
    breaker = CircuitBreaker()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allows_call()


def test_breaker_opens_at_threshold():
    breaker = CircuitBreaker(failure_threshold=3)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allows_call()


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


def test_breaker_half_opens_after_recovery_window():
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0.0)
    breaker.record_failure()
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allows_call()


def test_failed_trial_call_reopens_immediately():
    breaker = CircuitBreaker(failure_threshold=5, recovery_seconds=0.0)
    for _ in range(5):
        breaker.record_failure()
    assert breaker.allows_call()  # transitions to half-open
    breaker.record_failure()  # trial call fails
    breaker.recovery_seconds = 60.0
    assert breaker.state is BreakerState.OPEN


def test_successful_trial_call_closes_the_circuit():
    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=0.0)
    breaker.record_failure()
    assert breaker.allows_call()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


async def test_open_circuit_fails_fast_without_calling():
    called = False

    async def operation():
        nonlocal called
        called = True
        return "never"

    breaker = CircuitBreaker(failure_threshold=1, recovery_seconds=60.0)
    breaker.record_failure()
    with pytest.raises(CircuitOpenError):
        await call_with_resilience(operation, policy=RetryPolicy(), breaker=breaker)
    assert not called


async def test_breaker_records_outcomes_through_the_helper():
    breaker = CircuitBreaker(failure_threshold=2)

    async def failing():
        raise _http_error(500)

    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await call_with_resilience(
                failing, policy=RetryPolicy(attempts=1), breaker=breaker, sleep=_Recorder()
            )
    assert breaker.state is BreakerState.OPEN


def test_reset_restores_a_tripped_breaker():
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    breaker.reset()
    assert breaker.state is BreakerState.CLOSED
