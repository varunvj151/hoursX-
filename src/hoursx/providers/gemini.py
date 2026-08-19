"""Google Gemini adapter.

Gemini's wire format differs from the others in three ways that this adapter
absorbs, so nothing above ``hoursx.providers`` has to know about them:

- messages are ``contents`` with ``parts``, and the assistant role is ``model``
- there is no system role; instructions go in a separate ``systemInstruction``
- tool calls are ``functionCall`` parts rather than a distinct field
"""

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

_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider:
    name = "gemini"

    def __init__(
        self, api_key: str, base_url: str = _DEFAULT_BASE_URL, timeout: float = 120.0
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    # ------------------------------------------------------------- translation

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        system_parts = [m.content for m in request.messages if m.role == ChatRole.SYSTEM]
        contents: list[dict[str, Any]] = []

        for message in request.messages:
            if message.role == ChatRole.SYSTEM:
                continue
            if message.role == ChatRole.TOOL:
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    # Gemini keys the response by function name,
                                    # not by call id, so the id is carried in the
                                    # name slot the request used.
                                    "name": message.tool_call_id or "tool",
                                    "response": {"result": message.content},
                                }
                            }
                        ],
                    }
                )
                continue

            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"text": message.content})
            parts.extend(
                {"functionCall": {"name": call.name, "args": call.arguments}}
                for call in message.tool_calls
            )
            if not parts:
                continue
            contents.append(
                {"role": "model" if message.role == ChatRole.ASSISTANT else "user", "parts": parts}
            )

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": _strip_unsupported(tool.parameters),
                        }
                        for tool in request.tools
                    ]
                }
            ]
        return payload

    @staticmethod
    def _parse(body: dict[str, Any]) -> ChatResult:
        candidates = body.get("candidates") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        if candidates:
            for index, part in enumerate(candidates[0].get("content", {}).get("parts", [])):
                if "text" in part:
                    text_parts.append(part["text"])
                elif "functionCall" in part:
                    call = part["functionCall"]
                    tool_calls.append(
                        ToolCall(
                            # Gemini does not mint call ids, so one is derived
                            # from name and position to keep results pairable.
                            id=f"{call.get('name', 'call')}_{index}",
                            name=call.get("name", ""),
                            arguments=call.get("args") or {},
                        )
                    )

        usage = body.get("usageMetadata", {})
        reason = (candidates[0].get("finishReason") if candidates else "") or ""
        finish = "tool_calls" if tool_calls else "length" if reason == "MAX_TOKENS" else "stop"
        return ChatResult(
            message=ChatMessage(
                role=ChatRole.ASSISTANT, content="".join(text_parts), tool_calls=tool_calls
            ),
            usage=ChatUsage(
                input_tokens=usage.get("promptTokenCount", 0),
                output_tokens=usage.get("candidatesTokenCount", 0),
            ),
            finish_reason=finish,  # type: ignore[arg-type]
        )

    # ---------------------------------------------------------------- requests

    async def complete(self, request: ChatRequest) -> ChatResult:
        url = f"{self._base_url}/models/{request.model}:generateContent"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": self._api_key, "content-type": "application/json"},
                json=self._payload(request),
            )
            response.raise_for_status()
            return self._parse(response.json())

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamDelta]:
        url = f"{self._base_url}/models/{request.model}:streamGenerateContent?alt=sse"
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = ChatUsage()
        finish = "stop"

        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream(
                "POST",
                url,
                headers={"x-goog-api-key": self._api_key, "content-type": "application/json"},
                json=self._payload(request),
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = json.loads(line[5:].strip())
                candidates = chunk.get("candidates") or []
                if not candidates:
                    continue
                for index, part in enumerate(candidates[0].get("content", {}).get("parts", [])):
                    if "text" in part:
                        text_parts.append(part["text"])
                        yield StreamDelta(kind="text", text=part["text"])
                    elif "functionCall" in part:
                        call_data = part["functionCall"]
                        call = ToolCall(
                            id=f"{call_data.get('name', 'call')}_{len(tool_calls)}_{index}",
                            name=call_data.get("name", ""),
                            arguments=call_data.get("args") or {},
                        )
                        tool_calls.append(call)
                        yield StreamDelta(kind="tool_call", tool_call=call)
                if reason := candidates[0].get("finishReason"):
                    finish = "length" if reason == "MAX_TOKENS" else "stop"
                if meta := chunk.get("usageMetadata"):
                    usage = ChatUsage(
                        input_tokens=meta.get("promptTokenCount", 0),
                        output_tokens=meta.get("candidatesTokenCount", 0),
                    )

        yield StreamDelta(
            kind="done",
            result=ChatResult(
                message=ChatMessage(
                    role=ChatRole.ASSISTANT, content="".join(text_parts), tool_calls=tool_calls
                ),
                usage=usage,
                finish_reason="tool_calls" if tool_calls else finish,  # type: ignore[arg-type]
            ),
        )

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        url = f"{self._base_url}/models/{model}:batchEmbedContents"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                headers={"x-goog-api-key": self._api_key, "content-type": "application/json"},
                json={
                    "requests": [
                        {"model": f"models/{model}", "content": {"parts": [{"text": text}]}}
                        for text in texts
                    ]
                },
            )
            response.raise_for_status()
            return [item["values"] for item in response.json().get("embeddings", [])]


def _strip_unsupported(schema: Any) -> Any:
    """Remove JSON-schema keywords Gemini rejects.

    Pydantic emits ``$defs``, ``additionalProperties``, and ``title``; Gemini's
    function declarations accept only a subset and error on the rest. Stripping
    here keeps tool authors from having to know that.
    """
    if isinstance(schema, dict):
        return {
            key: _strip_unsupported(value)
            for key, value in schema.items()
            if key not in {"title", "additionalProperties", "$defs", "definitions", "default"}
        }
    if isinstance(schema, list):
        return [_strip_unsupported(item) for item in schema]
    return schema
