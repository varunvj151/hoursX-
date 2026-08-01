"""Router-level integration of retry, circuit breaking, and fallback chains."""

import httpx
import pytest

from hoursx.providers.echo import EchoProvider
from hoursx.providers.router import ModelRouter, ProviderError, _HashEmbedProvider
from hoursx.providers.types import ChatMessage, ChatRequest, ChatRole
from hoursx.resilience import RetryPolicy


def _request(text: str = "hello") -> ChatRequest:
    return ChatRequest(model="unset", messages=[ChatMessage(role=ChatRole.USER, content=text)])


class _FlakyProvider:
    """Fails `failures` times with a retryable error, then succeeds."""

    def __init__(self, name: str = "flaky", failures: int = 0, permanent: bool = False) -> None:
        self.name = name
        self.remaining = failures
        self.permanent = permanent
        self.calls = 0

    def _maybe_fail(self):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            request = httpx.Request("POST", "https://example.invalid")
            status = 401 if self.permanent else 503
            raise httpx.HTTPStatusError(
                "fail", request=request, response=httpx.Response(status, request=request)
            )

    async def complete(self, request):
        self._maybe_fail()
        from hoursx.providers.types import ChatResult

        return ChatResult(message=ChatMessage(role=ChatRole.ASSISTANT, content=f"{self.name} ok"))

    async def stream(self, request):
        self._maybe_fail()
        from hoursx.providers.types import ChatResult, StreamDelta

        yield StreamDelta(kind="text", text=f"{self.name} ok")
        yield StreamDelta(
            kind="done",
            result=ChatResult(
                message=ChatMessage(role=ChatRole.ASSISTANT, content=f"{self.name} ok")
            ),
        )

    async def embed(self, model, texts):
        self._maybe_fail()
        return [[1.0, 0.0] for _ in texts]


def _router(providers, **kwargs) -> ModelRouter:
    kwargs.setdefault("retry_policy", RetryPolicy(attempts=3, base_delay=0.001, jitter=False))
    return ModelRouter(providers, **kwargs)


# --------------------------------------------------------------------- retry


async def test_router_retries_transient_provider_failure():
    flaky = _FlakyProvider(failures=2)
    router = _router({"flaky": flaky}, aliases={"deep": "flaky/m"})
    result = await router.complete("deep", _request())
    assert result.message.content == "flaky ok"
    assert flaky.calls == 3


async def test_router_does_not_retry_permanent_failure():
    flaky = _FlakyProvider(failures=5, permanent=True)
    router = _router(
        {"flaky": flaky, "echo": EchoProvider()},
        aliases={"deep": "flaky/m"},
        fallbacks={"deep": ["echo/e"]},
    )
    result = await router.complete("deep", _request("fall through"))
    # One attempt on the permanent failure, then straight to the fallback.
    assert flaky.calls == 1
    assert result.message.content == "echo: fall through"


# ------------------------------------------------------------------ fallback


async def test_fallback_reaches_a_healthy_provider():
    router = _router(
        {"broken": _FlakyProvider(name="broken", failures=99), "echo": EchoProvider()},
        aliases={"deep": "broken/m"},
        fallbacks={"deep": ["echo/e"]},
    )
    result = await router.complete("deep", _request("recovered"))
    assert result.message.content == "echo: recovered"


async def test_all_providers_failing_raises_provider_error():
    router = _router(
        {"a": _FlakyProvider(name="a", failures=99), "b": _FlakyProvider(name="b", failures=99)},
        aliases={"deep": "a/m"},
        fallbacks={"deep": ["b/m"]},
    )
    with pytest.raises(ProviderError):
        await router.complete("deep", _request())


async def test_unresolvable_fallback_entry_is_skipped():
    router = _router(
        {"echo": EchoProvider()},
        aliases={"deep": "ghost/model"},
        fallbacks={"deep": ["echo/e"]},
    )
    result = await router.complete("deep", _request("skipped ghost"))
    assert result.message.content == "echo: skipped ghost"


# ----------------------------------------------------------- circuit breaker


async def test_repeated_failures_open_the_breaker():
    flaky = _FlakyProvider(failures=999)
    router = _router(
        {"flaky": flaky},
        aliases={"deep": "flaky/m"},
        breaker_threshold=2,
        breaker_recovery_seconds=60.0,
        retry_policy=RetryPolicy(attempts=1, base_delay=0.001, jitter=False),
    )
    for _ in range(2):
        with pytest.raises(ProviderError):
            await router.complete("deep", _request())
    breaker = router.breaker_for("flaky")
    assert breaker is not None and not breaker.allows_call()


async def test_open_breaker_short_circuits_to_fallback():
    flaky = _FlakyProvider(name="flaky", failures=999)
    router = _router(
        {"flaky": flaky, "echo": EchoProvider()},
        aliases={"deep": "flaky/m"},
        fallbacks={"deep": ["echo/e"]},
        breaker_threshold=1,
        breaker_recovery_seconds=60.0,
        retry_policy=RetryPolicy(attempts=1, base_delay=0.001, jitter=False),
    )
    await router.complete("deep", _request("first"))
    calls_after_first = flaky.calls
    result = await router.complete("deep", _request("second"))
    assert result.message.content == "echo: second"
    # The dead provider is not called again while its circuit is open.
    assert flaky.calls == calls_after_first


async def test_stream_selection_skips_a_broken_provider():
    router = _router(
        {"flaky": _FlakyProvider(name="flaky", failures=999), "echo": EchoProvider()},
        aliases={"deep": "flaky/m"},
        fallbacks={"deep": ["echo/e"]},
        breaker_threshold=1,
        breaker_recovery_seconds=60.0,
    )
    breaker = router.breaker_for("flaky")
    breaker.record_failure()
    deltas = [d async for d in router.stream("deep", _request("streamed"))]
    assert deltas[-1].kind == "done"
    assert "echo: streamed" in deltas[-1].result.message.content


async def test_stream_outcome_feeds_the_breaker():
    """Streaming bypasses the retry helper, so outcomes are reported explicitly —
    otherwise a provider failing mid-stream would never trip its circuit."""
    router = _router({"echo": EchoProvider()}, aliases={"deep": "echo/e"}, breaker_threshold=2)
    router.note_stream_outcome("deep", ok=False)
    router.note_stream_outcome("deep", ok=False)
    breaker = router.breaker_for("echo")
    assert not breaker.allows_call()
    router.note_stream_outcome("deep", ok=True)


async def test_note_stream_outcome_tolerates_unknown_alias():
    router = _router({"echo": EchoProvider()}, aliases={"deep": "echo/e"})
    router.note_stream_outcome("no-such-alias", ok=False)  # must not raise


async def test_stream_with_no_available_provider_raises():
    router = _router({"echo": EchoProvider()}, aliases={"deep": "ghost/m"})
    with pytest.raises(ProviderError):
        router.stream("deep", _request())


# ----------------------------------------------------------------- embedding


async def test_embed_retries_then_succeeds():
    flaky = _FlakyProvider(failures=1)
    router = _router({"flaky": flaky}, aliases={"embed": "flaky/e"})
    vectors = await router.embed(["text"])
    assert vectors == [[1.0, 0.0]]
    assert flaky.calls == 2


async def test_embed_uses_the_hash_provider_by_default():
    router = _router({"hash": _HashEmbedProvider()}, aliases={"embed": "hash/hash-embed-256"})
    [vector] = await router.embed(["deterministic"])
    assert len(vector) == 256


async def test_each_provider_gets_an_independent_breaker():
    router = _router(
        {"a": _FlakyProvider(name="a"), "b": _FlakyProvider(name="b")},
        breaker_threshold=1,
    )
    router.breaker_for("a").record_failure()
    assert not router.breaker_for("a").allows_call()
    assert router.breaker_for("b").allows_call()


def test_breaker_lookup_returns_none_for_unknown_provider():
    router = _router({"echo": EchoProvider()})
    assert router.breaker_for("nope") is None
