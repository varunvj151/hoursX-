"""WhatsApp Business Cloud API channel.

Meta signs every webhook with HMAC-SHA256 over the raw body, so unlike Telegram
this adapter verifies a real signature rather than an echoed secret.

Note the constraint that shapes usage: outside a 24-hour window from the user's
last message, WhatsApp only permits pre-approved template messages. Free-form
agent replies are therefore reliable in-session and rejected outside it, which
this adapter surfaces as an explicit failure rather than a silent drop.
"""

from __future__ import annotations

import hashlib
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

log = get_logger("channels.whatsapp")

_GRAPH_BASE = "https://graph.facebook.com/v21.0"
_MAX_MESSAGE_CHARS = 4096


class WhatsAppChannel:
    kind = ChannelKind.WHATSAPP

    def __init__(self, credentials: ChannelCredentials, timeout: float = 30.0) -> None:
        self._token = credentials.token
        self._app_secret = credentials.secret
        self._phone_number_id = credentials.account_id
        self._timeout = timeout

    def verify(self, *, headers: dict[str, str], body: bytes) -> None:
        """Verify Meta's ``X-Hub-Signature-256`` over the exact raw body.

        The signature covers the bytes as received, so this must run before any
        JSON parsing or re-serialisation — a round-trip through a dict would
        change the bytes and invalidate an authentic payload.
        """
        if not self._app_secret:
            return
        header = headers.get("x-hub-signature-256", "")
        if not header.startswith("sha256="):
            raise SignatureError("missing or malformed X-Hub-Signature-256 header")
        expected = hmac.new(self._app_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(header[7:], expected):
            raise SignatureError("whatsapp webhook signature did not match")

    def parse(self, payload: dict[str, Any]) -> InboundMessage | None:
        # Meta nests messages several levels deep and sends status callbacks
        # (delivered, read) through the same webhook; only real inbound text
        # becomes agent input.
        try:
            change = payload["entry"][0]["changes"][0]["value"]
        except (KeyError, IndexError, TypeError):
            return None
        messages = change.get("messages")
        if not messages:
            return None  # a status callback, not a message

        message = messages[0]
        sender = str(message.get("from", ""))
        if not sender:
            return None

        text = ""
        if message.get("type") == "text":
            text = message.get("text", {}).get("body", "")
        elif "caption" in message.get(message.get("type", ""), {}):
            text = message[message["type"]]["caption"]
        if not text:
            return None

        contacts = change.get("contacts") or [{}]
        display = contacts[0].get("profile", {}).get("name", "")

        return InboundMessage(
            channel=self.kind,
            # WhatsApp conversations are keyed by the sender's phone number;
            # there is no separate thread id.
            conversation_id=sender,
            external_id=str(message.get("id", "")),
            sender_id=sender,
            sender_display=display,
            text=text,
            raw=message,
        )

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if not self._token or not self._phone_number_id:
            return DeliveryResult(False, "WhatsApp token or phone number id is not configured")

        chunks = _split(message.text, _MAX_MESSAGE_CHARS)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for index, chunk in enumerate(chunks):
                try:
                    response = await client.post(
                        f"{_GRAPH_BASE}/{self._phone_number_id}/messages",
                        headers={"authorization": f"Bearer {self._token}"},
                        json={
                            "messaging_product": "whatsapp",
                            "to": message.conversation_id,
                            "type": "text",
                            "text": {"body": chunk, "preview_url": False},
                        },
                    )
                except httpx.HTTPError as exc:
                    return DeliveryResult(False, f"whatsapp unreachable: {exc}")
                if response.status_code >= 400:
                    detail = _describe_error(response)
                    # 131047 is Meta's code for the 24-hour window having closed.
                    if "131047" in detail:
                        return DeliveryResult(
                            False,
                            "the 24-hour customer service window has closed; only a "
                            "pre-approved template message can be sent now",
                            {"sent_chunks": index},
                        )
                    return DeliveryResult(False, f"whatsapp rejected the message: {detail}")
        return DeliveryResult(True, f"delivered {len(chunks)} message(s)", {"chunks": len(chunks)})

    def verification_challenge(self, params: dict[str, str]) -> str | None:
        """Answer Meta's one-time webhook verification handshake."""
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(
            params.get("hub.verify_token", ""), self._app_secret
        ):
            return params.get("hub.challenge")
        return None


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text or "(empty response)"]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _describe_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("error", body))[:250]
    except ValueError:
        return f"HTTP {response.status_code}"
