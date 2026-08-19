"""Channel ingress: the door external services knock on.

Three properties shape every route here:

- **The signature is the authentication.** These endpoints are necessarily
  unauthenticated in the usual sense — Telegram cannot present a bearer token —
  so the adapter's ``verify`` is the whole of the trust decision and runs before
  the body is parsed.
- **Acknowledge fast.** Providers retry, and some disable a webhook that stays
  slow. Routing creates a run and returns; the answer goes back later through
  the dispatcher.
- **Status codes are a control channel.** ``2xx`` stops the provider retrying,
  so it is used only when the message is safely recorded or genuinely needs no
  action. A configuration fault answers ``503`` on purpose: the provider retries
  and the operator sees the failure, instead of the message vanishing quietly.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from hoursx.api.deps import Actor, get_conductor, get_services, require
from hoursx.auth import Permission
from hoursx.channels.base import ChannelKind, InboundMessage, SignatureError
from hoursx.channels.router import ChannelRouter, ChannelTarget
from hoursx.db.models import ChannelBinding, ChannelCursor, ChannelReply, utcnow
from hoursx.errors import HoursXError
from hoursx.observability import get_logger, metrics
from hoursx.orchestration import Conductor
from hoursx.services import AppServices

log = get_logger("api.channels")

router = APIRouter(prefix="/v1/channels", tags=["channels"])


class ChannelStatus(BaseModel):
    configured: list[str]
    workspace_slug: str
    agent_handle: str
    webhook_base: str


class BindingOut(BaseModel):
    id: str
    channel: str
    routing_key: str
    session_id: str
    sender_display: str


class DeliveryStats(BaseModel):
    pending: int
    sent: int
    abandoned: int


# --------------------------------------------------------------------- operator


@router.get("", response_model=ChannelStatus)
async def channel_status(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> ChannelStatus:
    """What is wired up, and where the provider webhooks should point."""
    settings = services.settings
    return ChannelStatus(
        configured=services.channels.kinds(),
        workspace_slug=settings.channel_workspace_slug,
        agent_handle=settings.channel_agent_handle,
        webhook_base=f"{settings.public_url.rstrip('/')}/v1/channels",
    )


@router.get("/bindings", response_model=list[BindingOut])
async def list_bindings(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> list[BindingOut]:
    """Which external conversations map to which sessions."""
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(ChannelBinding)
                    .where(ChannelBinding.workspace_id == actor.workspace.id)
                    .order_by(ChannelBinding.created_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    return [
        BindingOut(
            id=row.id,
            channel=row.channel,
            routing_key=row.routing_key,
            session_id=row.session_id,
            sender_display=row.sender_display,
        )
        for row in rows
    ]


@router.get("/deliveries", response_model=DeliveryStats)
async def delivery_stats(
    actor: Actor = Depends(require(Permission.OBSERVE)),
    services: AppServices = Depends(get_services),
) -> DeliveryStats:
    """Outstanding and failed replies.

    An abandoned reply is a person who asked something and never heard back, so
    it is worth an operator's attention rather than only a log line.
    """
    counts = {"pending": 0, "sent": 0, "abandoned": 0}
    async with services.db.session() as db:
        rows = (
            (
                await db.execute(
                    select(ChannelReply.status).where(
                        ChannelReply.workspace_id == actor.workspace.id
                    )
                )
            )
            .scalars()
            .all()
        )
    for state in rows:
        if state in counts:
            counts[state] += 1
    return DeliveryStats(**counts)


@router.post("/telegram/registration")
async def register_telegram_webhook(
    actor: Actor = Depends(require(Permission.WORKSPACE_MANAGE)),
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    """Point Telegram at this deployment.

    Registration is an operator action rather than a startup side effect: it
    rewrites where Telegram sends every update, and a dev machine booting must
    not steal production's messages.
    """
    channel = services.channels.get(ChannelKind.TELEGRAM)
    if channel is None:
        raise HoursXError("Telegram is not configured")
    url = f"{services.settings.public_url.rstrip('/')}/v1/channels/telegram/webhook"
    result = await channel.register_webhook(url)  # type: ignore[attr-defined]
    return {"ok": result.ok, "detail": result.summary, "url": url}


# ---------------------------------------------------------------------- ingress


@router.get("/whatsapp/webhook")
async def whatsapp_verification(
    request: Request,
    services: AppServices = Depends(get_services),
) -> Response:
    """Answer Meta's one-time subscription handshake."""
    channel = services.channels.get(ChannelKind.WHATSAPP)
    if channel is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    challenge = channel.verification_challenge(dict(request.query_params))  # type: ignore[attr-defined]
    if challenge is None:
        return Response("verification token did not match", status_code=status.HTTP_403_FORBIDDEN)
    return Response(challenge, media_type="text/plain")


@router.post("/{kind}/webhook")
async def receive_webhook(
    kind: str,
    request: Request,
    services: AppServices = Depends(get_services),
    conductor: Conductor = Depends(get_conductor),
) -> Response:
    """Verify, parse, and route one inbound payload."""
    try:
        channel_kind = ChannelKind(kind)
    except ValueError:
        return _ack("unknown channel", status.HTTP_404_NOT_FOUND)

    channel = services.channels.get(channel_kind)
    if channel is None:
        return _ack(f"{kind} is not configured", status.HTTP_404_NOT_FOUND)

    body = await request.body()
    try:
        # Verification reads the bytes exactly as received; parsing first and
        # re-serialising would change them and reject authentic payloads.
        channel.verify(headers={k.lower(): v for k, v in request.headers.items()}, body=body)
    except SignatureError as exc:
        metrics.incr(f"channels.{kind}.rejected")
        log.warning("rejected %s webhook: %s", kind, exc)
        return _ack("signature verification failed", status.HTTP_401_UNAUTHORIZED)

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return _ack("payload was not JSON", status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict):
        return _ack("payload was not an object", status.HTTP_400_BAD_REQUEST)

    inbound = channel.parse(payload)
    if inbound is None:
        # Receipts, typing indicators, and edits all arrive here. They are
        # ordinary traffic, not errors, and must be acknowledged.
        metrics.incr(f"channels.{kind}.ignored")
        return _ack("nothing to act on")

    channel_router = ChannelRouter(services, conductor)
    try:
        target = await channel_router.resolve_target(services.settings)
    except HoursXError as exc:
        # The operator has to see this; a 200 would let the provider forget the
        # message and leave no trace that anyone tried to talk to us.
        log.error("cannot route %s message: %s", kind, exc.message)
        metrics.incr(f"channels.{kind}.misconfigured")
        return _ack(exc.message, status.HTTP_503_SERVICE_UNAVAILABLE)

    messages = (
        await _resolve_gmail(services, channel, inbound, target)
        if channel_kind is ChannelKind.GMAIL
        else [inbound]
    )

    routed = 0
    for message in messages:
        try:
            result = await channel_router.handle_inbound(message, target=target)
        except HoursXError as exc:
            log.error("could not route %s message: %s", kind, exc.message)
            return _ack(exc.message, status.HTTP_503_SERVICE_UNAVAILABLE)
        routed += 0 if result.duplicate else 1

    return _ack(f"accepted {routed} message(s)")


# --------------------------------------------------------------------- internals


def _ack(detail: str, code: int = status.HTTP_200_OK) -> Response:
    return Response(
        content=json.dumps({"detail": detail}),
        status_code=code,
        media_type="application/json",
    )


async def _resolve_gmail(
    services: AppServices,
    channel,
    notification: InboundMessage,
    target: ChannelTarget,
) -> list[InboundMessage]:
    """Turn a Gmail change notification into the messages it refers to.

    The first notification only establishes a baseline: Gmail enumerates history
    *after* a position, so without a stored one there is no window to read. That
    costs one message at setup time and is the honest alternative to guessing.
    """
    position = notification.conversation_id
    async with services.db.session() as db:
        cursor = (
            await db.execute(
                select(ChannelCursor).where(
                    ChannelCursor.workspace_id == target.workspace_id,
                    ChannelCursor.channel == ChannelKind.GMAIL.value,
                )
            )
        ).scalar_one_or_none()
        start = cursor.position if cursor else ""
        if cursor is None:
            db.add(
                ChannelCursor(
                    workspace_id=target.workspace_id,
                    channel=ChannelKind.GMAIL.value,
                    position=position,
                )
            )
    if not start:
        log.info("gmail baseline established at history %s", position)
        return []

    message_ids, next_position = await channel.list_history(start)
    resolved: list[InboundMessage] = []
    for message_id in message_ids:
        message = await channel.fetch_message(message_id)
        if message is not None:
            resolved.append(message)

    async with services.db.session() as db:
        row = (
            await db.execute(
                select(ChannelCursor).where(
                    ChannelCursor.workspace_id == target.workspace_id,
                    ChannelCursor.channel == ChannelKind.GMAIL.value,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            # An expired history id comes back empty; re-baseline on the
            # notification rather than retrying a window Gmail no longer has.
            row.position = next_position or position
            row.updated_at = utcnow()
    return resolved
