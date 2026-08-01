"""OpenAI-compatible chat/embeddings adapter.

One adapter covers OpenAI itself and every compatible local server (Ollama,
vLLM, LM Studio, llama.cpp server) — the base URL and key are configuration.
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


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._headers = {"content-type": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"

    def _payload(self, request: ChatRequest, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.role == ChatRole.TOOL:
                messages.append(
                    {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
                )
            elif msg.role == ChatRole.ASSISTANT and msg.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in msg.tool_calls
                        ],
                    }
                )
            else:
                messages.append({"role": msg.role.value, "content": msg.content})
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in request.tools
            ]
        return payload

    @staticmethod
    def _parse_choice(choice: dict[str, Any], usage_raw: dict[str, Any]) -> ChatResult:
        message = choice.get("message", {})
        tool_calls = [
            ToolCall(
                id=raw["id"],
                name=raw["function"]["name"],
                arguments=json.loads(raw["function"].get("arguments") or "{}"),
            )
            for raw in message.get("tool_calls") or []
        ]
        reason = choice.get("finish_reason")
        finish = (
            "tool_calls" if reason == "tool_calls" else "length" if reason == "length" else "stop"
        )
        return ChatResult(
            message=ChatMessage(
                role=ChatRole.ASSISTANT,
                content=message.get("content") or "",
                tool_calls=tool_calls,
            ),
            usage=ChatUsage(
                input_tokens=usage_raw.get("prompt_tokens", 0),
                output_tokens=usage_raw.get("completion_tokens", 0),
            ),
            finish_reason=finish,
        )

    async def complete(self, request: ChatRequest) -> ChatResult:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers=self._headers,
                json=self._payload(request, stream=False),
            )
            response.raise_for_status()
            body = response.json()
            return self._parse_choice(body["choices"][0], body.get("usage", {}))

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamDelta]:
        text_parts: list[str] = []
        # index -> accumulating {id, name, arguments-json-fragments}
        partial_calls: dict[int, dict[str, Any]] = {}
        finish = "stop"

        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers=self._headers,
                json=self._payload(request, stream=True),
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                if content := delta.get("content"):
                    text_parts.append(content)
                    yield StreamDelta(kind="text", text=content)
                for raw in delta.get("tool_calls") or []:
                    slot = partial_calls.setdefault(
                        raw.get("index", 0), {"id": "", "name": "", "args": []}
                    )
                    if raw.get("id"):
                        slot["id"] = raw["id"]
                    fn = raw.get("function", {})
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"].append(fn["arguments"])
                if reason := choice.get("finish_reason"):
                    finish = (
                        "tool_calls"
                        if reason == "tool_calls"
                        else "length"
                        if reason == "length"
                        else "stop"
                    )

        tool_calls: list[ToolCall] = []
        for _, slot in sorted(partial_calls.items()):
            call = ToolCall(
                id=slot["id"] or f"call_{len(tool_calls)}",
                name=slot["name"],
                arguments=json.loads("".join(slot["args"]) or "{}"),
            )
            tool_calls.append(call)
            yield StreamDelta(kind="tool_call", tool_call=call)

        yield StreamDelta(
            kind="done",
            result=ChatResult(
                message=ChatMessage(
                    role=ChatRole.ASSISTANT, content="".join(text_parts), tool_calls=tool_calls
                ),
                finish_reason=finish,  # type: ignore[arg-type]
            ),
        )

    async def embed(self, model: str, texts: Sequence[str]) -> list[list[float]]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/embeddings",
                headers=self._headers,
                json={"model": model, "input": list(texts)},
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            return [item["embedding"] for item in data]
