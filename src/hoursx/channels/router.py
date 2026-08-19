"""Channel registry and inbound routing.

This is the piece that makes the whole thing an automation platform rather than
a set of API clients: an inbound message becomes an agent run, and the run's
answer goes back out the channel it arrived on.

    Telegram/WhatsApp/Gmail  ->  ingress  ->  session  ->  agent run  ->  reply

Conversations map to sessions one-to-one and persist, so an agent talking to a
person over Telegram has the same memory and history it would have in the web
console. The channel is transport; the conversation is the product.

Routing stops at "the run exists and a reply is owed". Sending the reply is
:mod:`hoursx.channels.dispatch`, because a webhook must be acknowledged in
seconds while an agent run can take minutes.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from hoursx.channels.base import (
    Channel,
    ChannelCredentials,
    ChannelKind,
    InboundMessage,
)
from hoursx.config import HoursXSettings
from hoursx.db.models import (
    AgentProfile,
    ChannelBinding,
    ChannelReply,
    Session,
    Workspace,
    WorkspaceMember,
)
from hoursx.errors import NotFoundError
from hoursx.observability import get_logger, metrics

log = get_logger("channels.router")


class ChannelRegistry:
    """Holds the channels this deployment has credentials for."""

    def __init__(self) -> None:
        self._channels: dict[ChannelKind, Channel] = {}

    def register(self, channel: Channel) -> None:
        self._channels[channel.kind] = channel

    def get(self, kind: ChannelKind) -> Channel | None:
        return self._channels.get(kind)

    def kinds(self) -> list[str]:
        return sorted(kind.value for kind in self._channels)

    def __len__(self) -> int:
        return len(self._channels)


def build_registry(settings: HoursXSettings) -> ChannelRegistry:
    """Assemble channels from configuration.

    A channel with no credentials is simply absent rather than present and
    broken, so an unconfigured deployment has no inbound surface at all and the
    ingress route answers 404 instead of failing somewhere deeper.
    """
    registry = ChannelRegistry()

    if settings.telegram_token:
        from hoursx.channels.telegram import TelegramChannel

        registry.register(
            TelegramChannel(
                ChannelCredentials(
                    token=settings.telegram_token,
                    secret=settings.telegram_webhook_secret,
                )
            )
        )
    if settings.whatsapp_token and settings.whatsapp_phone_number_id:
        from hoursx.channels.whatsapp import WhatsAppChannel

        registry.register(
            WhatsAppChannel(
                ChannelCredentials(
                    token=settings.whatsapp_token,
                    secret=settings.whatsapp_app_secret,
                    account_id=settings.whatsapp_phone_number_id,
                )
            )
        )
    if settings.gmail_access_token:
        from hoursx.channels.gmail import GmailChannel

        registry.register(
            GmailChannel(
                ChannelCredentials(
                    token=settings.gmail_access_token,
                    account_id=settings.gmail_address,
                )
            )
        )
    return registry


@dataclass(frozen=True)
class RoutedRun:
    """What routing produced: a run, the session it belongs to, and whether the
    message was new. ``duplicate`` is true for a redelivered webhook, which is
    a successful outcome rather than an error."""

    run_id: str
    session_id: str
    duplicate: bool


@dataclass(frozen=True)
class ChannelTarget:
    """Which workspace and agent inbound messages belong to."""

    workspace_id: str
    agent_handle: str


class ChannelRouter:
    """Turns inbound messages into agent runs with a recorded reply obligation."""

    def __init__(self, services, conductor) -> None:
        self._services = services
        self._conductor = conductor

    async def resolve_target(self, settings: HoursXSettings) -> ChannelTarget:
        """Find the workspace and agent that own inbound messages.

        A webhook carries no HoursX identity, so this comes from configuration.
        Resolution fails loudly: a misconfigured deployment that quietly dropped
        every message would look exactly like one nobody was messaging.
        """
        if not settings.channel_workspace_slug:
            raise NotFoundError(
                "channels are configured but HOURSX_CHANNEL_WORKSPACE_SLUG is not set, "
                "so inbound messages have no workspace to belong to"
            )
        async with self._services.db.session() as db:
            workspace = (
                await db.execute(
                    select(Workspace).where(Workspace.slug == settings.channel_workspace_slug)
                )
            ).scalar_one_or_none()
        if workspace is None:
            raise NotFoundError(
                f"no workspace with slug '{settings.channel_workspace_slug}'",
                slug=settings.channel_workspace_slug,
            )
        if not settings.channel_agent_handle:
            raise NotFoundError(
                "channels are configured but HOURSX_CHANNEL_AGENT_HANDLE is not set, "
                "so there is no agent to answer inbound messages"
            )
        return ChannelTarget(workspace_id=workspace.id, agent_handle=settings.channel_agent_handle)

    async def handle_inbound(self, inbound: InboundMessage, *, target: ChannelTarget) -> RoutedRun:
        """Route one message to the agent bound to its conversation."""
        session_id, owner_id = await self._session_for(inbound, target=target)
        run_id = await self._conductor.submit_message(
            session_id=session_id,
            user_id=owner_id,
            text=_compose_goal(inbound),
            idempotency_key=inbound.dedupe_key(),
        )
        duplicate = not await self._owe_reply(inbound, run_id=run_id, target=target)
        metrics.incr(
            f"channels.{inbound.channel.value}.{'redelivered' if duplicate else 'inbound'}"
        )
        return RoutedRun(run_id=run_id, session_id=session_id, duplicate=duplicate)

    # ----------------------------------------------------------------- helpers

    async def _owe_reply(
        self, inbound: InboundMessage, *, run_id: str, target: ChannelTarget
    ) -> bool:
        """Record that this run owes an answer. False when one is already owed.

        The uniqueness constraint on ``run_id`` is the real guard; this check
        just avoids provoking an integrity error on the ordinary redelivery
        path.
        """
        async with self._services.db.session() as db:
            existing = (
                await db.execute(select(ChannelReply.id).where(ChannelReply.run_id == run_id))
            ).scalar_one_or_none()
            if existing is not None:
                return False
            db.add(
                ChannelReply(
                    workspace_id=target.workspace_id,
                    run_id=run_id,
                    channel=inbound.channel.value,
                    conversation_id=inbound.conversation_id,
                    subject=f"Re: {inbound.subject}" if inbound.subject else "",
                    reply_to=_reply_handle(inbound),
                )
            )
        return True

    async def _session_for(
        self, inbound: InboundMessage, *, target: ChannelTarget
    ) -> tuple[str, str]:
        """Find or create the session bound to this conversation.

        Returns the session id and the user who owns it, together, because
        every caller needs both and a second lookup would only re-read the row
        just written.
        """
        routing_key = inbound.routing_key()
        async with self._services.db.session() as db:
            binding = (
                await db.execute(
                    select(ChannelBinding).where(
                        ChannelBinding.workspace_id == target.workspace_id,
                        ChannelBinding.routing_key == routing_key,
                    )
                )
            ).scalar_one_or_none()
            if binding is not None:
                session = await db.get(Session, binding.session_id)
                if session is not None:
                    return session.id, session.user_id

            profile = (
                await db.execute(
                    select(AgentProfile).where(
                        AgentProfile.workspace_id == target.workspace_id,
                        AgentProfile.handle == target.agent_handle,
                    )
                )
            ).scalar_one_or_none()
            if profile is None:
                raise NotFoundError(
                    f"no agent '{target.agent_handle}' in this workspace to answer "
                    f"{inbound.channel.value} messages",
                    agent_handle=target.agent_handle,
                )

            owner_id = await _first_member(db, target.workspace_id)
            session = Session(
                workspace_id=target.workspace_id,
                user_id=owner_id,
                agent_profile_id=profile.id,
                title=_session_title(inbound),
                sandbox_dir="",
            )
            db.add(session)
            await db.flush()
            session.sandbox_dir = f"session-{session.id}"
            db.add(
                ChannelBinding(
                    workspace_id=target.workspace_id,
                    channel=inbound.channel.value,
                    routing_key=routing_key,
                    session_id=session.id,
                    sender_id=inbound.sender_id,
                    sender_display=inbound.sender_display,
                )
            )
            await db.flush()
            log.info("bound %s conversation to session %s", inbound.channel.value, session.id)
            return session.id, owner_id


async def _first_member(db, workspace_id: str) -> str:
    """The account a channel conversation is attributed to.

    Someone messaging a bot has no HoursX account, but runs, quotas, and the
    audit trail all need an owner. The workspace's founding member is that
    owner, which keeps channel traffic inside the same limits as console work.
    """
    member = (
        (
            await db.execute(
                select(WorkspaceMember)
                .where(WorkspaceMember.workspace_id == workspace_id)
                .order_by(WorkspaceMember.created_at)
            )
        )
        .scalars()
        .first()
    )
    if member is None:
        raise NotFoundError("workspace has no members to own the session")
    return member.user_id


def _session_title(inbound: InboundMessage) -> str:
    who = inbound.sender_display or inbound.sender_id or "unknown"
    return f"{inbound.channel.value}: {who}"[:200]


def _reply_handle(inbound: InboundMessage) -> str:
    """Where a reply is addressed, when the conversation id is not enough.

    Email needs the sender's address; chat channels reply into the conversation
    itself and leave this empty.
    """
    if inbound.channel is ChannelKind.GMAIL:
        return inbound.sender_id
    return ""


def _compose_goal(inbound: InboundMessage) -> str:
    """Give the agent the message plus the context it needs to reply well."""
    if inbound.subject:
        return f"Subject: {inbound.subject}\n\n{inbound.text}"
    return inbound.text
