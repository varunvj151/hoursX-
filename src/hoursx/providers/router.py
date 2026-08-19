"""Model routing.

Callers address models by *alias* ("deep", "fast", "embed") or explicit
``provider/model`` ref. The router resolves aliases, dispatches to the right
provider, and walks a configured fallback chain when a provider fails — so agent
profiles express intent, and vendor choice stays in configuration.

Each provider gets its own circuit breaker, so one dead vendor cannot slow every
request while a healthy fallback exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from hoursx.config import HoursXSettings
from hoursx.errors import ProviderUnavailableError
from hoursx.observability import get_logger
from hoursx.providers.echo import EchoProvider
from hoursx.providers.hashing import hash_embedding
from hoursx.providers.types import ChatRequest, ChatResult, ModelProvider, StreamDelta
from hoursx.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, call_with_resilience

log = get_logger("providers.router")


class ProviderError(ProviderUnavailableError):
    """A provider failed or a ref could not be resolved.

    Subclasses the domain error so the API maps it to 503 without the route
    layer knowing anything about providers.
    """


class _HashEmbedProvider:
    """Registry-shaped wrapper around the deterministic hashing embedder."""

    name = "hash"

    async def complete(self, request: ChatRequest) -> ChatResult:
        raise ProviderError("hash provider serves embeddings only")

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamDelta]:
        raise ProviderError("hash provider serves embeddings only")

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        return [hash_embedding(text) for text in texts]


class ModelRouter:
    """Alias resolution, provider dispatch, retry/breaker, and fallback chains."""

    def __init__(
        self,
        providers: dict[str, ModelProvider],
        aliases: dict[str, str] | None = None,
        fallbacks: dict[str, list[str]] | None = None,
        *,
        retry_policy: RetryPolicy | None = None,
        breaker_threshold: int = 5,
        breaker_recovery_seconds: float = 30.0,
    ) -> None:
        self._providers = providers
        self._aliases = dict(aliases or {})
        self._fallbacks = dict(fallbacks or {})
        self._retry = retry_policy or RetryPolicy()
        self._breakers = {
            name: CircuitBreaker(
                failure_threshold=breaker_threshold,
                recovery_seconds=breaker_recovery_seconds,
                name=name,
            )
            for name in providers
        }

    def resolve(self, ref_or_alias: str) -> tuple[ModelProvider, str]:
        """Resolve an alias or ``provider/model`` ref to (provider, model)."""
        ref = self._aliases.get(ref_or_alias, ref_or_alias)
        provider_name, _, model = ref.partition("/")
        if not model:
            raise ProviderError(f"model ref {ref!r} is not of the form provider/model")
        provider = self._providers.get(provider_name)
        if provider is None:
            raise ProviderError(f"no provider registered for {provider_name!r}")
        return provider, model

    def breaker_for(self, provider_name: str) -> CircuitBreaker | None:
        return self._breakers.get(provider_name)

    def _chain(self, ref_or_alias: str) -> list[str]:
        primary = self._aliases.get(ref_or_alias, ref_or_alias)
        return [primary, *self._fallbacks.get(ref_or_alias, [])]

    async def complete(self, ref_or_alias: str, request: ChatRequest) -> ChatResult:
        """Complete via the first provider in the chain that succeeds.

        Each link gets its own retry budget; a link that is circuit-broken is
        skipped instantly so the chain reaches a healthy provider fast.
        """
        last_error: Exception | None = None
        for ref in self._chain(ref_or_alias):
            try:
                provider, model = self.resolve(ref)
            except ProviderError as exc:
                last_error = exc
                continue
            scoped = request.model_copy(update={"model": model})
            try:
                return await call_with_resilience(
                    lambda p=provider, r=scoped: p.complete(r),
                    policy=self._retry,
                    breaker=self._breakers.get(provider.name),
                )
            except CircuitOpenError as exc:
                last_error = exc
                log.warning("skipping %s: circuit open", provider.name)
            except Exception as exc:  # noqa: BLE001 — any failure advances the chain
                last_error = exc
                log.warning("provider %s failed: %s", provider.name, exc)
        raise ProviderError(
            f"all providers failed for {ref_or_alias!r}: {last_error}"
        ) from last_error

    def stream(self, ref_or_alias: str, request: ChatRequest) -> AsyncIterator[StreamDelta]:
        """Stream from the first resolvable, non-broken provider in the chain.

        Streaming never retries or falls back *mid-stream*: once deltas have
        been emitted the caller has consumed part of an answer, and restarting
        would duplicate text. Selection happens before the first byte only.
        """
        last_error: Exception | None = None
        for ref in self._chain(ref_or_alias):
            try:
                provider, model = self.resolve(ref)
            except ProviderError as exc:
                last_error = exc
                continue
            breaker = self._breakers.get(provider.name)
            if breaker is not None and not breaker.allows_call():
                last_error = CircuitOpenError(provider.name)
                continue
            return provider.stream(request.model_copy(update={"model": model}))
        raise ProviderError(
            f"no available provider for {ref_or_alias!r}: {last_error}"
        ) from last_error

    def note_stream_outcome(self, ref_or_alias: str, *, ok: bool) -> None:
        """Feed a completed stream's outcome back into the breaker.

        Streaming bypasses ``call_with_resilience``, so the runtime reports the
        result here; otherwise a provider that always fails mid-stream would
        never trip its circuit.
        """
        try:
            provider, _ = self.resolve(self._chain(ref_or_alias)[0])
        except (ProviderError, IndexError):
            return
        breaker = self._breakers.get(provider.name)
        if breaker is None:
            return
        breaker.record_success() if ok else breaker.record_failure()

    async def embed(self, texts: Sequence[str], ref_or_alias: str = "embed") -> list[list[float]]:
        provider, model = self.resolve(ref_or_alias)
        return await call_with_resilience(
            lambda: provider.embed(model, texts),
            policy=self._retry,
            breaker=self._breakers.get(provider.name),
        )


def build_default_router(settings: HoursXSettings) -> ModelRouter:
    """Assemble the router from configuration. Providers with no credentials are
    simply absent; the deterministic echo/hash providers are always present so a
    zero-config install still functions end to end."""
    providers: dict[str, ModelProvider] = {
        "echo": EchoProvider(),
        "hash": _HashEmbedProvider(),
    }
    if settings.anthropic_api_key:
        from hoursx.providers.anthropic import AnthropicProvider

        providers["anthropic"] = AnthropicProvider(settings.anthropic_api_key)
    if settings.openai_api_key:
        from hoursx.providers.openai_compat import OpenAICompatProvider

        providers["openai"] = OpenAICompatProvider(
            name="openai", base_url=settings.openai_base_url, api_key=settings.openai_api_key
        )
    if settings.gemini_api_key:
        from hoursx.providers.gemini import GeminiProvider

        providers["gemini"] = GeminiProvider(settings.gemini_api_key)
    if settings.local_base_url:
        from hoursx.providers.openai_compat import OpenAICompatProvider

        providers["local"] = OpenAICompatProvider(name="local", base_url=settings.local_base_url)
    return ModelRouter(
        providers,
        settings.model_aliases,
        settings.model_fallbacks,
        retry_policy=RetryPolicy(attempts=settings.provider_retry_attempts),
        breaker_threshold=settings.breaker_failure_threshold,
        breaker_recovery_seconds=settings.breaker_recovery_seconds,
    )
