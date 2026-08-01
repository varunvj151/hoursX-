"""Provider streaming, router resolution, and fallback behavior."""

import pytest

from hoursx.providers.echo import EchoProvider
from hoursx.providers.hashing import cosine_similarity, hash_embedding
from hoursx.providers.router import ModelRouter, ProviderError, _HashEmbedProvider
from hoursx.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ChatRole,
    collect_stream,
)


def _request(text: str) -> ChatRequest:
    return ChatRequest(model="unset", messages=[ChatMessage(role=ChatRole.USER, content=text)])


async def test_echo_completes_with_last_user_message():
    result = await EchoProvider().complete(_request("hello there"))
    assert result.message.content == "echo: hello there"


async def test_echo_stream_chunks_and_terminates():
    provider = EchoProvider()
    deltas = [d async for d in provider.stream(_request("a" * 40))]
    text = "".join(d.text for d in deltas if d.kind == "text")
    assert text == "echo: " + "a" * 40
    assert deltas[-1].kind == "done"
    result = await collect_stream(provider.stream(_request("x")))
    assert isinstance(result, ChatResult)


def test_hash_embedding_is_deterministic_and_normalized():
    a1, a2 = hash_embedding("agent platform"), hash_embedding("agent platform")
    assert a1 == a2
    assert abs(sum(v * v for v in a1) - 1.0) < 1e-9


def test_hash_embedding_similar_texts_score_higher():
    docs = hash_embedding("kubernetes deployment manifests for the API")
    close = hash_embedding("deployment manifests kubernetes")
    far = hash_embedding("chocolate cake recipe with vanilla")
    assert cosine_similarity(docs, close) > cosine_similarity(docs, far)


def test_router_resolves_alias_and_explicit_ref():
    router = ModelRouter({"echo": EchoProvider()}, aliases={"deep": "echo/e1"})
    provider, model = router.resolve("deep")
    assert provider.name == "echo" and model == "e1"
    provider, model = router.resolve("echo/other")
    assert model == "other"


@pytest.mark.parametrize("bad", ["nope/model", "echo", "missing-alias"])
def test_router_rejects_unresolvable(bad):
    router = ModelRouter({"echo": EchoProvider()})
    with pytest.raises(ProviderError):
        router.resolve(bad)


async def test_router_falls_back_on_provider_failure():
    class Broken:
        name = "broken"

        async def complete(self, request):
            raise RuntimeError("boom")

        def stream(self, request):
            raise RuntimeError("boom")

        async def embed(self, model, texts):
            raise RuntimeError("boom")

    router = ModelRouter(
        {"broken": Broken(), "echo": EchoProvider()},
        aliases={"deep": "broken/b"},
        fallbacks={"deep": ["echo/e"]},
    )
    result = await router.complete("deep", _request("fallback works"))
    assert result.message.content == "echo: fallback works"


async def test_router_raises_when_all_fail():
    class Broken:
        name = "broken"

        async def complete(self, request):
            raise RuntimeError("boom")

        def stream(self, request):
            raise RuntimeError("boom")

        async def embed(self, model, texts):
            raise RuntimeError("boom")

    router = ModelRouter({"broken": Broken()}, aliases={"deep": "broken/b"})
    with pytest.raises(ProviderError):
        await router.complete("deep", _request("x"))


async def test_hash_provider_embeds_via_router():
    router = ModelRouter({"hash": _HashEmbedProvider()}, aliases={"embed": "hash/hash-embed-256"})
    [vec] = await router.embed(["some text"])
    assert len(vec) == 256
