"""Shared chat/embedding types and the :class:`ModelProvider` protocol.

Every provider adapts its vendor wire format to these shapes; nothing outside
``hoursx.providers`` ever sees vendor-specific payloads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """The model asking for a tool invocation."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolDescriptor(BaseModel):
    """What a provider needs to advertise a tool to the model."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema


class ChatMessage(BaseModel):
    role: ChatRole
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # set on ChatRole.TOOL result messages


class ChatRequest(BaseModel):
    """A provider-agnostic chat completion request. ``model`` is the bare model
    name — routing has already stripped the ``provider/`` prefix."""

    model: str
    messages: list[ChatMessage]
    tools: list[ToolDescriptor] = Field(default_factory=list)
    temperature: float = 0.4
    max_tokens: int = 4096


class ChatUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


FinishReason = Literal["stop", "tool_calls", "length", "error"]


class ChatResult(BaseModel):
    message: ChatMessage
    usage: ChatUsage = Field(default_factory=ChatUsage)
    finish_reason: FinishReason = "stop"


class StreamDelta(BaseModel):
    """One streaming increment: text, a complete tool call, or the final result."""

    kind: Literal["text", "tool_call", "done"]
    text: str = ""
    tool_call: ToolCall | None = None
    result: ChatResult | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """The contract every model backend implements."""

    name: str

    async def complete(self, request: ChatRequest) -> ChatResult: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamDelta]: ...

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]: ...


async def collect_stream(deltas: AsyncIterator[StreamDelta]) -> ChatResult:
    """Drain a delta stream into its final :class:`ChatResult`."""
    async for delta in deltas:
        if delta.kind == "done" and delta.result is not None:
            return delta.result
    raise RuntimeError("stream ended without a terminal result")
