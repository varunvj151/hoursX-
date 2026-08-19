"""Channel contracts: how the outside world reaches an agent, and vice versa.

A *channel* is a two-way transport between an external service and a HoursX
agent. Telegram, WhatsApp, Gmail, and Slack all reduce to the same three
operations, so they share one protocol:

- **verify** an inbound webhook actually came from the service
- **parse** a provider-specific payload into a normalised inbound message
- **send** a reply back

Everything provider-specific stops at that boundary. The runtime above it never
learns what a Telegram ``chat_id`` or a Gmail ``threadId`` is; it sees a
:class:`InboundMessage` with a conversation key.

This is the pattern behind workflow tools like n8n — a normalised envelope plus
per-service adapters — with the difference that the thing in the middle is an
agent rather than a static node graph.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ChannelKind(StrEnum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    GMAIL = "gmail"
    SLACK = "slack"
    WEBHOOK = "webhook"


class InboundMessage(BaseModel):
    """A normalised message arriving from any channel."""

    channel: ChannelKind
    # Stable per-conversation key: the thread an agent replies into. Opaque
    # above this layer, and the only thing needed to route a reply back.
    conversation_id: str
    # Stable per-sender key, used for authorisation rather than display.
    sender_id: str
    # The provider's own message id, when it supplies one. This is the only
    # reliable way to tell a redelivery from a genuinely repeated message.
    external_id: str = ""
    sender_display: str = ""
    text: str = ""
    subject: str = ""
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    def routing_key(self) -> str:
        """Identifies the conversation across restarts and replicas."""
        return f"{self.channel.value}:{self.conversation_id}"

    def dedupe_key(self) -> str:
        """A stable key for "this exact message", safe to compare across
        processes.

        Every provider here retries webhooks it believes were not acknowledged,
        so the same message arrives more than once as a matter of course.
        Digesting rather than hashing matters: :func:`hash` is salted per
        process, so a redelivery landing on a second replica would look like a
        new message and start a duplicate run.
        """
        identity = self.external_id or f"body:{self.text}"
        digest = hashlib.sha256(f"{self.routing_key()}|{identity}".encode()).hexdigest()
        return f"ch:{digest[:48]}"


class OutboundMessage(BaseModel):
    conversation_id: str
    text: str
    subject: str = ""
    reply_to: str = ""


@dataclass
class DeliveryResult:
    ok: bool
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


class ChannelError(Exception):
    """A channel could not complete an operation."""


class SignatureError(ChannelError):
    """An inbound payload failed authenticity verification.

    Separate from a parse failure on purpose: a bad signature is a possible
    forgery attempt and is logged and refused, whereas a payload the adapter
    simply does not handle is ordinary and ignored quietly.
    """


@dataclass(frozen=True)
class ChannelCredentials:
    """What a channel needs to authenticate, in and out.

    Held as opaque strings; the platform never inspects them and the logging
    formatter redacts the field names they arrive under.
    """

    token: str = ""
    secret: str = ""
    account_id: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Channel(Protocol):
    """The contract every transport implements."""

    kind: ChannelKind

    def verify(self, *, headers: dict[str, str], body: bytes) -> None:
        """Raise :class:`SignatureError` if the payload is not authentic."""
        ...

    def parse(self, payload: dict[str, Any]) -> InboundMessage | None:
        """Normalise an inbound payload, or None when it is not a message.

        Returning None rather than raising matters: services send delivery
        receipts, typing indicators, and edits down the same webhook, and none
        of those should look like an error.
        """
        ...

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver a reply back to the conversation."""
        ...
