"""Orphaned-run recovery.

A worker that dies mid-run leaves the row in ``running`` forever: the claim is
strictly single-winner, so nothing will ever pick it up again. That safety
property needs a matching liveness property, and this is it.

The runtime heartbeats a claimed run; a run whose heartbeat has gone stale is
presumed orphaned and returned to ``queued`` for another worker. The threshold
must exceed the longest legitimate gap between heartbeats — a slow model call or
a long tool — or healthy work would be stolen mid-flight.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from hoursx.db.models import Run, utcnow
from hoursx.observability import get_logger, metrics

log = get_logger("recovery")

# Generous by design: re-queuing live work is far worse than a late recovery.
DEFAULT_STALE_AFTER = timedelta(minutes=15)


async def heartbeat(session, run_id: str) -> None:
    """Mark a claimed run as still alive."""
    await session.execute(update(Run).where(Run.id == run_id).values(heartbeat_at=utcnow()))


async def requeue_orphaned_runs(
    services, *, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> list[str]:
    """Return runs abandoned by dead workers to ``queued``.

    Only ``running`` rows are eligible. ``awaiting_approval`` is excluded on
    purpose: it is waiting on a human, not a worker, and could sit there
    legitimately for days.
    """
    cutoff = utcnow() - stale_after
    requeued: list[str] = []
    async with services.db.session() as db:
        candidates = (
            (
                await db.execute(
                    select(Run.id).where(
                        Run.status == "running",
                        Run.heartbeat_at.is_not(None),
                        Run.heartbeat_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
        for run_id in candidates:
            # Re-assert the state in the UPDATE: the worker may have recovered
            # between the SELECT and here.
            result = await db.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == "running", Run.heartbeat_at < cutoff)
                .values(status="queued", heartbeat_at=None)
            )
            if result.rowcount == 1:
                requeued.append(run_id)

    if requeued:
        metrics.incr("runs.requeued_orphaned", len(requeued))
        log.warning("re-queued %d orphaned runs: %s", len(requeued), requeued)
    return requeued
