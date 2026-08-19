"""Gmail channel.

Gmail differs from the chat channels in two ways worth naming:

- **Push is a pointer, not a payload.** Gmail's Pub/Sub notification says "this
  mailbox changed"; the message itself must then be fetched. So ``parse``
  yields a lightweight inbound record and the fetch happens separately.
- **Replies must thread.** An email reply that omits ``In-Reply-To`` and
  ``References`` starts a new conversation in the recipient's client, which
  reads as a broken bot. Those headers are carried through deliberately.

Authentication is an OAuth2 access token supplied by the operator. Obtaining
and refreshing it is a credential-management concern that belongs outside this
adapter, which simply uses what it is given and reports clearly when the token
is rejected.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

import httpx

from hoursx.channels.base import (
    ChannelCredentials,
    ChannelKind,
    DeliveryResult,
    InboundMessage,
    OutboundMessage,
)
from hoursx.observability import get_logger

log = get_logger("channels.gmail")

_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailChannel:
    kind = ChannelKind.GMAIL

    def __init__(self, credentials: ChannelCredentials, timeout: float = 30.0) -> None:
        self._token = credentials.token
        self._address = credentials.account_id
        self._timeout = timeout

    def verify(self, *, headers: dict[str, str], body: bytes) -> None:
        """Pub/Sub authenticity is established by the push subscription's OIDC
        token at the ingress layer, so there is nothing adapter-specific to
        check here."""
        return None

    def parse(self, payload: dict[str, Any]) -> InboundMessage | None:
        """Turn a Pub/Sub push envelope into a pointer at the changed mailbox.

        The notification carries a history id rather than a message, so the
        text is empty until :meth:`fetch_message` resolves it.
        """
        message = payload.get("message")
        if not isinstance(message, dict) or "data" not in message:
            return None
        try:
            decoded = base64.urlsafe_b64decode(message["data"]).decode(errors="replace")
        except (ValueError, TypeError):
            return None
        import json

        try:
            notification = json.loads(decoded)
        except json.JSONDecodeError:
            return None

        address = notification.get("emailAddress", self._address)
        history_id = str(notification.get("historyId", ""))
        if not history_id:
            return None
        return InboundMessage(
            channel=self.kind,
            conversation_id=history_id,
            sender_id=address,
            sender_display=address,
            text="",
            raw=notification,
        )

    async def list_history(self, start_position: str) -> tuple[list[str], str]:
        """Message ids added since *start_position*, plus the new position.

        Returns the caller's own position unchanged on any failure, so a
        transient error re-reads the same window next time rather than skipping
        past unread mail. Skipping would lose messages silently, which is worse
        than processing one twice — the run's idempotency key catches the repeat.
        """
        if not self._token or not start_position:
            return [], start_position
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.get(
                    f"{_API_BASE}/history",
                    headers={"authorization": f"Bearer {self._token}"},
                    params={
                        "startHistoryId": start_position,
                        "historyTypes": "messageAdded",
                    },
                )
            except httpx.HTTPError as exc:
                log.warning("gmail history unreachable: %s", exc)
                return [], start_position
        if response.status_code == 404:
            # The position is older than Gmail's retention window; there is no
            # way to enumerate what was missed, so re-baseline rather than loop.
            log.warning("gmail history id %s expired; re-baselining", start_position)
            return [], ""
        if response.status_code != 200:
            log.warning("gmail history failed: %s", _describe_error(response))
            return [], start_position

        body = response.json()
        message_ids: list[str] = []
        for entry in body.get("history", []) or []:
            for added in entry.get("messagesAdded", []) or []:
                message = added.get("message", {})
                labels = set(message.get("labelIds", []) or [])
                # Our own outbound replies land in the same mailbox; treating
                # them as input would make the agent answer itself forever.
                if labels & {"SENT", "DRAFT", "TRASH"}:
                    continue
                if message.get("id"):
                    message_ids.append(message["id"])
        return message_ids, str(body.get("historyId") or start_position)

    async def fetch_message(self, message_id: str) -> InboundMessage | None:
        """Resolve a Gmail message id into a normalised inbound message."""
        if not self._token:
            return None
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{_API_BASE}/messages/{message_id}",
                headers={"authorization": f"Bearer {self._token}"},
                params={"format": "full"},
            )
        if response.status_code != 200:
            log.warning("gmail fetch failed: %s", _describe_error(response))
            return None

        body = response.json()
        headers = {
            header["name"].lower(): header["value"]
            for header in body.get("payload", {}).get("headers", [])
        }
        return InboundMessage(
            channel=self.kind,
            conversation_id=body.get("threadId", message_id),
            external_id=message_id,
            sender_id=headers.get("from", ""),
            sender_display=headers.get("from", ""),
            subject=headers.get("subject", ""),
            text=_extract_text(body.get("payload", {})) or body.get("snippet", ""),
            raw={"message_id": message_id, "headers": headers},
        )

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if not self._token:
            return DeliveryResult(False, "no Gmail access token configured")

        mail = EmailMessage()
        mail["To"] = message.reply_to or message.conversation_id
        mail["Subject"] = message.subject or "Re:"
        if self._address:
            mail["From"] = self._address
        mail.set_content(message.text or "(empty response)")

        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        payload: dict[str, Any] = {"raw": raw}
        # Threading the reply keeps it in the same conversation rather than
        # opening a new one in the recipient's client.
        if message.conversation_id:
            payload["threadId"] = message.conversation_id

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    f"{_API_BASE}/messages/send",
                    headers={"authorization": f"Bearer {self._token}"},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                return DeliveryResult(False, f"gmail unreachable: {exc}")

        if response.status_code == 401:
            return DeliveryResult(
                False, "Gmail rejected the access token; it has expired or been revoked"
            )
        if response.status_code >= 400:
            return DeliveryResult(False, f"gmail rejected the message: {_describe_error(response)}")
        return DeliveryResult(True, "email sent", {"thread_id": message.conversation_id})


def _extract_text(part: dict[str, Any]) -> str:
    """Depth-first search for the first text/plain body in a MIME tree."""
    if part.get("mimeType") == "text/plain":
        data = part.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode(errors="replace")
    for child in part.get("parts", []) or []:
        if text := _extract_text(child):
            return text
    return ""


def _describe_error(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", response.status_code))[:200]
    except ValueError:
        return f"HTTP {response.status_code}"
