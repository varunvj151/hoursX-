"""Deterministic provider for development and tests.

``EchoProvider`` replays scripted results when given any, otherwise echoes the
last user message. It exists so the entire platform — run loop, streaming, tool
dispatch, approvals — can be exercised hermetically, with no network or vendor
account.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from hoursx.providers.hashing import hash_embedding
from hoursx.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ChatRole,
    StreamDelta,
)


class EchoProvider:
    name = "echo"

    def __init__(self, script: Sequence[ChatResult] | None = None) -> None:
        self._script: list[ChatResult] = list(script or [])

    def _next(self, request: ChatRequest) -> ChatResult:
        if self._script:
            return self._script.pop(0)
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == ChatRole.USER),
            "",
        )
        return ChatResult(
            message=ChatMessage(role=ChatRole.ASSISTANT, content=f"echo: {last_user}")
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        return self._next(request)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamDelta]:
        result = self._next(request)
        text = result.message.content
        # Chunk text to exercise real streaming consumers.
        for start in range(0, len(text), 16):
            yield StreamDelta(kind="text", text=text[start : start + 16])
        for call in result.message.tool_calls:
            yield StreamDelta(kind="tool_call", tool_call=call)
        yield StreamDelta(kind="done", result=result)

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        return [hash_embedding(text) for text in texts]
