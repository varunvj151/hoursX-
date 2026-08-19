"""Outbound dispatch: settling the replies HoursX owes.

A webhook has to be acknowledged in seconds; an agent run can take minutes. The
two are decoupled by a durable obligation — :class:`ChannelReply` — written when
a message is accepted and settled here once the run has an outcome.

Delivery is at-least-once. A dispatcher that dies between sending and recording
the send will try again, so a person may occasionally see an answer twice. The
alternative, marking a reply sent before it is, loses answers instead, and a
lost answer is indistinguishable from an agent that ignored you.

Every terminal run produces a message, including failures and cancellations.
Silence is the one outcome that is never acceptable: the person on the other end
cannot tell it apart from a broken bot.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import or_, select, update

from hoursx.channels.base import ChannelKind, OutboundMessage
from hoursx.channels.router import ChannelRegistry
from hoursx.db.models import ChannelReply, Run, utcnow
from hoursx.observability import get_logger, metrics

log = get_logger("channels.dispatch")

# Runs in these states will never change again, so their reply can be settled.
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")

_APPROVAL_NOTICE = (
    "I need a human decision before I can continue. "
    "Once someone approves it in the HoursX console I will finish and reply here."
)


class ChannelDispatcher:
    """Sweeps owed replies and sends them out the channel they came in on."""

    def __init__(self, services, registry: ChannelRegistry) -> None:
        self._services = services
        self._registry = registry
        self._max_attempts = max(1, services.settings.channel_reply_max_attempts)

    async def sweep_once(self) -> int:
        """Settle every reply that can be settled now. Returns messages sent."""
        sent = 0
        for reply_id in await self._due_replies():
            sent += await self._settle(reply_id)
        return sent

    async def run_forever(self, interval: float | None = None) -> None:
        """Sweep on an interval until cancelled.

        Polling rather than subscribing to the event bus is deliberate: the bus
        drops events under load by design, and a dropped event here would mean a
        person never got an answer. The database row cannot be dropped.
        """
        wait = interval or self._services.settings.channel_dispatch_interval_seconds
        while True:
            # Shielded so shutdown lands *between* sweeps. A cancel delivered
            # inside one arrives mid-transaction, with a delivery claimed and
            # its outcome unrecorded — and tearing the database session down at
            # that point leaves the connection unusable for whatever runs next.
            sweep = asyncio.ensure_future(self.sweep_once())
            try:
                await asyncio.shield(sweep)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await sweep  # let the in-flight sweep close out cleanly
                raise
            except Exception:  # noqa: BLE001 — one bad sweep must not stop dispatch
                log.exception("channel dispatch sweep failed")
            await asyncio.sleep(wait)

    # ----------------------------------------------------------------- internals

    async def _due_replies(self) -> list[str]:
        """Ids of pending replies whose run has something worth reporting."""
        async with self._services.db.session() as db:
            rows = (
                (
                    await db.execute(
                        select(ChannelReply.id)
                        # Outer join so a reply whose run vanished is still
                        # selected and settled. An inner join would leave it
                        # pending forever, which is the silent failure this
                        # whole table exists to prevent.
                        .join(Run, Run.id == ChannelReply.run_id, isouter=True)
                        .where(
                            ChannelReply.status == "pending",
                            ChannelReply.attempts < self._max_attempts,
                            or_(
                                Run.id.is_(None),
                                Run.status.in_((*TERMINAL_RUN_STATUSES, "awaiting_approval")),
                            ),
                        )
                        .order_by(ChannelReply.created_at)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def _settle(self, reply_id: str) -> int:
        """Deliver one reply. Returns 1 when a message actually went out."""
        async with self._services.db.session() as db:
            reply = await db.get(ChannelReply, reply_id)
            if reply is None or reply.status != "pending":
                return 0
            run = await db.get(Run, reply.run_id)
            if run is None:
                reply.status = "abandoned"
                reply.detail = "the run this reply belonged to no longer exists"
                return 0
            interim = run.status == "awaiting_approval"
            if interim and reply.interim_sent:
                return 0
            text = _APPROVAL_NOTICE if interim else _answer_text(run)
            channel_kind = reply.channel
            conversation_id = reply.conversation_id
            subject = reply.subject
            reply_to = reply.reply_to
            attempts = reply.attempts

        channel = self._registry.get(ChannelKind(channel_kind))
        if channel is None:
            # Credentials were removed after the message was accepted. Nothing
            # will ever deliver this, so stop retrying and say why.
            async with self._services.db.session() as db:
                await db.execute(
                    update(ChannelReply)
                    .where(ChannelReply.id == reply_id, ChannelReply.status == "pending")
                    .values(status="abandoned", detail=f"{channel_kind} is no longer configured")
                )
            metrics.incr(f"channels.{channel_kind}.unconfigured")
            return 0

        if not await self._claim(reply_id, attempts=attempts, interim=interim):
            return 0  # another dispatcher took this one

        result = await channel.send(
            OutboundMessage(
                conversation_id=conversation_id,
                text=text,
                subject=subject,
                reply_to=reply_to,
            )
        )

        if interim:
            # The interim note is best-effort: the real answer is still owed, so
            # a failure here must not consume the reply's attempt budget.
            metrics.incr(f"channels.{channel_kind}.interim")
            return 1 if result.ok else 0

        async with self._services.db.session() as db:
            row = await db.get(ChannelReply, reply_id)
            if row is None:
                return 0
            if result.ok:
                row.status = "sent"
                row.detail = result.summary
                row.sent_at = utcnow()
            elif row.attempts >= self._max_attempts:
                row.status = "abandoned"
                row.detail = f"gave up after {row.attempts} attempts: {result.summary}"
                log.error(
                    "abandoned %s reply for run %s: %s", channel_kind, row.run_id, result.summary
                )
            else:
                row.detail = result.summary

        metrics.incr(f"channels.{channel_kind}.{'sent' if result.ok else 'send_failed'}")
        return 1 if result.ok else 0

    async def _claim(self, reply_id: str, *, attempts: int, interim: bool) -> bool:
        """Take ownership of one delivery.

        Bumping ``attempts`` in the same statement that filters on it makes the
        claim single-winner without a lock: two dispatchers racing on the same
        row see the same prior count, and only one UPDATE matches.
        """
        if interim:
            condition = ChannelReply.interim_sent.is_(False)
            values: dict = {"interim_sent": True}
        else:
            condition = ChannelReply.attempts == attempts
            values = {"attempts": attempts + 1}
        async with self._services.db.session() as db:
            claimed = await db.execute(
                update(ChannelReply)
                .where(ChannelReply.id == reply_id, ChannelReply.status == "pending", condition)
                .values(**values)
            )
        return claimed.rowcount == 1


def _answer_text(run: Run) -> str:
    """What to say about a finished run, whatever happened to it."""
    if run.status == "succeeded":
        return run.final_answer or "I finished, but produced no answer to send."
    if run.status == "cancelled":
        return "That request was cancelled before I could finish it."
    return f"I could not finish that: {run.error or 'the run did not succeed'}"
