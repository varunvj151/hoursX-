"""Anthropic Messages API adapter (direct HTTP, no vendor SDK)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from hoursx.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ChatRole,
    ChatUsage,
    StreamDelta,
    ToolCall,
)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, timeout: float = 120.0) -> None:
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        self._timeout = timeout

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role == ChatRole.SYSTEM]
        messages: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role == ChatRole.SYSTEM:
                continue
            if msg.role == ChatRole.TOOL:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )
            elif msg.role == ChatRole.ASSISTANT and msg.tool_calls:
                blocks: list[dict[str, Any]] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in msg.tool_calls
                )
                messages.append({"role": "assistant", "content": blocks})
            else:
                messages.append({"role": msg.role.value, "content": msg.content})
        payload: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in request.tools
            ]
        return payload

    @staticmethod
    def _parse(body: dict[str, Any]) -> ChatResult:
        text = ""
        tool_calls: list[ToolCall] = []
        for block in body.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(id=block["id"], name=block["name"], arguments=block.get("input", {}))
                )
        usage = body.get("usage", {})
        stop = body.get("stop_reason")
        finish = (
            "tool_calls" if stop == "tool_use" else "length" if stop == "max_tokens" else "stop"
        )
        return ChatResult(
            message=ChatMessage(role=ChatRole.ASSISTANT, content=text, tool_calls=tool_calls),
            usage=ChatUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ),
            finish_reason=finish,
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                _API_URL, headers=self._headers, json=self._payload(request)
            )
            response.raise_for_status()
            return self._parse(response.json())

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamDelta]:
        payload = self._payload(request) | {"stream": True}
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        pending_tool: dict[str, Any] | None = None
        pending_json: list[str] = []
        finish: str = "stop"
        usage = ChatUsage()

        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream("POST", _API_URL, headers=self._headers, json=payload) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:].strip())
                kind = event.get("type")
                if kind == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        pending_tool = {"id": block["id"], "name": block["name"]}
                        pending_json = []
                elif kind == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta["text"])
                        yield StreamDelta(kind="text", text=delta["text"])
                    elif delta.get("type") == "input_json_delta":
                        pending_json.append(delta.get("partial_json", ""))
                elif kind == "content_block_stop" and pending_tool is not None:
                    arguments = json.loads("".join(pending_json) or "{}")
                    call = ToolCall(
                        id=pending_tool["id"], name=pending_tool["name"], arguments=arguments
                    )
                    tool_calls.append(call)
                    pending_tool = None
                    yield StreamDelta(kind="tool_call", tool_call=call)
                elif kind == "message_delta":
                    stop = event.get("delta", {}).get("stop_reason")
                    if stop == "tool_use":
                        finish = "tool_calls"
                    elif stop == "max_tokens":
                        finish = "length"
                    usage.output_tokens = event.get("usage", {}).get(
                        "output_tokens", usage.output_tokens
                    )

        yield StreamDelta(
            kind="done",
            result=ChatResult(
                message=ChatMessage(
                    role=ChatRole.ASSISTANT, content="".join(text_parts), tool_calls=tool_calls
                ),
                usage=usage,
                finish_reason=finish,  # type: ignore[arg-type]
            ),
        )

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError(
            "Anthropic does not serve embeddings; point the 'embed' alias at another provider"
        )
