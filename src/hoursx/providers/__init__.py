"""Model provider abstraction: chat, streaming, embeddings, and routing."""

from hoursx.providers.router import ModelRouter, ProviderError, build_default_router
from hoursx.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ChatRole,
    ChatUsage,
    ModelProvider,
    StreamDelta,
    ToolCall,
    ToolDescriptor,
)

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "ChatRole",
    "ChatUsage",
    "ModelProvider",
    "ModelRouter",
    "ProviderError",
    "StreamDelta",
    "ToolCall",
    "ToolDescriptor",
    "build_default_router",
]
