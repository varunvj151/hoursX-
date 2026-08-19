"""Telegram Bot API channel.

Telegram is the reference implementation of the channel protocol: it is fully
exercisable without OAuth, and every other adapter follows its shape.

Getting a token is a manual step — you message **@BotFather** in Telegram, run
``/newbot``, and it hands you one. BotFather is a bot you talk to, not an API
you integrate against, so there is nothing to automate there. Paste the token
into ``HOURSX_TELEGRAM_TOKEN`` and this adapter does the rest.
"""

from __future__ import annotations

import hmac
from typing import Any

import httpx

from hoursx.channels.base import (
    ChannelCredentials,
    ChannelKind,
    DeliveryResult,
    InboundMessage,
    OutboundMessage,
    SignatureError,
)
from hoursx.observability import get_logger

log = get_logger("channels.telegram")

_API_BASE = "https://api.telegram.org"
# Telegram rejects anything longer and silently truncates nothing.
_MAX_MESSAGE_CHARS = 4096


class TelegramChannel:
    kind = ChannelKind.TELEGRAM

    def __init__(self, credentials: ChannelCredentials, timeout: float = 30.0) -> None:
        self._token = credentials.token
        self._secret = credentials.secret
        self._timeout = timeout

    def verify(self, *, headers: dict[str, str], body: bytes) -> None:
        """Check Telegram's webhook secret header.

        Telegram does not sign payloads; it echoes a secret the operator set
        when registering the webhook. Comparison is constant-time because it is
        a shared secret, and the header is required whenever a secret is
        configured — a missing header must not silently pass.
        """
        if not self._secret:
            return
        provided = headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(provided, self._secret):
            raise SignatureError("telegram webhook secret did not match")

    def parse(self, payload: dict[str, Any]) -> InboundMessage | None:
        # Telegram sends edits, callbacks, and channel posts down the same
        # webhook; only plain messages with text become agent input.
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text") or message.get("caption") or ""
        if not text or "id" not in chat:
            return None

        display = " ".join(
            part for part in (sender.get("first_name"), sender.get("last_name")) if part
        ) or sender.get("username", "")

        attachments: list[dict[str, Any]] = []
        for key in ("document", "photo", "voice", "audio", "video"):
            if key in message:
                attachments.append({"kind": key, "payload": message[key]})

        return InboundMessage(
            channel=self.kind,
            conversation_id=str(chat["id"]),
            external_id=str(message.get("message_id", "")),
            sender_id=str(sender.get("id", chat["id"])),
            sender_display=display,
            text=text,
            attachments=attachments,
            raw=message,
        )

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if not self._token:
            return DeliveryResult(False, "no Telegram bot token configured")

        chunks = _split(message.text, _MAX_MESSAGE_CHARS)
        sent = 0
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for chunk in chunks:
                body: dict[str, Any] = {
                    "chat_id": message.conversation_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                }
                if message.reply_to and sent == 0:
                    body["reply_to_message_id"] = message.reply_to
                try:
                    response = await client.post(
                        f"{_API_BASE}/bot{self._token}/sendMessage", json=body
                    )
                except httpx.HTTPError as exc:
                    return DeliveryResult(False, f"telegram unreachable: {exc}")
                if response.status_code != 200:
                    detail = _describe_error(response)
                    log.warning("telegram send failed: %s", detail)
                    return DeliveryResult(
                        False,
                        f"telegram rejected the message: {detail}",
                        {"sent_chunks": sent},
                    )
                sent += 1
        return DeliveryResult(
            True,
            f"delivered {sent} message(s) to {message.conversation_id}",
            {"chunks": sent},
        )

    async def register_webhook(self, url: str) -> DeliveryResult:
        """Point Telegram at this deployment's ingress endpoint."""
        if not self._token:
            return DeliveryResult(False, "no Telegram bot token configured")
        body: dict[str, Any] = {"url": url, "allowed_updates": ["message", "edited_message"]}
        if self._secret:
            body["secret_token"] = self._secret
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{_API_BASE}/bot{self._token}/setWebhook", json=body)
        if response.status_code != 200:
            return DeliveryResult(False, f"could not register webhook: {_describe_error(response)}")
        return DeliveryResult(True, f"webhook registered at {url}")


def _split(text: str, limit: int) -> list[str]:
    """Split on line boundaries where possible, so replies stay readable."""
    if len(text) <= limit:
        return [text or "(empty response)"]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


def _describe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("description") or body)[:200]
    except ValueError:
        return f"HTTP {response.status_code}"
